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


class App:
    def __init__(self, config, collector, store, demo=False, tickets=None, routers=None, updater=None,
                 users=None, save_config=None, paths=None):
        self.config = config
        self.collector = collector
        self.store = store
        self.demo = demo
        self.tickets = tickets
        self.routers = routers
        self.updater = updater
        self.users = users
        self.save_config = save_config or (lambda c: None)
        self.paths = paths or {}
        self.throttle = LoginThrottle()

    def offset(self):
        return int(float(self.config.get("utc_offset_hours", 1)) * 3600)  # Niger : UTC+1

    def zones_for(self, who):
        return [z for z in self.config.get("zones", []) if who.can_zone(z["id"])]

    def zones_list(self, who):
        out = []
        for z in self.zones_for(who):
            pvs = self.routers.list_pv(z["id"]) if self.routers else []
            if who.is_reseller:  # le revendeur ne voit que son point de vente
                mine = self.users.pv_for(who, z["id"]) if self.users else None
                pvs = [p for p in pvs if p["id"] == mine]
            out.append({"id": z["id"], "name": z["name"], "ssid": z.get("ssid") or z["name"],
                        "code_length": z.get("code_length", 8), "points_vente": pvs})
        return out

    def sales_scope(self, who):
        zone_ids = None if who.zones is None else list(who.zones)
        return zone_ids, (who.username if who.is_reseller else None)

    def overview(self, range_key, who):
        hours = {"6h": 6, "24h": 24, "7j": 168}.get(range_key, 24)
        bucket = 300 if hours <= 24 else 3600
        state = self.collector.snapshot()
        zones = []
        for z in self.zones_for(who):
            s = state.get(z["id"], {})
            pending = not (z.get("router") or {}).get("host")
            zones.append({
                "id": z["id"], "name": z["name"], "location": z.get("location", ""),
                "status": "pending" if pending else "unknown" if not s else ("online" if s.get("online") else "offline"),
                "has_logo": bool(self.routers and self.routers.logo(z["id"]) is not None),
                "error": s.get("error"), "checked_at": s.get("checked_at"),
                "last_seen": s.get("last_seen"), "identity": s.get("identity"),
                "board": s.get("board"), "version": s.get("version"),
                "uptime_s": s.get("uptime_s"), "cpu": s.get("cpu"), "memory": s.get("memory"),
                "tickets": s.get("tickets"), "active": s.get("active", 0),
                "rx_bps": s.get("rx_bps", 0), "tx_bps": s.get("tx_bps", 0),
            })
        online = [z for z in zones if z["status"] == "online"]
        off = self.offset()
        midnight = (int(time.time()) + off) // 86400 * 86400 - off
        zone_ids, mine = self.sales_scope(who)
        sales = self.tickets.sales(midnight, zone_ids, mine) if self.tickets else []
        since = int(time.time()) - hours * 3600
        if who.zones is None:
            history = self.store.history(since, bucket)
        else:
            merged = {}
            for zid in who.zones:
                for pt in self.store.history(since, bucket, zid):
                    m = merged.setdefault(pt["ts"], {"ts": pt["ts"], "active": 0, "rx_bps": 0, "tx_bps": 0})
                    for k in ("active", "rx_bps", "tx_bps"):
                        m[k] += pt[k]
            history = [merged[k] for k in sorted(merged)]
        events = [e for e in self.store.events(40) if who.can_zone(e["zone_id"])][:15]
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
        names = {self.config["admin"]["username"]: "Administrateur"}
        if self.users:
            rows, _ = self.users._q("SELECT username, name FROM users")
            names.update(dict(rows))
        rep = self.tickets.report(d1 * 86400 - off, (d2 + 1) * 86400 - off, off, zone_ids, mine, names)
        rep.update({"from": str(dt.date(1970, 1, 1) + dt.timedelta(days=d1)),
                    "to": str(dt.date(1970, 1, 1) + dt.timedelta(days=d2)),
                    "today": str(dt.date(1970, 1, 1) + dt.timedelta(days=today)),
                    "me": who.public(), "demo": self.demo,
                    "zones": [z["name"] for z in self.zones_for(who)]})
        if who.is_reseller:
            rep["commission"] = round(rep["totals"]["amount"] * (who.commission or 0) / 100)
        return rep

    def batch_visible(self, b, who):
        if not b or not who.can_zone(b["zone_id"]):
            return False
        return not who.is_reseller or b.get("created_by") == who.username


