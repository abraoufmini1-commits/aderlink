"""Tests : client RouterOS contre un faux routeur, puis collecte et serveur web.

    python3 -m unittest discover -s tests -v
"""
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from app.collector import Collector, Store, parse_duration
from app.routeros import RouterOS, RouterOSError, _encode_length
from app.server import App, hash_password, make_handler, verify_password


# --- faux routeur MikroTik -------------------------------------------------------------
def _read_word(conn):
    c = conn.recv(1)[0]
    if c < 0x80:
        n = c
    elif c < 0xC0:
        n = ((c & 0x3F) << 8) | conn.recv(1)[0]
    else:
        raise NotImplementedError
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    return data.decode()


def _sentence(conn):
    words = []
    while True:
        w = _read_word(conn)
        if w == "":
            return words
        words.append(w)


def _send(conn, *words):
    conn.sendall(b"".join(_encode_length(len(w.encode())) + w.encode() for w in words) + b"\x00")


class FakeRouter(threading.Thread):
    def __init__(self, password="secret", rx=1_000_000):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.password = password
        self.rx = rx
        self.users = {}
        self.added = []
        self.profiles = {"default": {".id": "*0", "name": "default"}, "1-heure": {".id": "*1", "name": "1-heure"}}
        self.active_users = ["482915", "771204"]

    def run(self):
        while True:
            conn, _ = self.sock.accept()
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            while True:
                words = _sentence(conn)
                cmd = words[0]
                attrs = dict(w[1:].split("=", 1) for w in words[1:] if w.startswith("="))
                if cmd == "/login":
                    if attrs.get("password") == self.password:
                        _send(conn, "!done")
                    else:
                        _send(conn, "!trap", "=message=invalid user name or password (6)")
                        _send(conn, "!done")
                elif cmd == "/system/resource/print":
                    _send(conn, "!re", "=uptime=1w2d03:04:05", "=cpu-load=12",
                          "=free-memory=50000000", "=total-memory=200000000",
                          "=board-name=hEX S", "=version=7.16.1 (stable)")
                    _send(conn, "!done")
                elif cmd == "/system/identity/print":
                    _send(conn, "!re", "=name=Zone Test")
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/active/print":
                    q = next((w[len("?user="):] for w in words if w.startswith("?user=")), None)
                    for i, user in enumerate(self.active_users):
                        if q is None or q == user:
                            _send(conn, "!re", f"=.id=*A{i}", f"=user={user}", f"=address=10.5.50.{12 + i}",
                                  f"=mac-address=AA:BB:CC:00:11:{22 + i}", "=uptime=1h2m3s",
                                  "=bytes-in=12345678", "=bytes-out=2345678")
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/active/remove":
                    idx = int(attrs[".id"][2:])
                    self.active_users = [u for i, u in enumerate(self.active_users) if i != idx]
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/print" and "=count-only=" in words:
                    _send(conn, "!done", "=ret=250")
                elif cmd == "/ip/hotspot/user/profile/print":
                    q = next((w[len("?name="):] for w in words if w.startswith("?name=")), None)
                    for name, pr in self.profiles.items():
                        if q is None or q == name:
                            _send(conn, "!re", *[f"={k}={v}" for k, v in pr.items()])
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/profile/add":
                    self.profiles[attrs["name"]] = dict(attrs, **{".id": f"*P{len(self.profiles)}"})
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/profile/set":
                    for pr in self.profiles.values():
                        if pr[".id"] == attrs[".id"]:
                            pr.update({k: v for k, v in attrs.items() if k != ".id"})
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/add":
                    if attrs["name"] in self.users:
                        _send(conn, "!trap", "=message=failure: already have user with this name")
                    else:
                        self.users[attrs["name"]] = dict(attrs, **{".id": f"*{len(self.users) + 1}", "uptime": "0s"})
                        self.added.append(attrs)
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/print" and any(w.startswith("?") for w in words):
                    key, _, val = next(w for w in words if w.startswith("?"))[1:].partition("=")
                    for u in list(self.users.values()):
                        if u.get(key) == val:
                            _send(conn, "!re", *[f"={k}={v}" for k, v in u.items()])
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/remove":
                    for name, u in list(self.users.items()):
                        if u[".id"] == attrs[".id"]:
                            del self.users[name]
                    _send(conn, "!done")
                elif cmd == "/ip/hotspot/user/set":
                    for u in self.users.values():
                        if u[".id"] == attrs[".id"]:
                            u.update({k: v for k, v in attrs.items() if k != ".id"})
                    _send(conn, "!done")
                elif cmd == "/interface/print":
                    self.rx += 2_500_000
                    _send(conn, "!re", "=name=ether1", f"=rx-byte={self.rx}", f"=tx-byte={self.rx // 5}")
                    _send(conn, "!done")
                else:
                    _send(conn, "!trap", "=message=no such command")
                    _send(conn, "!done")
        except (IndexError, OSError):
            conn.close()


class RouterOSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = FakeRouter()
        cls.router.start()

    def test_length_encoding(self):
        self.assertEqual(_encode_length(5), b"\x05")
        self.assertEqual(_encode_length(0x80), b"\x80\x80")
        self.assertEqual(_encode_length(0x4000), b"\xC0\x40\x00")

    def test_query_and_count(self):
        with RouterOS("127.0.0.1", "aderlink", "secret", port=self.router.port) as api:
            res = api.query("/system/resource/print")[0]
            self.assertEqual(res["board-name"], "hEX S")
            self.assertEqual(len(api.query("/ip/hotspot/active/print")), 2)
            self.assertEqual(api.count("/ip/hotspot/user/print"), 250)

    def test_bad_password(self):
        with self.assertRaises(RouterOSError):
            RouterOS("127.0.0.1", "aderlink", "faux", port=self.router.port).connect()

    def test_unreachable(self):
        with self.assertRaises(RouterOSError):
            RouterOS("127.0.0.1", "a", "b", port=1, timeout=1).connect()

    def test_parse_duration(self):
        self.assertEqual(parse_duration("1w2d03:04:05"), 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(parse_duration("1h2m3s"), 3723)


class CollectorAndServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = FakeRouter()
        cls.router.start()
        cls.config = {
            "admin": {"username": "admin", "password_hash": hash_password("motdepasse-test")},
            "secret_key": "x" * 64,
            "poll_seconds": 30,
            "zones": [
                {"id": "z1", "name": "Zone Test", "router": {
                    "host": "127.0.0.1", "port": cls.router.port, "username": "aderlink",
                    "password": "secret", "wan_interface": "ether1"}},
                {"id": "z2", "name": "Zone Coupée", "router": {
                    "host": "127.0.0.1", "port": 1, "username": "a", "password": "b"}},
            ],
        }
        cls.store = Store(":memory:")
        cls.collector = Collector(cls.config, cls.store)
        cls.collector.poll_all()
        time.sleep(0.2)
        cls.collector.poll_all()  # 2e passage : calcul du débit
        app = App(cls.config, cls.collector, cls.store)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_snapshot(self):
        snap = self.collector.snapshot()
        self.assertTrue(snap["z1"]["online"])
        self.assertEqual(snap["z1"]["active"], 2)
        self.assertEqual(snap["z1"]["tickets"], 250)
        self.assertEqual(snap["z1"]["memory"], 75)
        self.assertGreater(snap["z1"]["rx_bps"], 0)
        self.assertFalse(snap["z2"]["online"])
        self.assertTrue(any(e["kind"] == "offline" for e in self.store.events()))

    def test_password_hash(self):
        h = hash_password("abc")
        self.assertTrue(verify_password("abc", h))
        self.assertFalse(verify_password("abd", h))

    def test_api_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/overview")
        self.assertEqual(ctx.exception.code, 401)

    def _opener(self, password):
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        data = f"username=admin&password={password}".encode()
        opener.open(self.base + "/login", data=data)
        return opener

    def test_login_and_overview(self):
        opener = self._opener("motdepasse-test")
        data = json.loads(opener.open(self.base + "/api/overview?range=24h").read())
        self.assertEqual(data["totals"]["zones"], 2)
        self.assertEqual(data["totals"]["zones_online"], 1)
        self.assertEqual(data["totals"]["active"], 2)
        self.assertTrue(data["history"])
        sessions = json.loads(opener.open(self.base + "/api/zones/z1/sessions").read())
        self.assertEqual(sessions["sessions"][0]["user"], "482915")
        page = opener.open(self.base + "/").read().decode()
        self.assertIn("ADER LINK", page)

    def test_wrong_password(self):
        opener = self._opener("mauvais")
        with self.assertRaises(urllib.error.HTTPError):
            opener.open(self.base + "/api/overview")


class TicketTests(unittest.TestCase):
    def setUp(self):
        from app.tickets import Tickets, router_duration
        self.router_duration = router_duration
        self.router = FakeRouter()
        self.router.start()
        self.config = {"zones": [{"id": "z1", "name": "Zone Test", "ssid": "ADER WIFI", "router": {
            "host": "127.0.0.1", "port": self.router.port, "username": "aderlink", "password": "secret"}}]}
        self.store = Store(":memory:")
        self.t = Tickets(self.config, self.store)

    def test_duration(self):
        self.assertEqual(self.router_duration(3600), "1h")
        self.assertEqual(self.router_duration(90061), "1d1h1m1s")

    def test_profiles(self):
        self.assertEqual(self.t.profiles("z1"), ["default", "1-heure"])

    def test_create_and_remove(self):
        res = self.t.create_batch({"zone_id": "z1", "count": 5, "price": 200, "uptime_s": 3600,
                                   "data_mb": 500, "validity_s": 86400, "profile": "1-heure"})
        self.assertEqual(res["created"], 5)
        self.assertEqual(len(self.router.users), 5)
        sent = self.router.added[0]
        self.assertEqual(sent["limit-uptime"], "1h")
        self.assertEqual(sent["password"], sent["name"])
        self.assertEqual(sent["limit-bytes-total"], str(500 * 1024 * 1024))
        self.assertEqual(sent["comment"], f"ADERLINK-{res['id']}")
        b = self.t.get_batch(res["id"])
        self.assertEqual(len(b["tickets"]), 5)
        self.assertEqual(b["ssid"], "ADER WIFI")
        # un ticket est utilisé -> il ne doit pas être supprimé
        used = b["tickets"][0]["code"]
        self.t.on_poll(self.config["zones"][0], None, [{"user": used}])
        out = self.t.remove_unused(res["id"])
        self.assertEqual(out["removed"], 4)
        self.assertEqual(list(self.router.users), [used])
        self.assertEqual(self.t.list_batches()[0]["used"], 1)
        self.assertEqual(self.t.sales(0)[0]["amount"], 200)

    def test_validity_expiry(self):
        res = self.t.create_batch({"zone_id": "z1", "count": 1, "price": 500, "validity_s": 86400})
        code = self.t.get_batch(res["id"])["tickets"][0]["code"]
        self.t._mark_used("z1", code, int(time.time()) - 90000)  # 1re connexion il y a 25 h
        self.router.active_users.append(code)
        from app.routeros import RouterOS
        with RouterOS("127.0.0.1", "aderlink", "secret", port=self.router.port) as api:
            self.t.on_poll(self.config["zones"][0], api, [])
        self.assertEqual(self.router.users[code]["disabled"], "yes")
        self.assertNotIn(code, self.router.active_users)
        self.assertTrue(self.t.find(code)[0]["expired_at"])

    def test_forfait_then_batch(self):
        f = self.t.create_forfait({"zone_id": "z1", "name": "1 heure", "price": 200, "uptime_s": 3600,
                                   "data_mb": 500, "validity_s": 86400, "down_mbps": 2, "up_mbps": 1,
                                   "shared_users": 1})
        self.assertEqual(f["router_profile"], "AL-1-heure")
        pr = self.router.profiles["AL-1-heure"]
        self.assertEqual(pr["rate-limit"], "1M/2M")
        self.assertEqual(pr["shared-users"], "1")
        res = self.t.create_batch({"forfait_id": f["id"], "count": 3})
        self.assertEqual(res["created"], 3)
        self.assertEqual(self.router.added[0]["profile"], "AL-1-heure")
        self.assertEqual(self.router.added[0]["limit-uptime"], "1h")
        self.assertEqual(self.t.list_forfaits("z1")[0]["stock"], 3)
        from app.tickets import TicketError
        with self.assertRaises(TicketError):  # même nom
            self.t.create_forfait({"zone_id": "z1", "name": "1 HEURE"})
        # mise à jour d'un profil routeur existant au lieu d'un doublon
        self.t.archive_forfait(f["id"])
        self.t.create_forfait({"zone_id": "z1", "name": "1 heure", "down_mbps": 5, "up_mbps": 2.5})
        self.assertEqual(self.router.profiles["AL-1-heure"]["rate-limit"], "2500k/5M")
        self.assertEqual(len([n for n in self.router.profiles if n.startswith("AL-")]), 1)

    def test_sales_history(self):
        f = self.t.create_forfait({"zone_id": "z1", "name": "Jour", "price": 100})
        b = self.t.create_batch({"forfait_id": f["id"], "count": 4})
        codes = [t["code"] for t in self.t.get_batch(b["id"])["tickets"]]
        now = int(time.time())
        self.t._mark_used("z1", codes[0], now)
        self.t._mark_used("z1", codes[1], now - 3 * 86400)
        h = self.t.sales_history(7)
        self.assertEqual(len(h["series"]), 7)
        self.assertEqual(h["series"][-1]["amount"], 100)
        self.assertEqual(h["period"]["tickets"], 2)
        self.assertEqual(h["all_time"], {"tickets": 2, "amount": 200, "generated": 4})

    def test_bad_input(self):
        from app.tickets import TicketError
        with self.assertRaises(TicketError):
            self.t.create_batch({"zone_id": "z1", "count": 0})
        with self.assertRaises(TicketError):
            self.t.create_batch({"zone_id": "inconnue", "count": 3})


if __name__ == "__main__":
    unittest.main()


class RoutersAndUpdaterTests(unittest.TestCase):
    def test_routers_crud_install_pv(self):
        from app.routers import Routers, RouterError
        saved = []
        cfg = {"zones": []}
        r = Routers(cfg, Store(":memory:"), None, save=lambda c: saved.append(True))
        z = r.create({"name": "WIFI TASSI", "host": "127.0.0.1", "ssid": "TASSI", "code_length": 8})
        self.assertTrue(saved)
        self.assertEqual(z["code_length"], 8)
        self.assertEqual([p["code"] for p in z["points_vente"]], ["PV-01"])
        with self.assertRaises(RouterError):
            r.create({"name": "wifi tassi", "host": "127.0.0.1"})
        with self.assertRaises(RouterError):
            r.create({"name": "X", "host": "bad host;"})
        info = r.install_info(z["id"], "127.0.0.1:8080")
        self.assertIn("/install/", info["line"])
        self.assertIn(":8080/install/", info["url"])
        token = info["url"].rsplit("/", 1)[1][:-4]
        zone = r.by_token(token)
        script = r.install_script(zone)
        self.assertIn(zone["router"]["password"], script)
        self.assertIn('/ip service set api disabled=no port=8728 address=127.0.0.1/32', script)
        self.assertIsNone(r.by_token("faux"))
        r.update(z["id"], {"name": "WIFI TASSI 2", "host": "10.0.0.1", "code_length": 6})
        self.assertEqual(cfg["zones"][0]["router"]["host"], "10.0.0.1")
        pv = r.create_pv(z["id"], {"name": "Revendeur Moussa"})
        self.assertEqual(pv["code"], "PV-02")
        r.archive_pv(pv["id"])
        self.assertEqual(len(r.list_pv(z["id"])), 1)
        r.delete(z["id"])
        self.assertEqual(cfg["zones"], [])

    def test_updater_installs_and_keeps_config(self):
        import io, tempfile, zipfile
        from pathlib import Path
        from app import updater as U
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "app").mkdir()
            (root / "app" / "__main__.py").write_text("old")
            (root / "config.json").write_text('{"secret": 1}')
            (root / "VERSION").write_text("5.0.0")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("aderlink-main/app/__main__.py", "new")
                zf.writestr("aderlink-main/app/static/x.js", "js")
                zf.writestr("aderlink-main/VERSION", "5.1.0")
                zf.writestr("aderlink-main/config.json", "PIEGE")
            calls = []

            def fake_get(url, timeout=20):
                return b"5.1.0\n" if url.startswith("https://raw.githubusercontent.com") else buf.getvalue()
            old = (U.ROOT, U.VERSION_FILE, U._get)
            U.ROOT, U.VERSION_FILE, U._get = root, root / "VERSION", fake_get
            try:
                up = U.Updater({"update": {"repo": "ader/aderlink"}}, on_restart=lambda: calls.append(1))
                info = up.check(install=True)
                time.sleep(1.8)
            finally:
                U.ROOT, U.VERSION_FILE, U._get = old
            self.assertEqual(info["current"], "5.1.0")
            self.assertEqual((root / "app" / "__main__.py").read_text(), "new")
            self.assertEqual((root / "app" / "static" / "x.js").read_text(), "js")
            self.assertEqual((root / "config.json").read_text(), '{"secret": 1}')
            self.assertEqual(calls, [1])


