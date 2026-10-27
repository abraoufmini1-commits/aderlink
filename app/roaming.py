"""ADER Roaming : un même ticket valable sur plusieurs MikroTik, même éloignés.

- Un « réseau ADER Roaming » regroupe des routeurs (d'un même propriétaire).
- Les tickets Roaming ne sont PAS copiés dans les routeurs : chaque routeur demande à
  ADER LINK (serveur RADIUS) si le code est bon.
- Le temps (validité, durée) et les données sont comptés ici, pour tout le réseau :
  2 Go utilisés ici et là-bas = 2 Go en tout.
"""
import re
import secrets
import socket
import threading
import time

from . import radius as R

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
INTERIM_S = 60              # le routeur envoie la consommation toutes les minutes
ACTIVE_S = 3 * INTERIM_S    # une session sans nouvelles depuis 3 min est considérée finie


class RoamingError(Exception):
    pass


class Roaming:
    def __init__(self, config, store, tickets, collector=None, log=print):
        self.config = config
        self.store = store
        self.tickets = tickets
        self.collector = collector
        self.log = log
        self.threads = []
        self.listening = {}
        self.kicker = None  # fonction (zone_id, code) qui coupe une session sur un routeur
        self.wg_conf = lambda: None  # réglages du tunnel (branché au démarrage)
        self.seen = {}      # zone_id -> heure du dernier paquet RADIUS reçu de ce routeur
        self.ignored = {}   # adresse -> (heure, nom) : paquets d'un routeur non reconnu
        self.setup = {}     # zone_id -> {"ok": bool, "msg": str, "ts": int} : réglage automatique
        with store.lock:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS roaming_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, owner TEXT, secret TEXT, created_at INTEGER);
                CREATE TABLE IF NOT EXISTS roaming_members (zone_id TEXT PRIMARY KEY, group_id INTEGER);
                CREATE TABLE IF NOT EXISTS roaming_sessions (
                    session_id TEXT, nas_zone TEXT, code TEXT, home_zone TEXT, started INTEGER, updated INTEGER,
                    bytes INTEGER DEFAULT 0, seconds INTEGER DEFAULT 0, active INTEGER DEFAULT 1, mac TEXT,
                    PRIMARY KEY (session_id, nas_zone));
                CREATE INDEX IF NOT EXISTS idx_rs_code ON roaming_sessions(home_zone, code);
                """
            )
            store.db.commit()

    def _q(self, sql, params=(), commit=False):
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            rows = cur.fetchall()
            if commit:
                self.store.db.commit()
            return rows, cur.lastrowid

    def _zone(self, zid):
        return next((z for z in self.config.get("zones", []) if z["id"] == zid), None)

    # --- réseaux ------------------------------------------------------------------------------
    def group(self, gid):
        rows, _ = self._q("SELECT id, name, owner, secret, created_at FROM roaming_groups WHERE id = ?", (gid,))
        if not rows:
            return None
        gid, name, owner, secret, created = rows[0]
        members, _ = self._q("SELECT zone_id FROM roaming_members WHERE group_id = ? ORDER BY rowid", (gid,))
        zones = [self._zone(m[0]) for m in members]
        stats, _ = self._q(
            "SELECT COUNT(*), SUM(t.first_used IS NULL AND t.removed = 0), SUM(t.first_used IS NOT NULL) "
            "FROM tickets t JOIN batches b ON b.id = t.batch_id WHERE b.roaming_id = ?", (gid,))
        total, left, used = stats[0]
        now = int(time.time())
        online, _ = self._q("SELECT COUNT(*) FROM roaming_sessions s JOIN roaming_members m ON m.zone_id = s.nas_zone "
                            "WHERE m.group_id = ? AND s.active = 1 AND s.updated >= ?", (gid, now - ACTIVE_S))
        return {"id": gid, "name": name, "owner": owner, "secret": secret, "created_at": created,
                "zones": [{"id": z["id"], "name": z["name"]} for z in zones if z],
                "tickets": total or 0, "left": left or 0, "used": used or 0, "connected": online[0][0]}

    def groups(self, visible=lambda zid: True, owner=None, is_admin=True):
        rows, _ = self._q("SELECT id FROM roaming_groups ORDER BY id")
        out = []
        for (gid,) in rows:
            g = self.group(gid)
            if is_admin or g["owner"] == owner:
                g["zones"] = [dict(z, **self.zone_status(z["id"])) for z in g["zones"] if visible(z["id"])]
                out.append(g)
        return out

    def group_of_zone(self, zid):
        rows, _ = self._q("SELECT group_id FROM roaming_members WHERE zone_id = ?", (zid,))
        return self.group(rows[0][0]) if rows else None

    def save_group(self, data, owner=None, allowed=lambda zid: True, gid=None):
        name = " ".join(str(data.get("name") or "").split())[:40]
        if not name:
            raise RoamingError("Donnez un nom au réseau")
        zone_ids = [str(z) for z in (data.get("zones") or [])]
        for zid in zone_ids:
            if not self._zone(zid) or not allowed(zid):
                raise RoamingError("Routeur introuvable")
        if len(zone_ids) < 2:
            raise RoamingError("Choisissez au moins 2 routeurs")
        if gid is None:
            _, gid = self._q("INSERT INTO roaming_groups (name, owner, secret, created_at) VALUES (?,?,?,?)",
                             (name, owner, secrets.token_hex(12), int(time.time())), commit=True)
        else:
            self._q("UPDATE roaming_groups SET name = ? WHERE id = ?", (name, gid), commit=True)
        with self.store.lock:
            self.store.db.execute("DELETE FROM roaming_members WHERE group_id = ?", (gid,))
            # un routeur n'appartient qu'à un seul réseau : il quitte l'ancien s'il en avait un
            self.store.db.executemany("INSERT OR REPLACE INTO roaming_members (zone_id, group_id) VALUES (?,?)",
                                      [(z, gid) for z in zone_ids])
            self.store.db.commit()
        self.reset_setup(gid)
        return self.group(gid)

    def delete_group(self, gid):
        self._q("DELETE FROM roaming_members WHERE group_id = ?", (gid,))
        self._q("DELETE FROM roaming_groups WHERE id = ?", (gid,), commit=True)
        return {"ok": True}

    def member_zones(self, gid):
        rows, _ = self._q("SELECT zone_id FROM roaming_members WHERE group_id = ?", (gid,))
        return [r[0] for r in rows]

    # --- script RouterOS ------------------------------------------------------------------------
    def ports(self):
        r = self.config.get("radius") or {}
        return int(r.get("auth_port", 1812)), int(r.get("acct_port", 1813))

    def script_lines(self, group, server, server_v6=None):
        """server_v6 : adresse du serveur pour un RouterOS 6 (autre tunnel) ; choisie sur le routeur."""
        auth, acct = self.ports()
        add = (f'/radius add service=hotspot address=$rs secret="{group["secret"]}" '
               f'authentication-port={auth} accounting-port={acct} timeout=3s comment="ADER Roaming"')
        pick = f':local rs "{server}"; '
        if server_v6:
            pick += f':if ([:pick [/system resource get version] 0 1] = "6") do={{ :set rs "{server_v6}" }}; '
        return [
            "# ADER Roaming : les tickets du reseau sont verifies par ADER LINK",
            '/radius remove [find comment="ADER Roaming"]',
            pick + add,
            f"/ip hotspot profile set [find] use-radius=yes radius-interim-update={INTERIM_S}s",
            "# (le passage automatique d'un routeur a l'autre est regle par ADER LINK lui-meme, sans toucher a vos autres reglages)",
            f':put "ADER Roaming active : reseau {_ascii(group["name"])}"',
        ]

    # --- réglage automatique des routeurs (sans coller de script) ------------------------------
    def server_for(self, zone):
        """Adresse du serveur RADIUS vue par ce routeur : par son tunnel (WireGuard ou OpenVPN)."""
        wg = self.wg_conf() or {}
        r = zone.get("router") or {}
        if not wg or r.get("tunnel") not in ("wg", "ovpn"):
            return None
        return wg.get("ovpn_server", "10.67.0.1") if r["tunnel"] == "ovpn" else wg.get("server", "10.66.0.1")

    def apply_zone(self, zone, api, g=None):
        """Règle le RADIUS ADER Roaming sur un routeur, par l'API. Renvoie (ok, message)."""
        from .routeros import RouterOSError
        g = g or self.group_of_zone(zone["id"])
        if not g:
            return True, "Pas dans un réseau Roaming"
        srv = self.server_for(zone)
        if not srv:
            return False, "Pas de tunnel vers le serveur : recollez la ligne d'installation du routeur"
        auth, acct = self.ports()
        try:
            for r in api.query("/radius/print", "?comment=ADER Roaming"):
                api.query("/radius/remove", f"=.id={r['.id']}")
            api.query("/radius/add", "=service=hotspot", f"=address={srv}", f"=secret={g['secret']}",
                      f"=authentication-port={auth}", f"=accounting-port={acct}", "=timeout=3s", "=comment=ADER Roaming")
            for prof in api.query("/ip/hotspot/profile/print"):
                api.query("/ip/hotspot/profile/set", f"=.id={prof['.id']}", "=use-radius=yes",
                          f"=radius-interim-update={INTERIM_S}s")
                # On ne touche PAS à l'onglet Login (modes de connexion) : il reste comme vous l'avez réglé.
                # Seule exception : réparer l'onglet Login si la version 6.32.0 d'ADER LINK l'avait remplacé
                # (exactement « mac, http-chap, http-pap, cookie ») -> remis comme avant : tout coché sauf Trial.
                modes = sorted(m for m in (prof.get("login-by") or "").split(",") if m)
                if modes == ["cookie", "http-chap", "http-pap", "mac"]:
                    for value in ("cookie,http-chap,https,http-pap,mac,mac-cookie", "cookie,http-chap,http-pap,mac,mac-cookie"):
                        try:
                            api.query("/ip/hotspot/profile/set", f"=.id={prof['.id']}", f"=login-by={value}",
                                      "=mac-auth-mode=mac-as-username")
                            break
                        except RouterOSError:
                            continue
        except RouterOSError as e:
            return False, f"Le routeur a refusé : {e}"
        return True, f"Réglé (serveur {srv})"

    def diagnose(self, gid):
        """Regarde chaque routeur du réseau par l'API : serveurs RADIUS, compteurs, profils hotspot."""
        from .tickets import open_router
        from .routeros import RouterOSError
        g = self.group(gid)
        out = []
        for zid in self.member_zones(gid):
            z = self._zone(zid)
            if not z:
                continue
            d = {"id": zid, "name": z["name"], "expected": self.server_for(z), **self.zone_status(zid),
                 "radius": [], "profiles": [], "error": None}
            try:
                with open_router(z) as api:
                    for r in api.query("/radius/print"):
                        e = {"address": r.get("address", ""), "service": r.get("service", ""),
                             "ours": r.get("comment") == "ADER Roaming", "disabled": r.get("disabled") in ("true", "yes")}
                        try:
                            m = api.query("/radius/monitor", f"=numbers={r['.id']}", "=once=")
                            m = m[0] if m else {}
                            e.update({k: m.get(k, "") for k in ("requests", "accepts", "rejects", "resends", "timeouts", "bad-replies", "pending")})
                        except RouterOSError:
                            pass
                        d["radius"].append(e)
                    for pr in api.query("/ip/hotspot/profile/print"):
                        d["profiles"].append({"name": pr.get("name", ""), "use_radius": pr.get("use-radius") in ("true", "yes")})
                    d["servers"] = [{"name": h.get("name", ""), "profile": h.get("profile", "")} for h in api.query("/ip/hotspot/print")]
            except (RouterOSError, OSError) as e:
                d["error"] = str(e)
            out.append(d)
        return {"name": g["name"] if g else "", "routers": out}

    def ensure(self, zone, api):
        """Appelé à chaque relevé : règle le routeur une fois (puis toutes les heures s'il a échoué)."""
        g = self.group_of_zone(zone["id"])
        if not g:
            return
        st = self.setup.get(zone["id"])
        if st and st.get("gid") == g["id"] and (st["ok"] or time.time() - st["ts"] < 3600):
            return
        ok, msg = self.apply_zone(zone, api, g)
        self.setup[zone["id"]] = {"ok": ok, "msg": msg, "ts": int(time.time()), "gid": g["id"]}
        if not ok:
            self.log(f"ADER Roaming : {zone['name']} : {msg}")

    def reset_setup(self, gid):
        """Après une modification du réseau : chaque routeur sera réglé à nouveau au prochain relevé."""
        for zid in self.member_zones(gid):
            self.setup.pop(zid, None)

    def zone_status(self, zid):
        now = int(time.time())
        st = self.setup.get(zid) or {}
        seen = self.seen.get(zid)
        return {"setup_ok": st.get("ok"), "setup_msg": st.get("msg", "En attente du prochain relevé"),
                "last_seen": seen, "seen_ago": now - seen if seen else None}

    # --- RADIUS -------------------------------------------------------------------------------
    def zone_for(self, ip, pkt):
        """Quel routeur parle ? Par son adresse, sinon par son nom (identity)."""
        nas_id = pkt.text(R.NAS_IDENTIFIER)
        nas_ip = pkt.get(R.NAS_IP_ADDRESS)
        nas_ip = socket.inet_ntoa(nas_ip) if nas_ip and len(nas_ip) == 4 else None
        rows, _ = self._q("SELECT zone_id FROM roaming_members")
        members = [self._zone(r[0]) for r in rows]
        members = [z for z in members if z]
        for z in members:
            r = z.get("router") or {}
            if ip in (r.get("host"), z.get("last_ip")):
                return z
        state = self.collector.snapshot() if self.collector else {}
        for z in members:
            if nas_id and nas_id == (state.get(z["id"]) or {}).get("identity"):
                return z
        for z in members:
            if nas_ip and nas_ip == (z.get("router") or {}).get("host"):
                return z
        return None

    def _ticket(self, gid, code):
        rows, _ = self.tickets._q(
            "SELECT t.zone_id, t.first_used, t.expired_at, t.bytes_used, t.uptime_used, "
            "b.validity_s, b.uptime_s, b.data_mb, b.forfait_id, b.id "
            "FROM tickets t JOIN batches b ON b.id = t.batch_id "
            "WHERE t.code = ? AND b.roaming_id = ? AND t.removed = 0 AND b.removed = 0 ORDER BY b.id DESC LIMIT 1",
            (code, gid))
        if not rows:
            return None
        k = ("home", "first_used", "expired_at", "bytes_used", "uptime_used", "validity_s", "uptime_s", "data_mb",
             "forfait_id", "batch_id")
        return dict(zip(k, rows[0]))

    def authorize(self, gid, code, now=None, zone_id=None, mac=None):
        """Décide d'un Access-Request. Renvoie (accepté, message, attributs)."""
        now = now or int(time.time())
        t = self._ticket(gid, code)
        if not t:
            return False, "Code inconnu sur ce reseau", {}
        if t["data_mb"] and (t["bytes_used"] or 0) >= t["data_mb"] * 1024 * 1024:
            return False, "Donnees epuisees", {}
        if t["expired_at"]:
            return False, "Ticket expire", {}
        limits = {}
        if t["validity_s"]:
            if t["first_used"] and t["first_used"] + t["validity_s"] <= now:
                self._expire(t["home"], code, now)
                return False, "Ticket expire", {}
            limits["timeout"] = (t["first_used"] or now) + t["validity_s"] - now
        if t["uptime_s"]:
            left = t["uptime_s"] - (t["uptime_used"] or 0)
            if left <= 0:
                return False, "Temps de connexion epuise", {}
            limits["timeout"] = min(limits.get("timeout", left), left)
        if t["data_mb"]:
            left = t["data_mb"] * 1024 * 1024 - (t["bytes_used"] or 0)
            if left <= 0:
                return False, "Donnees epuisees", {}
            limits["bytes"] = left
        f = self.tickets.get_forfait(t["forfait_id"]) if t["forfait_id"] else None
        shared = (f or {}).get("shared_users") or 1
        # Une seule connexion à la fois sur tout le réseau (ou « appareils par ticket » du profil) :
        # sessions en cours + connexions acceptées il y a moins de 90 s (avant que le routeur ne confirme).
        # Le même appareil qui se reconnecte sur le même routeur n'est pas compté contre lui-même.
        rows, _ = self._q("SELECT nas_zone, mac, session_id, updated FROM roaming_sessions WHERE home_zone = ? AND code = ? "
                          "AND active = 1 AND updated >= ?", (t["home"], code, now - ACTIVE_S))
        same = lambda r: bool(mac) and (r[1] or "").upper() == mac.upper()
        busy = [r for r in rows if not same(r) and not (r[2].startswith("pending-") and r[3] < now - 90)]
        moved = {r[0] for r in rows if same(r) and r[0] != zone_id}  # le même téléphone est passé sur un autre routeur
        if len(busy) >= shared:
            other = any(r[0] != zone_id for r in busy)
            return False, ("Ticket deja connecte sur un autre routeur du reseau : deconnectez-le d'abord" if other
                           else "Ticket deja connecte sur un autre appareil"), {}
        if moved and zone_id:
            with self.store.lock:
                self.store.db.execute("UPDATE roaming_sessions SET active = 0 WHERE home_zone = ? AND code = ? AND upper(mac) = ? "
                                      "AND nas_zone != ?", (t["home"], code, mac.upper(), zone_id))
                self.store.db.commit()
            if self.kicker:  # on ferme l'ancienne connexion là où il était
                threading.Thread(target=self._kick_zones, args=(moved, [code, mac]), daemon=True).start()
        if zone_id:  # réservation immédiate : bloque un 2e appareil pendant que le routeur ouvre la session
            with self.store.lock:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO roaming_sessions (session_id, nas_zone, code, home_zone, started, updated, mac, active) "
                    "VALUES (?,?,?,?,?,?,?,1)", (f"pending-{mac or 'x'}", zone_id, code, t["home"], now, now, mac))
                self.store.db.commit()
        if f:
            rate = self.tickets._rate(f["down_mbps"], f["up_mbps"])
            if rate:
                limits["rate"] = rate
        if not t["first_used"]:
            self.tickets._mark_used(t["home"], code, now)
        return True, "", limits

    def _expire(self, home, code, now):
        self._q("UPDATE tickets SET expired_at = ? WHERE zone_id = ? AND code = ? AND expired_at IS NULL",
                (now, home, code), commit=True)

    def account(self, gid, zone, pkt, now=None):
        now = now or int(time.time())
        code = pkt.text(R.USER_NAME) or ""
        cls = pkt.text(R.CLASS) or ""
        if cls.startswith("AL:"):
            code = cls[3:]
        elif MAC_RE.match(code):  # session ouverte automatiquement : le nom est l'adresse MAC
            code = self.code_for_mac(gid, code) or code
        t = self._ticket(gid, code)
        if not t:
            return
        sid = pkt.text(R.ACCT_SESSION_ID) or ""
        status = pkt.int(R.ACCT_STATUS_TYPE)
        total = (pkt.int(R.ACCT_INPUT_OCTETS) + (pkt.int(R.ACCT_INPUT_GIGAWORDS) << 32) +
                 pkt.int(R.ACCT_OUTPUT_OCTETS) + (pkt.int(R.ACCT_OUTPUT_GIGAWORDS) << 32))
        secs = pkt.int(R.ACCT_SESSION_TIME)
        with self.store.lock:
            db = self.store.db
            db.execute("DELETE FROM roaming_sessions WHERE session_id LIKE 'pending-%' AND nas_zone = ? AND code = ?",
                       (zone["id"], code))
            db.execute("INSERT OR IGNORE INTO roaming_sessions (session_id, nas_zone, code, home_zone, started, updated, mac) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, zone["id"], code, t["home"], now, now, pkt.text(R.CALLING_STATION_ID)))
            db.execute("UPDATE roaming_sessions SET updated = ?, bytes = MAX(bytes, ?), seconds = MAX(seconds, ?), active = ? "
                       "WHERE session_id = ? AND nas_zone = ?", (now, total, secs, 0 if status == R.ACCT_STOP else 1, sid, zone["id"]))
            db.execute("UPDATE tickets SET bytes_used = (SELECT COALESCE(SUM(bytes), 0) FROM roaming_sessions WHERE home_zone = ? AND code = ?), "
                       "uptime_used = (SELECT COALESCE(SUM(seconds), 0) FROM roaming_sessions WHERE home_zone = ? AND code = ?), "
                       "first_used = COALESCE(first_used, ?) WHERE zone_id = ? AND code = ?",
                       (t["home"], code, t["home"], code, now, t["home"], code))
            db.commit()
        if status != R.ACCT_START and self.finished(gid, code, now):
            self.end_ticket(gid, code, t["home"], now)

    def finished(self, gid, code, now=None):
        """Vrai si le ticket n'a plus de temps ni de données (compté sur tout le réseau)."""
        now = now or int(time.time())
        t = self._ticket(gid, code)
        if not t:
            return False
        if t["validity_s"] and t["first_used"] and t["first_used"] + t["validity_s"] <= now:
            return True
        if t["uptime_s"] and (t["uptime_used"] or 0) >= t["uptime_s"]:
            return True
        return bool(t["data_mb"] and (t["bytes_used"] or 0) >= t["data_mb"] * 1024 * 1024)

    def end_ticket(self, gid, code, home, now=None):
        """Ticket fini : refusé partout, et coupé sur TOUS les routeurs du réseau où il serait encore connecté."""
        self._expire(home, code, now or int(time.time()))
        if self.kicker:
            threading.Thread(target=self._kick_all, args=(gid, code), daemon=True).start()

    def _kick_all(self, gid, code):
        rows, _ = self._q("SELECT DISTINCT mac FROM roaming_sessions WHERE code = ? AND mac IS NOT NULL", (code,))
        self._kick_zones(self.member_zones(gid), [code] + [r[0] for r in rows if r[0]])

    def _kick_zones(self, zones, names):
        for zid in zones:
            for name in names:  # la session porte le code, ou l'adresse MAC si le téléphone est entré tout seul
                try:
                    self.kicker(zid, name)
                except Exception as e:  # routeur injoignable : la limite envoyée au routeur coupe quand même
                    self.log(f"ADER Roaming : coupure de {name} sur {zid} impossible ({e})")

    def code_for_mac(self, gid, mac):
        """Le ticket Roaming en cours de ce téléphone sur ce réseau (le plus récent), s'il y en a un."""
        zones = self.member_zones(gid)
        if not zones or not mac:
            return None
        rows, _ = self._q("SELECT s.code FROM roaming_sessions s JOIN roaming_members m ON m.zone_id = s.nas_zone "
                          "WHERE m.group_id = ? AND upper(s.mac) = ? AND s.code NOT LIKE '%:%' ORDER BY s.updated DESC LIMIT 1",
                          (gid, mac.upper().replace("-", ":")))
        return rows[0][0] if rows else None

    def sweep(self, now=None):
        """Toutes les minutes : les tickets dont la validité est passée sont fermés sur tout le réseau."""
        now = now or int(time.time())
        rows, _ = self.tickets._q(
            "SELECT b.roaming_id, t.code, t.zone_id FROM tickets t JOIN batches b ON b.id = t.batch_id "
            "WHERE b.roaming_id IS NOT NULL AND t.expired_at IS NULL AND t.removed = 0 AND t.first_used IS NOT NULL "
            "AND b.validity_s > 0 AND t.first_used + b.validity_s <= ?", (now,))
        for gid, code, home in rows:
            self.end_ticket(gid, code, home, now)

    def handle(self, data, ip, kind):
        """Traite un paquet reçu ; renvoie la réponse à envoyer (ou None pour l'ignorer)."""
        try:
            pkt = R.Packet.parse(data)
        except R.RadiusError:
            return None
        zone = self.zone_for(ip, pkt)
        if not zone:
            self.ignored[ip] = (int(time.time()), pkt.text(R.NAS_IDENTIFIER) or "")
            self.log(f"ADER Roaming : paquet ignoré de {ip} (routeur inconnu ou hors réseau)")
            return None
        self.seen[zone["id"]] = int(time.time())
        g = self.group_of_zone(zone["id"])
        if not g:
            return None
        secret = g["secret"].encode()
        if kind == "auth" and pkt.code == R.ACCESS_REQUEST:
            if not R.check_message_authenticator(pkt, secret):
                return None
            code = pkt.text(R.USER_NAME) or ""
            mac = pkt.text(R.CALLING_STATION_ID)
            if MAC_RE.match(code):
                # connexion automatique : ce téléphone a-t-il un ticket Roaming en cours sur ce réseau ?
                mac = code.upper().replace("-", ":")
                found = self.code_for_mac(g["id"], mac)
                if not found or not any(R.password_ok(pkt, secret, pw) for pw in (code, mac, "")):
                    return R.reply(pkt, R.ACCESS_REJECT, secret, R.attr(R.REPLY_MESSAGE, "Entrez votre code"))
                code = found
                ok, msg, lim = self.authorize(g["id"], code, zone_id=zone["id"], mac=mac)
            elif not R.password_ok(pkt, secret, code):
                ok, msg, lim = False, "Code ou mot de passe incorrect", {}
            else:
                ok, msg, lim = self.authorize(g["id"], code, zone_id=zone["id"], mac=mac)
            if not ok:
                return R.reply(pkt, R.ACCESS_REJECT, secret, R.attr(R.REPLY_MESSAGE, msg))
            attrs = R.attr(R.ACCT_INTERIM_INTERVAL, INTERIM_S) + R.attr(R.CLASS, f"AL:{code}")
            if "timeout" in lim:
                attrs += R.attr(R.SESSION_TIMEOUT, max(1, int(lim["timeout"])))
            if "bytes" in lim:
                b = int(lim["bytes"])
                attrs += R.vsa(R.MIKROTIK, R.MT_TOTAL_LIMIT, b & 0xFFFFFFFF)
                attrs += R.vsa(R.MIKROTIK, R.MT_TOTAL_LIMIT_GIGAWORDS, b >> 32)
            if "rate" in lim:
                attrs += R.vsa(R.MIKROTIK, R.MT_RATE_LIMIT, lim["rate"])
            return R.reply(pkt, R.ACCESS_ACCEPT, secret, attrs)
        if kind == "acct" and pkt.code == R.ACCOUNTING_REQUEST:
            if not R.check_accounting(pkt, secret):
                return None
            self.account(g["id"], zone, pkt)
            return R.reply(pkt, R.ACCOUNTING_RESPONSE, secret)
        return None

    # --- écoute UDP -----------------------------------------------------------------------------
    def start(self, host="0.0.0.0"):
        auth, acct = self.ports()
        for kind, port in (("auth", auth), ("acct", acct)):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((host, port))
            except OSError as e:
                self.log(f"ADER Roaming : port UDP {port} indisponible ({e})")
                continue
            self.listening[kind] = sock.getsockname()[1]
            th = threading.Thread(target=self._loop, args=(sock, kind), daemon=True, name=f"radius-{kind}")
            th.start()
            self.threads.append(th)
        if self.listening:
            self.log(f"ADER Roaming : serveur RADIUS en écoute (UDP {auth}/{acct})")

        def loop():
            while True:
                time.sleep(60)
                try:
                    self.sweep()
                except Exception as e:
                    self.log(f"ADER Roaming : vérification des tickets : {e!r}")
        threading.Thread(target=loop, daemon=True, name="roaming-sweep").start()

    def _loop(self, sock, kind):
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                return
            try:
                out = self.handle(data, addr[0], kind)
                if out:
                    sock.sendto(out, addr)
            except Exception as e:  # un paquet bizarre ne doit jamais arrêter le serveur
                self.log(f"ADER Roaming : erreur {e!r}")


def _ascii(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode()
    return "".join(c for c in s if c.isalnum() or c in " -_")[:40]
