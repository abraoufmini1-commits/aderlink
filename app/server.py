"""Serveur web ADER LINK : authentification, API JSON et tableau de bord."""
import base64
import hashlib
import hmac
import json
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
class App:
    def __init__(self, config, collector, store, demo=False, tickets=None, routers=None, updater=None):
        self.config = config
        self.collector = collector
        self.store = store
        self.demo = demo
        self.tickets = tickets
        self.routers = routers
        self.updater = updater
        self.throttle = LoginThrottle()

    def zones_list(self):
        return [{"id": z["id"], "name": z["name"], "ssid": z.get("ssid") or z["name"],
                 "code_length": z.get("code_length", 6),
                 "points_vente": self.routers.list_pv(z["id"]) if self.routers else []}
                for z in self.config.get("zones", [])]

    def overview(self, range_key):
        hours = {"6h": 6, "24h": 24, "7j": 168}.get(range_key, 24)
        bucket = 300 if hours <= 24 else 3600
        state = self.collector.snapshot()
        zones = []
        for z in self.config.get("zones", []):
            s = state.get(z["id"], {})
            zones.append({
                "id": z["id"], "name": z["name"], "location": z.get("location", ""),
                "status": "unknown" if not s else ("online" if s.get("online") else "offline"),
                "error": s.get("error"), "checked_at": s.get("checked_at"),
                "last_seen": s.get("last_seen"), "identity": s.get("identity"),
                "board": s.get("board"), "version": s.get("version"),
                "uptime_s": s.get("uptime_s"), "cpu": s.get("cpu"), "memory": s.get("memory"),
                "tickets": s.get("tickets"), "active": s.get("active", 0),
                "rx_bps": s.get("rx_bps", 0), "tx_bps": s.get("tx_bps", 0),
            })
        online = [z for z in zones if z["status"] == "online"]
        off = int(float(self.config.get("utc_offset_hours", 1)) * 3600)  # Niger : UTC+1
        midnight = (int(time.time()) + off) // 86400 * 86400 - off
        sales = self.tickets.sales(midnight) if self.tickets else []
        return {
            "sales_today": {"tickets": sum(s["tickets"] for s in sales),
                            "amount": sum(s["amount"] for s in sales)},
            "demo": self.demo,
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
            "history": self.store.history(int(time.time()) - hours * 3600, bucket),
            "events": self.store.events(15),
        }

    def sessions(self, zone_id):
        zone = next((z for z in self.config.get("zones", []) if z["id"] == zone_id), None)
        if not zone:
            return None
        s = self.collector.snapshot().get(zone_id, {})
        return {"zone": {"id": zone_id, "name": zone["name"]},
                "online": s.get("online", False), "sessions": s.get("sessions", [])}


