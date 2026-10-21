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

ROLES = ("admin", "gerant", "revendeur")
ROLE_LABEL = {"admin": "Administrateur", "gerant": "Gérant", "revendeur": "Revendeur"}


class UserError(Exception):
    pass


class Principal:
    """L'utilisateur connecté et ce qu'il a le droit de voir ou faire."""

    def __init__(self, username, name, role, zones=None, user_id=None, commission=0):
        self.username = username
        self.name = name or username
        self.role = role
        self.zones = zones  # None = tous les routeurs
        self.user_id = user_id
        self.commission = commission

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
            "users": ("admin",), "settings": ("admin",), "routers_write": ("admin",),
            "routers_read": ("admin", "gerant"), "forfaits_write": ("admin", "gerant"),
            "lots_manage": ("admin", "gerant"), "dashboard_full": ("admin", "gerant"),
        }
        return self.role in rules.get(action, ROLES)

    def public(self):
        return {"username": self.username, "name": self.name, "role": self.role,
                "role_label": ROLE_LABEL[self.role], "zones": self.zones, "commission": self.commission}


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
            try:
                store.db.execute("ALTER TABLE points_vente ADD COLUMN user_id INTEGER")
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
            "active", "created_at", "last_login")

    def _row(self, row):
        u = dict(zip(self.COLS, row))
        u["zones"] = json.loads(u["zones"] or "[]")
        return u

    def _get(self, username):
        rows, _ = self._q("SELECT * FROM users WHERE lower(username) = lower(?)", (username,))
        return self._row(rows[0]) if rows else None

    def get(self, user_id):
        rows, _ = self._q("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._row(rows[0]) if rows else None

    # --- connexion ---------------------------------------------------------------------
    def authenticate(self, username, password):
        admin = self.config["admin"]
        if username.lower() == admin["username"].lower():
            return admin["username"] if self.verify(password, admin["password_hash"]) else None
        u = self._get(username)
        if u and u["active"] and self.verify(password, u["password_hash"]):
            self._q("UPDATE users SET last_login = ? WHERE id = ?", (int(time.time()), u["id"]), commit=True)
            return u["username"]
        return None

    def principal(self, username):
        if not username:
            return None
        if username == self.config["admin"]["username"]:
            return Principal(username, "Administrateur", "admin")
        u = self._get(username)
        if not u or not u["active"]:
            return None  # compte supprimé ou désactivé : la session tombe
        known = {z["id"] for z in self.config.get("zones", [])}
        return Principal(u["username"], u["name"], u["role"], [z for z in u["zones"] if z in known],
                         u["id"], u["commission"] or 0)

    # --- gestion -----------------------------------------------------------------------
    def _validate(self, data, current=None):
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
        if role not in ("gerant", "revendeur"):
            raise UserError("Choisissez le rôle : gérant ou revendeur")
        known = {z["id"] for z in self.config.get("zones", [])}
        zones = [z for z in (data.get("zones") or []) if z in known]
        if not zones:
            raise UserError("Choisissez au moins un routeur")
        try:
            commission = float(data.get("commission") or 0)
        except (TypeError, ValueError):
            raise UserError("Commission invalide")
        if not 0 <= commission <= 100:
            raise UserError("Commission entre 0 et 100 %")
        phone = re.sub(r"[^0-9+ ]", "", str(data.get("phone") or ""))[:20].strip()
        return name, username, role, zones, commission if role == "revendeur" else 0, phone

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

    def create(self, data):
        name, username, role, zones, commission, phone = self._validate(data)
        pwd = self._password(data, required=True)
        _, uid = self._q(
            "INSERT INTO users (username, name, phone, role, password_hash, zones, commission, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (username, name, phone, role, self.hash(pwd), json.dumps(zones), commission, int(time.time())), commit=True)
        user = self.get(uid)
        self._sync_pv(user)
        return dict(self.public(user), password=pwd)

    def update(self, user_id, data):
        user = self.get(user_id)
        if not user:
            raise UserError("Utilisateur introuvable")
        name, username, role, zones, commission, phone = self._validate(data, current=user)
        self._q("UPDATE users SET username=?, name=?, phone=?, role=?, zones=?, commission=? WHERE id=?",
                (username, name, phone, role, json.dumps(zones), commission, user_id), commit=True)
        user = self.get(user_id)
        self._sync_pv(user)
        return self.public(user)

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
        self._sync_pv(user)  # archive ses points de vente ; ses lots et ventes restent dans l'historique
        self._q("DELETE FROM users WHERE id = ?", (user_id,), commit=True)
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
                "active": bool(u["active"]), "created_at": u["created_at"], "last_login": u["last_login"]}

    def list(self, since):
        rows, _ = self._q("SELECT * FROM users ORDER BY active DESC, role, name")
        out = []
        for r in rows:
            u = self.public(self._row(r))
            stats, _ = self._q(
                "SELECT COUNT(t.code), COALESCE(SUM(b.price), 0) FROM tickets t JOIN batches b ON b.id = t.batch_id "
                "WHERE b.created_by = ? AND t.first_used >= ?", (u["username"], since))
            gen, _ = self._q("SELECT COALESCE(SUM(count), 0), COUNT(*) FROM batches WHERE created_by = ?", (u["username"],))
            u["stats"] = {"sold": stats[0][0], "amount": stats[0][1], "generated": gen[0][0], "lots": gen[0][1],
                          "commission_due": round(stats[0][1] * u["commission"] / 100)}
            out.append(u)
        return out
