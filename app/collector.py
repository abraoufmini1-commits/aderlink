"""Collecte périodique des données sur les routeurs MikroTik de chaque zone.

Un fil d'exécution interroge tous les routeurs en parallèle toutes les
`poll_seconds` secondes, garde le dernier état en mémoire et enregistre un
historique (SQLite) pour les courbes et le journal d'activité.
"""
import math
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .routeros import RouterOS, RouterOSError

HISTORY_DAYS = 7
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """'1w2d03:04:05' ou '2d3h4m5s' -> secondes."""
    if not text:
        return 0
    total = 0
    m = re.search(r"(\d+):(\d+):(\d+)$", text)
    if m:
        h, mi, s = map(int, m.groups())
        total += h * 3600 + mi * 60 + s
        text = text[: m.start()]
    for value, unit in re.findall(r"(\d+)([wdhms])", text):
        total += int(value) * _UNITS[unit]
    return total


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


class Store:
    """Historique et journal dans une petite base SQLite."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER, zone_id TEXT, online INTEGER,
                active INTEGER, rx_bps INTEGER, tx_bps INTEGER);
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
            CREATE TABLE IF NOT EXISTS events (
                ts INTEGER, zone_id TEXT, kind TEXT, message TEXT);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            """
        )

    def add_sample(self, ts, zone_id, online, active, rx, tx):
        with self.lock:
            self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?)",
                            (ts, zone_id, int(online), active, rx, tx))
            self.db.commit()

    def add_event(self, ts, zone_id, kind, message):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (ts, zone_id, kind, message))
            self.db.commit()

    def prune(self):
        limit = int(time.time()) - HISTORY_DAYS * 86400
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (limit,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (limit,))
            self.db.commit()

    def history(self, since, bucket, zone_id=None):
        """Utilisateurs connectés (somme des zones) par tranche de `bucket` secondes."""
        sql = ("SELECT (ts / ?) * ? AS b, zone_id, MAX(active), MAX(rx_bps), MAX(tx_bps) "
               "FROM samples WHERE ts >= ?")
        params = [bucket, bucket, since]
        if zone_id:
            sql += " AND zone_id = ?"
            params.append(zone_id)
        sql += " GROUP BY b, zone_id ORDER BY b"
        with self.lock:
            rows = self.db.execute(sql, params).fetchall()
        merged = {}
        for b, _zone, active, rx, tx in rows:
            point = merged.setdefault(b, {"ts": b, "active": 0, "rx_bps": 0, "tx_bps": 0})
            point["active"] += active or 0
            point["rx_bps"] += rx or 0
            point["tx_bps"] += tx or 0
        return list(merged.values())

    def events(self, limit=20):
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, zone_id, kind, message FROM events ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(ts=r[0], zone_id=r[1], kind=r[2], message=r[3]) for r in rows]


