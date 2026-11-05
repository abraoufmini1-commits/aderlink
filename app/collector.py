"""Collecte périodique des données sur les routeurs MikroTik de chaque zone.

Un fil d'exécution interroge tous les routeurs en parallèle toutes les
`poll_seconds` secondes, garde le dernier état en mémoire et enregistre un
historique (SQLite) pour les courbes et le journal d'activité.
"""
import math
import queue
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


MASS_HOLD = 4  # relevés (environ 2 min) pendant lesquels une coupure générale est ignorée


def open_files():
    """Combien de fichiers et connexions le programme a ouverts (Linux), pour surveiller les fuites."""
    import os
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def internet_ok():
    """Le serveur a-t-il Internet ? (deux serveurs DNS publics, 3 s chacun)"""
    import socket
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=3).close()
            return True
        except OSError:
            pass
    return False


class TrackedLock:
    """Verrou de la base qui note qui le garde longtemps (pour trouver ce qui ralentit le serveur)."""
    def __init__(self):
        import collections
        self._lock = threading.Lock()
        self.long = collections.deque(maxlen=12)  # (heure, secondes, endroit)
        self._t = 0.0
        self._where = ""
        self.owner = None

    def acquire(self, *a, **k):
        ok = self._lock.acquire(*a, **k)
        if ok:
            self._t = time.time()
            self.owner = threading.get_ident()
        return ok

    def release(self):
        self.owner = None
        held = time.time() - self._t
        if held > 0.5:
            import sys
            f = sys._getframe(1)
            parts = []
            while f and len(parts) < 3:
                if "collector.py" not in f.f_code.co_filename or f.f_code.co_name not in ("__exit__", "release"):
                    parts.append(f"{f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_code.co_name}")
                f = f.f_back
            self.long.append((int(self._t), round(held, 2), " < ".join(parts)))
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def locked(self):
        return self._lock.locked()


