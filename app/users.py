"""Comptes utilisateurs : administrateur, gérants et revendeurs.

- L'administrateur principal reste celui de config.json (créé par « setup »).
- Les autres comptes sont dans la base SQLite.
- Gérant : tickets, profils, lots, routeurs de ses zones (lecture), pas d'utilisateurs ni de paramètres.
- Revendeur : crée et imprime des tickets sur ses routeurs, avec SON point de vente ; ne voit que ses lots.
"""
import json
import re
import secrets
import time

ROLES = ("admin", "client", "gerant", "revendeur")
ROLE_LABEL = {"admin": "Administrateur", "client": "Gérant", "gerant": "Employé", "revendeur": "Client"}
# (noms affichés : « client » = gérant de ses propres routeurs ; « revendeur » = client qui vend des tickets)


class UserError(Exception):
    pass


class SubscriptionExpired(Exception):
    """Mot de passe correct, mais l'abonnement du compte est terminé."""


UNITS = {"heure": 3600, "jour": 86400, "mois": None, "an": None}  # mois et ans : calendrier réel


def add_months(ts, months, utc_offset_s=3600):
    """Ajoute des mois de calendrier (le 31 janvier + 1 mois = 28/29 février), en heure locale."""
    import calendar
    t = time.gmtime(ts + utc_offset_s)
    m = t.tm_mon - 1 + months
    year, month = t.tm_year + m // 12, m % 12 + 1
    day = min(t.tm_mday, calendar.monthrange(year, month)[1])
    return calendar.timegm((year, month, day, t.tm_hour, t.tm_min, t.tm_sec)) - utc_offset_s


class Principal:
    """L'utilisateur connecté et ce qu'il a le droit de voir ou faire."""

    def __init__(self, username, name, role, zones=None, user_id=None, commission=0, expires_at=None, max_routers=0):
        self.username = username
        self.name = name or username
        self.role = role
        self.zones = zones  # None = tous les routeurs
        self.user_id = user_id
        self.commission = commission
        self.expires_at = expires_at  # None = sans limite
        self.max_routers = max_routers  # clients : nombre de routeurs autorisés
        self.parent = None  # équipe d'un client : identifiant de ce client

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_reseller(self):
        return self.role == "revendeur"

    def can_zone(self, zone_id):
        return self.zones is None or zone_id in self.zones

    def can(self, action):
        rules = {
            "users": ("admin",), "team": ("admin", "client"), "settings": ("admin",), "routers_write": ("admin", "client"),
            "routers_read": ("admin", "client", "gerant"), "forfaits_write": ("admin", "client", "gerant"),
            "lots_manage": ("admin", "client", "gerant"), "dashboard_full": ("admin", "client", "gerant"),
        }
        return self.role in rules.get(action, ROLES)

    def public(self):
        return {"username": self.username, "name": self.name, "role": self.role,
                "role_label": ROLE_LABEL[self.role], "zones": self.zones, "commission": self.commission,
                "expires_at": self.expires_at, "now": int(time.time()), "max_routers": self.max_routers,
                "parent": self.parent}