class Collector:
    def __init__(self, config, store, demo=False):
        self.config = config
        self.store = store
        self.demo = demo
        self.state = {}          # zone_id -> dernier état
        self._counters = {}      # zone_id -> (ts, rx_bytes, tx_bytes)
        self.lock = threading.Lock()
        self.poll_seconds = max(10, int(config.get("poll_seconds", 30)))
        self._stop = threading.Event()
        self.ticket_hook = None  # fonction (zone, api, sessions) branchée par le module Tickets
        if demo:
            self._demo_backfill()

    # --- boucle --------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="aderlink-collector").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        cycles = 0
        while not self._stop.is_set():
            self.poll_all()
            cycles += 1
            if cycles % 120 == 0:
                self.store.prune()
            self._stop.wait(self.poll_seconds)

    def poll_all(self):
        zones = self.config.get("zones", [])
        if not zones:
            return
        with ThreadPoolExecutor(max_workers=min(16, len(zones))) as pool:
            for zone, snap in zip(zones, pool.map(self._poll_zone, zones)):
                self._record(zone, snap)

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}

    # --- interrogation d'un routeur ------------------------------------------------
    def _poll_zone(self, zone):
        if self.demo:
            return self._demo_snapshot(zone)
        r = zone["router"]
        now = time.time()
        if not r.get("host"):  # pas encore installé : rien à interroger
            return {"online": False, "pending": True, "checked_at": int(now), "sessions": [],
                    "error": "En attente d'installation"}
        try:
            with RouterOS(r["host"], r["username"], r["password"],
                          port=r.get("port", 8729 if r.get("ssl") else 8728),
                          use_ssl=r.get("ssl", False)) as api:
                res = (api.query("/system/resource/print") or [{}])[0]
                ident = (api.query("/system/identity/print") or [{}])[0]
                sessions = api.query("/ip/hotspot/active/print")
                if self.ticket_hook:
                    try:
                        self.ticket_hook(zone, api, [{"user": s.get("user")} for s in sessions])
                    except Exception as e:  # le suivi des tickets ne doit jamais bloquer la collecte
                        print(f"[tickets] {zone['name']} : {e}")
                try:
                    tickets = api.count("/ip/hotspot/user/print")
                except RouterOSError:
                    tickets = None
                rx = tx = None
                wan = r.get("wan_interface")
                if wan:
                    iface = api.query("/interface/print", f"?name={wan}")
                    if iface:
                        rx, tx = _int(iface[0].get("rx-byte")), _int(iface[0].get("tx-byte"))
        except RouterOSError as e:
            return {"online": False, "error": str(e), "checked_at": int(now)}

        rx_bps = tx_bps = 0
        if rx is not None:
            prev = self._counters.get(zone["id"])
            if prev and now > prev[0] and rx >= prev[1] and tx >= prev[2]:
                dt = now - prev[0]
                rx_bps = int((rx - prev[1]) * 8 / dt)
                tx_bps = int((tx - prev[2]) * 8 / dt)
            self._counters[zone["id"]] = (now, rx, tx)

        total_mem = _int(res.get("total-memory"))
        free_mem = _int(res.get("free-memory"))
        return {
            "online": True,
            "error": None,
            "checked_at": int(now),
            "identity": ident.get("name", ""),
            "board": res.get("board-name", ""),
            "version": res.get("version", ""),
            "uptime_s": parse_duration(res.get("uptime")),
            "cpu": _int(res.get("cpu-load")),
            "memory": round(100 * (total_mem - free_mem) / total_mem) if total_mem else None,
            "tickets": tickets,
            "rx_bps": rx_bps,
            "tx_bps": tx_bps,
            "sessions": [
                {
                    "user": s.get("user", ""),
                    "ip": s.get("address", ""),
                    "mac": s.get("mac-address", ""),
                    "uptime_s": parse_duration(s.get("uptime")),
                    "bytes_in": _int(s.get("bytes-in")),
                    "bytes_out": _int(s.get("bytes-out")),
                }
                for s in sessions
            ],
        }

    def _record(self, zone, snap):
        zid = zone["id"]
        snap["active"] = len(snap.get("sessions", []))
        with self.lock:
            prev = self.state.get(zid)
            if prev and prev.get("online") and not snap["online"]:
                # On garde les dernières infos connues pour l'affichage.
                for k in ("identity", "board", "version", "tickets"):
                    snap.setdefault(k, prev.get(k))
                snap["last_seen"] = prev.get("checked_at")
            elif prev and not snap["online"]:
                for k in ("identity", "board", "version", "tickets", "last_seen"):
                    snap.setdefault(k, prev.get(k))
            self.state[zid] = snap
        was_online = prev.get("online") if prev else None
        if snap.get("pending"):
            return
        if was_online is None:
            if not snap["online"] and not self.demo:
                self.store.add_event(snap["checked_at"], zid, "offline",
                                     f"{zone['name']} injoignable au démarrage")
        elif was_online != snap["online"]:
            if snap["online"]:
                self.store.add_event(snap["checked_at"], zid, "online", f"{zone['name']} de nouveau en ligne")
            else:
                self.store.add_event(snap["checked_at"], zid, "offline", f"{zone['name']} hors ligne")
        self.store.add_sample(snap["checked_at"], zid, snap["online"], snap["active"],
                              snap.get("rx_bps", 0), snap.get("tx_bps", 0))

    # --- mode démonstration -------------------------------------------------------
    def _demo_load(self, zone, ts):
        """Fréquentation fictive : creux la nuit, pic en soirée."""
        hour = (ts / 3600 + 1) % 24  # heure locale UTC+1
        base = 0.15 + 0.85 * max(0.0, math.sin((hour - 7) / 16 * math.pi)) ** 1.5
        capacity = zone.get("demo_capacity", 40)
        return max(0, int(capacity * base + random.uniform(-2, 2)))

    def _demo_backfill(self):
        now = int(time.time())
        for zone in self.config.get("zones", []):
            for ts in range(now - 86400, now, 300):
                online = not (zone.get("demo_offline") and ts > now - 2400)
                active = self._demo_load(zone, ts) if online else 0
                self.store.add_sample(ts, zone["id"], online, active, active * 180_000, active * 40_000)
            if zone.get("demo_offline"):
                self.store.add_event(now - 2400, zone["id"], "offline", f"{zone['name']} hors ligne")
        z = self.config.get("zones", [])
        if z:
            self.store.add_event(now - 9000, z[0]["id"], "online", f"{z[0]['name']} de nouveau en ligne")

    def _demo_snapshot(self, zone):
        now = int(time.time())
        if zone.get("demo_offline"):
            return {"online": False, "error": "Délai dépassé (démo)", "checked_at": now,
                    "identity": zone["name"], "board": "hAP ax²", "version": "7.16",
                    "tickets": 120, "last_seen": now - 2400}
        n = self._demo_load(zone, now)
        if self.ticket_hook:
            self.ticket_hook(zone, None, [])
        rnd = random.Random(zone["id"])
        sessions = []
        for i in range(n):
            sessions.append({
                "user": f"{rnd.randint(100000, 999999)}",
                "ip": f"10.5.50.{10 + i}",
                "mac": ":".join(f"{rnd.randint(0, 255):02X}" for _ in range(6)),
                "uptime_s": rnd.randint(60, 4 * 3600),
                "bytes_in": rnd.randint(1, 400) * 1_000_000,
                "bytes_out": rnd.randint(1, 60) * 1_000_000,
            })
        return {
            "online": True, "error": None, "checked_at": now,
            "identity": zone["name"], "board": zone.get("demo_board", "hEX S"),
            "version": "7.16.1", "uptime_s": 3 * 86400 + rnd.randint(0, 80000),
            "cpu": random.randint(4, 35), "memory": random.randint(30, 55),
            "tickets": zone.get("demo_capacity", 40) * 6,
            "rx_bps": n * random.randint(120_000, 260_000), "tx_bps": n * random.randint(25_000, 60_000),
            "sessions": sessions,
        }
