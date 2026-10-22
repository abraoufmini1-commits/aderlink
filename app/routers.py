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
MAX_LOGO = 400_000  # octets


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
                CREATE TABLE IF NOT EXISTS zone_logos (zone_id TEXT PRIMARY KEY, mime TEXT, data BLOB);
                UPDATE points_vente SET name = 'Profil principal' WHERE name = 'Boutique principale';
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
                  "ssid": z.get("ssid") or z["name"], "code_length": z.get("code_length", 8)})
        s = self.collector.snapshot().get(z["id"], {}) if self.collector else {}
        d["status"] = "unknown" if not s else ("online" if s.get("online") else "offline")
        for k in ("board", "version", "uptime_s", "cpu", "memory", "tickets", "active", "error", "last_seen", "identity"):
            d[k] = s.get(k)
        d["points_vente"] = self.list_pv(z["id"])
        d["owner"] = z.get("owner")
        d["currency"] = z.get("currency", "FCFA")
        d["winbox"] = z.get("winbox", "")
        d["installed"] = bool(r.get("host"))
        d["auto_host"] = bool(r.get("auto_host"))
        d["has_logo"] = self.logo(z["id"]) is not None
        if not d["installed"]:
            d["status"] = "pending"
        return d

    # --- routeurs ---------------------------------------------------------------------
    def list(self):
        return [self.public(z) for z in self.zones()]

    def owned(self, owner):
        return [z for z in self.zones() if z.get("owner") == owner]

    def _validate(self, data, current=None, owner=None):
        name = _clean(data.get("name"), 40)
        if not name:
            raise RouterError("Donnez un nom au réseau")
        host = _clean(data.get("host"), 64).replace(" ", "")
        if host and not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
            raise RouterError("Adresse IP du routeur invalide (ex. 10.10.0.1) — ou laissez vide")
        try:
            port = int(data.get("port") or 8728)
            code_length = int(data.get("code_length") or 8)
        except (TypeError, ValueError):
            raise RouterError("Valeurs invalides")
        if not 1 <= port <= 65535:
            raise RouterError("Port API invalide")
        if not 4 <= code_length <= 12:
            raise RouterError("Longueur des codes : entre 4 et 12")
        owner = current.get("owner") if current else owner
        for z in self.zones():  # le nom doit être unique chez un même propriétaire
            if z is not current and z.get("owner") == owner and z["name"].lower() == name.lower():
                raise RouterError("Vous avez déjà un routeur avec ce nom")
        wan = _clean(data.get("wan_interface") or "ether1", 32).replace(" ", "")
        return name, host, port, code_length, wan

    @staticmethod
    def _currency(data, current="FCFA"):
        cur = _clean(data.get("currency"), 8) if "currency" in data else current
        return cur or "FCFA"

    def create(self, data, owner=None):
        name, host, port, code_length, wan = self._validate(data, owner=owner)
        with self.lock:
            zid = f"{_slug(name)}-{secrets.token_hex(2)}"
            z = {
                "id": zid, "name": name, "location": _clean(data.get("location"), 60),
                "ssid": _clean(data.get("ssid"), 32) or name, "code_length": code_length,
                "currency": self._currency(data),
                "created_at": int(time.time()), "install_token": secrets.token_urlsafe(18),
                "router": {"host": host, "auto_host": not host, "port": port, "ssl": False, "username": "aderlink",
                           "password": secrets.token_urlsafe(15).replace("-", "x").replace("_", "y"),
                           "wan_interface": wan},
            }
            if owner:
                z["owner"] = owner  # routeur d'un client
            self.zones().append(z)
            self._persist()
        self.ensure_default_pv(zid)
        return self.public(z)

    def update(self, zone_id, data):
        z = self.zone(zone_id)
        name, host, port, code_length, wan = self._validate(data, current=z)
        with self.lock:
            z.update(name=name, location=_clean(data.get("location"), 60) if "location" in data else z.get("location", ""),
                     ssid=_clean(data.get("ssid"), 32) or name, code_length=code_length,
                     currency=self._currency(data, z.get("currency", "FCFA")))
            r = z.setdefault("router", {})
            if "host" in data:  # vide = l'adresse est détectée à l'installation
                if host and host != r.get("host"):
                    r.update(host=host, auto_host=False)
                elif not host and not r.get("auto_host"):
                    r.update(host="", auto_host=True)  # on repasse en détection automatique
            r.update(port=port, wan_interface=wan)
            if "winbox" in data:
                wb = _clean(data.get("winbox"), 80).replace(" ", "")
                if wb and not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", wb):
                    raise RouterError("Adresse Winbox invalide (ex. 41.203.10.5:8291)")
                z["winbox"] = wb
            self._persist()
        return self.public(z)

    def delete(self, zone_id):
        z = self.zone(zone_id)
        with self.lock:
            self.zones().remove(z)
            self._persist()
        self._q("DELETE FROM zone_logos WHERE zone_id = ?", (zone_id,), commit=True)
        return {"ok": True}

    # --- Winbox ------------------------------------------------------------------------------
    def find_winbox(self):
        """winbox64.exe / winbox.exe : réglage, dossier d'ADER LINK, Téléchargements, Bureau, Documents, Program Files."""
        import glob
        import os
        from pathlib import Path
        own = self.config.get("winbox_path")
        if own and os.path.isfile(own):
            return own
        home = Path.home()
        places = [Path(__file__).resolve().parent.parent, home / "Downloads", home / "Téléchargements", home / "Desktop",
                  home / "Bureau", home / "Documents", Path(os.environ.get("ProgramFiles", "C:/Program Files")),
                  Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
                  Path(os.environ.get("LOCALAPPDATA", str(home))) / "Programs"]
        for base in places:
            for depth in ("", "*/", "*/*/"):
                hits = sorted(glob.glob(str(base / f"{depth}[Ww]in[Bb]ox*.exe")), key=lambda x: ("64" not in x, x))
                if hits:
                    return hits[0]
        return None

    @staticmethod
    def winbox_addr(z):
        r = z.get("router") or {}
        return z.get("winbox") or (f"{r['host']}" if r.get("host") else "")

    def open_winbox(self, zone_id, local):
        """Ouvre Winbox sur le PC où tourne ADER LINK, l'adresse du routeur déjà remplie."""
        import os
        import subprocess
        z = self.zone(zone_id)
        host = self.winbox_addr(z)
        if not host:
            return {"ok": False, "host": "", "message": "Ce routeur n'est pas encore installé."}
        if self.demo:
            return {"ok": False, "host": host, "message": "Indisponible en démonstration. Adresse copiée."}
        if not local or os.name != "nt":
            return {"ok": False, "host": host,
                    "message": "Winbox ne s'ouvre que sur le PC où tourne ADER LINK. L'adresse est copiée : collez-la dans Winbox."}
        exe = self.find_winbox()
        if not exe:
            return {"ok": False, "host": host,
                    "message": "Winbox introuvable. Mettez winbox64.exe dans le dossier aderlink, puis recommencez. L'adresse est copiée."}
        subprocess.Popen([exe, host], close_fds=True)  # host déjà vérifié (adresse ou adresse:port)
        return {"ok": True, "host": host, "message": "Winbox s'ouvre avec l'adresse " + host + "."}

    def test(self, zone_id):
        z = self.zone(zone_id)
        if self.demo:
            return {"ok": True, "message": "Connexion réussie (démo)"}
        r = z["router"]
        if not r.get("host"):
            return {"ok": False, "message": "le routeur ne s'est pas encore signalé. Collez la ligne dans le Terminal de Winbox."}
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
        return (self.config.get("server_ip_for_routers")
                or local_ip_towards(z["router"].get("host") or "8.8.8.8") or "127.0.0.1")

    def installed(self, z, client_ip):
        """Appelé par le routeur à la fin du script : on retient son adresse."""
        r = z["router"]
        with self.lock:
            if not r.get("host") or r.get("auto_host"):
                r["host"], r["auto_host"] = client_ip, True
            z["last_ip"] = client_ip  # ADER Roaming reconnaît le routeur à cette adresse
            z["installed_at"] = int(time.time())
            self._persist()
        return r["host"]

    # --- logo ------------------------------------------------------------------------------
    def logo(self, zone_id):
        rows, _ = self._q("SELECT mime, data FROM zone_logos WHERE zone_id = ?", (zone_id,))
        return rows[0] if rows else None

    def set_logo(self, zone_id, data_url):
        import base64
        self.zone(zone_id)
        if not data_url:
            self._q("DELETE FROM zone_logos WHERE zone_id = ?", (zone_id,), commit=True)
            return {"ok": True}
        m = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", str(data_url))
        if not m:
            raise RouterError("Image invalide (PNG ou JPG)")
        raw = base64.b64decode(m.group(2))
        if len(raw) > MAX_LOGO:
            raise RouterError("Image trop lourde")
        self._q("INSERT OR REPLACE INTO zone_logos (zone_id, mime, data) VALUES (?,?,?)", (zone_id, m.group(1), raw),
                commit=True)
        return {"ok": True}

    def _base(self, z, request_host):
        host, _, port = (request_host or "").partition(":")
        if not host or host in ("127.0.0.1", "localhost", "::1"):
            host = self.server_ip_for(z)
        return (f"{host}:{port}" if port else host), host

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

    def install_script(self, z, request_host="", profiles=(), roaming=None):
        from .mikrotik import install_script
        r = z["router"]
        base, srv = self._base(z, request_host)
        g = roaming.group_of_zone(z["id"]) if roaming else None
        extra = roaming.script_lines(g, srv) if g else ()
        return install_script(z["name"], r["password"], srv, r.get("port", 8728),
                              f"http://{base}/install/{z['install_token']}/ok", profiles, r.get("username") or "aderlink",
                              extra=extra)

    # --- points de vente ------------------------------------------------------------------
    def list_pv(self, zone_id):
        rows, _ = self._q("SELECT id, name, code, created_at FROM points_vente WHERE zone_id = ? AND archived = 0 ORDER BY id",
                          (zone_id,))
        return [{"id": i, "name": n, "code": c, "created_at": t} for i, n, c, t in rows]

    def ensure_default_pv(self, zone_id):
        if not self.list_pv(zone_id):
            self.create_pv(zone_id, {"name": "Profil principal"})

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