class Users:
    def __init__(self, config, store, routers=None, hasher=None, verifier=None):
        self.config = config
        self.store = store
        self.routers = routers
        self.hash = hasher
        self.verify = verifier
        with store.lock:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, name TEXT, phone TEXT,
                    role TEXT, password_hash TEXT, zones TEXT, commission REAL DEFAULT 0,
                    active INTEGER DEFAULT 1, created_at INTEGER, last_login INTEGER);
                """
            )
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscription_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, at INTEGER, action TEXT,
                    label TEXT, old_expires INTEGER, new_expires INTEGER, note TEXT);
                CREATE INDEX IF NOT EXISTS idx_sub_user ON subscription_log(user_id);
                """
            )
            for sql in ("ALTER TABLE points_vente ADD COLUMN user_id INTEGER",
                        "ALTER TABLE users ADD COLUMN expires_at INTEGER",
                        "ALTER TABLE users ADD COLUMN max_routers INTEGER DEFAULT 0",
                        "ALTER TABLE users ADD COLUMN parent TEXT"):
                try:
                    store.db.execute(sql)
                except Exception:
                    pass
            store.db.commit()

    def _q(self, sql, params=(), commit=False):
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            rows = cur.fetchall()
            if commit:
                self.store.db.commit()
            return rows, cur.lastrowid

    COLS = ("id", "username", "name", "phone", "role", "password_hash", "zones", "commission",
            "active", "created_at", "last_login", "expires_at", "max_routers", "parent")

    def _row(self, row):
        u = dict(zip(self.COLS, row))
        u["zones"] = json.loads(u["zones"] or "[]")
        return u

    def _get(self, username):
        rows, _ = self._q(f"SELECT {', '.join(self.COLS)} FROM users WHERE lower(username) = lower(?)", (username,))
        return self._row(rows[0]) if rows else None

    def get(self, user_id):
        rows, _ = self._q(f"SELECT {', '.join(self.COLS)} FROM users WHERE id = ?", (user_id,))
        return self._row(rows[0]) if rows else None

    # --- connexion ---------------------------------------------------------------------
    def authenticate(self, username, password):
        admin = self.config["admin"]
        if username.lower() == admin["username"].lower():
            return admin["username"] if self.verify(password, admin["password_hash"]) else None
        u = self._get(username)
        if u and u["active"] and self.verify(password, u["password_hash"]):
            if self.blocked(u):
                raise SubscriptionExpired(u["username"])
            self._q("UPDATE users SET last_login = ? WHERE id = ?", (int(time.time()), u["id"]), commit=True)
            return u["username"]
        return None

    @staticmethod
    def expired(u):
        return bool(u.get("expires_at")) and time.time() >= u["expires_at"]

    def blocked(self, u):
        """Abonnement terminé — le sien, ou celui du client dont il fait partie de l'équipe."""
        if self.expired(u):
            return True
        if u.get("parent"):
            p = self._get(u["parent"])
            return not p or not p["active"] or self.expired(p)
        return False

    def is_expired(self, username):
        u = self._get(username) if username else None
        return bool(u and u["active"] and self.blocked(u))

    def principal(self, username):
        if not username:
            return None
        if username == self.config["admin"]["username"]:
            return Principal(username, "Administrateur", "admin")
        u = self._get(username)
        if not u or not u["active"] or self.blocked(u):
            return None  # compte supprimé, désactivé ou abonnement terminé : la session tombe
        if u["role"] == "client":  # un client voit les routeurs qu'il a lui-même ajoutés
            zones = [z["id"] for z in self.config.get("zones", []) if z.get("owner") == u["username"]]
        else:
            known = {z["id"] for z in self.config.get("zones", [])
                     if not u["parent"] or z.get("owner") == u["parent"]}  # équipe : routeurs du client seulement
            zones = [z for z in u["zones"] if z in known]
        expires = u["expires_at"]
        if u["parent"]:  # l'équipe suit l'abonnement du client
            expires = (self._get(u["parent"]) or {}).get("expires_at")
        p = Principal(u["username"], u["name"], u["role"], zones,
                      u["id"], u["commission"] or 0, expires, u["max_routers"] or 0)
        p.parent = u["parent"]
        return p

    # --- gestion -----------------------------------------------------------------------
    def _validate(self, data, current=None, parent=None):
        name = " ".join(str(data.get("name") or "").split())[:60]
        if not name:
            raise UserError("Indiquez le nom de la personne")
        username = str(data.get("username") or "").strip().lower()[:32]
        if not re.fullmatch(r"[a-z0-9._-]{3,32}", username):
            raise UserError("Identifiant : 3 à 32 caractères (lettres, chiffres, point, tiret)")
        if username == self.config["admin"]["username"].lower():
            raise UserError("Cet identifiant est réservé")
        other = self._get(username)
        if other and (not current or other["id"] != current["id"]):
            raise UserError("Cet identifiant est déjà utilisé")
        role = data.get("role")
        if role not in (("gerant", "revendeur") if parent else ("client", "gerant", "revendeur")):
            raise UserError("Choisissez le type de compte : gérant ou revendeur" if parent
                            else "Choisissez le type de compte : client, gérant ou revendeur")
        known = {z["id"] for z in self.config.get("zones", []) if not parent or z.get("owner") == parent}
        zones = [z for z in (data.get("zones") or []) if z in known] if role != "client" else []
        if role != "client" and not zones:
            raise UserError("Choisissez au moins un routeur")
        try:
            commission = float(data.get("commission") or 0)
        except (TypeError, ValueError):
            raise UserError("Commission invalide")
        if not 0 <= commission <= 100:
            raise UserError("Commission entre 0 et 100 %")
        phone = re.sub(r"[^0-9+ ]", "", str(data.get("phone") or ""))[:20].strip()
        return name, username, role, zones, commission if role == "revendeur" else 0, phone

    def _max_routers(self, data, default=0):
        try:
            n = int(str(data.get("max_routers", default) or 0).strip())
        except ValueError:
            raise UserError("Nombre de routeurs : un nombre entier")
        if not 0 <= n <= 1000:
            raise UserError("Nombre de routeurs entre 0 et 1000")
        return n

    def _password(self, data, required):
        pwd = str(data.get("password") or "")
        if not pwd and required:
            pwd = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
        if pwd and len(pwd) < 6:
            raise UserError("Mot de passe : 6 caractères minimum")
        return pwd

    def _sync_pv(self, user):
        """Un revendeur a son propre point de vente sur chacun de ses routeurs."""
        if not self.routers:
            return
        rows, _ = self._q("SELECT id, zone_id FROM points_vente WHERE user_id = ? AND archived = 0", (user["id"],))
        have = {z: pid for pid, z in rows}
        want = set(user["zones"]) if user["role"] == "revendeur" and user["active"] else set()
        for z in want - set(have):
            pv = self.routers.create_pv(z, {"name": user["name"]})
            self._q("UPDATE points_vente SET user_id = ? WHERE id = ?", (user["id"], pv["id"]), commit=True)
        for z in set(have) - want:
            self._q("UPDATE points_vente SET archived = 1 WHERE id = ?", (have[z],), commit=True)
        for z in want & set(have):
            self._q("UPDATE points_vente SET name = ? WHERE id = ?", (user["name"], have[z]), commit=True)

    def pv_for(self, principal, zone_id):
        if not principal.is_reseller:
            return None
        rows, _ = self._q("SELECT id FROM points_vente WHERE user_id = ? AND zone_id = ? AND archived = 0",
                          (principal.user_id, zone_id))
        return rows[0][0] if rows else None

    def create(self, data, parent=None):
        name, username, role, zones, commission, phone = self._validate(data, parent=parent)
        pwd = self._password(data, required=True)
        max_routers = self._max_routers(data, 1) if role == "client" else 0
        _, uid = self._q(
            "INSERT INTO users (username, name, phone, role, password_hash, zones, commission, created_at, max_routers, parent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (username, name, phone, role, self.hash(pwd), json.dumps(zones), commission, int(time.time()),
             max_routers, parent), commit=True)
        sub = data.get("subscription") or {}
        if not parent and (sub.get("unlimited") is False or sub.get("n")):
            self.subscription(uid, dict(sub, action="add"), note="Création du compte")
        user = self.get(uid)
        self._sync_pv(user)
        return dict(self.public(user), password=pwd)

    def update(self, user_id, data):
        user = self.get(user_id)
        if not user:
            raise UserError("Utilisateur introuvable")
        name, username, role, zones, commission, phone = self._validate(data, current=user, parent=user["parent"])
        max_routers = self._max_routers(data, user["max_routers"]) if role == "client" else 0
        if user["role"] == "client" and role != "client" and self.routers and self.routers.owned(user["username"]):
            raise UserError("Ce client a des routeurs : il doit rester client")
        self._q("UPDATE users SET username=?, name=?, phone=?, role=?, zones=?, commission=?, max_routers=? WHERE id=?",
                (username, name, phone, role, json.dumps(zones), commission, max_routers, user_id), commit=True)
        if username != user["username"]:  # son équipe suit son nouvel identifiant
            self._q("UPDATE users SET parent = ? WHERE parent = ?", (username, user["username"]), commit=True)
        if username != user["username"] and self.routers:  # les routeurs du client suivent son nouvel identifiant
            for z in self.routers.owned(user["username"]):
                z["owner"] = username
            self.routers._persist()
        user = self.get(user_id)
        self._sync_pv(user)
        return self.public(user)

    # --- abonnement -------------------------------------------------------------------
    def offset(self):
        return int(float(self.config.get("utc_offset_hours", 1)) * 3600)

    def extend(self, ts, n, unit):
        if unit == "mois":
            return add_months(ts, n, self.offset())
        if unit == "an":
            return add_months(ts, 12 * n, self.offset())
        return ts + n * UNITS[unit]

    @staticmethod
    def label(n, unit):
        if unit == "mois":
            return f"{n} mois"
        plural = "s" if n > 1 else ""
        return f"{n} {unit}{plural}"

    def subscription(self, user_id, data, note=None):
        """Gère l'abonnement d'un compte.

        action = add   : ajoute n heures / jours / mois / ans (à partir de la fin actuelle, ou de maintenant si terminé)
        action = set   : fixe la date de fin (horodatage « until »)
        action = stop  : termine l'abonnement tout de suite
        action = unlimited : plus de date de fin
        """
        user = self.get(user_id)
        if not user:
            raise UserError("Utilisateur introuvable")
        now = int(time.time())
        old = user["expires_at"]
        action = data.get("action")
        if action == "add":
            try:
                n = int(str(data.get("n", "")).strip())
            except ValueError:
                raise UserError("Indiquez un nombre entier (ex : 1, 3, 16)")
            unit = data.get("unit")
            if unit not in UNITS:
                raise UserError("Choisissez l'unité : heures, jours, mois ou ans")
            limits = {"heure": 24 * 366 * 10, "jour": 366 * 10, "mois": 120, "an": 10}
            if not 1 <= n <= limits[unit]:
                raise UserError(f"Nombre entre 1 et {limits[unit]} pour cette unité")
            start = old if old and old > now else now
            new, label = self.extend(start, n, unit), "+ " + self.label(n, unit)
        elif action == "set":
            try:
                new = int(data.get("until"))
            except (TypeError, ValueError):
                raise UserError("Date de fin invalide")
            if new <= now:
                raise UserError("La date de fin doit être dans le futur (utilisez « Arrêter maintenant »)")
            if new > now + 10 * 366 * 86400:
                raise UserError("10 ans au maximum")
            label = "Date de fin fixée"
        elif action == "stop":
            new, label = now, "Arrêté"
        elif action == "unlimited":
            new, label = None, "Sans limite"
        else:
            raise UserError("Action inconnue")
        self._q("UPDATE users SET expires_at = ? WHERE id = ?", (new, user_id), commit=True)
        self._q("INSERT INTO subscription_log (user_id, at, action, label, old_expires, new_expires, note) "
                "VALUES (?,?,?,?,?,?,?)", (user_id, now, action, label, old, new,
                                           (note or str(data.get("note") or ""))[:80] or None), commit=True)
        return dict(self.public(self.get(user_id)), history=self.history(user_id))

    def set_max_routers(self, user_id, data):
        user = self.get(user_id)
        if not user or user["role"] != "client":
            raise UserError("Seuls les clients ont un nombre de routeurs")
        old, new = user["max_routers"] or 0, self._max_routers(data)
        self._q("UPDATE users SET max_routers = ? WHERE id = ?", (new, user_id), commit=True)
        self._q("INSERT INTO subscription_log (user_id, at, action, label, old_expires, new_expires, note) "
                "VALUES (?,?,?,?,?,?,?)", (user_id, int(time.time()), "routers", f"Routeurs : {old} → {new}",
                                           user["expires_at"], user["expires_at"],
                                           str(data.get("note") or "")[:80] or None), commit=True)
        return dict(self.public(self.get(user_id)), history=self.history(user_id))

    def history(self, user_id, limit=30):
        rows, _ = self._q("SELECT at, action, label, old_expires, new_expires, note FROM subscription_log "
                          "WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [dict(zip(("at", "action", "label", "old_expires", "new_expires", "note"), r)) for r in rows]

    def reset_password(self, user_id, data):
        user = self.get(user_id)
        if not user:
            raise UserError("Utilisateur introuvable")
        pwd = self._password(data, required=True)
        self._q("UPDATE users SET password_hash = ? WHERE id = ?", (self.hash(pwd), user_id), commit=True)
        return {"password": pwd}

    def set_active(self, user_id, active):
        user = self.get(user_id)
        if not user:
            raise UserError("Utilisateur introuvable")
        self._q("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id), commit=True)
        user = self.get(user_id)
        self._sync_pv(user)
        return self.public(user)

    def delete(self, user_id):
        user = self.get(user_id)
        if not user:
            raise UserError("Utilisateur introuvable")
        user["active"] = 0
        if self.routers and self.routers.owned(user["username"]):  # ses routeurs reviennent à l'administrateur
            for z in self.routers.owned(user["username"]):
                z.pop("owner", None)
            self.routers._persist()
        self._sync_pv(user)  # archive ses points de vente ; ses lots et ventes restent dans l'historique
        self._q("DELETE FROM users WHERE id = ?", (user_id,), commit=True)
        self._q("DELETE FROM subscription_log WHERE user_id = ?", (user_id,), commit=True)
        rows, _ = self._q("SELECT id FROM users WHERE parent = ?", (user["username"],))
        for (tid,) in rows:  # l'équipe d'un client supprimé disparaît avec lui
            self.delete(tid)
        return {"ok": True}

    def change_own_password(self, principal, data, save_admin):
        old, new = str(data.get("old") or ""), str(data.get("new") or "")
        if len(new) < 6:
            raise UserError("Nouveau mot de passe : 6 caractères minimum")
        if principal.is_admin:
            if not self.verify(old, self.config["admin"]["password_hash"]):
                raise UserError("Mot de passe actuel incorrect")
            if len(new) < 10:
                raise UserError("Administrateur : 10 caractères minimum")
            self.config["admin"]["password_hash"] = self.hash(new)
            save_admin()
            return {"ok": True}
        u = self._get(principal.username)
        if not u or not self.verify(old, u["password_hash"]):
            raise UserError("Mot de passe actuel incorrect")
        self._q("UPDATE users SET password_hash = ? WHERE id = ?", (self.hash(new), u["id"]), commit=True)
        return {"ok": True}

    # --- lecture ------------------------------------------------------------------------
    def public(self, u):
        zones = {z["id"]: z["name"] for z in self.config.get("zones", [])}
        return {"id": u["id"], "username": u["username"], "name": u["name"], "phone": u["phone"] or "",
                "role": u["role"], "role_label": ROLE_LABEL[u["role"]], "zones": u["zones"],
                "zone_names": [zones[z] for z in u["zones"] if z in zones], "commission": u["commission"] or 0,
                "active": bool(u["active"]), "created_at": u["created_at"], "last_login": u["last_login"],
                "expires_at": u["expires_at"], "expired": self.expired(u), "now": int(time.time()),
                "max_routers": u["max_routers"] or 0,
                "routers_used": len(self.routers.owned(u["username"])) if self.routers else 0,
                "parent": u["parent"]}

    def list(self, since, parent=None):
        where = "WHERE parent = ?" if parent else ""
        rows, _ = self._q(f"SELECT {', '.join(self.COLS)} FROM users {where} ORDER BY active DESC, role, name",
                          (parent,) if parent else ())
        names = dict(self._q("SELECT username, name FROM users WHERE role = 'client'")[0])
        out = []
        for r in rows:
            u = self.public(self._row(r))
            u["parent_name"] = names.get(u["parent"]) if u["parent"] else None
            if u["parent"]:  # l'équipe suit l'abonnement de son client
                p = self._get(u["parent"]) or {}
                u["expires_at"], u["follows_parent"] = p.get("expires_at"), True
                u["expired"] = bool(u["expires_at"]) and time.time() >= u["expires_at"]
            if u["role"] == "client":  # client : toutes les ventes de ses routeurs
                zs = [z["id"] for z in self.routers.owned(u["username"])] if self.routers else []
                cond, args = "b.zone_id IN (%s)" % (",".join("?" * len(zs)) or "NULL"), zs
            else:
                cond, args = "b.created_by = ?", [u["username"]]
            stats, _ = self._q(
                "SELECT COUNT(t.code), COALESCE(SUM(b.price), 0) FROM tickets t JOIN batches b ON b.id = t.batch_id "
                f"WHERE {cond} AND t.first_used >= ?", args + [since])
            gen, _ = self._q(f"SELECT COALESCE(SUM(b.count), 0), COUNT(*) FROM batches b WHERE {cond}", args)
            u["stats"] = {"sold": stats[0][0], "amount": stats[0][1], "generated": gen[0][0], "lots": gen[0][1],
                          "commission_due": round(stats[0][1] * u["commission"] / 100)}
            out.append(u)
        return out
