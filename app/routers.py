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

from pathlib import Path

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
        self.wg_file = None  # data/wg-peers.conf, sur le serveur en ligne
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
        d["remote"] = self.remote(z)
        d["devices"] = self.devices(z)
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

    # --- accès à distance (Winbox, WebFig) par le tunnel --------------------------------------
    WINBOX_BASE, WEBFIG_BASE = 50000, 52000

    def remote(self, z):
        """Adresses publiques du serveur qui mènent au routeur par le tunnel."""
        wg, r = self.wg(), z.get("router") or {}
        if not wg or r.get("tunnel") not in ("wg", "ovpn") or not r.get("tunnel_ip"):
            return None
        n = int(r["tunnel_ip"].rsplit(".", 1)[1])
        ip = wg["endpoint"]
        return {"winbox": f"{ip}:{self.WINBOX_BASE + n}", "webfig": f"http://{ip}:{self.WEBFIG_BASE + n}",
                "tunnel": r["host"], "kind": "WireGuard" if r["tunnel"] == "wg" else "OpenVPN"}

    # --- appareils derrière un routeur (antenne, caméra…) : ouverts de partout par le tunnel ---
    DEVICE_BASE = 53000

    def devices(self, z):
        wg = self.wg()
        out = []
        for d in z.get("devices", []):
            port = self.DEVICE_BASE + d["id"]
            out.append(dict(d, public=f"{d['proto']}://{wg['endpoint']}:{port}" if wg else ""))
        return out

    def _router_rules(self, z, d, api):
        r = z["router"]
        iface = "wg-aderlink" if r.get("tunnel") == "wg" else "ovpn-aderlink"
        src = (self.wg() or {}).get("server" if r.get("tunnel") == "wg" else "ovpn_server", "10.66.0.1")
        port, tag = self.DEVICE_BASE + d["id"], f"ADER LINK appareil {d['id']}"
        api.query("/ip/firewall/nat/add", "=chain=dstnat", f"=in-interface={iface}", "=protocol=tcp",
                  f"=dst-port={port}", "=action=dst-nat", f"=to-addresses={d['ip']}", f"=to-ports={d['port']}",
                  f"=comment={tag}")
        # l'appareil répond au routeur, qui renvoie dans le tunnel
        api.query("/ip/firewall/nat/add", "=chain=srcnat", f"=src-address={src}", f"=dst-address={d['ip']}",
                  "=protocol=tcp", f"=dst-port={d['port']}", "=action=masquerade", f"=comment={tag}")
        # sur un réseau hotspot, l'appareil doit passer sans ticket (sinon ses réponses sont bloquées)
        if api.query("/ip/hotspot/print"):
            target = f"=mac-address={d['mac']}" if d.get("mac") else f"=address={d['ip']}"
            try:
                api.query("/ip/hotspot/ip-binding/add", target, "=type=bypassed", f"=comment={tag}")
            except RouterOSError:
                pass  # déjà en bypass

    def _remove_rules(self, api, did):
        for n in api.query("/ip/firewall/nat/print", f"?comment=ADER LINK appareil {did}"):
            api.query("/ip/firewall/nat/remove", f"=.id={n['.id']}")
        try:
            for b in api.query("/ip/hotspot/ip-binding/print", f"?comment=ADER LINK appareil {did}"):
                api.query("/ip/hotspot/ip-binding/remove", f"=.id={b['.id']}")
        except RouterOSError:
            pass

    def lan_scan(self, zone_id):
        """Appareils vus par le routeur (baux DHCP + voisins) : pour choisir une antenne sans chercher son adresse."""
        z = self.zone(zone_id)
        if self.demo:
            return {"found": [{"ip": "192.168.88.20", "name": "Grandstream GWN7660", "mac": "C0:74:AD:12:34:56"},
                              {"ip": "192.168.88.21", "name": "Antenne LiteBeam", "mac": "24:5A:4C:AA:BB:CC"}]}
        r = z["router"]
        found = {}
        try:
            with RouterOS(r["host"], r["username"], r["password"], port=r.get("port", 8728), timeout=8) as api:
                for l in api.query("/ip/dhcp-server/lease/print"):
                    ip = l.get("active-address") or l.get("address")
                    if ip and l.get("status", "bound") == "bound":
                        found[ip] = {"ip": ip, "name": l.get("host-name") or l.get("comment") or "",
                                     "mac": l.get("active-mac-address") or l.get("mac-address") or ""}
                for n in api.query("/ip/neighbor/print"):
                    ip = n.get("address4") or n.get("address")
                    if ip and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                        name = " ".join(x for x in (n.get("identity"), n.get("board")) if x)
                        f = found.setdefault(ip, {"ip": ip, "name": "", "mac": n.get("mac-address", "")})
                        f["name"] = f["name"] or name
        except RouterOSError as e:
            raise RouterError(f"Le routeur ne répond pas : {e}") from e
        known = {d["ip"] for d in z.get("devices", [])}
        out = [f for ip, f in sorted(found.items(), key=lambda x: tuple(int(p) for p in x[0].split("."))) if ip not in known]
        return {"found": out[:200]}

    def add_device(self, zone_id, data):
        z = self.zone(zone_id)
        if not self.remote(z):
            raise RouterError("Ce routeur doit d'abord être relié au serveur par le tunnel (Installer)")
        name = _clean(data.get("name"), 40)
        ip = str(data.get("ip") or "").strip()
        proto = "https" if data.get("proto") == "https" else "http"
        try:
            port = int(data.get("port") or (443 if proto == "https" else 80))
        except (TypeError, ValueError):
            raise RouterError("Port invalide")
        if not name:
            raise RouterError("Donnez un nom à l'appareil")
        if not re.fullmatch(r"(10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}", ip) or any(int(x) > 255 for x in ip.split(".")):
            raise RouterError("Adresse IP locale invalide (ex : 192.168.88.20)")
        if not 1 <= port <= 65535:
            raise RouterError("Port invalide")
        with self.lock:
            seq = int(self.config.get("device_seq", 0)) + 1
            if seq > 999:
                raise RouterError("Nombre maximum d'appareils atteint")
            self.config["device_seq"] = seq
        mac = str(data.get("mac") or "").strip().upper()
        mac = mac if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac) else ""
        d = {"id": seq, "name": name, "ip": ip, "port": port, "proto": proto, "mac": mac}
        if not self.demo:
            try:
                with RouterOS(z["router"]["host"], z["router"]["username"], z["router"]["password"],
                              port=z["router"].get("port", 8728), timeout=8) as api:
                    self._router_rules(z, d, api)
            except RouterOSError as e:
                raise RouterError(f"Le routeur a refusé : {e}") from e
        with self.lock:
            z.setdefault("devices", []).append(d)
            self._persist()
        self.write_peers()
        return {"devices": self.devices(z)}

    def remove_device(self, zone_id, did):
        z = self.zone(zone_id)
        if not self.demo and self.remote(z):
            try:
                with RouterOS(z["router"]["host"], z["router"]["username"], z["router"]["password"],
                              port=z["router"].get("port", 8728), timeout=8) as api:
                    self._remove_rules(api, did)
            except RouterOSError:
                pass  # routeur injoignable : on retire quand même la redirection du serveur
        with self.lock:
            z["devices"] = [d for d in z.get("devices", []) if d["id"] != did]
            self._persist()
        self.write_peers()
        return {"devices": self.devices(z)}

    def forwards_text(self):
        """« port-public adresse-tunnel port-routeur » : lu par l'aide système (redirections du serveur)."""
        out = []
        for z in self.zones():
            r = z.get("router") or {}
            if self.remote(z):
                n = int(r["tunnel_ip"].rsplit(".", 1)[1])
                out.append(f"{self.WINBOX_BASE + n} {r['host']} 8291")
                out.append(f"{self.WEBFIG_BASE + n} {r['host']} 80")
                for d in z.get("devices", []):  # le routeur transmet ensuite à l'appareil
                    port = self.DEVICE_BASE + d["id"]
                    out.append(f"{port} {r['host']} {port}")
        return "\n".join(out) + ("\n" if out else "")

    def open_winbox(self, zone_id, local):
        """Ouvre Winbox sur le PC où tourne ADER LINK, l'adresse du routeur déjà remplie."""
        import os
        import subprocess
        z = self.zone(zone_id)
        host = (self.remote(z) or {}).get("winbox") or self.winbox_addr(z)
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
        wg = self.wg()
        if wg:  # serveur en ligne : les routeurs parlent toujours à son adresse IP, en http (même avec un nom de domaine)
            return wg["endpoint"], wg["endpoint"]
        host, _, port = (request_host or "").partition(":")
        if not host or host in ("127.0.0.1", "localhost", "::1"):
            host = self.server_ip_for(z)
        return (f"{host}:{port}" if port else host), host

    def install_info(self, zone_id, request_host):
        z = self.zone(zone_id)
        token = self.ensure_token(z)
        base, _ = self._base(z, request_host)
        url = f"http://{base}/install/{token}.rsc"
        line = (f'/tool fetch url="{url}" dst-path=aderlink.rsc; :delay 1s; '
                f'/import file-name=aderlink.rsc; /file remove aderlink.rsc')
        wg = self.wg()
        return {"line": line, "url": url, "router": self.public(z),
                "server_ip": wg.get("server", "10.66.0.1") if wg else self.server_ip_for(z),
                "tunnel": self.tunnel_ip(z) if wg else None}

    # --- tunnel WireGuard (serveur en ligne) ------------------------------------------------
    def wg(self):
        w = self.config.get("wireguard") or {}
        return w if w.get("enabled") and w.get("pubkey") and w.get("endpoint") else None

    def tunnel_ip(self, z):
        """Adresse du routeur dans le tunnel (10.66.0.10, .11, …), attribuée une fois pour toutes."""
        r = z["router"]
        if not r.get("tunnel_ip"):
            net = (self.wg() or {}).get("net", "10.66.0")
            with self.lock:
                used = {(x.get("router") or {}).get("tunnel_ip") for x in self.zones()}
                n = next(i for i in range(10, 251) if f"{net}.{i}" not in used)
                r["tunnel_ip"] = f"{net}.{n}"
                self._persist()
        return r["tunnel_ip"]

    def tunnel_up(self, z, pubkey):
        """Le routeur a créé son tunnel et envoie sa clé publique : on l'ajoute au serveur."""
        pubkey = (pubkey or "").replace(" ", "+").strip()
        if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", pubkey):
            raise RouterError("Clé du tunnel invalide")
        r = z["router"]
        ip = self.tunnel_ip(z)
        with self.lock:
            r["wg_pub"] = pubkey
            r["host"], r["auto_host"], r["tunnel"] = ip, False, "wg"  # ADER LINK passe désormais par le tunnel
            z["installed_at"] = int(time.time())
            self._persist()
        self.write_peers()
        return r["host"]

    def vpn_login(self, z):
        """Identifiant OpenVPN (RouterOS 6) propre à ce routeur."""
        r = z["router"]
        if not r.get("vpn_password"):
            with self.lock:
                r["vpn_password"] = secrets.token_hex(12)
                self._persist()
        return "al" + self.tunnel_ip(z).rsplit(".", 1)[1], r["vpn_password"]

    def ovpn_ip(self, z):
        return (self.wg() or {}).get("ovpn_net", "10.67.0") + "." + self.tunnel_ip(z).rsplit(".", 1)[1]

    def ovpn_up(self, z):
        """Un RouterOS 6 a créé son tunnel OpenVPN : ADER LINK passe désormais par lui."""
        ip = self.ovpn_ip(z)
        r = z["router"]
        with self.lock:
            r["host"], r["auto_host"], r["tunnel"] = ip, False, "ovpn"
            z["installed_at"] = int(time.time())
            self._persist()
        self.write_peers()
        return ip

    def ovpn_text(self):
        """« identifiant mot-de-passe adresse » : lu par l'aide système, qui crée les accès OpenVPN."""
        out = []
        for z in self.zones():
            r = z.get("router") or {}
            if r.get("vpn_password") and r.get("tunnel_ip"):
                user, pwd = self.vpn_login(z)
                out.append(f"{user} {pwd} {self.ovpn_ip(z)}")
        return "\n".join(out) + ("\n" if out else "")

    def peers_text(self):
        out = []
        for z in self.zones():
            r = z.get("router") or {}
            if r.get("wg_pub") and r.get("tunnel_ip"):
                out += ["[Peer]", f"PublicKey = {r['wg_pub']}", f"AllowedIPs = {r['tunnel_ip']}/32", ""]
        return "\n".join(out)

    def write_peers(self):
        """Le fichier est lu par l'aide système « aderlink-wg » (root), qui met le tunnel à jour."""
        if not self.wg_file:
            return
        # écrits sur place : l'aide système réagit à la fermeture du fichier
        Path(self.wg_file).with_name("ovpn-users.conf").write_text(self.ovpn_text())
        Path(self.wg_file).with_name("forwards.conf").write_text(self.forwards_text())
        Path(self.wg_file).write_text(self.peers_text())

    def install_script(self, z, request_host="", profiles=(), roaming=None):
        from .mikrotik import install_script, tunnel_lines
        r = z["router"]
        base, srv = self._base(z, request_host)
        wg = self.wg()
        extra = []
        if wg:
            user, pwd = self.vpn_login(z)
            self.write_peers()  # l'accès OpenVPN existe avant que le routeur ne se connecte
            extra += tunnel_lines(wg, self.tunnel_ip(z).rsplit(".", 1)[1], f"http://{base}/install/{z['install_token']}", user, pwd)
        g = roaming.group_of_zone(z["id"]) if roaming else None
        if g:  # par le tunnel s'il existe : le serveur reconnaît le routeur à son adresse de tunnel
            extra += (roaming.script_lines(g, wg.get("server", "10.66.0.1"), wg.get("ovpn_server", "10.67.0.1")) if wg
                      else roaming.script_lines(g, srv))
        api_from = f'{wg.get("server", "10.66.0.1")}/32,{wg.get("ovpn_server", "10.67.0.1")}' if wg else None
        return install_script(z["name"], r["password"], srv, r.get("port", 8728),
                              f"http://{base}/install/{z['install_token']}/ok", profiles, r.get("username") or "aderlink",
                              extra=extra, api_from=api_from)

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
