"""Module Tickets : lots de comptes hotspot MikroTik, suivi et validité.

- Création d'un lot : génération de codes, ajout dans /ip/hotspot/user du routeur.
- Suivi : la collecte signale les tickets vus connectés (1re utilisation).
- Validité après 1re connexion : à l'échéance, le ticket est désactivé sur le
  routeur et la session en cours est coupée.
"""
import random
import secrets
import time

from .routeros import RouterOS, RouterOSError

MAX_PER_BATCH = 1000


def router_duration(seconds):
    """Secondes -> format RouterOS (ex. 1d2h30m)."""
    seconds = int(seconds)
    if seconds <= 0:
        return ""
    parts = []
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts)


def comment_for(batch_id):
    return f"ADERLINK-{batch_id}"


def open_router(zone):
    r = zone["router"]
    return RouterOS(r["host"], r["username"], r["password"],
                    port=r.get("port", 8729 if r.get("ssl") else 8728),
                    use_ssl=r.get("ssl", False))


class TicketError(Exception):
    pass


class Tickets:
    def __init__(self, config, store, demo=False):
        self.config = config
        self.store = store
        self.demo = demo
        self.roaming = None  # ADER Roaming, branché au démarrage
        with store.lock:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id TEXT, created_at INTEGER, count INTEGER,
                    profile TEXT, price INTEGER, uptime_s INTEGER,
                    data_mb INTEGER, validity_s INTEGER, note TEXT,
                    removed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tickets (
                    code TEXT, zone_id TEXT, batch_id INTEGER,
                    first_used INTEGER, expired_at INTEGER, removed INTEGER DEFAULT 0,
                    PRIMARY KEY (zone_id, code));
                CREATE INDEX IF NOT EXISTS idx_tickets_batch ON tickets(batch_id);
                CREATE TABLE IF NOT EXISTS forfaits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id TEXT, name TEXT, router_profile TEXT, price INTEGER,
                    uptime_s INTEGER, data_mb INTEGER, validity_s INTEGER,
                    down_mbps REAL, up_mbps REAL, shared_users INTEGER,
                    created_at INTEGER, archived INTEGER DEFAULT 0);
                """
            )
            for table, col in (("batches", "forfait_id INTEGER"), ("batches", "pos_id INTEGER"),
                               ("forfaits", "color TEXT"), ("batches", "created_by TEXT"),
                               ("batches", "roaming_id INTEGER"), ("tickets", "bytes_used INTEGER DEFAULT 0"),
                               ("tickets", "uptime_used INTEGER DEFAULT 0")):  # base créée par une version précédente
                try:
                    store.db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                except Exception:
                    pass
            store.db.commit()

    # --- outils ---------------------------------------------------------------
    def zone(self, zone_id):
        z = next((z for z in self.config.get("zones", []) if z["id"] == zone_id), None)
        if not z:
            raise TicketError("Zone inconnue")
        return z

    def _q(self, sql, params=(), commit=False):
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            rows = cur.fetchall()
            if commit:
                self.store.db.commit()
            return rows, cur.lastrowid

    def profiles(self, zone_id):
        zone = self.zone(zone_id)
        if self.demo:
            return ["default", "1-heure", "journee", "semaine"]
        try:
            with open_router(zone) as api:
                return [p.get("name") for p in api.query("/ip/hotspot/user/profile/print")]
        except RouterOSError as e:
            raise TicketError(str(e)) from e

    # --- forfaits (profils) -------------------------------------------------------
    FORFAIT_COLS = ("id", "zone_id", "name", "router_profile", "price", "uptime_s", "data_mb",
                    "validity_s", "down_mbps", "up_mbps", "shared_users", "created_at", "archived", "color")
    COLORS = ("bleu", "orange", "vert", "rouge", "violet", "cyan", "rose", "marron", "nuit")

    def _forfait(self, row):
        f = dict(zip(self.FORFAIT_COLS, row))
        f["color"] = f.get("color") or "bleu"
        rows, _ = self._q("SELECT COUNT(*) FROM tickets t JOIN batches b ON b.id = t.batch_id "
                          "WHERE b.forfait_id = ? AND t.removed = 0 AND t.first_used IS NULL", (f["id"],))
        f["stock"] = rows[0][0]
        return f

    def list_forfaits(self, zone_id):
        self.zone(zone_id)
        rows, _ = self._q("SELECT * FROM forfaits WHERE zone_id = ? AND archived = 0 ORDER BY price, id", (zone_id,))
        return [self._forfait(r) for r in rows]

    def get_forfait(self, forfait_id):
        rows, _ = self._q("SELECT * FROM forfaits WHERE id = ?", (forfait_id,))
        return self._forfait(rows[0]) if rows else None

    def profile_specs(self, zone_id):
        """Profils à créer sur le routeur par le script d'installation."""
        return [{"profile": f["router_profile"], "shared": f["shared_users"] or 1,
                 "rate": self._rate(f["down_mbps"], f["up_mbps"]), "validity_s": f["validity_s"] or 0}
                for f in self.list_forfaits(zone_id)]

    @staticmethod
    def _rate(down, up):
        if not down and not up:
            return ""
        fmt = lambda v: (f"{int(v)}M" if float(v).is_integer() else f"{int(float(v) * 1000)}k") if v else "0"
        return f"{fmt(up)}/{fmt(down)}"  # MikroTik : réception (envoi du client) / émission (réception du client)

    def _forfait_values(self, data):
        name = " ".join(str(data.get("name") or "").split())[:40]
        if not name:
            raise TicketError("Donnez un nom au profil")
        try:
            v = {"name": name,
                 "price": max(0, int(data.get("price", 0))),
                 "uptime_s": max(0, int(data.get("uptime_s", 0))),
                 "data_mb": max(0, int(data.get("data_mb", 0))),
                 "validity_s": max(0, int(data.get("validity_s", 0))),
                 "down_mbps": max(0.0, float(data.get("down_mbps", 0) or 0)),
                 "up_mbps": max(0.0, float(data.get("up_mbps", 0) or 0)),
                 "shared_users": int(data.get("shared_users", 1) or 1)}
        except (TypeError, ValueError):
            raise TicketError("Valeurs invalides")
        if not 1 <= v["shared_users"] <= 10:
            raise TicketError("Appareils simultanés : entre 1 et 10")
        v["color"] = data.get("color") if data.get("color") in self.COLORS else "bleu"
        return v

    def _push_profile(self, zone, router_profile, v):
        """Crée ou met à jour le profil hotspot sur le routeur (vitesse, appareils, validité)."""
        if self.demo:
            return
        from .mikrotik import on_login
        try:
            with open_router(zone) as api:
                args = [f"=shared-users={v['shared_users']}", f"=rate-limit={self._rate(v['down_mbps'], v['up_mbps'])}",
                        f"=on-login={on_login(v['validity_s'])}"]
                found = api.query("/ip/hotspot/user/profile/print", f"?name={router_profile}")
                if found:
                    api.query("/ip/hotspot/user/profile/set", f"=.id={found[0]['.id']}", *args)
                else:
                    api.query("/ip/hotspot/user/profile/add", f"=name={router_profile}", *args)
        except RouterOSError as e:
            msg = str(e)
            if "permission" in msg.lower() or "not enough" in msg.lower():
                msg += " — le compte « aderlink » du routeur doit avoir le droit write"
            raise TicketError(f"Le routeur a refusé : {msg}") from e

    def _name_taken(self, zone_id, name, exclude=None):
        rows, _ = self._q("SELECT id FROM forfaits WHERE zone_id = ? AND archived = 0 AND lower(name) = lower(?) AND id != ?",
                          (zone_id, name, exclude or 0))
        return bool(rows)

    def create_forfait(self, data):
        zone = self.zone(str(data.get("zone_id", "")))
        v = self._forfait_values(data)
        if self._name_taken(zone["id"], v["name"]):
            raise TicketError("Un profil porte déjà ce nom sur ce routeur")
        slug = "".join(c if c.isalnum() else "-" for c in v["name"].lower())
        slug = "-".join(x for x in slug.split("-") if x)[:28] or "profil"
        router_profile = f"AL-{slug}"
        self._push_profile(zone, router_profile, v)
        _, fid = self._q(
            "INSERT INTO forfaits (zone_id, name, router_profile, price, uptime_s, data_mb, validity_s, "
            "down_mbps, up_mbps, shared_users, created_at, color) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (zone["id"], v["name"], router_profile, v["price"], v["uptime_s"], v["data_mb"], v["validity_s"],
             v["down_mbps"], v["up_mbps"], v["shared_users"], int(time.time()), v["color"]), commit=True)
        return self.get_forfait(fid)

    def update_forfait(self, forfait_id, data):
        """Modifie un profil. Vitesse, appareils et validité changent aussi pour les tickets déjà créés
        (ils sont portés par le profil du routeur) ; prix, données et durée valent pour les prochains lots."""
        f = self.get_forfait(forfait_id)
        if not f or f["archived"]:
            raise TicketError("Profil introuvable")
        zone = self.zone(f["zone_id"])
        v = self._forfait_values(data)
        if self._name_taken(zone["id"], v["name"], forfait_id):
            raise TicketError("Un profil porte déjà ce nom sur ce routeur")
        self._push_profile(zone, f["router_profile"], v)  # le nom sur le routeur ne change pas
        self._q("UPDATE forfaits SET name=?, price=?, uptime_s=?, data_mb=?, validity_s=?, down_mbps=?, up_mbps=?, "
                "shared_users=?, color=? WHERE id = ?",
                (v["name"], v["price"], v["uptime_s"], v["data_mb"], v["validity_s"], v["down_mbps"], v["up_mbps"],
                 v["shared_users"], v["color"], forfait_id), commit=True)
        return self.get_forfait(forfait_id)

    def archive_forfait(self, forfait_id):
        if not self.get_forfait(forfait_id):
            raise TicketError("Profil introuvable")
        # Le profil reste sur le routeur : les tickets déjà vendus continuent de fonctionner.
        self._q("UPDATE forfaits SET archived = 1 WHERE id = ?", (forfait_id,), commit=True)
        return {"ok": True}

    # --- création --------------------------------------------------------------
    def create_batch(self, data, created_by=None):
        forfait = None
        if data.get("forfait_id"):
            try:
                forfait = self.get_forfait(int(data["forfait_id"]))
            except (TypeError, ValueError):
                forfait = None
            if not forfait or forfait["archived"]:
                raise TicketError("Profil introuvable")
            data = dict(data, zone_id=forfait["zone_id"], profile=forfait["router_profile"],
                        price=forfait["price"], uptime_s=forfait["uptime_s"], data_mb=forfait["data_mb"],
                        validity_s=forfait["validity_s"], note=data.get("note") or forfait["name"])
        zone = self.zone(str(data.get("zone_id", "")))
        try:
            pos_id = int(data["pos_id"]) if data.get("pos_id") else None
        except (TypeError, ValueError):
            pos_id = None
        try:
            count = int(data.get("count", 0))
            code_len = int(data.get("code_length") or zone.get("code_length", 8))
            price = max(0, int(data.get("price", 0)))
            uptime_s = max(0, int(data.get("uptime_s", 0)))
            data_mb = max(0, int(data.get("data_mb", 0)))
            validity_s = max(0, int(data.get("validity_s", 0)))
        except (TypeError, ValueError):
            raise TicketError("Valeurs invalides")
        if not 1 <= count <= MAX_PER_BATCH:
            raise TicketError(f"Quantité entre 1 et {MAX_PER_BATCH}")
        if not 4 <= code_len <= 12:
            raise TicketError("Longueur du code entre 4 et 12 chiffres")
        same_password = data.get("password_mode", "same") != "empty"
        profile = str(data.get("profile") or "default")[:64]
        note = str(data.get("note") or "")[:80]
        group = None
        if data.get("roaming"):
            group = self.roaming.group_of_zone(zone["id"]) if self.roaming else None
            if not group:
                raise TicketError("Ce routeur ne fait partie d'aucun réseau ADER Roaming")

        _, batch_id = self._q(
            "INSERT INTO batches (zone_id, created_at, count, profile, price, uptime_s, data_mb, validity_s, note, "
            "forfait_id, pos_id, created_by, roaming_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (zone["id"], int(time.time()), 0, profile, price, uptime_s, data_mb, validity_s, note,
             forfait["id"] if forfait else None, pos_id, created_by, group["id"] if group else None), commit=True)

        zone_ids = [zone["id"]] + ([z["id"] for z in group["zones"]] if group else [])
        rows, _ = self._q("SELECT code FROM tickets WHERE zone_id IN (%s)" % ",".join("?" * len(zone_ids)), zone_ids)
        taken = {r[0] for r in rows}
        added, error = [], None

        def new_code():
            while True:
                c = "".join(secrets.choice("0123456789") for _ in range(code_len))
                if c not in taken and c[0] != "0":
                    taken.add(c)
                    return c

        args_common = [f"=profile={profile}", f"=comment={comment_for(batch_id)}"]
        if uptime_s:
            args_common.append(f"=limit-uptime={router_duration(uptime_s)}")
        if data_mb:
            args_common.append(f"=limit-bytes-total={data_mb * 1024 * 1024}")

        try:
            if self.demo or group:  # Roaming : rien sur les routeurs, ADER LINK vérifie les codes
                added = [new_code() for _ in range(count)]
            else:
                with open_router(zone) as api:
                    # tout est envoyé d'un coup (rapide même par le tunnel), puis on refait les rares doublons
                    for _ in range(5):
                        need = count - len(added)
                        if need <= 0:
                            break
                        codes = [new_code() for _ in range(need)]
                        res = api.pipeline([("/ip/hotspot/user/add", f"=name={c}",
                                             f"=password={c if same_password else ''}", *args_common) for c in codes])
                        fatal = None
                        for c, err in zip(codes, res):
                            if err is None:
                                added.append(c)
                            elif "already" not in err.lower():
                                fatal = fatal or err
                        if fatal:
                            raise RouterOSError(fatal)
        except RouterOSError as e:
            error = str(e)

        with self.store.lock:
            self.store.db.executemany(
                "INSERT INTO tickets (code, zone_id, batch_id) VALUES (?,?,?)",
                [(c, zone["id"], batch_id) for c in added])
            self.store.db.execute("UPDATE batches SET count = ? WHERE id = ?", (len(added), batch_id))
            self.store.db.commit()

        if error and not added:
            self._q("DELETE FROM batches WHERE id = ?", (batch_id,), commit=True)
            raise TicketError(f"Le routeur a refusé : {error}")
        return {"id": batch_id, "created": len(added), "requested": count, "error": error,
                "roaming": bool(group)}

    # --- lecture ------------------------------------------------------------------
    def _batch_dict(self, row):
        (bid, zone_id, created, count, profile, price, uptime_s, data_mb, validity_s, note, removed) = row[:11]
        zone = next((z for z in self.config.get("zones", []) if z["id"] == zone_id), {})
        stats, _ = self._q(
            "SELECT SUM(first_used IS NOT NULL), SUM(expired_at IS NOT NULL), SUM(removed) "
            "FROM tickets WHERE batch_id = ?", (bid,))
        used, expired, rem = stats[0]
        return {
            "id": bid, "zone_id": zone_id, "zone_name": zone.get("name", zone_id),
            "ssid": zone.get("ssid") or zone.get("name", ""), "currency": zone.get("currency") or "FCFA",
            "created_at": created, "count": count, "profile": profile, "price": price,
            "uptime_s": uptime_s, "data_mb": data_mb, "validity_s": validity_s, "note": note,
            "used": used or 0, "expired": expired or 0, "removed": rem or 0,
            "forfait_id": row[11] if len(row) > 11 else None,
            "pos_id": row[12] if len(row) > 12 else None,
            "pos_name": self._pos_name(row[12]) if len(row) > 12 and row[12] else None,
            "color": self._forfait_color(row[11]) if len(row) > 11 and row[11] else "bleu",
            "created_by": row[13] if len(row) > 13 else None,
            "roaming_id": row[14] if len(row) > 14 else None,
            "roaming_name": self._roaming_name(row[14]) if len(row) > 14 and row[14] else None,
        }

    def _roaming_name(self, gid):
        rows, _ = self._q("SELECT name FROM roaming_groups WHERE id = ?", (gid,))
        return rows[0][0] if rows else "ADER Roaming"

    def _forfait_color(self, forfait_id):
        rows, _ = self._q("SELECT color FROM forfaits WHERE id = ?", (forfait_id,))
        return (rows[0][0] if rows else None) or "bleu"

    def _pos_name(self, pos_id):
        try:
            rows, _ = self._q("SELECT name FROM points_vente WHERE id = ?", (pos_id,))
            return rows[0][0] if rows else None
        except Exception:
            return None

    def list_batches(self, limit=100, zone_id=None):
        if zone_id:
            rows, _ = self._q("SELECT * FROM batches WHERE zone_id = ? ORDER BY id DESC LIMIT ?", (zone_id, limit))
        else:
            rows, _ = self._q("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,))
        return [self._batch_dict(r) for r in rows]

    def get_batch(self, batch_id):
        rows, _ = self._q("SELECT * FROM batches WHERE id = ?", (batch_id,))
        if not rows:
            return None
        b = self._batch_dict(rows[0])
        trows, _ = self._q(
            "SELECT code, first_used, expired_at, removed FROM tickets WHERE batch_id = ? ORDER BY rowid",
            (batch_id,))
        b["tickets"] = [{"code": c, "first_used": f, "expired_at": e, "removed": bool(r)} for c, f, e, r in trows]
        return b

    def find(self, code):
        rows, _ = self._q(
            "SELECT batch_id, zone_id, first_used, expired_at, removed FROM tickets WHERE code = ?", (code,))
        out = []
        for batch_id, zone_id, first, expired, removed in rows:
            b = self.get_batch(batch_id)
            ends = first + b["validity_s"] if first and b["validity_s"] else None
            out.append({"code": code, "batch": {k: b[k] for k in b if k != "tickets"},
                        "first_used": first, "expired_at": expired, "removed": bool(removed),
                        "ends_at": ends})
        return out

    # --- suppression des tickets non utilisés ---------------------------------------------
    def remove_unused(self, batch_id):
        b = self.get_batch(batch_id)
        if not b:
            raise TicketError("Lot introuvable")
        unused = [t["code"] for t in b["tickets"] if not t["first_used"] and not t["removed"]]
        if not unused:
            return {"removed": 0}
        removed = []
        if self.demo or b.get("roaming_id"):
            removed = unused
        else:
            zone = self.zone(b["zone_id"])
            try:
                with open_router(zone) as api:
                    users = api.query("/ip/hotspot/user/print", f"?comment={comment_for(batch_id)}")
                    for u in users:
                        name = u.get("name")
                        if name not in unused:
                            continue
                        if u.get("uptime") not in (None, "", "0s"):
                            # utilisé entre deux relevés : on le garde
                            self._mark_used(b["zone_id"], name)
                            continue
                        api.query("/ip/hotspot/user/remove", f"=.id={u['.id']}")
                        removed.append(name)
            except RouterOSError as e:
                raise TicketError(str(e)) from e
        with self.store.lock:
            self.store.db.executemany(
                "UPDATE tickets SET removed = 1 WHERE zone_id = ? AND code = ?",
                [(b["zone_id"], c) for c in removed])
            self.store.db.commit()
        return {"removed": len(removed)}

    # --- suivi pendant la collecte ----------------------------------------------------------
    def _mark_used(self, zone_id, code, ts=None):
        self._q("UPDATE tickets SET first_used = ? WHERE zone_id = ? AND code = ? AND first_used IS NULL",
                (ts or int(time.time()), zone_id, code), commit=True)

    def on_poll(self, zone, api, sessions):
        """Appelé à chaque relevé d'une zone en ligne (api = connexion ouverte, ou None en démo)."""
        now = int(time.time())
        zid = zone["id"]
        users = [s.get("user") for s in sessions if s.get("user")]
        if self.demo and api is None:
            # démo : quelques tickets « vendus » au fil des relevés
            rows, _ = self._q("SELECT code FROM tickets WHERE zone_id = ? AND first_used IS NULL "
                              "AND removed = 0 ORDER BY RANDOM() LIMIT 1", (zid,)) if random.random() < 0.25 else ([], None)
            users = [r[0] for r in rows]
        if users:
            with self.store.lock:
                self.store.db.executemany(
                    "UPDATE tickets SET first_used = ? WHERE zone_id = ? AND code = ? AND first_used IS NULL",
                    [(now, zid, u) for u in users])
                self.store.db.commit()
        due, _ = self._q(
            "SELECT t.code FROM tickets t JOIN batches b ON b.id = t.batch_id "
            "WHERE t.zone_id = ? AND b.validity_s > 0 AND t.first_used IS NOT NULL "
            "AND t.expired_at IS NULL AND t.removed = 0 AND t.first_used + b.validity_s <= ?",
            (zid, now))
        for (code,) in due:
            if api is not None:
                try:
                    for u in api.query("/ip/hotspot/user/print", f"?name={code}"):
                        api.query("/ip/hotspot/user/set", f"=.id={u['.id']}", "=disabled=yes")
                    for s in api.query("/ip/hotspot/active/print", f"?user={code}"):
                        api.query("/ip/hotspot/active/remove", f"=.id={s['.id']}")
                except RouterOSError as e:
                    self.store.add_event(now, zid, "offline", f"Ticket {code} non désactivé : {e}")
                    continue
            self._q("UPDATE tickets SET expired_at = ? WHERE zone_id = ? AND code = ?", (now, zid, code), commit=True)

    # --- ventes ---------------------------------------------------------------------------
    @staticmethod
    def _scope(zone_ids, created_by):
        sql, params = "", []
        if zone_ids is not None:
            sql += " AND b.zone_id IN (%s)" % (",".join("?" * len(zone_ids)) or "NULL")
            params += list(zone_ids)
        if created_by:
            sql += " AND b.created_by = ?"
            params.append(created_by)
        return sql, params

    def sales_history(self, days, utc_offset_s=3600, zone_ids=None, created_by=None):
        """Ventes par jour (heure locale) sur `days` jours + totaux depuis le début."""
        now = int(time.time())
        today = (now + utc_offset_s) // 86400
        first = today - days + 1
        sc, sp = self._scope(zone_ids, created_by)
        rows, _ = self._q(
            "SELECT (t.first_used + ?) / 86400 AS d, COUNT(*), SUM(b.price) FROM tickets t "
            "JOIN batches b ON b.id = t.batch_id WHERE t.first_used IS NOT NULL "
            "AND (t.first_used + ?) / 86400 >= ?" + sc + " GROUP BY d", [utc_offset_s, utc_offset_s, first] + sp)
        by_day = {d: (n, a or 0) for d, n, a in rows}
        series = [{"day": d * 86400 - utc_offset_s, "tickets": by_day.get(d, (0, 0))[0],
                   "amount": by_day.get(d, (0, 0))[1]} for d in range(first, today + 1)]
        tot, _ = self._q("SELECT COUNT(*), SUM(b.price) FROM tickets t JOIN batches b ON b.id = t.batch_id "
                         "WHERE t.first_used IS NOT NULL" + sc, sp)
        gen, _ = self._q("SELECT COUNT(*) FROM tickets t JOIN batches b ON b.id = t.batch_id WHERE 1=1" + sc, sp)
        return {"series": series,
                "period": {"tickets": sum(x["tickets"] for x in series), "amount": sum(x["amount"] for x in series)},
                "all_time": {"tickets": tot[0][0], "amount": tot[0][1] or 0, "generated": gen[0][0]}}

    def report(self, start, end, utc_offset_s=3600, zone_ids=None, created_by=None, names=None):
        """Rapport des ventes entre start et end (horodatages, end exclu).

        Un ticket est « vendu » au moment de sa première connexion, au prix de son forfait.
        """
        sc, sp = self._scope(zone_ids, created_by)
        base = ("FROM tickets t JOIN batches b ON b.id = t.batch_id "
                "LEFT JOIN forfaits f ON f.id = b.forfait_id LEFT JOIN points_vente pv ON pv.id = b.pos_id "
                "WHERE t.first_used >= ? AND t.first_used < ?" + sc)
        params = [start, end] + sp

        def group(key):
            rows, _ = self._q(f"SELECT {key} AS k, COUNT(*), COALESCE(SUM(b.price), 0) {base} GROUP BY k "
                              "ORDER BY 3 DESC, 2 DESC", params)
            return rows

        zones = {z["id"]: z["name"] for z in self.config.get("zones", [])}
        names = names or {}
        tot, _ = self._q(f"SELECT COUNT(*), COALESCE(SUM(b.price), 0) {base}", params)
        prev, _ = self._q(f"SELECT COUNT(*), COALESCE(SUM(b.price), 0) {base}", [2 * start - end, start] + sp)
        gen, _ = self._q("SELECT COUNT(*), COALESCE(SUM(b.price), 0) FROM tickets t JOIN batches b ON b.id = t.batch_id "
                         "WHERE b.created_at >= ? AND b.created_at < ?" + sc, params)
        stock, _ = self._q("SELECT COUNT(*) FROM tickets t JOIN batches b ON b.id = t.batch_id "
                           "WHERE t.first_used IS NULL AND t.removed = 0" + sc, sp)
        first_day, last_day = (start + utc_offset_s) // 86400, (end - 1 + utc_offset_s) // 86400
        days = {d: (n, a) for d, n, a in group(f"(t.first_used + {int(utc_offset_s)}) / 86400")}
        line = lambda label, n, a: {"label": label, "tickets": n, "amount": a}
        return {
            "start": start, "end": end,
            "totals": {"tickets": tot[0][0], "amount": tot[0][1], "generated": gen[0][0],
                       "generated_value": gen[0][1], "stock": stock[0][0]},
            "previous": {"tickets": prev[0][0], "amount": prev[0][1]},
            "by_day": [dict(line("", *days.get(d, (0, 0))), day=d * 86400 - utc_offset_s,
                            date=time.strftime("%Y-%m-%d", time.gmtime(d * 86400)))
                       for d in range(first_day, last_day + 1)],
            "by_zone": [line(zones.get(k, k), n, a) for k, n, a in group("b.zone_id")],
            "by_forfait": [line(k or "–", n, a) for k, n, a in group("COALESCE(f.name, b.profile)")],
            "by_pos": [line(k or "Sans point de vente", n, a) for k, n, a in group("pv.name")],
            "by_seller": [line(names.get(k, k) if k else "Administrateur", n, a) for k, n, a in group("b.created_by")],
        }

    def sales(self, since, zone_ids=None, created_by=None):
        sc, sp = self._scope(zone_ids, created_by)
        rows, _ = self._q(
            "SELECT b.zone_id, COUNT(*), SUM(b.price) FROM tickets t JOIN batches b ON b.id = t.batch_id "
            "WHERE t.first_used >= ?" + sc + " GROUP BY b.zone_id", [since] + sp)
        return [{"zone_id": z, "tickets": n, "amount": a or 0} for z, n, a in rows]