def make_handler(app):
    secret = app.config["secret_key"]
    admin = app.config["admin"]
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

        def user(self):
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(COOKIE)
            return read_token(secret, morsel.value) if morsel else None

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
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send(200, path.read_bytes(), ctype)

        # --- routes ---
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            p = url.path
            if p == "/favicon.ico":
                return self.static("favicon.ico")
            if p == "/api/version":
                from .updater import local_version
                return self.json({"version": app.updater.state["current"] if app.updater else local_version()})
            if p.startswith("/install/") and p.endswith(".rsc") and app.routers:
                z = app.routers.by_token(p[len("/install/"):-4])
                if not z:
                    return self.send(404, b"# Lien d'installation inconnu")
                return self.send(200, app.routers.install_script(z).encode(), "text/plain; charset=utf-8")
            if p == "/healthz":
                return self.send(200, b"ok")
            if p == "/login":
                return self.static("login.html")
            if p == "/api/public":
                return self.json({"demo": app.demo})
            if p.startswith("/static/"):
                return self.static(p[len("/static/"):])
            if p == "/logout":
                return self.redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            user = self.user()
            pages = {"/": "dashboard.html", "/tickets": "tickets.html", "/imprimer": "print.html",
                     "/lots": "lots.html", "/controle": "controle.html", "/routeurs": "routeurs.html",
                     "/parametres": "parametres.html"}
            if p in pages:
                return self.static(pages[p]) if user else self.redirect("/login")
            if not user:
                return self.json({"error": "Non connecté"}, 401)
            if p == "/api/zones":
                return self.json({"zones": app.zones_list(), "demo": app.demo})
            if app.routers and p == "/api/routers":
                return self.json({"routers": app.routers.list(), "demo": app.demo})
            if app.routers and p.startswith("/api/routers/") and p.endswith("/install"):
                return self.router_call(lambda: app.routers.install_info(p.split("/")[3], self.headers.get("Host", "")))
            if app.updater and p == "/api/settings":
                from .routers import local_ip_towards
                zones = app.config.get("zones", [])
                lan = local_ip_towards(zones[0]["router"]["host"] if zones and zones[0].get("router") else "8.8.8.8")
                port = (self.headers.get("Host", "").partition(":")[2]) or "8080"
                return self.json({"update": app.updater.info(), "demo": app.demo,
                                  "phone_url": f"http://{lan}:{port}" if lan else None})
            if app.tickets and p.startswith("/api/zones/") and p.endswith("/profiles"):
                return self.ticket_call(lambda: {"profiles": app.tickets.profiles(p.split("/")[3])})
            if app.tickets and p.startswith("/api/zones/") and p.endswith("/forfaits"):
                return self.ticket_call(lambda: {"forfaits": app.tickets.list_forfaits(p.split("/")[3])})
            if app.tickets and p == "/api/batches":
                zid = parse_qs(url.query).get("zone", [None])[0]
                return self.json({"batches": app.tickets.list_batches(zone_id=zid), "demo": app.demo})
            if app.tickets and p.startswith("/api/batches/"):
                try:
                    b = app.tickets.get_batch(int(p.split("/")[3]))
                except ValueError:
                    b = None
                return self.json(dict(b, demo=app.demo)) if b else self.json({"error": "Lot introuvable"}, 404)
            if app.tickets and p == "/api/sales":
                try:
                    days = max(1, min(365, int(parse_qs(url.query).get("days", ["30"])[0])))
                except ValueError:
                    days = 30
                off = int(float(app.config.get("utc_offset_hours", 1)) * 3600)
                return self.json(app.tickets.sales_history(days, off))
            if app.tickets and p == "/api/tickets/find":
                code = parse_qs(url.query).get("code", [""])[0].strip()[:20]
                return self.json({"results": app.tickets.find(code)})
            if p == "/api/me":
                return self.json({"username": user, "demo": app.demo})
            if p == "/api/overview":
                rng = parse_qs(url.query).get("range", ["24h"])[0]
                return self.json(app.overview(rng))
            if p.startswith("/api/zones/") and p.endswith("/sessions"):
                data = app.sessions(p.split("/")[3])
                return self.json(data) if data else self.json({"error": "Zone inconnue"}, 404)
            self.send(404, b"Introuvable")

        def router_call(self, fn, status=200):
            from .routers import RouterError
            try:
                return self.json(fn(), status)
            except RouterError as e:
                return self.json({"error": str(e)}, 400)

        def ticket_call(self, fn, status=200):
            from .tickets import TicketError
            try:
                return self.json(fn(), status)
            except TicketError as e:
                return self.json({"error": str(e)}, 400)

        def api_post(self, p):
            # Protection CSRF : en-tête personnalisé impossible à envoyer depuis un autre site
            if self.headers.get("X-AderLink") != "1":
                return self.json({"error": "Requête refusée"}, 403)
            if not self.user():
                return self.json({"error": "Non connecté"}, 401)
            if not app.tickets:
                return self.send(404, b"Introuvable")
            length = min(int(self.headers.get("Content-Length", 0) or 0), 16384)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.json({"error": "JSON invalide"}, 400)
            parts = p.split("/")
            if app.routers:
                R = app.routers
                if p == "/api/routers":
                    return self.router_call(lambda: R.create(body), 201)
                if len(parts) == 5 and parts[2] == "routers":
                    zid, action = parts[3], parts[4]
                    acts = {"update": lambda: R.update(zid, body), "delete": lambda: R.delete(zid),
                            "test": lambda: R.test(zid), "pv": lambda: R.create_pv(zid, body)}
                    if action in acts:
                        return self.router_call(acts[action], 201 if action == "pv" else 200)
                if len(parts) == 5 and parts[2] == "pv" and parts[4] == "archive" and parts[3].isdigit():
                    return self.router_call(lambda: R.archive_pv(int(parts[3])))
            if app.updater and p == "/api/settings/update":
                if app.demo:
                    return self.json({"error": "Indisponible en mode démonstration"}, 400)
                repo = str(body.get("repo", "")).strip().strip("/")
                repo = repo.replace("https://github.com/", "").replace("http://github.com/", "")
                if repo and (repo.count("/") != 1 or not all(c.isalnum() or c in "-_./" for c in repo)):
                    return self.json({"error": "Écrivez le dépôt sous la forme utilisateur/aderlink"}, 400)
                app.config["update"] = {"repo": repo, "branch": str(body.get("branch") or "main").strip(),
                                        "auto": bool(body.get("auto", True))}
                if app.routers:
                    app.routers._persist()
                return self.json({"update": app.updater.info()})
            if app.updater and p == "/api/settings/check":
                return self.json({"update": app.updater.check(install=bool(body.get("install")))})
            if p == "/api/batches":
                return self.ticket_call(lambda: app.tickets.create_batch(body), 201)
            if p == "/api/forfaits":
                return self.ticket_call(lambda: app.tickets.create_forfait(body), 201)
            if len(parts) == 5 and parts[2] == "forfaits" and parts[4] == "archive" and parts[3].isdigit():
                return self.ticket_call(lambda: app.tickets.archive_forfait(int(parts[3])))
            if len(parts) == 5 and parts[2] == "batches" and parts[4] == "remove-unused" and parts[3].isdigit():
                return self.ticket_call(lambda: app.tickets.remove_unused(int(parts[3])))
            return self.send(404, b"Introuvable")

        def do_POST(self):
            p = urlparse(self.path).path
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
            if hmac.compare_digest(username, admin["username"]) and verify_password(password, admin["password_hash"]):
                app.throttle.reset(ip)
                cookie = (f"{COOKIE}={make_token(secret, username)}; Path=/; HttpOnly; SameSite=Strict; "
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
