"""Formules d'abonnement : ce que les clients (propriétaires de Wi-Fi zones) achètent à ADER TECHNOLOGIE.

Chaque formule fixe un prix par mois, un nombre de routeurs, et des options :
- remote   : accès à distance Winbox / WebFig
- antennas : appareils derrière les routeurs (antennes, caméras…)
- roaming  : ADER Roaming (roaming_routers = combien de routeurs dans un réseau ; -1 = sans limite)
- excel    : export Excel des rapports
- support  : support prioritaire

Les réglages communs (remises, essai gratuit, routeur en plus, jours de grâce) sont dans config["billing"].
"""
import time

FEATURES = ("remote", "antennas", "roaming", "excel", "support")
FEATURE_LABEL = {"remote": "Accès à distance Winbox et WebFig", "antennas": "Antennes et appareils à distance",
                 "roaming": "ADER Roaming", "excel": "Export Excel des rapports", "support": "Support prioritaire"}

DEFAULT_PLANS = [
    {"name": "Découverte", "price": 3000, "routers": 3, "roaming_routers": 0, "features": []},
    {"name": "Starter", "price": 6000, "routers": 8, "roaming_routers": 0, "features": ["remote"]},
    {"name": "Pro", "price": 12000, "routers": 20, "roaming_routers": 10,
     "features": ["remote", "antennas", "roaming", "excel"], "highlight": True},
    {"name": "Business", "price": 25000, "routers": 50, "roaming_routers": -1,
     "features": ["remote", "antennas", "roaming", "excel", "support"]},
]
DEFAULT_BILLING = {"discounts": {"3": 5, "6": 10, "12": 15}, "trial_days": 7, "trial_plan": "Starter",
                   "extra_router_price": 700, "grace_days": 3}

# Ce que voit un compte sans formule (anciens clients, administrateur) : tout est permis
ALL = {f: True for f in FEATURES}


class PlanError(Exception):
    pass


