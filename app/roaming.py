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
        self.shared = {}    # zone_id -> True si le routeur a aussi un autre système de tickets (autre RADIUS)
        import collections
        self._goz = {}       # zone_id -> (heure, réseau) : cache court
        self._members = None
        self._tl = threading.local()
        self.flow = {}  # file d'attente des demandes RADIUS (en attente, traitées, abandonnées)
        import queue
        self._writes = queue.Queue()
        self.async_writes = False  # True dans le programme Roaming : écritures à côté, par lots
        self.write_lost = 0
        threading.Thread(target=self._writer, daemon=True, name="roaming-writer").start()
        self._held, self._held_lock = {}, threading.Lock()  # réservations immédiates, en mémoire
        self.kicker_many = None  # fonction (zone_id, [noms]) : coupe plusieurs sessions avec une seule connexion
        self._kicks, self._kick_lock, self._ended, self._kicked_at = queue.Queue(), threading.Lock(), {}, {}
        threading.Thread(target=self._kicker_loop, daemon=True, name="roaming-kicker").start()
        self.journal = collections.deque(maxlen=80)  # dernières demandes de connexion (heure, routeur, code, résultat)
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
            # MAC en majuscules partout, puis index : la connexion automatique par MAC ne relit plus toute la table
            if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_rs_mac'").fetchone():
                store.db.execute("UPDATE roaming_sessions SET mac = upper(replace(mac, '-', ':')) WHERE mac IS NOT NULL")
                store.db.execute("CREATE INDEX idx_rs_mac ON roaming_sessions(mac, updated)")
            store.db.execute("CREATE INDEX IF NOT EXISTS idx_rs_active ON roaming_sessions(active, updated)")
            # chaque paquet de consommation efface la réservation « pending » du routeur : par index (avant : toute la table)
            store.db.execute("CREATE INDEX IF NOT EXISTS idx_rs_nas_code ON roaming_sessions(nas_zone, code)")
            store.db.execute("CREATE INDEX IF NOT EXISTS idx_rs_code_only ON roaming_sessions(code)")
            store.db.commit()

    def _q(self, sql, params=(), commit=False):
        if not commit and sql.lstrip()[:6].upper() == "SELECT":  # lecture : sans attendre le collecteur
            return self.store.read(sql, params), None
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            rows = cur.fetchall()
            # toujours enregistrer une écriture : une écriture laissée ouverte bloquait les lectures et le journal
            self.store.db.commit()
            return rows, cur.lastrowid

    def _zone(self, zid):
        return next((z for z in self.config.get("zones", []) if z["id"] == zid), None)

    # --- réseaux ------------------------------------------------------------------------------
    def group(self, gid, stats=True):
        rows, _ = self._q("SELECT id, name, owner, secret, created_at FROM roaming_groups WHERE id = ?", (gid,))
        if not rows:
            return None
        gid, name, owner, secret, created = rows[0]
        members, _ = self._q("SELECT zone_id FROM roaming_members WHERE group_id = ? ORDER BY rowid", (gid,))
        zones = [self._zone(m[0]) for m in members]
        if not stats:  # léger : pour chaque paquet RADIUS et chaque relevé (les compteurs ne servent qu'à l'écran)
            return {"id": gid, "name": name, "owner": owner, "secret": secret, "created_at": created,
                    "zones": [{"id": z["id"], "name": z["name"]} for z in zones if z]}
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

    def group_of_zone(self, zid, ttl=30):
        """Le réseau d'un routeur (sans les compteurs), gardé 30 s : appelé très souvent."""
        now = time.time()
        hit = self._goz.get(zid)
        if hit and now - hit[0] < ttl:
            return hit[1]
        rows, _ = self._q("SELECT group_id FROM roaming_members WHERE zone_id = ?", (zid,))
        g = self.group(rows[0][0], stats=False) if rows else None
        self._goz[zid] = (now, g)
        return g

    def _forget_cache(self):
        self._goz.clear()
        self._members = None

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
        self._forget_cache()
        self.reset_setup(gid)
        return self.group(gid)

    def delete_group(self, gid):
        self._q("DELETE FROM roaming_members WHERE group_id = ?", (gid,))
        self._q("DELETE FROM roaming_groups WHERE id = ?", (gid,), commit=True)
        self._forget_cache()
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
            ours_fields = self._our_fields(srv, g, zone["id"])
            api.query("/radius/add", *ours_fields)
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
            # Un autre système de tickets (autre serveur RADIUS) sur ce routeur ? Les deux marchent ensemble :
            # ADER Roaming passe EN PREMIER ; pour un code qu'il ne connaît pas, il ne répond pas,
            # et le routeur demande alors à l'autre serveur (quelques secondes plus tard).
            first_ok, first_msg = self.put_first(zone, api, ours_fields)
        except RouterOSError as e:
            return False, f"Le routeur a refusé : {e}"
        if not first_ok:
            return False, first_msg
        if self.shared.get(zone["id"]):
            return True, f"Réglé (serveur {srv}), avec l'autre système de tickets du routeur"
        return True, f"Réglé (serveur {srv})"

    def _timeout(self, zid=None):
        """Temps que le routeur attend ADER Roaming : 8 s, ou 3 s s'il a aussi un autre système de tickets
        (pour que ses clients à lui n'attendent pas trop quand le code n'est pas un ticket ADER LINK)."""
        return "3s" if zid and self.shared.get(zid) else "8s"

    def _our_fields(self, srv, g, zid=None):
        auth, acct = self.ports()
        return ["=service=hotspot", f"=address={srv}", f"=secret={g['secret']}",
                f"=authentication-port={auth}", f"=accounting-port={acct}", f"=timeout={self._timeout(zid)}",
                "=comment=ADER Roaming"]

    # champs d'une entrée RADIUS qu'on peut recopier tels quels
    RADIUS_FIELDS = ("service", "called-id", "domain", "address", "secret", "authentication-port", "accounting-port",
                     "timeout", "accounting-backup", "realm", "src-address", "protocol", "certificate",
                     "require-message-auth", "comment", "disabled")

    def put_first(self, zone, api, ours_fields):
        """Met ADER Roaming avant les autres serveurs RADIUS hotspot. Renvoie (ok, message).
        On ne change jamais les réglages de l'autre système de tickets :
        1) commande « move » ; 2) on recrée NOTRE entrée juste avant la sienne (place-before) ;
        3) seulement si le routeur montre le mot de passe de l'autre entrée : on la recopie à l'identique
           après la nôtre, puis on enlève l'ancienne."""
        from .routeros import RouterOSError

        def hs(e):
            return "hotspot" in (e.get("service") or "") and e.get("disabled") not in ("true", "yes")

        def state():
            entries = api.query("/radius/print")
            ours = [e for e in entries if e.get("comment") == "ADER Roaming"]
            before = []
            for e in entries:
                if e.get("comment") == "ADER Roaming":
                    break
                if hs(e):
                    before.append(e)
            self.shared[zone["id"]] = any(e.get("comment") != "ADER Roaming" and hs(e) for e in entries)
            return entries, ours, before

        entries, ours, before = state()
        if not ours or not before:
            return True, "ok"
        errs = []
        # 1) déplacer
        try:
            api.query("/radius/move", f"=numbers={ours[0]['.id']}", f"=destination={before[0]['.id']}")
        except RouterOSError as e:
            errs.append(f"move : {e}")
        entries, ours, before = state()
        # 2) recréer la nôtre juste avant l'autre
        if before and ours:
            try:
                api.query("/radius/add", *ours_fields, f"=place-before={before[0]['.id']}")
                for e in ours:
                    api.query("/radius/remove", f"=.id={e['.id']}")
            except RouterOSError as e:
                errs.append(f"place-before : {e}")
            entries, ours, before = state()
            if not ours:  # par sécurité : la nôtre doit toujours exister
                api.query("/radius/add", *ours_fields)
                entries, ours, before = state()
        # 3) recopier l'autre à l'identique (seulement si son mot de passe est lisible)
        for e in before:
            if not e.get("secret"):
                errs.append(f"mot de passe de {e.get('address')} non lisible")
                continue
            fields = [f"={k}={e[k]}" for k in self.RADIUS_FIELDS if e.get(k) not in (None, "")]
            try:
                api.query("/radius/add", *fields)
            except RouterOSError as e2:
                errs.append(f"copie : {e2}")
                continue
            api.query("/radius/remove", f"=.id={e['.id']}")  # la copie est en place : on enlève l'ancienne
        if before:
            entries, ours, before = state()
        if before:
            err = " ; ".join(errs) or "raison inconnue"
            self.log(f"ADER Roaming : {zone['name']} : impossible de passer avant {before[0].get('address')} ({err})")
            return False, f"Le routeur refuse de mettre ADER Roaming en premier ({err})"
        return True, "ok"

    def diagnose(self, gid):
        """Regarde chaque routeur du réseau par l'API : serveurs RADIUS, compteurs, profils hotspot."""
        from .tickets import open_router
        from .routeros import RouterOSError
        g = self.group(gid, stats=False)
        from concurrent.futures import ThreadPoolExecutor

        def one(zid):  # tous les routeurs du réseau lus en même temps (avant : l'un après l'autre)
            z = self._zone(zid)
            if not z:
                return None
            d = {"id": zid, "name": z["name"], "expected": self.server_for(z), **self.zone_status(zid),
                 "radius": [], "profiles": [], "error": None}
            try:
                with open_router(z) as api:
                    # on corrige l'ordre tout de suite (ADER Roaming avant l'autre système de tickets)
                    if g and self.server_for(z):
                        try:
                            if not any(e.get("comment") == "ADER Roaming" for e in api.query("/radius/print")):
                                ok, msg = self.apply_zone(z, api, g)  # réglage effacé : on le remet tout de suite
                                self.setup[zid] = {"ok": ok, "msg": msg, "ts": int(time.time()), "gid": g["id"],
                                                   "checked": time.time()}
                            else:
                                ok, msg = self.put_first(z, api, self._our_fields(self.server_for(z), g, z["id"]))
                            d["order_error"] = None if ok else msg
                        except RouterOSError as e:
                            d["order_error"] = f"Le routeur a refusé : {e}"
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
            return d
        zones = self.member_zones(gid)
        with ThreadPoolExecutor(max_workers=max(1, min(12, len(zones)))) as pool:
            out = [d for d in pool.map(one, zones) if d]
        zids = {d["id"] for d in out}
        recent = [{"at": x[0], "zone": x[2], "code": x[3], "result": x[4], "message": x[5],
                   "took": x[6] if len(x) > 6 else None}
                  for x in reversed(self.journal) if x[1] in zids][:30]
        return {"name": g["name"] if g else "", "routers": out, "recent": recent}

    CHECK_S = 600  # toutes les 10 minutes : l'autre système de tickets peut remettre sa configuration

    def needs_ensure(self, zone):
        """Le réglage ADER Roaming de ce routeur est-il à faire ou à revérifier (toutes les 10 min) ?"""
        g = self.group_of_zone(zone["id"])
        if not g:
            return False
        st = self.setup.get(zone["id"])
        return not (st and st.get("gid") == g["id"] and time.time() - st.get("checked", st["ts"]) < self.CHECK_S)

    def ensure(self, zone, api):
        """Appelé à chaque relevé : règle le routeur, puis vérifie toutes les 10 minutes que le réglage
        ADER Roaming est toujours là et passe toujours en premier (sinon on le remet aussitôt)."""
        from .routeros import RouterOSError
        g = self.group_of_zone(zone["id"])
        if not g:
            return
        now = time.time()
        st = self.setup.get(zone["id"])
        if st and st.get("gid") == g["id"] and now - st.get("checked", st["ts"]) < self.CHECK_S:
            return
        if st and st.get("gid") == g["id"] and st["ok"] and self.server_for(zone):
            try:
                entries = api.query("/radius/print")
                present = any(e.get("comment") == "ADER Roaming" for e in entries)
                if present:
                    want = self._timeout(zone["id"])
                    for e in entries:  # le délai d'attente du routeur, mis à jour sans recréer le réglage
                        if e.get("comment") == "ADER Roaming" and (e.get("timeout") or "").replace("ms", "") not in (want, want[:-1] + "000"):
                            api.query("/radius/set", f"=.id={e['.id']}", f"=timeout={want}")
                    ok, msg = self.put_first(zone, api, self._our_fields(self.server_for(zone), g, zone["id"]))
                    st["checked"] = now
                    if ok:
                        return
                    self.log(f"ADER Roaming : {zone['name']} : {msg}")
            except RouterOSError as e:
                st.update(ok=False, msg=f"Le routeur a refusé : {e}", ts=int(now), checked=now)
                return
            # réglage effacé (souvent par l'autre système) ou pas en premier : on remet tout
            self.log(f"ADER Roaming : {zone['name']} : réglage absent ou pas en premier, remis automatiquement")
        ok, msg = self.apply_zone(zone, api, g)
        self.setup[zone["id"]] = {"ok": ok, "msg": msg, "ts": int(now), "gid": g["id"], "checked": now}
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
        if self._members is None or time.time() - self._members[0] > 30:
            rows, _ = self._q("SELECT zone_id FROM roaming_members")
            self._members = (time.time(), [r[0] for r in rows])
        members = [self._zone(zid) for zid in self._members[1]]
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
        # réservations encore en mémoire (pas encore écrites dans la base) : comptées aussi
        with self._held_lock:
            held = [h for h in self._held.get((t["home"], code), []) if h[2] >= now - 90]
            self._held[(t["home"], code)] = held
        known = {(r[1] or "").upper() for r in rows}
        for z, m, ts in held:
            r = (z, m, "pending-" + (m or "x"), ts)
            if not same(r) and (m or "").upper() not in known:
                busy.append(r)
        moved = {r[0] for r in rows if same(r) and r[0] != zone_id}  # le même téléphone est passé sur un autre routeur
        if len(busy) >= shared:
            other = any(r[0] != zone_id for r in busy)
            return False, ("Ticket deja connecte sur un autre routeur du reseau : deconnectez-le d'abord" if other
                           else "Ticket deja connecte sur un autre appareil"), {}
        if moved and zone_id:
            a = (t["home"], code, mac.upper(), zone_id)
            self._write(lambda db: db.execute("UPDATE roaming_sessions SET active = 0 WHERE home_zone = ? AND code = ? "
                                              "AND mac = ? AND nas_zone != ?", a))
            if self.kicker:  # on ferme l'ancienne connexion là où il était
                self._kick_zones(moved, [code, mac])
        # réservation : bloque un 2e appareil pendant que le routeur ouvre la session. Écrite à côté, APRÈS la
        # réponse : si l'application principale écrit au même moment, la base peut faire attendre jusqu'à 5 s,
        # et c'était autant de secondes d'attente pour le client devant la page de connexion.
        if zone_id:
            with self._held_lock:
                if len(self._held) > 5000:
                    self._held.clear()
                self._held.setdefault((t["home"], code), []).append((zone_id, _mac(mac), now))
            args = (f"pending-{mac or 'x'}", zone_id, code, t["home"], now, now, _mac(mac))
            self._reserve(args)
        if f:
            rate = self.tickets._rate(f["down_mbps"], f["up_mbps"])
            if rate:
                limits["rate"] = rate
        # La 1re utilisation (début de la validité, ticket compté vendu) n'est plus notée ici : seulement
        # quand des données passent vraiment (account). Avant, un ticket accepté mais resté à 0 Ko était « brûlé ».
        return True, "", limits

    def _reserve(self, args):
        # args = (session_id, nas_zone, code, home, started, updated, mac)
        def job(db):
            # vérifié sous le même verrou que la confirmation du routeur : si elle est déjà passée,
            # la réservation est devenue inutile et ne doit pas bloquer le ticket
            with self._held_lock:
                if not any(h[0] == args[1] and h[2] == args[4] for h in self._held.get((args[3], args[2]), [])):
                    return
            db.execute("INSERT OR REPLACE INTO roaming_sessions (session_id, nas_zone, code, home_zone, started, updated, mac, active) "
                       "VALUES (?,?,?,?,?,?,?,1)", args)
        self._write(job)

    def _expire(self, home, code, now):
        self._write(lambda db: db.execute(
            "UPDATE tickets SET expired_at = ? WHERE zone_id = ? AND code = ? AND expired_at IS NULL", (now, home, code)))

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
        with self._held_lock:  # le routeur a confirmé : la base prend le relais de la réservation en mémoire
            self._held.pop((t["home"], code), None)
        mac = _mac(pkt.text(R.CALLING_STATION_ID))
        nas, home = zone["id"], t["home"]

        def job(db):
            db.execute("DELETE FROM roaming_sessions WHERE session_id LIKE 'pending-%' AND nas_zone = ? AND code = ?",
                       (nas, code))
            # Après un redémarrage, le routeur recommence ses numéros de session : un numéro déjà vu peut
            # appartenir à un AUTRE ticket. L'ancienne ligne est renommée (son historique reste compté pour
            # son ticket) au lieu de recevoir la consommation du nouveau ticket.
            db.execute("UPDATE roaming_sessions SET session_id = session_id || '-old-' || started, active = 0 "
                       "WHERE session_id = ? AND nas_zone = ? AND code != ?", (sid, nas, code))
            db.execute("INSERT OR IGNORE INTO roaming_sessions (session_id, nas_zone, code, home_zone, started, updated, mac) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nas, code, home, now, now, mac))
            db.execute("UPDATE roaming_sessions SET updated = ?, bytes = MAX(bytes, ?), seconds = MAX(seconds, ?), active = ? "
                       "WHERE session_id = ? AND nas_zone = ?", (now, total, secs, 0 if status == R.ACCT_STOP else 1, sid, nas))
            db.execute("UPDATE tickets SET bytes_used = (SELECT COALESCE(SUM(bytes), 0) FROM roaming_sessions WHERE home_zone = ? AND code = ?), "
                       "uptime_used = (SELECT COALESCE(SUM(seconds), 0) FROM roaming_sessions WHERE home_zone = ? AND code = ?), "
                       "first_used = CASE WHEN ? > 0 THEN COALESCE(first_used, ?) ELSE first_used END "
                       "WHERE zone_id = ? AND code = ?",
                       (home, code, home, code, total, now, home, code))
        self._write(job)  # réponse au routeur tout de suite ; consommation enregistrée juste après
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
        """Ticket fini : refusé partout, et coupé là où il est encore connecté.
        Une seule fois (puis au plus toutes les 5 minutes s'il reste connecté) : avant, CHAQUE paquet de
        consommation d'un ticket fini relançait des connexions vers TOUS les routeurs du réseau, des centaines
        par minute, et le programme Roaming n'avait plus le temps de répondre aux clients."""
        now = now or int(time.time())
        self._expire(home, code, now)
        if not self.kicker:
            return
        key = (gid, code)
        with self._kick_lock:
            if now - self._ended.get(key, 0) < 300:
                return
            if len(self._ended) > 20000:
                self._ended.clear()
            self._ended[key] = now
        self._kick_all(gid, code)

    def _kick_all(self, gid, code):
        """Coupe le ticket sur les routeurs où il a une session en cours (sinon : tout le réseau, une fois)."""
        rows, _ = self._q("SELECT nas_zone, mac, active, updated FROM roaming_sessions WHERE code = ?", (code,))
        members = set(self.member_zones(gid))
        now = int(time.time())
        live = {z for z, m, a, u in rows if a and u >= now - ACTIVE_S and z in members}
        names = [code] + sorted({m for z, m, a, u in rows if m})
        self._kick_zones(live or members, names)

    def _kick_zones(self, zones, names):
        """Mis en file : un seul fil fait les coupures, une connexion par routeur pour tous les noms."""
        for zid in zones:
            self._kicks.put((zid, tuple(names)))

    def _kicker_loop(self):
        while True:
            zid, names = self._kicks.get()
            now = time.time()
            with self._kick_lock:  # déjà coupé il y a moins de 2 minutes : inutile de recommencer
                names = [n for n in names if now - self._kicked_at.get((zid, n), 0) > 120]
                for n in names:
                    self._kicked_at[(zid, n)] = now
                if len(self._kicked_at) > 50000:
                    self._kicked_at.clear()
            if not names:
                continue
            try:
                if self.kicker_many:
                    self.kicker_many(zid, names)  # une seule connexion au routeur
                else:
                    for name in names:  # la session porte le code, ou l'adresse MAC si le téléphone est entré tout seul
                        self.kicker(zid, name)
            except Exception as e:  # routeur injoignable : la limite envoyée au routeur coupe quand même
                self.log(f"ADER Roaming : coupure sur {zid} impossible ({e})")

    def code_for_mac(self, gid, mac):
        """Le ticket Roaming en cours de ce téléphone sur ce réseau (le plus récent), s'il y en a un."""
        zones = self.member_zones(gid)
        if not zones or not mac:
            return None
        # par l'index de la MAC (avant : toute la table relue à chaque téléphone qui se connecte au Wi-Fi)
        rows, _ = self._q("SELECT code, nas_zone FROM roaming_sessions WHERE mac = ? AND code NOT LIKE '%:%' "
                          "ORDER BY updated DESC LIMIT 20", (_mac(mac),))
        zs = set(zones)
        return next((c for c, z in rows if z in zs), None)

    def prune_sessions(self, now=None):
        """Détail des connexions d'un ticket fini depuis plus de 7 jours : effacé (la table ne grossit plus).
        Le ticket, sa vente et sa consommation totale restent : ils sont déjà enregistrés sur le ticket."""
        now = now or int(time.time())
        with self.store.lock:
            self.store.db.execute(
                "DELETE FROM roaming_sessions WHERE active = 0 AND updated < ? AND EXISTS (SELECT 1 FROM tickets t "
                "WHERE t.zone_id = roaming_sessions.home_zone AND t.code = roaming_sessions.code "
                "AND t.expired_at IS NOT NULL AND t.expired_at < ?)",
                (now - 7 * 86400, now - 7 * 86400))
            self.store.db.commit()
        self.revive_unused(now)

    def revive_unused(self, now=None):
        """Tickets Roaming « brûlés » : marqués utilisés (ou finis) sans avoir jamais consommé (0 Ko, 0 min),
        à cause de l'ancienne règle ou d'une surcharge. Ils redeviennent neufs : validité repartie à zéro."""
        now = now or int(time.time())
        with self.store.lock:
            cur = self.store.db.execute(
                "UPDATE tickets SET first_used = NULL, expired_at = NULL WHERE first_used IS NOT NULL "
                "AND COALESCE(bytes_used, 0) = 0 AND COALESCE(uptime_used, 0) = 0 AND removed = 0 AND first_used < ? "
                "AND batch_id IN (SELECT id FROM batches WHERE roaming_id IS NOT NULL) "
                "AND NOT EXISTS (SELECT 1 FROM roaming_sessions s WHERE s.home_zone = tickets.zone_id "
                "AND s.code = tickets.code AND s.bytes > 0)", (now - 600,))
            self.store.db.commit()
        if cur.rowcount:
            self.log(f"ADER Roaming : {cur.rowcount} ticket(s) jamais consommés remis à neuf")
        return cur.rowcount

    def sweep(self, now=None):
        """Toutes les minutes : les tickets dont la validité est passée sont fermés sur tout le réseau."""
        now = now or int(time.time())
        rows, _ = self.tickets._q(
            "SELECT b.roaming_id, t.code, t.zone_id FROM tickets t JOIN batches b ON b.id = t.batch_id "
            "WHERE b.roaming_id IS NOT NULL AND t.expired_at IS NULL AND t.removed = 0 AND t.first_used IS NOT NULL "
            "AND b.validity_s > 0 AND t.first_used + b.validity_s <= ?", (now,))
        for gid, code, home in rows:
            self.end_ticket(gid, code, home, now)

    def _note(self, zone, code, result, msg):
        """Journal des dernières demandes (bouton « Vérifier les routeurs ») : pour comprendre un refus."""
        e = [int(time.time()), zone["id"], zone["name"], str(code)[:24], result, msg, None]
        self.journal.append(e)
        self._tl.entry = e  # le temps de réponse est ajouté une fois la réponse envoyée

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
            shared = self.shared.get(zone["id"])
            if MAC_RE.match(code):
                # connexion automatique : ce téléphone a-t-il un ticket Roaming en cours sur ce réseau ?
                mac = code.upper().replace("-", ":")
                found = self.code_for_mac(g["id"], mac)
                if not found and shared:
                    return None  # pas à nous : le routeur demandera à l'autre système
                if not found or not any(R.password_ok(pkt, secret, pw) for pw in (code, mac, "")):
                    return R.reply(pkt, R.ACCESS_REJECT, secret, R.attr(R.REPLY_MESSAGE, "Entrez votre code"))
                code = found
                ok, msg, lim = self.authorize(g["id"], code, zone_id=zone["id"], mac=mac)
            elif not R.password_ok(pkt, secret, code):
                ok, msg, lim = False, "Code ou mot de passe incorrect", {}
            else:
                ok, msg, lim = self.authorize(g["id"], code, zone_id=zone["id"], mac=mac)
            if not ok and shared and not self._ticket(g["id"], code):
                self._note(zone, code, "autre", "Code inconnu d'ADER LINK : laissé à l'autre système")
                return None  # code inconnu d'ADER LINK : on se tait, le routeur demande à l'autre système
            if not ok:
                self._note(zone, code, "refus", msg)
                return R.reply(pkt, R.ACCESS_REJECT, secret, R.attr(R.REPLY_MESSAGE, msg))
            self._note(zone, code, "ok", "Accepté")
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
            n = 0
            while True:
                time.sleep(60)
                n += 1
                if n % 360 == 1:  # toutes les 6 heures
                    try:
                        self.prune_sessions()
                    except Exception as e:
                        self.log(f"ADER Roaming : nettoyage des sessions : {e!r}")
                try:
                    self.sweep()
                except Exception as e:
                    self.log(f"ADER Roaming : vérification des tickets : {e!r}")
        threading.Thread(target=loop, daemon=True, name="roaming-sweep").start()

    MAX_WAIT = {"auth": 6, "acct": 15}  # au-delà, le routeur a déjà abandonné (ou renverra) : inutile de répondre

    def _loop(self, sock, kind):
        # plusieurs paquets traités en même temps : une demande de connexion n'attend jamais derrière les autres
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix=f"radius-{kind}")
        # connexions automatiques par adresse MAC (très nombreuses : chaque téléphone qui capte le Wi-Fi) :
        # dans leur propre file, pour qu'un code tapé par un client ne passe jamais derrière elles
        pool_mac = ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"radius-{kind}-mac") if kind == "auth" else pool
        st = self.flow.setdefault(kind, {"pending": 0, "done_at": time.time(), "dropped": 0, "dup": 0, "lag": 0.0})
        waiting = set()  # demandes en file (adresse, n°, signature) : une copie renvoyée par le routeur est ignorée
        lock = threading.Lock()

        def work(data, addr, t0, key):
            try:
                age = time.time() - t0
                if age > self.MAX_WAIT[kind]:
                    st["dropped"] += 1  # trop vieille : le routeur ne l'attend plus, on passe aux demandes fraîches
                    return
                self._tl.entry = None
                try:
                    out = self.handle(data, addr[0], kind)
                    if out:
                        sock.sendto(out, addr)
                except Exception as e:  # un paquet bizarre ne doit jamais arrêter le serveur
                    self.log(f"ADER Roaming : erreur {e!r}")
                took = time.time() - t0  # depuis l'arrivée du paquet (attente comprise)
                st["lag"] = round(took, 2)
                if self._tl.entry is not None:
                    self._tl.entry[6] = round(took, 2)
            finally:
                st["done_at"] = time.time()
                with lock:
                    st["pending"] -= 1
                    waiting.discard(key)
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                return
            key = (addr, data[1:2], data[4:20])
            with lock:
                if key in waiting:
                    st["dup"] += 1
                    continue
                waiting.add(key)
                st["pending"] += 1
            (pool_mac if kind == "auth" and _is_mac_login(data) else pool).submit(work, data, addr, time.time(), key)

    def stuck(self):
        """Des demandes attendent mais plus rien n'est traité depuis 60 s : le programme est coincé."""
        now = time.time()
        return any(f["pending"] > 0 and now - f["done_at"] > 60 for f in self.flow.values())

    def _write(self, job):
        """Écriture job(db) faite à côté : la réponse au routeur n'attend jamais la base.
        Dans les tests (async_writes = False) : tout de suite, comme avant."""
        if not self.async_writes:
            with self.store.lock:
                job(self.store.db)
                self.store.db.commit()
            return
        if self._writes.qsize() > 20000:
            self.write_lost += 1  # afflux énorme : on protège la mémoire
            return
        self._writes.put(job)

    def _writer(self):
        """Un seul fil écrit, jusqu'à 200 écritures en une seule fois : quand la base est occupée par un autre
        programme, un seul moment libre suffit pour tout enregistrer (avant : une attente par écriture,
        1 600 tâches bloquées à la porte de la base, réponses aux routeurs en retard de 3 minutes)."""
        import queue as _queue
        import sqlite3
        while True:
            batch = [self._writes.get()]
            while len(batch) < 200:
                try:
                    batch.append(self._writes.get_nowait())
                except _queue.Empty:
                    break
            for attempt in range(6):
                try:
                    with self.store.lock:
                        for job in batch:
                            try:
                                job(self.store.db)
                            except sqlite3.OperationalError:
                                raise
                            except Exception as e:  # une écriture bizarre ne doit pas faire perdre les autres
                                self.log(f"ADER Roaming : écriture non faite ({e!r})")
                        self.store.db.commit()
                    break
                except sqlite3.OperationalError as e:  # base occupée : on recommence le lot un peu plus tard
                    try:
                        self.store.db.rollback()
                    except sqlite3.Error:
                        pass
                    time.sleep(1 + attempt)
            else:
                self.write_lost += len(batch)
                self.log(f"ADER Roaming : {len(batch)} écritures perdues (base occupée trop longtemps)")


def _is_mac_login(data):
    """La demande porte-t-elle une adresse MAC comme nom (connexion automatique) ? Lecture rapide du paquet."""
    try:
        pkt = R.Packet.parse(data)
        return bool(MAC_RE.match(pkt.text(R.USER_NAME) or ""))
    except Exception:
        return False


def _mac(m):
    """Adresse MAC écrite toujours pareil (majuscules, deux-points) : la recherche passe par l'index."""
    return str(m).upper().replace("-", ":") if m else m


def _ascii(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode()
    return "".join(c for c in s if c.isalnum() or c in " -_")[:40]