PAGES = {"/": ("dashboard.html", None), "/tickets": ("tickets.html", None), "/imprimer": ("print.html", None),
         "/lots": ("lots.html", None), "/controle": ("controle.html", None),
         "/routeurs": ("routeurs.html", "routers_read"), "/parametres": ("parametres.html", "settings"),
         "/utilisateurs": ("utilisateurs.html", "team"), "/rapports": ("rapports.html", None), "/analytique": ("analytique.html", None), "/profil": ("profil.html", None)}


def make_handler(app):
    secret = app.config["secret_key"]
    secure = app.config.get("secure_cookies", False)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ADERLINK"
        sys_version = ""

        def log_message(self, fmt, *args):  # journal discret
            if "/api/" not in self.path:
                super().log_message(fmt, *args)

        # --- outils ---
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
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
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

        def static(self, name):
            path = (STATIC / name).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():
                return self.send(404, b"Introuvable")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self.send(200, path.read_bytes(), ctype)

        def call(self, fn, status=200):
            from .routers import RouterError
            from .tickets import TicketError
            from .users import UserError
            try:
                out = fn()
                if out is None:  # réponse déjà envoyée (image)
                    return None
                return self.json(out, status)
            except Forbidden as e:
                return self.json({"error": str(e) or "Accès refusé"}, 403)
            except (RouterError, TicketError, UserError) as e:
                return self.json({"error": str(e)}, 400)

        def send_backup(self):
            """Sauvegarde complète : config.json + base de données (copie cohérente)."""
            import io
            import sqlite3
            import tempfile
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("config.json", json.dumps(app.config, ensure_ascii=False, indent=2))
                with tempfile.TemporaryDirectory() as tmp:
                    copy = Path(tmp) / "aderlink.db"
                    dst = sqlite3.connect(copy)
                    with app.store.lock:
                        app.store.db.backup(dst)
                    dst.close()
                    zf.write(copy, "aderlink.db")
                zf.writestr("LISEZMOI.txt", "Sauvegarde ADER LINK. A restaurer dans Parametres > Restaurer une sauvegarde.\n")
            name = time.strftime("aderlink-sauvegarde-%Y-%m-%d-%H%M.zip", time.localtime())
            return self.send(200, buf.getvalue(), "application/zip",
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
            for k in ("secure_cookies", "behind_proxy", "server_ip_for_routers", "wireguard"):
                if k in app.config:
                    cfg[k] = app.config[k]
                else:
                    cfg.pop(k, None)
            dbp = Path(app.paths["db"])
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
            url = urlparse(self.path)
            p = url.path
            q = parse_qs(url.query)
            if p == "/favicon.ico":
                return self.static("favicon.ico")
            if p == "/api/version":
                from .updater import local_version
                return self.json({"version": app.updater.state["current"] if app.updater else local_version()})
            if p.startswith("/install/") and p.endswith(".rsc") and app.routers:
                z = app.routers.by_token(p[len("/install/"):-4])
                if not z:
                    return self.send(404, b"# Lien d'installation inconnu")
                profiles = app.tickets.profile_specs(z["id"]) if app.tickets else []
                return self.send(200, app.routers.install_script(z, self.headers.get("Host", ""), profiles).encode(),
                                 "text/plain; charset=utf-8")
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
                                         f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
                return self.static("login.html")
            if p == "/api/public":
                return self.json({"demo": app.demo, "contact": app.config.get("contact") or {}})
            if p.startswith("/static/"):
                return self.static(p[len("/static/"):])
            if p == "/logout":
                return self.redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
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
            if p == "/api/zones":
                return {"zones": app.zones_list(who), "demo": app.demo}
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
                    return {"forfaits": app.tickets.list_forfaits(zid)}
                if what == "profiles" and app.tickets:
                    return {"profiles": app.tickets.profiles(zid)}
            if app.routers and p == "/api/routers":
                self.need(who.can("routers_read"))
                out = {"routers": [r for r in app.routers.list() if who.can_zone(r["id"])], "demo": app.demo,
                       "can_write": who.can("routers_write"), "role": who.role}
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
            if app.routers and len(parts) == 5 and parts[2] == "routers" and parts[4] == "install":
                self.need(who.can("routers_write") and who.can_zone(parts[3]))
                return app.routers.install_info(parts[3], self.headers.get("Host", ""))
            if app.tickets and p == "/api/batches":
                zid = q.get("zone", [None])[0]
                lots = [b for b in app.tickets.list_batches(limit=300, zone_id=zid) if app.batch_visible(b, who)]
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
                out = analytics(app.store, app.tickets, days, app.offset(), zone_ids, mine)
                out.update(demo=app.demo, me=who.public())
                return out
            if app.tickets and p == "/api/report":
                return app.report(q, who)
            if app.tickets and p == "/api/tickets/find":
                code = q.get("code", [""])[0].strip()[:20]
                res = [r for r in app.tickets.find(code) if app.batch_visible(r["batch"], who)]
                return {"results": res}
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
                    body = dict(body, pos_id=U.pv_for(who, zid) if U else None)
                elif body.get("pos_id") and R:
                    pv = R.get_pv(int(body["pos_id"]))
                    self.need(pv and pv["zone_id"] == zid, "Point de vente invalide")
                res = T.create_batch(body, created_by=who.username)
                if app.collector and res.get("created"):
                    app.collector.add_tickets(zid, res["created"])
                return res
            if p == "/api/forfaits":
                self.need(who.can("forfaits_write"))
                self.need(who.can_zone(str(body.get("zone_id", ""))))
                return T.create_forfait(body)
            if len(parts) == 5 and parts[2] == "forfaits" and parts[4] == "archive" and parts[3].isdigit():
                self.need(who.can("forfaits_write"))
                f = T.get_forfait(int(parts[3]))
                self.need(f and who.can_zone(f["zone_id"]), "Profil introuvable")
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
                self.need(who.can("routers_write"))
                acts = {"update": lambda: R.update(zid, body), "delete": lambda: R.delete(zid),
                        "logo": lambda: R.set_logo(zid, body.get("image")),
                        "pv": lambda: R.create_pv(zid, body),
                        "winbox": lambda: R.open_winbox(zid, self.client_ip() in ("127.0.0.1", "::1"))}
                if action in acts:
                    return acts[action]()
            if R and len(parts) == 5 and parts[2] == "pv" and parts[4] == "archive" and parts[3].isdigit():
                pv = R.get_pv(int(parts[3]))
                self.need(who.can("routers_write") and pv and who.can_zone(pv["zone_id"]))
                return R.archive_pv(int(parts[3]))
            # utilisateurs
            if U and p == "/api/me/password":
                return U.change_own_password(who, body, lambda: app.save_config(app.config))
            if U and p == "/api/users":
                self.need(who.can("team"))
                return U.create(body, parent=None if who.is_admin else who.username)
            if U and len(parts) == 5 and parts[2] == "users" and parts[3].isdigit():
                uid, action = int(parts[3]), parts[4]
                # le client ne gère que son équipe, et pas l'abonnement ni les routeurs autorisés
                self.need(who.can("users") or (self.my_team(who, uid) and action not in ("subscription", "routers")))
                acts = {"update": lambda: U.update(uid, body), "password": lambda: U.reset_password(uid, body),
                        "enable": lambda: U.set_active(uid, True), "disable": lambda: U.set_active(uid, False),
                        "subscription": lambda: U.subscription(uid, body),
                        "routers": lambda: U.set_max_routers(uid, body),
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
                cookie = (f"{COOKIE}={make_token(secret, ok)}; Path=/; HttpOnly; SameSite=Strict; "
                          f"Max-Age={SESSION_HOURS * 3600}" + ("; Secure" if secure else ""))
                return self.redirect("/", cookie)
            app.throttle.fail(ip)
            self.redirect("/login?e=1")

    return Handler


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"ADER LINK en écoute sur http://{host}:{port}" + ("  (mode démonstration)" if app.demo else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
