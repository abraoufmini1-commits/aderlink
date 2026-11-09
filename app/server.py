"""Serveur web ADER LINK : authentification, API JSON et tableau de bord."""
import base64
import hashlib
import hmac
import json
import re
import mimetypes
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).parent / "static"
SESSION_HOURS = 12
COOKIE = "aderlink_session"


# --- mots de passe et sessions -------------------------------------------------------
def hash_password(password, salt=None, rounds=310_000):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${digest.hex()}"


def verify_password(password, stored):
    try:
        _algo, rounds, salt, digest = stored.split("$")
    except (ValueError, AttributeError):
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
    return hmac.compare_digest(test.hex(), digest)


def make_token(secret, username):
    payload = f"{username}|{int(time.time()) + SESSION_HOURS * 3600}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_token(secret, token):
    try:
        username, expires, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except Exception:
        return None
    expected = hmac.new(secret.encode(), f"{username}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected) or int(expires) < time.time():
        return None
    return username


def FEATURE_LABEL():
    from .plans import FEATURE_LABEL as L
    return L


class LoginThrottle:
    """5 échecs en 15 minutes depuis une même adresse -> blocage temporaire."""

    def __init__(self, limit=5, window=900):
        self.limit, self.window = limit, window
        self.fails = {}
        self.lock = threading.Lock()

    def blocked(self, ip):
        with self.lock:
            now = time.time()
            self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < self.window]
            return len(self.fails[ip]) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def reset(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


# --- application -----------------------------------------------------------------
class Forbidden(Exception):
    pass


def waits_router(path, method):
    """Actions qui attendent la réponse d'un routeur par internet : leur lenteur est normale."""
    return method == "POST" or path.endswith(("/check", "/test"))


def slow_label(path, method):
    """Nom clair d'une page lente, pour « État du serveur »."""
    parts = path.split("/")
    if method == "POST":
        if path == "/api/batches":
            return "Création d'un lot de tickets (envoi au routeur)"
        if path.endswith("/remove-unused"):
            return "Suppression des tickets non utilisés d'un lot (sur le routeur)"
        if path.startswith("/api/forfaits"):
            return "Création ou modification d'un profil (sur le routeur)"
        return "Action : " + path
    if path.endswith("/check"):
        return "Bouton « Vérifier les routeurs » (attend les routeurs)"
    if path.endswith("/test"):
        return "Bouton « Tester » un routeur (attend le routeur)"
    names = {"/api/batches": "Ouverture de la page Lots", "/api/overview": "Tableau de bord",
             "/api/report": "Rapports", "/api/forfaits": "Profils", "/api/zones": "Liste des routeurs"}
    return names.get(path, path)


class App:
    def __init__(self, config, collector, store, demo=False, tickets=None, routers=None, updater=None,
                 users=None, save_config=None, paths=None, roaming=None, guard=None):
        self.config = config
        self.collector = collector
        self.store = store
        self._cache, self._cache_lock, self._refreshing = {}, threading.Lock(), set()
        import collections
        self.slow = collections.deque(maxlen=15)  # pages lentes (heure, adresse, secondes)
        self.started = int(time.time())
        self.demo = demo
        self.tickets = tickets
        self.routers = routers
        self.updater = updater
        self.users = users
        self.save_config = save_config or (lambda c: None)
        self.paths = paths or {}
        self.roaming = roaming
        self.guard = guard
        self.throttle = LoginThrottle()

    def offset(self):
        return int(float(self.config.get("utc_offset_hours", 1)) * 3600)  # Niger : UTC+1

    def zones_for(self, who):
        return [z for z in self.config.get("zones", []) if who.can_zone(z["id"])]

    def zones_list(self, who):
        out = []
        for z in self.zones_for(who):
            pvs = self.routers.list_pv(z["id"]) if self.routers else []
            if who.is_reseller:  # le client ne voit que ses points de vente
                mine = set(self.users.pvs_of(who, z["id"])) if self.users else set()
                pvs = [p for p in pvs if p["id"] in mine]
            g = self.roaming.group_of_zone(z["id"]) if self.roaming else None
            out.append({"id": z["id"], "name": z["name"], "ssid": z.get("ssid") or z["name"],
                        "code_length": z.get("code_length", 8), "points_vente": pvs,
                        "roaming": {"id": g["id"], "name": g["name"], "routers": len(g["zones"])} if g else None})
        return out

    def sales_scope(self, who):
        zone_ids = None if who.zones is None else list(who.zones)
        return zone_ids, (who.username if who.is_reseller else None)

    def diag(self):
        """État du serveur, pour comprendre une lenteur (Paramètres, administrateur)."""
        import os
        from .collector import open_files
        try:
            load = [round(x, 2) for x in os.getloadavg()]
            cpus = os.cpu_count()
        except OSError:
            load, cpus = None, None
        mem = None
        try:
            info = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines() if ":" in l)
            tot, avail = int(info["MemTotal"].split()[0]), int(info["MemAvailable"].split()[0])
            mem = {"total_mb": tot // 1024, "used_pct": round(100 * (tot - avail) / tot)}
        except (OSError, KeyError, ValueError):
            pass
        db = self.paths.get("db") if self.paths else None
        db_mb = None
        try:
            from pathlib import Path as _P
            db_mb = round(sum(f.stat().st_size for f in _P(db).parent.glob(_P(db).name + "*")) / 1048576, 1) if db else None
        except OSError:
            pass
        t0 = time.time()
        with self.store.lock:
            lock_wait = round(time.time() - t0, 3)
        cyc = self.collector.last_cycle
        wal_mb = None
        try:
            from pathlib import Path as _P
            w = _P(str(db) + "-wal") if db else None
            wal_mb = round(w.stat().st_size / 1048576, 1) if w and w.exists() else 0
        except OSError:
            pass
        # où les fils du programme passent leur temps : 10 coups d'œil en 1 seconde
        import sys
        import collections
        spots = collections.Counter()
        me = threading.get_ident()
        idle = ("wait", "select", "accept", "recv", "recv_into", "readinto", "readline", "get", "sleep",
                "_recv_exact", "create_connection", "recvfrom",
                "_wait_for_tstate_lock", "serve_forever", "poll", "acquire", "_recv", "result")
        for _ in range(10):
            for tid, f in sys._current_frames().items():
                if tid == me:
                    continue
                top = f
                while f and "/app/" not in f.f_code.co_filename:
                    f = f.f_back
                if f is None or top.f_code.co_name in idle:
                    continue
                # boucles qui attendent un paquet ou dorment (l'appel en C n'apparaît pas) : pas du travail
                if top is f and f.f_code.co_name in ("_loop", "loop", "_run", "run_forever"):
                    continue
                spots[f"{f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_code.co_name} ({top.f_code.co_name})"] += 1
            # fils qui attendent un routeur (réseau) : comptés à part, ce n'est pas du travail du processeur
            time.sleep(0.1)
        return {"open_files": open_files(), "threads": threading.active_count(), "load": load, "cpus": cpus,
                "memory": mem, "db_mb": db_mb, "wal_mb": wal_mb, "lock_wait_s": lock_wait,
                "hot": [{"where": k, "n": v} for k, v in spots.most_common(6)],
                "lock_long": [{"at": a, "seconds": b, "where": c} for a, b, c in reversed(getattr(self.store.lock, "long", []))][:6],
                "cycle": {"at": cyc[0], "seconds": cyc[1]} if cyc else None, "poll_seconds": self.collector.poll_seconds,
                "routers": len(self.config.get("zones", [])), "started": self.started,
                "collect": ({"age": int(time.time() - self.collector.updated) if self.collector.updated else None,
                             "restarts": self.collector.restarts, "mode": self.collector.mode,
                             "error": self.collector.last_error} if hasattr(self.collector, "restarts") else None),
                "slow": [{"at": x[0], "path": x[1], "seconds": x[2], "label": slow_label(x[1], x[3] if len(x) > 3 else "GET"),
                          "waits_router": waits_router(x[1], x[3] if len(x) > 3 else "GET")} for x in reversed(self.slow)]}

    def overview(self, range_key, who):
        hours = {"6h": 6, "24h": 24, "7j": 168}.get(range_key, 24)
        bucket = 300 if hours <= 24 else 3600
        state = self.collector.snapshot()
        since = int(time.time()) - hours * 3600
        per_zone = list(self._cached(("zone_hist", hours), lambda: self._zone_history(since, bucket), ttl=30 if hours <= 24 else 300))
        # + les 5 dernières minutes, encore en mémoire (pas encore écrites dans la base)
        for b, zid, online, active, rx, tx in [tuple(v) for v in list(getattr(self.collector, "_agg", {}).values())]:
            per_zone.append((b // bucket * bucket, zid, active if online else None, rx, tx))
        peaks = {}
        for b, zid, active, rx, tx in per_zone:
            if (active or 0) > peaks.get(zid, 0):
                peaks[zid] = active
        logos = self._cached(("logos",), lambda: self.routers.logo_ids() if self.routers else set(), ttl=60)
        zones = []
        for z in self.zones_for(who):
            s = state.get(z["id"], {})
            pending = not (z.get("router") or {}).get("host")
            zones.append({
                "id": z["id"], "name": z["name"], "location": z.get("location", ""),
                "status": "pending" if pending else "unknown" if not s else ("online" if s.get("online") else "offline"),
                "has_logo": z["id"] in logos,
                "error": s.get("error"), "checked_at": s.get("checked_at"),
                "last_seen": s.get("last_seen"), "identity": s.get("identity"),
                "board": s.get("board"), "version": s.get("version"),
                "uptime_s": s.get("uptime_s"), "cpu": s.get("cpu"), "memory": s.get("memory"),
                "tickets": s.get("tickets"), "active": s.get("active", 0),
                "peak": max(peaks.get(z["id"]) or 0, s.get("active", 0) if s.get("online") else 0),
                "rx_bps": s.get("rx_bps", 0), "tx_bps": s.get("tx_bps", 0),
            })
        online = [z for z in zones if z["status"] == "online"]
        off = self.offset()
        midnight = (int(time.time()) + off) // 86400 * 86400 - off
        zone_ids, mine = self.sales_scope(who)
        sales = self.tickets.sales(midnight, zone_ids, mine) if self.tickets else []
        keep = None if who.zones is None else set(who.zones)
        merged = {}
        for b, zid, active, rx, tx in per_zone:  # même lecture pour tout le monde : chacun ne garde que ses routeurs
            if keep is not None and zid not in keep:
                continue
            m = merged.setdefault(b, {"ts": b, "active": 0, "rx_bps": 0, "tx_bps": 0})
            m["active"] += active or 0
            m["rx_bps"] += rx or 0
            m["tx_bps"] += tx or 0
        history = [merged[k] for k in sorted(merged)]
        events = [e for e in self.store.events(40) if who.can_zone(e["zone_id"])][:15]
        return self._overview_result(zones, online, sales, history, events, who)

    # Les courbes et pics changent seulement à chaque passage du collecteur : on les garde 30 s
    def _cached(self, key, fn, ttl=30):
        """Résultat gardé `ttl` secondes. Une fois périmé, l'ancien résultat est rendu TOUT DE SUITE et le
        nouveau est calculé à côté : seule la toute première ouverture attend le calcul (avant : un clic
        sur deux tombait sur le recalcul de l'historique des 72 routeurs, 2 à 3 s d'attente)."""
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            if hit and now - hit[0] < ttl * 10:
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    threading.Thread(target=self._refresh_cache, args=(key, fn), daemon=True).start()
                return hit[1]
        return self._refresh_cache(key, fn)

    def _refresh_cache(self, key, fn):
        try:
            val = fn()
            with self._cache_lock:
                if len(self._cache) > 200:
                    self._cache.clear()
                self._cache[key] = (time.time(), val)
            return val
        finally:
            with self._cache_lock:
                self._refreshing.discard(key)

    def keep_warm(self):
        """Historique des courbes recalculé à l'avance, en continu (dès le démarrage) : personne
        n'attend jamais ce calcul en ouvrant le tableau de bord."""
        def loop():
            n = 0
            while True:
                for hours, bucket, every in ((24, 300, 1), (168, 3600, 10)):
                    if n % every == 0:
                        try:
                            since = int(time.time()) - hours * 3600
                            val = self._zone_history(since, bucket)
                            with self._cache_lock:
                                self._cache[("zone_hist", hours)] = (time.time(), val)
                        except Exception as e:
                            print(f"[pages] courbes non préparées : {e!r}", flush=True)
                n += 1
                time.sleep(25)
        threading.Thread(target=loop, daemon=True, name="pages-warm").start()

    def _zone_history(self, since, bucket):
        """Une seule lecture de l'historique (tous les routeurs), gardée 30 s : sert aux pics ET aux courbes."""
        return self.store.read(
            "SELECT (ts / ?) * ? AS b, zone_id, MAX(CASE WHEN online = 1 THEN active END), MAX(rx_bps), MAX(tx_bps) "
            "FROM samples WHERE ts >= ? GROUP BY b, zone_id", (bucket, bucket, since))

    def _peaks(self, since):
        return self.store.read(
            "SELECT zone_id, MAX(active) FROM samples WHERE ts >= ? AND online = 1 GROUP BY zone_id", (since,))

    def _merged_history(self, zone_ids, since, bucket):
        merged = {}
        for zid in zone_ids:
            for pt in self.store.history(since, bucket, zid):
                m = merged.setdefault(pt["ts"], {"ts": pt["ts"], "active": 0, "rx_bps": 0, "tx_bps": 0})
                for k in ("active", "rx_bps", "tx_bps"):
                    m[k] += pt[k]
        return [merged[k] for k in sorted(merged)]

    def _overview_result(self, zones, online, sales, history, events, who):
        return {
            "sales_today": {"tickets": sum(s["tickets"] for s in sales),
                            "amount": sum(s["amount"] for s in sales)},
            "demo": self.demo, "me": who.public(),
            "generated_at": int(time.time()),
            "poll_seconds": self.collector.poll_seconds,
            "totals": {
                "zones": len(zones),
                "zones_online": len(online),
                "active": sum(z["active"] for z in online),
                "rx_bps": sum(z["rx_bps"] for z in online),
                "tx_bps": sum(z["tx_bps"] for z in online),
                "tickets": sum(z["tickets"] or 0 for z in zones),
            },
            "zones": zones,
            "history": history,
            "events": events,
        }

    def sessions(self, zone_id):
        zone = next((z for z in self.config.get("zones", []) if z["id"] == zone_id), None)
        if not zone:
            return None
        s = self.collector.snapshot().get(zone_id, {})
        return {"zone": {"id": zone_id, "name": zone["name"]},
                "online": s.get("online", False), "sessions": s.get("sessions", [])}

    def report(self, q, who):
        """Période en jours locaux : ?from=AAAA-MM-JJ&to=AAAA-MM-JJ (inclus)."""
        import datetime as dt
        off = self.offset()
        today = (int(time.time()) + off) // 86400

        def day(key, default):
            try:
                return dt.date.fromisoformat(q.get(key, [""])[0]).toordinal() - dt.date(1970, 1, 1).toordinal()
            except ValueError:
                return default
        d1, d2 = day("from", today - 6), day("to", today)
        d1, d2 = min(d1, d2), max(d1, d2)
        d1 = max(d1, d2 - 366)  # un an au plus
        zone_ids, mine = self.sales_scope(who)
        only = (q.get("zone") or [""])[0]
        chosen = next((z for z in self.zones_for(who) if z["id"] == only), None)
        if chosen:  # rapport d'un seul routeur
            zone_ids = [chosen["id"]]
        names = {self.config["admin"]["username"]: "Administrateur"}
        if self.users:
            rows, _ = self.users._q("SELECT username, name FROM users")
            names.update(dict(rows))
        # même rapport demandé plusieurs fois (impression, Excel, autre onglet) : gardé 60 s
        rep = dict(self._cached(("report", d1, d2, tuple(zone_ids or ()), mine),
                                lambda: self.tickets.report(d1 * 86400 - off, (d2 + 1) * 86400 - off, off, zone_ids, mine, names),
                                ttl=60))
        rep.update({"from": str(dt.date(1970, 1, 1) + dt.timedelta(days=d1)),
                    "to": str(dt.date(1970, 1, 1) + dt.timedelta(days=d2)),
                    "today": str(dt.date(1970, 1, 1) + dt.timedelta(days=today)),
                    "me": who.public(), "demo": self.demo,
                    "zones": [chosen["name"]] if chosen else [z["name"] for z in self.zones_for(who)],
                    "zone_list": [{"id": z["id"], "name": z["name"]} for z in self.zones_for(who)],
                    "zone": chosen["id"] if chosen else ""})
        if who.is_reseller:
            rep["commission"] = round(rep["totals"]["amount"] * (who.commission or 0) / 100)
        return rep

    def subscription_info(self, who):
        """Page « Mon abonnement » : formule, date de fin, ce qui est utilisé, et toutes les formules avec leurs prix."""
        U = self.users
        owner = U._get(who.parent) if who.parent else U._get(who.username)
        if not owner:
            return {"none": True, "contact": self.config.get("contact") or {}}
        P = U.plans
        plan = P.get(owner.get("plan_id"))
        lim = P.limits(owner.get("plan_id"), owner.get("extra_routers"), owner.get("max_routers") or 0)
        owned = [z["id"] for z in self.routers.owned(owner["username"])] if self.routers else []
        in_roaming = 0
        if self.roaming:
            rows, _ = self.roaming._q("SELECT zone_id FROM roaming_members")
            in_roaming = len([r for (r,) in rows if r in owned])
        now = int(time.time())
        months = (1, 3, 6, 12)
        return {"name": owner["name"], "plan": plan, "extra_routers": owner.get("extra_routers") or 0,
                "limits": lim, "expires_at": owner["expires_at"], "now": now,
                "days_left": None if not owner["expires_at"] else max(0, (owner["expires_at"] - now) // 86400),
                "used": {"routers": len(owned), "roaming": in_roaming},
                "plans": [dict(p, quotes=[P.quote(p, m) for m in months]) for p in P.list()],
                "billing": P.billing(), "labels": FEATURE_LABEL(), "contact": self.config.get("contact") or {},
                "history": U.history(owner["id"], 12) if not who.parent else []}

    def batch_visible(self, b, who):
        if not b or not who.can_zone(b["zone_id"]):
            return False
        return not who.is_reseller or b.get("created_by") == who.username


_STATIC_CACHE = {}


def static_file(path):
    """Contenu d'un fichier statique et son empreinte (relu seulement s'il a changé)."""
    st = path.stat()
    hit = _STATIC_CACHE.get(path)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1], hit[2]
    body = path.read_bytes()
    tag = hashlib.sha1(body).hexdigest()[:12]
    _STATIC_CACHE[path] = ((st.st_mtime_ns, st.st_size), body, tag)
    return body, tag


_ASSET = re.compile(rb'(["\'])/static/([A-Za-z0-9._-]+)\1')


def versioned_html(body):
    """Ajoute ?v=<empreinte> aux fichiers d'une page : le navigateur les garde, et une mise à jour change l'adresse."""
    def sub(m):
        f = STATIC / m.group(2).decode()
        if not f.is_file():
            return m.group(0)
        return m.group(1) + b"/static/" + m.group(2) + b"?v=" + static_file(f)[1].encode() + m.group(1)
    return _ASSET.sub(sub, body)


PAGES = {"/": ("dashboard.html", None), "/tickets": ("tickets.html", None), "/imprimer": ("print.html", None),
         "/lots": ("lots.html", None), "/controle": ("controle.html", None),
         "/routeurs": ("routeurs.html", "routers_read"), "/parametres": ("parametres.html", "settings"),
         "/utilisateurs": ("utilisateurs.html", "team"), "/rapports": ("rapports.html", None), "/analytique": ("analytique.html", None), "/profil": ("profil.html", None),
         "/roaming": ("roaming.html", "routers_write"), "/formules": ("formules.html", "settings"),
         "/abonnement": ("abonnement.html", "routers_write")}


def make_handler(app):
    secret = app.config["secret_key"]
    secure = app.config.get("secure_cookies", False)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ADERLINK"
        sys_version = ""
        protocol_version = "HTTP/1.1"  # connexion gardée ouverte avec Caddy : chaque clic va plus vite
        timeout = 75

        def _timed(self, fn):
            """Mesure le temps de réponse (après réception de la demande) : les pages lentes sont notées."""
            t0 = time.time()
            try:
                return fn()
            finally:
                took = time.time() - t0
                path = (self.path or "").split("?")[0][:60]
                if took > 1.5 and not path.startswith("/api/restore"):
                    app.slow.append((int(t0), path, round(took, 1), self.command))
                    print(f"[lent] {path} : {took:.1f} s", flush=True)

        def log_message(self, fmt, *args):  # journal discret
            if "/api/" not in self.path:
                super().log_message(fmt, *args)

        # --- outils ---
        def need_feature(self, who, feature):
            if not who.has(feature):
                plan = (who.limits or {}).get("plan") or {}
                raise Forbidden(f"« {FEATURE_LABEL().get(feature, feature)} » n'est pas compris dans votre formule "
                                f"{plan.get('name', '')}. Passez à la formule supérieure dans « Mon abonnement ».")

        def client_ip(self):
            if app.config.get("behind_proxy"):
                fwd = self.headers.get("X-Forwarded-For")
                if fwd:
                    return fwd.split(",")[0].strip()
            return self.client_address[0]

        def token_user(self):
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(COOKIE)
            return read_token(secret, morsel.value) if morsel else None

        def who(self):
            username = self.token_user()
            if not username:
                return None
            if app.users:
                return app.users.principal(username)
            from .users import Principal
            return Principal(username, "Administrateur", "admin") if username == app.config["admin"]["username"] else None

        def send(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                             "script-src 'self'; frame-ancestors 'none'")
            headers = dict(headers or {})
            if self.command not in ("GET", "HEAD"):  # le corps n'est pas toujours lu en entier : on ne réutilise pas la connexion
                self.close_connection = True
                self.send_header("Connection", "close")
            self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, data, status=200):
            self.send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

        def redirect(self, to, cookie=None):
            headers = {"Location": to}
            if cookie is not None:
                headers["Set-Cookie"] = cookie
            self.send(HTTPStatus.SEE_OTHER, headers=headers)

        def https(self):
            """Vrai si le visiteur est en HTTPS (derrière Caddy, avec un nom de domaine)."""
            return bool(app.config.get("behind_proxy")) and self.headers.get("X-Forwarded-Proto", "") == "https"

        def static(self, name, versioned=False):
            path = (STATIC / name).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():
                return self.send(404, b"Introuvable")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            body, tag = static_file(path)
            if path.suffix == ".html":  # pages : toujours fraîches, mais leurs fichiers sont gardés par le navigateur
                body = versioned_html(body)
                return self.send(200, body, ctype)
            etag = f'"{tag}"'
            cache = ("public, max-age=31536000, immutable" if versioned else "no-cache")
            if self.headers.get("If-None-Match") == etag:
                return self.send(304, b"", ctype, {"ETag": etag, "Cache-Control": cache})
            return self.send(200, body, ctype, {"ETag": etag, "Cache-Control": cache})

        def call(self, fn, status=200):
            from .routers import RouterError
            from .tickets import TicketError
            from .users import UserError
            from .roaming import RoamingError
            from .guard import GuardError
            try:
                out = fn()
                if out is None:  # réponse déjà envoyée (image)
                    return None
                return self.json(out, status)
            except Forbidden as e:
                return self.json({"error": str(e) or "Accès refusé"}, 403)
            except (RouterError, TicketError, UserError, RoamingError, GuardError) as e:
                return self.json({"error": str(e)}, 400)

        def send_backup(self):
            """Sauvegarde complète : config.json + base de données (copie cohérente)."""
            from .guard import make_backup
            name = time.strftime("aderlink-sauvegarde-%Y-%m-%d-%H%M.zip", time.localtime())
            keys = Path(app.paths["db"]).parent / "server-keys" if app.paths.get("db") else None
            return self.send(200, make_backup(app.config, app.store, keys_dir=keys), "application/zip",
                             {"Content-Disposition": f'attachment; filename="{name}"'})

        def restore_backup(self, who):
            """Remplace la configuration et la base par celles d'une sauvegarde, puis redémarre."""
            import io
            import zipfile
            if self.headers.get("X-AderLink") != "1":
                return self.json({"error": "Requête refusée"}, 403)
            if not who or not who.can("settings"):
                return self.json({"error": "Accès refusé"}, 403)
            if app.demo or not app.paths.get("db"):
                return self.json({"error": "Indisponible en mode démonstration"}, 400)
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not 0 < length <= 200 * 1024 * 1024:
                return self.json({"error": "Fichier vide ou trop gros (200 Mo au plus)"}, 413)
            raw = self.rfile.read(length)
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
                cfg = json.loads(zf.read("config.json"))
                db = zf.read("aderlink.db")
            except (zipfile.BadZipFile, KeyError, ValueError):
                return self.json({"error": "Ce fichier n'est pas une sauvegarde ADER LINK"}, 400)
            if not (isinstance(cfg, dict) and cfg.get("admin", {}).get("password_hash") and cfg.get("secret_key")
                    and db[:16] == b"SQLite format 3\x00"):
                return self.json({"error": "Sauvegarde incomplète ou abîmée"}, 400)
            # le serveur garde ses propres réglages de réseau (adresse, HTTPS, proxy)
            for k in ("secure_cookies", "behind_proxy", "server_ip_for_routers", "wireguard", "telegram"):  # (OpenVPN : dans « wireguard »)
                if k in app.config:
                    cfg[k] = app.config[k]
                else:
                    cfg.pop(k, None)
            dbp = Path(app.paths["db"])
            # clés des tunnels de l'ancien serveur : gardées ici, l'installation (install.sh) les reprend
            from .guard import KEY_FILES
            keys = [n for n in zf.namelist() if n.startswith("server-keys/") and n.split("/", 1)[1] in KEY_FILES]
            if keys:
                kd = dbp.parent / "server-keys"
                kd.mkdir(mode=0o700, exist_ok=True)
                for n in keys:
                    f = kd / n.split("/", 1)[1]
                    f.write_bytes(zf.read(n))
                    f.chmod(0o600)
            tmp = dbp.with_suffix(".restore")
            tmp.write_bytes(db)
            app.config.clear()
            app.config.update(cfg)
            app.save_config(app.config)
            with app.store.lock:  # on bascule sur la base restaurée au redémarrage
                app.store.db.close()
                for suffix in ("-wal", "-shm"):
                    Path(str(dbp) + suffix).unlink(missing_ok=True)
                tmp.replace(dbp)
            self.json({"ok": True, "message": "Sauvegarde restaurée. ADER LINK redémarre…"})
            if app.updater:
                threading.Timer(1.0, app.updater.on_restart).start()
            return None

        def my_team(self, who, uid):
            if who.role != "client" or not app.users:
                return False
            u = app.users.get(uid)
            return bool(u and u.get("parent") == who.username)

        def need(self, cond, msg="Accès refusé pour votre compte"):
            if not cond:
                raise Forbidden(msg)

        # --- routes GET ---
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            return self._timed(self._do_get)

        def _do_get(self):
            url = urlparse(self.path)
            p = url.path
            q = parse_qs(url.query)
            if p == "/favicon.ico":
                return self.static("favicon.ico")
            if p == "/sw.js":  # application installable (téléphone, PC)
                return self.static("sw.js")
            if p == "/api/version":
                from .updater import local_version
                return self.json({"version": app.updater.state["current"] if app.updater else local_version()})
            if p.startswith("/install/") and p.endswith(".rsc") and app.routers:
                z = app.routers.by_token(p[len("/install/"):-4])
                if not z:
                    return self.send(404, b"# Lien d'installation inconnu")
                profiles = app.tickets.profile_specs(z["id"]) if app.tickets else []
                return self.send(200, app.routers.install_script(z, self.headers.get("Host", ""), profiles, app.roaming).encode(),
                                 "text/plain; charset=utf-8")
            if p.startswith("/install/") and p.endswith("/ovpn") and app.routers:
                z = app.routers.by_token(p[len("/install/"):-5])
                if not z or not app.routers.wg():
                    return self.send(404, b"inconnu")
                app.routers.ovpn_up(z)
                if app.collector and not app.demo:
                    threading.Thread(target=lambda: (time.sleep(15), app.collector.poll_all()), daemon=True).start()
                return self.send(200, b"ok")
            if p.startswith("/install/") and p.endswith("/wg") and app.routers:
                z = app.routers.by_token(p[len("/install/"):-3])
                if not z or not app.routers.wg():
                    return self.send(404, b"inconnu")
                from .routers import RouterError
                try:
                    app.routers.tunnel_up(z, (parse_qs(urlparse(self.path).query, keep_blank_values=True).get("pub") or [""])[0])
                except RouterError as e:
                    return self.send(400, str(e).encode())
                if app.collector and not app.demo:
                    threading.Thread(target=lambda: (time.sleep(8), app.collector.poll_all()), daemon=True).start()
                return self.send(200, b"ok")
            if p.startswith("/install/") and p.endswith("/ok") and app.routers:
                z = app.routers.by_token(p[len("/install/"):-3])
                if not z:
                    return self.send(404, b"inconnu")
                app.routers.installed(z, self.client_ip())
                if app.collector and not app.demo:
                    threading.Thread(target=app.collector.poll_all, daemon=True).start()
                return self.send(200, b"ok")
            if p == "/winbox/install.ps1":  # public : ne contient aucun secret
                return self.send(200, (Path(__file__).parent / "winbox" / "install.ps1").read_bytes(), "text/plain; charset=utf-8")
            if p == "/winbox/activer-winbox.bat":
                host = re.sub(r"[^A-Za-z0-9.\-:\[\]]", "", self.headers.get("Host", ""))[:80] or "127.0.0.1:8080"
                bat = ("@echo off\r\ntitle ADER LINK - Activer Winbox\r\necho Activation du bouton Winbox d'ADER LINK...\r\n"
                       f"powershell -NoProfile -ExecutionPolicy Bypass -Command \"iex ((New-Object Net.WebClient)"
                       f".DownloadString('http://{host}/winbox/install.ps1'))\"\r\npause\r\n")
                return self.send(200, bat.encode(), "application/octet-stream",
                                 {"Content-Disposition": 'attachment; filename="activer-winbox-aderlink.bat"'})
            if p == "/healthz":
                return self.send(200, b"ok")
            if p == "/login":
                if "e" not in q and app.users and app.users.is_expired(self.token_user()):
                    return self.redirect("/login?e=expire",
                                         f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                return self.static("login.html")
            if p == "/api/public":
                return self.json({"demo": app.demo, "contact": app.config.get("contact") or {}})
            if p.startswith("/static/"):
                return self.static(p[len("/static/"):], versioned="v" in q)
            if p == "/logout":
                return self.redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            who = self.who()
            if p in PAGES:
                page, right = PAGES[p]
                if not who:
                    return self.redirect("/login")
                return self.static(page) if (right is None or who.can(right)) else self.redirect("/")
            if not who:
                return self.json({"error": "Non connecté"}, 401)
            return self.call(lambda: self.api_get(p, q, who))

        def api_get(self, p, q, who):
            parts = p.split("/")
            if p == "/api/me":
                return dict(who.public(), demo=app.demo)
            if app.users and p == "/api/plans":
                P = app.users.plans
                out = {"plans": P.list(), "billing": P.billing(), "labels": FEATURE_LABEL(),
                       "contact": app.config.get("contact") or {}}
                if who.is_admin:
                    out["clients"] = P.clients_count()
                return out
            if app.users and p == "/api/subscription":
                return app.subscription_info(who)
            if p == "/api/zones":
                return {"zones": app.zones_list(who), "demo": app.demo}
            if p == "/api/diag":
                self.need(who.can("settings"))
                return app.diag()
            if p == "/api/overview":
                return app.overview(q.get("range", ["24h"])[0], who)
            if len(parts) == 5 and parts[2] == "zones":
                zid, what = parts[3], parts[4]
                self.need(who.can_zone(zid))
                if what == "sessions":
                    data = app.sessions(zid)
                    self.need(data is not None, "Routeur inconnu")
                    return data
                if what == "forfaits" and app.tickets:
                    return {"forfaits": app.tickets.list_forfaits(zid, who.username if who.is_reseller else None)}
                if what == "profiles" and app.tickets:
                    return {"profiles": app.tickets.profiles(zid)}
            if app.routers and p == "/api/routers":
                self.need(who.can("routers_read"))
                out = {"routers": [r for r in app.routers.list() if who.can_zone(r["id"])], "demo": app.demo,
                       "can_write": who.can("routers_write"), "role": who.role,
                       "features": {f: who.has(f) for f in ("remote", "antennas")}}
                if not who.has("remote"):  # accès à distance : selon la formule
                    for r in out["routers"]:
                        r["remote"] = None
                if who.role == "client":
                    out["quota"] = {"max": who.max_routers, "used": len(app.routers.owned(who.username))}
                elif who.is_admin and app.users:
                    rows, _ = app.users._q("SELECT username, name FROM users WHERE role = 'client'")
                    names = dict(rows)
                    for r in out["routers"]:
                        r["owner_name"] = names.get(r["owner"], r["owner"]) if r.get("owner") else None
                return out
            if app.routers and len(parts) == 5 and parts[2] == "routers" and parts[4] == "logo":
                self.need(who.can_zone(parts[3]))
                logo = app.routers.logo(parts[3])
                self.need(logo is not None, "Pas de logo")
                return self.send(200, bytes(logo[1]), logo[0], {"Cache-Control": "private, max-age=86400"})
            if app.routers and len(parts) == 5 and parts[2] == "routers" and parts[4] == "login":
                self.need(who.can("routers_write") and who.can_zone(parts[3]))
                return app.routers.get_login(parts[3])
            if app.routers and len(parts) == 5 and parts[2] == "routers" and parts[4] == "scan":
                self.need(who.can("routers_write") and who.can_zone(parts[3]))
                self.need_feature(who, "antennas")
                return app.routers.lan_scan(parts[3])
            if app.routers and len(parts) == 5 and parts[2] == "routers" and parts[4] == "install":
                self.need(who.can("routers_write") and who.can_zone(parts[3]))
                return app.routers.install_info(parts[3], self.headers.get("Host", ""))
            if p == "/api/roaming" and app.roaming:
                self.need(who.can("routers_write"))
                groups = app.roaming.groups(who.can_zone, who.username, who.role == "admin")
                for g in groups:
                    g.pop("secret", None)
                now = int(time.time())
                ignored = [{"ip": ip, "ago": now - ts, "name": nm} for ip, (ts, nm) in app.roaming.ignored.items()
                           if now - ts < 3600] if who.role == "admin" else []
                return {"groups": groups, "listening": app.roaming.listening, "ports": app.roaming.ports(), "ignored": ignored,
                        "zones": [{"id": z["id"], "name": z["name"],
                                   "group": (app.roaming.group_of_zone(z["id"]) or {}).get("id")} for z in app.zones_for(who)]}
            if len(parts) == 5 and parts[2] == "roaming" and parts[4] == "script" and parts[3].isdigit() and app.roaming:
                self.need(who.can("routers_write"))
                g = app.roaming.group(int(parts[3]))
                self.need(g and (who.role == "admin" or g["owner"] == who.username), "Réseau introuvable")
                z = next((app.routers.zone(zid["id"]) for zid in g["zones"]), None)
                wg = app.routers.wg()
                srv = wg.get("server", "10.66.0.1") if wg else (app.routers._base(z, self.headers.get("Host", ""))[1] if z else "")
                lines = (app.roaming.script_lines(g, srv, wg.get("ovpn_server", "10.67.0.1")) if wg
                         else app.roaming.script_lines(g, srv))
                return {"script": "\r\n".join(lines), "server": srv}
            if app.tickets and p == "/api/batches/summary":
                zone_ids, mine = app.sales_scope(who)
                summary = app.tickets.batch_summary(zone_ids, mine)
                return {"zones": [dict(id=z["id"], name=z["name"], **summary.get(z["id"], {"lots": 0, "tickets": 0, "used": 0,
                                  "left": 0, "sold": 0, "left_value": 0, "last": None})) for z in app.zones_for(who)],
                        "demo": app.demo}
            if app.tickets and p == "/api/batches":
                zid = q.get("zone", [None])[0]
                if zid:  # un seul routeur : tous ses lots
                    self.need(who.can_zone(zid), "Routeur introuvable")
                    lots = [b for b in app.tickets.list_batches(limit=2000, zone_id=zid) if app.batch_visible(b, who)]
                    return {"batches": lots, "demo": app.demo, "can_manage": who.can("lots_manage")}
                lots = [b for b in app.tickets.list_batches(limit=300) if app.batch_visible(b, who)]
                return {"batches": lots[:150], "demo": app.demo, "can_manage": who.can("lots_manage")}
            if app.tickets and len(parts) == 4 and parts[2] == "batches" and parts[3].isdigit():
                b = app.tickets.get_batch(int(parts[3]))
                self.need(app.batch_visible(b, who), "Lot introuvable")
                return dict(b, demo=app.demo)
            if app.tickets and p == "/api/sales":
                try:
                    days = max(1, min(365, int(q.get("days", ["30"])[0])))
                except ValueError:
                    days = 30
                zone_ids, mine = app.sales_scope(who)
                return app.tickets.sales_history(days, app.offset(), zone_ids, mine)
            if app.tickets and p == "/api/analytique":
                from .analytics import analytics
                try:
                    days = int(q.get("days", ["30"])[0])
                except ValueError:
                    days = 30
                days = days if days in (7, 30, 90) else 30
                zone_ids, mine = app.sales_scope(who)
                out = dict(app._cached(("analytique", days, tuple(zone_ids or ()), mine),
                                       lambda: analytics(app.store, app.tickets, days, app.offset(), zone_ids, mine), ttl=300))
                out.update(demo=app.demo, me=who.public())
                return out
            if app.tickets and p == "/api/report":
                return app.report(q, who)
            if app.tickets and p == "/api/tickets/find":
                code = q.get("code", [""])[0].strip()[:20]
                res = [r for r in app.tickets.find(code) if app.batch_visible(r["batch"], who)]
                return {"results": app.tickets.status(res)}
            if app.users and p == "/api/users":
                self.need(who.can("team"))
                off = app.offset()
                month = time.localtime(time.time() + off - time.timezone)
                since = int(time.mktime((month.tm_year, month.tm_mon, 1, 0, 0, 0, 0, 0, -1))) - off + time.timezone
                mine = None if who.is_admin else who.username
                return {"users": app.users.list(since, parent=mine), "role": who.role,
                        "zones": [{"id": z["id"], "name": z["name"], "owner": z.get("owner")}
                                  for z in app.config.get("zones", []) if who.can_zone(z["id"])]}
            if app.users and len(parts) == 5 and parts[2] == "users" and parts[3].isdigit() and parts[4] == "history":
                self.need(who.can("users") or self.my_team(who, int(parts[3])))
                return {"history": app.users.history(int(parts[3]))}
            if p == "/api/backup":
                self.need(who.can("settings"))
                return self.send_backup()
            if p == "/api/security" and app.guard:
                self.need(who.can("settings"))
                return app.guard.status()
            if p.startswith("/api/backups/") and app.guard:
                self.need(who.can("settings"))
                f = app.guard.backup_path(p[len("/api/backups/"):])
                self.need(f, "Sauvegarde introuvable")
                return self.send(200, f.read_bytes(), "application/zip",
                                 {"Content-Disposition": f'attachment; filename="{f.name}"'})
            if app.updater and p == "/api/settings":
                self.need(who.can("settings"))
                from .routers import local_ip_towards
                zones = app.config.get("zones", [])
                lan = local_ip_towards(zones[0]["router"]["host"] if zones and zones[0].get("router") else "8.8.8.8")
                port = (self.headers.get("Host", "").partition(":")[2]) or "8080"
                return {"update": app.updater.info(), "demo": app.demo, "contact": app.config.get("contact") or {},
                        "phone_url": f"http://{lan}:{port}" if lan else None}
            raise Forbidden("Introuvable")

        # --- routes POST ---
        def api_post(self, p):
            # Protection CSRF : en-tête personnalisé impossible à envoyer depuis un autre site
            if self.headers.get("X-AderLink") != "1":
                return self.json({"error": "Requête refusée"}, 403)
            who = self.who()
            if not who:
                return self.json({"error": "Non connecté"}, 401)
            cap = 700_000 if p.endswith("/logo") else 16384  # un logo en base64, sinon de petits formulaires
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > cap:
                return self.json({"error": "Envoi trop volumineux"}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.json({"error": "JSON invalide"}, 400)
            status = 201 if p in ("/api/batches", "/api/forfaits", "/api/routers", "/api/users") else 200
            return self.call(lambda: self.api_post_do(p, body, who), status)

        def api_post_do(self, p, body, who):
            parts = p.split("/")
            T, R, U = app.tickets, app.routers, app.users
            # tickets
            if p == "/api/batches":
                f = T.get_forfait(int(body.get("forfait_id") or 0)) if body.get("forfait_id") else None
                zid = f["zone_id"] if f else str(body.get("zone_id", ""))
                self.need(who.can_zone(zid))
                if who.is_reseller:
                    self.need(f is not None, "Choisissez un profil")
                    self.need(not f.get("reseller") or f["reseller"] == who.username, "Profil introuvable")
                    mine = U.pvs_of(who, zid) if U else []
                    want = int(body.get("pos_id") or 0)
                    body = dict(body, pos_id=want if want in mine else (mine[0] if mine else None))
                elif body.get("pos_id") and R:
                    pv = R.get_pv(int(body["pos_id"]))
                    self.need(pv and pv["zone_id"] == zid, "Point de vente invalide")
                res = T.create_batch(body, created_by=who.username)
                if app.collector and res.get("created") and not res.get("roaming"):
                    app.collector.add_tickets(zid, res["created"])
                return res
            # sécurité : Telegram et sauvegardes
            if p.startswith("/api/security/") and app.guard:
                self.need(who.can("settings"))
                G, act = app.guard, p[len("/api/security/"):]
                fn = {"telegram": lambda: G.set_token(body.get("token")), "link": G.link, "unlink": G.unlink,
                      "test": G.test, "person-add": G.add_person,
                      "person": lambda: G.set_person(body.get("id"), body.get("alerts"), bool(body.get("remove"))),
                      "backup": lambda: dict(G.backup_now(), **G.status())}.get(act)
                self.need(fn, "Introuvable")
                return fn()
            # ADER Roaming
            if app.roaming and len(parts) == 5 and parts[2] == "roaming" and parts[4] == "check" and parts[3].isdigit():
                self.need(who.can("routers_write"))
                g = app.roaming.group(int(parts[3]))
                self.need(g and (who.role == "admin" or g["owner"] == who.username), "Réseau introuvable")
                return app.roaming.diagnose(g["id"])
            if app.roaming and (p == "/api/roaming" or (len(parts) >= 4 and parts[2] == "roaming" and parts[3].isdigit())):
                self.need(who.can("routers_write"))
                if not (len(parts) == 5 and parts[4] == "delete"):
                    self.need_feature(who, "roaming")
                    lim = (who.limits or {}).get("roaming_routers", -1)
                    if who.limits and lim != -1 and len(body.get("zones") or []) > lim:
                        raise Forbidden(f"Votre formule permet {lim} routeur{'s' if lim > 1 else ''} en Roaming. "
                                        "Passez à la formule supérieure dans « Mon abonnement ».")
                owner = None if who.role == "admin" else who.username
                if p == "/api/roaming":
                    g = app.roaming.save_group(body, owner, who.can_zone)
                else:
                    g = app.roaming.group(int(parts[3]))
                    self.need(g and (who.role == "admin" or g["owner"] == who.username), "Réseau introuvable")
                    if len(parts) == 5 and parts[4] == "delete":
                        return app.roaming.delete_group(g["id"])
                    g = app.roaming.save_group(body, g["owner"], who.can_zone, gid=g["id"])
                g.pop("secret", None)
                return g
            if p == "/api/forfaits":
                self.need(who.can("forfaits_write"))
                self.need(who.can_zone(str(body.get("zone_id", ""))))
                return T.create_forfait(body, reseller=who.username if who.is_reseller else None)
            if len(parts) == 4 and parts[2] == "forfaits" and parts[3].isdigit():
                self.need(who.can("forfaits_write"))
                f = T.get_forfait(int(parts[3]))
                self.need(f and who.can_zone(f["zone_id"]), "Profil introuvable")
                self.need(not who.is_reseller or f.get("reseller") == who.username, "Vous ne pouvez modifier que vos profils")
                return T.update_forfait(int(parts[3]), body)
            if len(parts) == 5 and parts[2] == "forfaits" and parts[4] == "archive" and parts[3].isdigit():
                self.need(who.can("forfaits_write"))
                f = T.get_forfait(int(parts[3]))
                self.need(f and who.can_zone(f["zone_id"]), "Profil introuvable")
                self.need(not who.is_reseller or f.get("reseller") == who.username, "Vous ne pouvez retirer que vos profils")
                return T.archive_forfait(int(parts[3]))
            if len(parts) == 5 and parts[2] == "batches" and parts[4] == "remove-unused" and parts[3].isdigit():
                self.need(who.can("lots_manage"))
                self.need(app.batch_visible(T.get_batch(int(parts[3])), who), "Lot introuvable")
                return T.remove_unused(int(parts[3]))
            # routeurs
            if R and p == "/api/routers":
                self.need(who.can("routers_write"))
                if who.role == "client":
                    used = len(R.owned(who.username))
                    if used >= who.max_routers:
                        c = app.config.get("contact") or {}
                        raise Forbidden(f"Votre abonnement permet {who.max_routers} routeur{'s' if who.max_routers > 1 else ''}. "
                                        f"Pour en ajouter, contactez {c.get('name') or 'votre fournisseur'}"
                                        + (f" au {c['phone']}" if c.get("phone") else "") + ".")
                    return R.create(body, owner=who.username)
                return R.create(body)
            if R and len(parts) == 5 and parts[2] == "routers":
                zid, action = parts[3], parts[4]
                self.need(who.can_zone(zid))
                if action == "test":
                    self.need(who.can("routers_read"))
                    return R.test(zid)
                if action == "pv" and who.is_reseller and U:  # un client crée son propre point de vente
                    return U.create_pv_for(who, zid, body)
                self.need(who.can("routers_write"))
                if action == "winbox":
                    self.need_feature(who, "remote")
                if action == "devices":
                    self.need_feature(who, "antennas")
                acts = {"update": lambda: R.update(zid, body), "delete": lambda: R.delete(zid),
                        "logo": lambda: (app._cache.pop(("logos",), None), R.set_logo(zid, body.get("image")))[1],
                        "pv": lambda: R.create_pv(zid, body),
                        "winbox": lambda: R.open_winbox(zid, self.client_ip() in ("127.0.0.1", "::1")),
                        "devices": lambda: R.add_device(zid, body),
                        "login": lambda: R.set_login(zid, body, who.can_zone)}
                if action in acts:
                    return acts[action]()
            if R and len(parts) == 7 and parts[2] == "routers" and parts[4] == "devices" and parts[6] == "delete" and parts[5].isdigit():
                self.need(who.can_zone(parts[3]) and who.can("routers_write"))
                return R.remove_device(parts[3], int(parts[5]))
            if R and len(parts) == 5 and parts[2] == "pv" and parts[4] == "archive" and parts[3].isdigit():
                pv = R.get_pv(int(parts[3]))
                self.need(pv and who.can_zone(pv["zone_id"]))
                if who.is_reseller:  # un client retire seulement ses points de vente, jamais le principal
                    mine = U.pvs_of(who, pv["zone_id"]) if U else []
                    self.need(pv["id"] in mine[1:], "Vous ne pouvez retirer que les points de vente que vous avez créés")
                else:
                    self.need(who.can("routers_write"))
                return R.archive_pv(int(parts[3]))
            # utilisateurs
            if U and p == "/api/me/password":
                return U.change_own_password(who, body, lambda: app.save_config(app.config))
            if U and p == "/api/users":
                self.need(who.can("team"))
                return U.create(body, parent=None if who.is_admin else who.username)
            if U and p == "/api/plans":
                self.need(who.can("settings"))
                return U.plans.create(body)
            if U and p == "/api/billing":
                self.need(who.can("settings"))
                b = U.plans.set_billing(body)
                app.save_config(app.config)
                return {"billing": b}
            if U and len(parts) == 4 and parts[2] == "plans" and parts[3].isdigit():
                self.need(who.can("settings"))
                return U.plans.update(int(parts[3]), body)
            if U and len(parts) == 5 and parts[2] == "plans" and parts[3].isdigit() and parts[4] == "archive":
                self.need(who.can("settings"))
                return U.plans.archive(int(parts[3]))
            if U and len(parts) == 5 and parts[2] == "users" and parts[3].isdigit():
                uid, action = int(parts[3]), parts[4]
                # le client ne gère que son équipe, et pas l'abonnement ni les routeurs autorisés
                self.need(who.can("users") or (self.my_team(who, uid) and action not in ("subscription", "routers", "plan")))
                acts = {"update": lambda: U.update(uid, body), "password": lambda: U.reset_password(uid, body),
                        "enable": lambda: U.set_active(uid, True), "disable": lambda: U.set_active(uid, False),
                        "subscription": lambda: U.subscription(uid, body),
                        "routers": lambda: U.set_max_routers(uid, body),
                        "plan": lambda: U.set_plan(uid, body),
                        "delete": lambda: U.delete(uid)}
                if action in acts:
                    return acts[action]()
            # paramètres
            if app.updater and p == "/api/settings/update":
                self.need(who.can("settings"))
                self.need(not app.demo, "Indisponible en mode démonstration")
                repo = str(body.get("repo", "")).strip().strip("/")
                repo = repo.replace("https://github.com/", "").replace("http://github.com/", "")
                if repo and (repo.count("/") != 1 or not all(c.isalnum() or c in "-_./" for c in repo)):
                    raise Forbidden("Écrivez le dépôt sous la forme utilisateur/aderlink")
                app.config["update"] = {"repo": repo, "branch": str(body.get("branch") or "main").strip(),
                                        "auto": bool(body.get("auto", True))}
                app.save_config(app.config)
                return {"update": app.updater.info()}
            if p == "/api/settings/contact":
                self.need(who.can("settings"))
                import re as _re
                name = " ".join(str(body.get("name") or "").split())[:60]
                phone = _re.sub(r"[^0-9+ ]", "", str(body.get("phone") or ""))[:20].strip()
                app.config["contact"] = {"name": name, "phone": phone}
                app.save_config(app.config)
                return {"contact": app.config["contact"]}
            if app.updater and p == "/api/settings/check":
                self.need(who.can("settings"))
                return {"update": app.updater.check(install=bool(body.get("install")))}
            raise Forbidden("Introuvable")

        def do_POST(self):
            return self._timed(self._do_post)

        def _do_post(self):
            p = urlparse(self.path).path
            if p == "/api/restore":
                return self.restore_backup(self.who())
            if p.startswith("/api/"):
                return self.api_post(p)
            if p != "/login":
                return self.send(404, b"Introuvable")
            ip = self.client_ip()
            if app.throttle.blocked(ip):
                return self.redirect("/login?e=bloque")
            length = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            username = form.get("username", [""])[0].strip()
            password = form.get("password", [""])[0]
            if app.users:
                from .users import SubscriptionExpired
                try:
                    ok = app.users.authenticate(username, password)
                except SubscriptionExpired:
                    app.throttle.reset(ip)
                    return self.redirect("/login?e=expire")
            else:
                admin = app.config["admin"]
                ok = admin["username"] if (hmac.compare_digest(username, admin["username"])
                                          and verify_password(password, admin["password_hash"])) else None
            if ok:
                app.throttle.reset(ip)
                cookie = (f"{COOKIE}={make_token(secret, ok)}; Path=/; HttpOnly; SameSite=Lax; "
                          f"Max-Age={SESSION_HOURS * 3600}" + ("; Secure" if secure or self.https() else ""))
                return self.redirect("/", cookie)
            app.throttle.fail(ip)
            self.redirect("/login?e=1")

    return Handler


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    app.keep_warm()
    print(f"ADER LINK en écoute sur http://{host}:{port}" + ("  (mode démonstration)" if app.demo else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