class Plans:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        with store.lock:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, routers INTEGER,
                    roaming_routers INTEGER DEFAULT 0, features TEXT, highlight INTEGER DEFAULT 0,
                    position INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, created_at INTEGER);
                """
            )
            for sql in ("ALTER TABLE users ADD COLUMN plan_id INTEGER",
                        "ALTER TABLE users ADD COLUMN extra_routers INTEGER DEFAULT 0"):
                try:
                    store.db.execute(sql)
                except Exception:
                    pass
            if not store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0]:
                for i, p in enumerate(DEFAULT_PLANS):
                    store.db.execute("INSERT INTO plans (name, price, routers, roaming_routers, features, highlight, "
                                     "position, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                     (p["name"], p["price"], p["routers"], p["roaming_routers"],
                                      ",".join(p["features"]), 1 if p.get("highlight") else 0, i, int(time.time())))
            store.db.commit()

    def _q(self, sql, params=(), commit=False):
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            rows = cur.fetchall()
            if commit:
                self.store.db.commit()
            return rows, cur.lastrowid

    # --- réglages communs ----------------------------------------------------------------
    def billing(self):
        b = dict(DEFAULT_BILLING)
        b.update(self.config.get("billing") or {})
        b["discounts"] = {str(k): v for k, v in (b.get("discounts") or {}).items()}
        return b

    def set_billing(self, data):
        b = self.billing()
        def num(key, lo, hi, cast=int):
            try:
                v = cast(str(data.get(key, b[key])).replace(" ", "").replace(",", "."))
            except (TypeError, ValueError):
                raise PlanError("Nombre invalide")
            if not lo <= v <= hi:
                raise PlanError(f"Valeur entre {lo} et {hi}")
            return v
        b["trial_days"] = num("trial_days", 0, 60)
        b["extra_router_price"] = num("extra_router_price", 0, 1_000_000)
        b["grace_days"] = num("grace_days", 0, 30)
        if data.get("trial_plan") is not None:
            b["trial_plan"] = str(data.get("trial_plan"))[:40]
        disc = data.get("discounts")
        if isinstance(disc, dict):
            out = {}
            for k in ("3", "6", "12"):
                try:
                    v = float(str(disc.get(k, b["discounts"].get(k, 0))).replace(",", "."))
                except ValueError:
                    raise PlanError("Remise invalide")
                if not 0 <= v <= 90:
                    raise PlanError("Remise entre 0 et 90 %")
                out[k] = v
            b["discounts"] = out
        self.config["billing"] = b
        return b

    # --- formules ------------------------------------------------------------------------
    COLS = ("id", "name", "price", "routers", "roaming_routers", "features", "highlight", "position", "archived")

    def _row(self, r):
        p = dict(zip(self.COLS, r))
        p["features"] = [f for f in (p["features"] or "").split(",") if f in FEATURES]
        p["highlight"], p["archived"] = bool(p["highlight"]), bool(p["archived"])
        return p

    def list(self, include_archived=False):
        rows, _ = self._q(f"SELECT {', '.join(self.COLS)} FROM plans " + ("" if include_archived else "WHERE archived = 0 ")
                          + "ORDER BY position, price")
        return [self._row(r) for r in rows]

    def get(self, plan_id):
        if not plan_id:
            return None
        rows, _ = self._q(f"SELECT {', '.join(self.COLS)} FROM plans WHERE id = ?", (plan_id,))
        return self._row(rows[0]) if rows else None

    def by_name(self, name):
        return next((p for p in self.list() if p["name"].lower() == str(name or "").lower()), None)

    def _values(self, data):
        name = " ".join(str(data.get("name") or "").split())[:30]
        if not name:
            raise PlanError("Donnez un nom à la formule")
        try:
            price = int(str(data.get("price") or 0).replace(" ", ""))
            routers = int(str(data.get("routers") or 0))
            roaming = int(str(data.get("roaming_routers") if data.get("roaming_routers") not in (None, "") else 0))
        except ValueError:
            raise PlanError("Prix et nombres : des nombres entiers")
        if not 0 <= price <= 10_000_000:
            raise PlanError("Prix invalide")
        if not 1 <= routers <= 1000:
            raise PlanError("Nombre de routeurs entre 1 et 1000")
        if roaming < -1 or roaming > 1000:
            raise PlanError("Routeurs Roaming : -1 (sans limite) ou un nombre")
        feats = [f for f in (data.get("features") or []) if f in FEATURES]
        if "roaming" not in feats:
            roaming = 0
        return name, price, routers, roaming, ",".join(feats), 1 if data.get("highlight") else 0

    def create(self, data):
        v = self._values(data)
        if self.by_name(v[0]):
            raise PlanError("Une formule porte déjà ce nom")
        pos = (self._q("SELECT COALESCE(MAX(position), 0) + 1 FROM plans")[0][0][0])
        _, pid = self._q("INSERT INTO plans (name, price, routers, roaming_routers, features, highlight, position, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", v + (pos, int(time.time())), commit=True)
        return self.get(pid)

    def update(self, plan_id, data):
        if not self.get(plan_id):
            raise PlanError("Formule introuvable")
        v = self._values(data)
        other = self.by_name(v[0])
        if other and other["id"] != plan_id:
            raise PlanError("Une formule porte déjà ce nom")
        self._q("UPDATE plans SET name=?, price=?, routers=?, roaming_routers=?, features=?, highlight=? WHERE id=?",
                v + (plan_id,), commit=True)
        return self.get(plan_id)

    def archive(self, plan_id):
        used = self._q("SELECT COUNT(*) FROM users WHERE plan_id = ?", (plan_id,))[0][0][0]
        if used:
            raise PlanError(f"{used} client{'s ont' if used > 1 else ' a'} cette formule : changez-leur d'abord de formule")
        self._q("UPDATE plans SET archived = 1 WHERE id = ?", (plan_id,), commit=True)
        return {"ok": True}

    def clients_count(self):
        return dict(self._q("SELECT plan_id, COUNT(*) FROM users WHERE plan_id IS NOT NULL GROUP BY plan_id")[0])

    # --- prix ----------------------------------------------------------------------------
    def quote(self, plan, months, extra_routers=0):
        """Prix pour une durée, avec la remise et les routeurs en plus."""
        b = self.billing()
        monthly = plan["price"] + max(0, int(extra_routers or 0)) * int(b["extra_router_price"])
        gross = monthly * months
        disc = float(b["discounts"].get(str(months), 0) or 0)
        total = int(round(gross * (100 - disc) / 100))
        return {"months": months, "monthly": monthly, "gross": gross, "discount": disc, "total": total}

    # --- ce qu'un compte a le droit de faire -------------------------------------------
    def limits(self, plan_id, extra_routers=0, legacy_max=0):
        """Limites d'un client : selon sa formule, sinon (ancien compte) son nombre de routeurs et toutes les options."""
        p = self.get(plan_id)
        if not p:
            return {"plan": None, "max_routers": legacy_max, "roaming_routers": -1, "features": dict(ALL)}
        return {"plan": {"id": p["id"], "name": p["name"], "price": p["price"]},
                "max_routers": p["routers"] + max(0, int(extra_routers or 0)),
                "roaming_routers": p["roaming_routers"], "features": {f: f in p["features"] for f in FEATURES}}
