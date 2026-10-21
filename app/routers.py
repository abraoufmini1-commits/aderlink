"""Gestion des routeurs depuis l'application : ajout, modification, installation, points de vente.

Les routeurs sont enregistrés dans config.json (comme avant) ; les points de vente
dans la base SQLite. L'installation se fait par une ligne à coller dans Winbox :
le routeur télécharge un petit script (.rsc) qui crée le compte « aderlink »
et ouvre l'API à l'adresse de ce serveur uniquement.
"""
import re
import secrets
import socket
import threading
import time

from .routeros import RouterOS, RouterOSError

PUBLIC = ("id", "name", "location", "ssid", "code_length", "created_at")


class RouterError(Exception):
    pass


def _clean(text, n=60):
    return " ".join(str(text or "").split())[:n]


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "routeur")[:24]


def local_ip_towards(host):
    """Adresse IP de ce serveur vue depuis le routeur (sans rien envoyer)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 8728))
            return s.getsockname()[0]
    except OSError:
        return None


class Routers:
    def __init__(self, config, store, collector, save=None, demo=False):
        self.config = config
        self.store = store
        self.collector = collector
        self.save = save or (lambda cfg: None)
        self.demo = demo
        self.lock = threading.Lock()
        with store.lock:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS points_vente (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT, name TEXT, code TEXT,
                    created_at INTEGER, archived INTEGER DEFAULT 0);
                """
            )
            store.db.commit()

    # --- outils ------------------------------------------------------------------
    def zones(self):
        return self.config.setdefault("zones", [])

    def zone(self, zone_id):
        z = next((z for z in self.zones() if z["id"] == zone_id), None)
        if not z:
            raise RouterError("Routeur introuvable")
        return z

    def _q(self, sql, params=(), commit=False):
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            rows = cur.fetchall()
            if commit:
                self.store.db.commit()
            return rows, cur.lastrowid

    def _persist(self):
        if not self.demo:
            self.save(self.config)

    def public(self, z):
        d = {k: z.get(k) for k in PUBLIC}
        r = z.get("router", {})
        d.update({"host": r.get("host", ""), "port": r.get("port", 8728), "wan_interface": r.get("wan_interface", ""),
                  "ssid": z.get("ssid") or z["name"], "code_length": z.get("code_length", 6)})
        s = self.collector.snapshot().get(z["id"], {}) if self.collector else {}
        d["status"] = "unknown" if not s else ("online" if s.get("online") else "offline")
        for k in ("board", "version", "uptime_s", "cpu", "memory", "tickets", "active", "error", "last_seen", "identity"):
            d[k] = s.get(k)
        d["points_vente"] = self.list_pv(z["id"])
        return d

    # --- routeurs ---------------------------------------------------------------------
    def list(self):
        return [self.public(z) for z in self.zones()]

    def _validate(self, data, current=None):
        name = _clean(data.get("name"), 40)
        if not name:
            raise RouterError("Donnez un nom au site")
        host = _clean(data.get("host"), 64).replace(" ", "")
        if not host or not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
            raise RouterError("Adresse IP du routeur invalide (ex. 10.10.0.1)")
        try:
            port = int(data.get("port") or 8728)
            code_length = int(data.get("code_length") or 6)
        except (TypeError, ValueError):
            raise RouterError("Valeurs invalides")
        if not 1 <= port <= 65535:
            raise RouterError("Port API invalide")
        if not 4 <= code_length <= 12:
            raise RouterError("Longueur des codes : entre 4 et 12")
        for z in self.zones():
            if z is not current and z["name"].lower() == name.lower():
                raise RouterError("Un routeur porte déjà ce nom")
        wan = _clean(data.get("wan_interface") or "ether1", 32).replace(" ", "")
        return name, host, port, code_length, wan

    def create(self, data):
        name, host, port, code_length, wan = self._validate(data)
        with self.lock:
            zid = f"{_slug(name)}-{secrets.token_hex(2)}"
            z = {
                "id": zid, "name": name, "location": _clean(data.get("location"), 60),
                "ssid": _clean(data.get("ssid"), 32) or name, "code_length": code_length,
                "created_at": int(time.time()), "install_token": secrets.token_urlsafe(18),
                "router": {"host": host, "port": port, "ssl": False, "username": "aderlink",
                           "password": secrets.token_urlsafe(15).replace("-", "x").replace("_", "y"),
                           "wan_interface": wan},
            }
            self.zones().append(z)
            self._persist()
        self.ensure_default_pv(zid)
        return self.public(z)

    def update(self, zone_id, data):
        z = self.zone(zone_id)
        name, host, port, code_length, wan = self._validate(data, current=z)
        with self.lock:
            z.update(name=name, location=_clean(data.get("location"), 60),
                     ssid=_clean(data.get("ssid"), 32) or name, code_length=code_length)
            z.setdefault("router", {}).update(host=host, port=port, wan_interface=wan)
            self._persist()
        return self.public(z)

    def delete(self, zone_id):
        z = self.zone(zone_id)
        with self.lock:
            self.zones().remove(z)
            self._persist()
        return {"ok": True}

    def test(self, zone_id):
        z = self.zone(zone_id)
        if self.demo:
            return {"ok": True, "message": "Connexion réussie (démo)"}
        r = z["router"]
        try:
            with RouterOS(r["host"], r["username"], r["password"], port=r.get("port", 8728),
                          use_ssl=r.get("ssl", False), timeout=6) as api:
                res = (api.query("/system/resource/print") or [{}])[0]
                api.query("/ip/hotspot/user/profile/print")
            return {"ok": True, "message": f"Connecté : {res.get('board-name', '')} RouterOS {res.get('version', '')}"}
        except RouterOSError as e:
            return {"ok": False, "message": str(e)}

    # --- installation -------------------------------------------------------------------
    def ensure_token(self, z):
        if not z.get("install_token"):
            with self.lock:
                z["install_token"] = secrets.token_urlsafe(18)
                self._persist()
        return z["install_token"]

    def by_token(self, token):
        for z in self.zones():
            if z.get("install_token") and secrets.compare_digest(z["install_token"], token):
                return z
        return None

    def server_ip_for(self, z):
        return self.config.get("server_ip_for_routers") or local_ip_towards(z["router"]["host"]) or "127.0.0.1"

    def install_info(self, zone_id, request_host):
        z = self.zone(zone_id)
        token = self.ensure_token(z)
        host, _, port = (request_host or "").partition(":")
        if not host or host in ("127.0.0.1", "localhost", "::1"):
            host = self.server_ip_for(z)
        base = f"{host}:{port}" if port else host
        url = f"http://{base}/install/{token}.rsc"
        line = (f'/tool fetch url="{url}" dst-path=aderlink.rsc; :delay 1s; '
                f'/import file-name=aderlink.rsc; /file remove aderlink.rsc')
        return {"line": line, "url": url, "server_ip": self.server_ip_for(z), "router": self.public(z)}

    def install_script(self, z):
        r = z["router"]
        srv = self.server_ip_for(z)
        pwd = r["password"]
        name = re.sub(r'[^A-Za-z0-9 \-]', "", z["name"])
        return "\r\n".join([
            f"# ADER LINK - installation automatique : {name}",
            ':if ([:len [/user group find name="aderlink-api"]] = 0) do={ /user group add name="aderlink-api" '
            'policy=read,write,api comment="ADER LINK" } else={ /user group set [find name="aderlink-api"] policy=read,write,api }',
            f':if ([:len [/user find name="aderlink"]] = 0) do={{ /user add name="aderlink" group="aderlink-api" '
            f'password="{pwd}" address={srv}/32 comment="ADER LINK" }} else={{ /user set [find name="aderlink"] '
            f'group="aderlink-api" password="{pwd}" address={srv}/32 }}',
            f'/ip service set api disabled=no port={int(r.get("port", 8728))} address={srv}/32',
            ':log info "ADER LINK installe"',
            f':put "ADER LINK : installation terminee ({name}). Retournez dans l application et cliquez sur Verifier."',
            "",
        ])

    # --- points de vente ------------------------------------------------------------------
    def list_pv(self, zone_id):
        rows, _ = self._q("SELECT id, name, code, created_at FROM points_vente WHERE zone_id = ? AND archived = 0 ORDER BY id",
                          (zone_id,))
        return [{"id": i, "name": n, "code": c, "created_at": t} for i, n, c, t in rows]

    def ensure_default_pv(self, zone_id):
        if not self.list_pv(zone_id):
            self.create_pv(zone_id, {"name": "Boutique principale"})

    def create_pv(self, zone_id, data):
        self.zone(zone_id)
        name = _clean(data.get("name"), 40)
        if not name:
            raise RouterError("Donnez un nom au point de vente")
        rows, _ = self._q("SELECT COUNT(*) FROM points_vente WHERE zone_id = ?", (zone_id,))
        code = f"PV-{rows[0][0] + 1:02d}"
        _, pid = self._q("INSERT INTO points_vente (zone_id, name, code, created_at) VALUES (?,?,?,?)",
                         (zone_id, name, code, int(time.time())), commit=True)
        return {"id": pid, "name": name, "code": code}

    def archive_pv(self, pv_id):
        self._q("UPDATE points_vente SET archived = 1 WHERE id = ?", (pv_id,), commit=True)
        return {"ok": True}

    def get_pv(self, pv_id):
        rows, _ = self._q("SELECT id, zone_id, name, code FROM points_vente WHERE id = ?", (pv_id,))
        return dict(zip(("id", "zone_id", "name", "code"), rows[0])) if rows else None