class Store:
    """Historique et journal dans une petite base SQLite."""

    def __init__(self, path):
        self.lock = TrackedLock()
        self.path = path
        self._readers = queue.LifoQueue()  # quelques connexions en lecture, partagées (pas une par fil)
        self._reader_count = 0
        self._reader_lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        # WAL : les écritures du collecteur ne bloquent plus les lectures, et chaque enregistrement est bien plus rapide
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA temp_store=MEMORY")
            self.db.execute("PRAGMA cache_size=-200000")      # 200 Mo de cache (le serveur a 8 Go)
            self.db.execute("PRAGMA mmap_size=1073741824")    # lecture directe en mémoire
            self.db.execute("PRAGMA journal_size_limit=67108864")
        except sqlite3.Error:
            pass
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER, zone_id TEXT, online INTEGER,
                active INTEGER, rx_bps INTEGER, tx_bps INTEGER);
            CREATE INDEX IF NOT EXISTS idx_samples_cover ON samples(ts, zone_id, online, active, rx_bps, tx_bps);
            CREATE INDEX IF NOT EXISTS idx_samples_zone ON samples(zone_id, ts);
            CREATE TABLE IF NOT EXISTS events (
                ts INTEGER, zone_id TEXT, kind TEXT, message TEXT);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            """
        )
        if path != ":memory:":
            self._tune()

    def read(self, sql, params=()):
        """Lecture sans bloquer la base : connexions en lecture à part (base WAL).
        Une lecture ne prend JAMAIS le verrou des écritures (avant : une longue lecture le gardait
        plusieurs secondes et tout le serveur attendait). Base en mémoire (démo, tests) : connexion principale."""
        if self.path == ":memory:":
            with self.lock:
                return self.db.execute(sql, params).fetchall()
        if getattr(self.lock, "owner", None) == threading.get_ident():  # déjà dans une écriture : on lit ici
            return self.db.execute(sql, params).fetchall()
        if self.db.in_transaction and not self.lock.locked():
            # écriture laissée sans « commit » (personne n'écrit en ce moment) : on la voit par la connexion principale
            with self.lock:
                return self.db.execute(sql, params).fetchall()
        for attempt in (1, 2):
            con = self._take_reader()
            if con is None:
                break
            try:
                return con.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # connexion abîmée (rare) : on la remplace et on réessaie une fois
                try:
                    con.close()
                except sqlite3.Error:
                    pass
                with self._reader_lock:
                    self._reader_count -= 1
                con = None
                if attempt == 2:
                    raise
            finally:
                if con is not None:
                    self._readers.put(con)
        with self.lock:  # aucune connexion de lecture disponible (très rare)
            return self.db.execute(sql, params).fetchall()

    MAX_READERS = 8

    def _take_reader(self):
        try:
            return self._readers.get_nowait()
        except queue.Empty:
            pass
        with self._reader_lock:
            if self._reader_count >= self.MAX_READERS:
                con = None
            else:
                self._reader_count += 1
                con = True
        if con is None:
            try:
                return self._readers.get(timeout=20)  # on attend une connexion de lecture, pas le verrou
            except queue.Empty:
                return None
        try:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False, timeout=10)
            c.execute("PRAGMA cache_size=-32000")
            c.execute("PRAGMA mmap_size=1073741824")
            return c
        except sqlite3.Error:
            with self._reader_lock:
                self._reader_count -= 1
            return None

    def _tune(self):
        """Une fois : l'index (ts) seul est retiré — l'index complet commence aussi par ts et évite
        de relire la table ; puis statistiques pour que SQLite choisisse le bon index."""
        try:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_samples_ts'").fetchone():
                self.db.execute("DROP INDEX idx_samples_ts")
                self.db.execute("ANALYZE")
                self.db.commit()
        except sqlite3.Error:
            pass

    def add_sample(self, ts, zone_id, online, active, rx, tx):
        self.add_samples([(ts, zone_id, int(online), active, rx, tx)])

    def add_samples(self, rows):
        """Plusieurs relevés en une seule écriture (un enregistrement au lieu d'un par routeur)."""
        if not rows:
            return
        with self.lock:
            self.db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?)", rows)
            self.db.commit()

    def add_event(self, ts, zone_id, kind, message):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (ts, zone_id, kind, message))
            self.db.commit()

    def checkpoint(self):
        """Vide le journal WAL dans la base (sinon il grossit et tout ralentit)."""
        if self.path == ":memory:":
            return
        try:
            with self.lock:
                self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")  # sans attendre les lectures en cours
        except sqlite3.Error:
            pass

    def prune(self):
        """Efface l'historique de plus de 7 jours, par petits morceaux (la base n'est jamais bloquée longtemps)."""
        limit = int(time.time()) - HISTORY_DAYS * 86400
        for table in ("samples", "events"):
            while True:
                with self.lock:
                    cur = self.db.execute(f"DELETE FROM {table} WHERE rowid IN "
                                          f"(SELECT rowid FROM {table} WHERE ts < ? LIMIT 20000)", (limit,))
                    self.db.commit()
                if cur.rowcount < 20000:
                    break
                time.sleep(0.2)

    def history(self, since, bucket, zone_id=None):
        """Utilisateurs connectés (somme des zones) par tranche de `bucket` secondes."""
        sql = ("SELECT (ts / ?) * ? AS b, zone_id, MAX(active), MAX(rx_bps), MAX(tx_bps) "
               "FROM samples WHERE ts >= ?")
        params = [bucket, bucket, since]
        if zone_id:
            sql += " AND zone_id = ?"
            params.append(zone_id)
        sql += " GROUP BY b, zone_id ORDER BY b"
        rows = self.read(sql, params)
        merged = {}
        for b, _zone, active, rx, tx in rows:
            point = merged.setdefault(b, {"ts": b, "active": 0, "rx_bps": 0, "tx_bps": 0})
            point["active"] += active or 0
            point["rx_bps"] += rx or 0
            point["tx_bps"] += tx or 0
        return list(merged.values())

    def events(self, limit=20):
        rows = self.read("SELECT ts, zone_id, kind, message FROM events ORDER BY ts DESC LIMIT ?", (limit,))
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
        self.last_cycle = None   # (heure, durée en s) du dernier relevé complet
        self._pending_samples = None  # pendant un relevé complet : les mesures, écrites d'un coup à la fin
        self._mass = 0           # relevés de suite où presque tout est injoignable (coupure côté serveur)
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
        # nettoyage de l'historique : toutes les heures, et 2 minutes après le démarrage
        # (avant : compté en relevés, il ne se faisait jamais si le serveur redémarrait souvent)
        next_prune = time.time() + 120
        while not self._stop.is_set():
            self.poll_all()
            cycles += 1
            if time.time() >= next_prune:
                next_prune = time.time() + 3600
                try:
                    self.store.prune()
                except sqlite3.Error as e:
                    print(f"[collecte] nettoyage de l'historique : {e}", flush=True)
            if cycles % 20 == 0:
                self.store.checkpoint()
                n = open_files()
                if n is not None and n > 400:
                    print(f"[collecte] fichiers ouverts : {n}", flush=True)
            self._stop.wait(self.poll_seconds)

    def poll_all(self):
        zones = self.config.get("zones", [])
        if not zones:
            return
        t0 = time.time()
        # beaucoup de routeurs : plus de relevés en parallèle, pour qu'un routeur éteint (8 s d'attente) ne ralentisse pas les autres
        with ThreadPoolExecutor(max_workers=min(48, len(zones))) as pool:
            snaps = dict(zip((z["id"] for z in zones), pool.map(self._poll_zone, zones)))
            # Un routeur qui était en ligne et ne répond pas : on réessaie une fois, quelques secondes après,
            # avant de le dire hors ligne (une petite perte de paquets ne doit pas donner une fausse coupure).
            retry = [z for z in zones if self._failed(z, snaps[z["id"]])]
            if retry and not self._stop.wait(3):
                for z, snap in zip(retry, pool.map(self._poll_zone, retry)):
                    snaps[z["id"]] = snap
        failed = [z for z in zones if self._failed(z, snaps[z["id"]])]
        watched = [z for z in zones if (z.get("router") or {}).get("host")]
        # Beaucoup de routeurs coupés à la même seconde : la coupure vient du serveur ou d'Internet, pas des routeurs.
        if len(failed) >= 5 and len(failed) >= 0.6 * len(watched):
            net = internet_ok()
            self._mass += 1
            sample = "; ".join(f"{z['name']}: {snaps[z['id']].get('error')}" for z in failed[:3])
            print(f"[collecte] {len(failed)}/{len(watched)} routeurs injoignables en même temps "
                  f"(Internet du serveur : {'OK' if net else 'COUPÉ'}, relevé {time.time() - t0:.0f} s) — {sample}", flush=True)
            if self._mass == 1:
                self.store.add_event(int(time.time()), "", "server",
                                     f"Coupure côté serveur : {len(failed)} routeurs injoignables en même temps "
                                     f"(Internet du serveur {'OK' if net else 'coupé'})")
            if self._mass <= MASS_HOLD:  # on garde le dernier état connu quelques relevés, sans fausses alertes
                for z in zones:
                    if z in failed:
                        continue
                    self._record(z, snaps[z["id"]])
                return
        else:
            self._mass = 0
        self._pending_samples = []
        try:
            for zone in zones:
                self._record(zone, snaps[zone["id"]])
        finally:
            rows, self._pending_samples = self._pending_samples, None
            self.store.add_samples(rows)
        took = time.time() - t0
        self.last_cycle = (int(time.time()), round(took, 1))
        if took > self.poll_seconds:
            print(f"[collecte] relevé lent : {took:.0f} s pour {len(zones)} routeurs", flush=True)

    def _failed(self, zone, snap):
        prev = self.state.get(zone["id"])
        return bool(prev and prev.get("online") and not snap.get("online") and not snap.get("pending"))

    def add_tickets(self, zone_id, n):
        """Lot créé : on ajoute tout de suite les tickets au compteur du routeur (le relevé suivant confirme)."""
        with self.lock:
            st = self.state.get(zone_id)
            if st and st.get("tickets") is not None:
                st["tickets"] += n

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
        rows = self._pending_samples if self._pending_samples is not None else None
        if rows is not None:
            rows.append((snap["checked_at"], zid, int(snap["online"]), snap["active"], snap.get("rx_bps", 0), snap.get("tx_bps", 0)))
            return
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