class UsersAndRolesTests(unittest.TestCase):
    """Administrateur, gérant et revendeur : chacun ne voit que ce qui lui revient."""

    @classmethod
    def setUpClass(cls):
        from app.routers import Routers
        from app.tickets import Tickets
        from app.users import Users
        cls.config = {
            "admin": {"username": "admin", "password_hash": hash_password("admin-secret-1")},
            "secret_key": "y" * 64, "poll_seconds": 30,
            "zones": [{"id": "a", "name": "Zone A", "router": {"host": "127.0.0.1", "port": 1, "username": "u", "password": "p"}},
                      {"id": "b", "name": "Zone B", "router": {"host": "127.0.0.1", "port": 1, "username": "u", "password": "p"}}],
        }
        store = Store(":memory:")
        collector = Collector(cls.config, store, demo=True)
        cls.tickets = Tickets(cls.config, store, demo=True)
        routers = Routers(cls.config, store, collector, save=lambda c: None, demo=True)
        for z in cls.config["zones"]:
            routers.ensure_default_pv(z["id"])
        cls.users = Users(cls.config, store, routers, hash_password, verify_password)
        cls.fa = cls.tickets.create_forfait({"zone_id": "a", "name": "1H", "price": 200, "uptime_s": 3600})
        cls.fb = cls.tickets.create_forfait({"zone_id": "b", "name": "1H", "price": 200, "uptime_s": 3600})
        cls.tickets.create_batch({"forfait_id": cls.fa["id"], "count": 5}, created_by="admin")
        app = App(cls.config, collector, store, demo=True, tickets=cls.tickets, routers=routers, users=cls.users)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def login(self, user, pwd):
        jar = CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(self.base + "/login", data=f"username={user}&password={pwd}".encode())
        return op

    def get(self, op, path):
        return json.loads(op.open(self.base + path).read())

    def post(self, op, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-AderLink": "1"})
        try:
            return 200, json.loads(op.open(req).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_roles(self):
        admin = self.login("admin", "admin-secret-1")
        code, rev = self.post(admin, "/api/users", {"name": "Moussa Issa", "username": "moussa", "role": "revendeur",
                                                    "zones": ["a"], "commission": 10})
        self.assertEqual(code, 200, rev)
        self.assertEqual(len(rev["password"]), 10)  # mot de passe créé automatiquement
        code, ger = self.post(admin, "/api/users", {"name": "Awa", "username": "awa", "role": "gerant",
                                                    "zones": ["b"], "password": "awa-123456"})
        self.assertEqual(code, 200, ger)
        code, err = self.post(admin, "/api/users", {"name": "X", "username": "moussa", "role": "gerant", "zones": ["a"]})
        self.assertEqual(code, 400)

        # Revendeur : une seule zone, son point de vente, ses lots
        r = self.login("moussa", rev["password"])
        me = self.get(r, "/api/me")
        self.assertEqual(me["role"], "revendeur")
        zones = self.get(r, "/api/zones")["zones"]
        self.assertEqual([z["id"] for z in zones], ["a"])
        self.assertEqual([p["name"] for p in zones[0]["points_vente"]], ["Moussa Issa"])
        self.assertEqual(self.get(r, "/api/batches")["batches"], [])  # ne voit pas le lot de l'admin
        code, lot = self.post(r, "/api/batches", {"forfait_id": self.fa["id"], "count": 3, "pos_id": 1})
        self.assertEqual(code, 200, lot)
        detail = self.get(r, f"/api/batches/{lot['id']}")
        self.assertEqual(detail["pos_name"], "Moussa Issa")  # forcé sur son point de vente
        self.assertEqual(detail["created_by"], "moussa")
        self.assertEqual(len(self.get(r, "/api/batches")["batches"]), 1)
        self.assertEqual(self.post(r, "/api/batches", {"forfait_id": self.fb["id"], "count": 1})[0], 403)
        self.assertEqual(self.post(r, "/api/forfaits", {"zone_id": "a", "name": "X", "price": 1})[0], 403)
        self.assertEqual(self.post(r, "/api/users", {})[0], 403)
        for path in ("/api/routers", "/api/users", "/api/settings", "/api/zones/b/forfaits"):
            with self.assertRaises(urllib.error.HTTPError, msg=path):
                r.open(self.base + path)
        admin_lot = self.get(admin, "/api/batches")["batches"][-1]["id"]
        with self.assertRaises(urllib.error.HTTPError):
            r.open(self.base + f"/api/batches/{admin_lot}")

        # Gérant : zone B seulement, peut créer un profil, pas d'utilisateurs
        g = self.login("awa", "awa-123456")
        self.assertEqual([z["id"] for z in self.get(g, "/api/zones")["zones"]], ["b"])
        self.assertEqual(self.post(g, "/api/forfaits", {"zone_id": "b", "name": "Jour", "price": 500})[0], 200)
        self.assertEqual(self.post(g, "/api/forfaits", {"zone_id": "a", "name": "Jour", "price": 500})[0], 403)
        self.assertEqual(self.get(g, "/api/batches")["batches"], [])
        with self.assertRaises(urllib.error.HTTPError):
            g.open(self.base + "/api/users")

        # Statistiques et commission vues par l'admin
        self.tickets._q("UPDATE tickets SET first_used = ? WHERE batch_id = ?", (int(time.time()), lot["id"]), commit=True)
        users = {u["username"]: u for u in self.get(admin, "/api/users")["users"]}
        self.assertEqual(users["moussa"]["stats"]["sold"], 3)
        self.assertEqual(users["moussa"]["stats"]["commission_due"], 60)

        # Changer son mot de passe, puis désactivation : la session tombe
        self.assertEqual(self.post(r, "/api/me/password", {"old": "faux", "new": "nouveau-1"})[0], 400)
        self.assertEqual(self.post(r, "/api/me/password", {"old": rev["password"], "new": "nouveau-1"})[0], 200)
        self.post(admin, f"/api/users/{rev['id']}/disable", {})
        with self.assertRaises(urllib.error.HTTPError):
            r.open(self.base + "/api/me")
        with self.assertRaises(urllib.error.HTTPError):
            self.login("moussa", "nouveau-1").open(self.base + "/api/me")
        self.post(admin, f"/api/users/{rev['id']}/enable", {})
        self.assertEqual(self.get(self.login("moussa", "nouveau-1"), "/api/me")["username"], "moussa")
        # Rapport des ventes : l'admin voit tout, le revendeur seulement ses ventes
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3600))
        rep = self.get(admin, f"/api/report?from=2000-01-01&to={today}")
        self.assertEqual(rep["totals"]["tickets"], 3)
        self.assertEqual(rep["totals"]["amount"], 600)
        self.assertEqual(rep["by_seller"][0]["label"], "Moussa Issa")
        self.assertEqual(rep["by_pos"][0]["label"], "Moussa Issa")
        self.assertEqual(sum(d["amount"] for d in rep["by_day"]), 600)
        self.assertLessEqual(len(rep["by_day"]), 367)  # période limitée à un an
        mine = self.get(self.login("moussa", "nouveau-1"), "/api/report")
        self.assertEqual(mine["totals"]["amount"], 600)
        self.assertEqual(mine["commission"], 60)
        g_rep = self.get(g, "/api/report")
        self.assertEqual(g_rep["totals"]["tickets"], 0)  # le gérant de la zone B ne voit pas la zone A
        self.assertEqual(self.post(admin, f"/api/users/{rev['id']}/delete", {})[0], 200)

    def test_subscription(self):
        from app.users import add_months
        # mois de calendrier : 31 janvier + 1 mois = 28 février ; + 1 an
        jan31 = 1769817600 - 3600  # 31/01/2026 00:00 heure du Niger
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(add_months(jan31, 1) + 3600)), "2026-02-28")
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(add_months(jan31, 12) + 3600)), "2027-01-31")

        admin = self.login("admin", "admin-secret-1")
        code, u = self.post(admin, "/api/users", {"name": "Ali", "username": "ali", "role": "revendeur", "zones": ["a"],
                                                  "password": "ali-123456", "subscription": {"n": 1, "unit": "mois"}})
        self.assertEqual(code, 200, u)
        self.assertGreater(u["expires_at"], time.time() + 27 * 86400)
        first = u["expires_at"]
        # ajout à partir de la fin actuelle
        code, r = self.post(admin, f"/api/users/{u['id']}/subscription", {"action": "add", "n": 16, "unit": "heure", "note": "payé"})
        self.assertEqual(r["expires_at"], first + 16 * 3600)
        self.assertEqual(r["history"][0]["label"], "+ 16 heures")
        for bad in ({"action": "add", "n": 0, "unit": "jour"}, {"action": "add", "n": "x", "unit": "jour"},
                    {"action": "add", "n": 2, "unit": "semaine"}, {"action": "set", "until": 5}):
            self.assertEqual(self.post(admin, f"/api/users/{u['id']}/subscription", bad)[0], 400, bad)

        ali = self.login("ali", "ali-123456")
        me = self.get(ali, "/api/me")
        self.assertEqual(me["expires_at"], first + 16 * 3600)
        self.assertEqual(self.post(ali, f"/api/users/{u['id']}/subscription", {"action": "unlimited"})[0], 403)

        # arrêt : la session tombe et la connexion affiche « abonnement terminé »
        self.post(admin, f"/api/users/{u['id']}/subscription", {"action": "stop"})
        time.sleep(1.1)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            ali.open(self.base + "/api/me")
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("e=expire", ali.open(self.base + "/login").geturl())
        land = lambda pwd: urllib.request.urlopen(self.base + "/login", data=f"username=ali&password={pwd}".encode()).geturl()
        self.assertIn("e=expire", land("ali-123456"))
        self.assertIn("e=1", land("mauvais"))  # pas d'indice sans le bon mot de passe

        # renouvellement après la fin : on repart de maintenant
        code, r = self.post(admin, f"/api/users/{u['id']}/subscription", {"action": "add", "n": 3, "unit": "jour"})
        self.assertAlmostEqual(r["expires_at"], time.time() + 3 * 86400, delta=5)
        self.assertEqual(self.get(self.login("ali", "ali-123456"), "/api/me")["username"], "ali")
        code, r = self.post(admin, f"/api/users/{u['id']}/subscription", {"action": "unlimited"})
        self.assertIsNone(r["expires_at"])
        self.assertEqual(len(self.get(admin, f"/api/users/{u['id']}/history")["history"]), 5)
        # contact affiché sur la page de connexion
        self.post(admin, "/api/settings/contact", {"name": "ADER TECHNOLOGIE", "phone": "+227 90 11 22 33"})
        pub = json.loads(urllib.request.urlopen(self.base + "/api/public").read())
        self.assertEqual(pub["contact"]["phone"], "+227 90 11 22 33")
        self.post(admin, f"/api/users/{u['id']}/delete", {})
