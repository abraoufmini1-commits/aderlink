"""ADER Roaming dans un programme à part.

Le programme principal (pages, relevés des routeurs, rapports) et le serveur RADIUS des tickets Roaming
tournent dans deux programmes séparés : une demande de connexion d'un client ne fait plus jamais la queue
derrière les relevés des 70+ routeurs (chaque programme a son propre processeur).

Les deux programmes partagent la même base (SQLite, mode WAL) et s'échangent quelques informations
par deux petits fichiers dans data/ :
- roaming-main.json  (écrit par le principal) : routeurs qui ont un autre système de tickets, noms des routeurs ;
- roaming-child.json (écrit par ce programme) : journal des dernières demandes, dernier contact de chaque routeur.
"""
import collections
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

MAIN_STATE = "roaming-main.json"
CHILD_STATE = "roaming-child.json"


def _write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# --- côté programme Roaming ------------------------------------------------------------------
class _Snapshot:
    """Remplace le collecteur dans ce programme : seulement les noms (identity) des routeurs."""
    def __init__(self):
        self.state = {}

    def snapshot(self):
        return self.state


def run(config_path, data_dir, load_config):
    from .collector import Store
    from .roaming import Roaming
    from .tickets import Tickets, open_router

    data_dir = Path(data_dir)
    cfg = load_config()
    import sqlite3
    snap = _Snapshot()
    for attempt in range(24):  # base occupée au démarrage : on attend et on réessaie au lieu de s'arrêter
        try:
            store = Store(str(data_dir / "aderlink.db"))
            tickets = Tickets(cfg, store, maintenance=False)
            roaming = Roaming(cfg, store, tickets, snap, log=lambda m: print(m, flush=True))
            break
        except sqlite3.OperationalError as e:
            print(f"ADER Roaming : base occupée au démarrage ({e}), nouvel essai dans 5 s", flush=True)
            time.sleep(5)
    else:
        raise RuntimeError("base de données occupée depuis plus de 2 minutes au démarrage")
    tickets.roaming = roaming
    roaming.async_writes = True  # la réponse aux routeurs n'attend jamais la base

    def kick(zid, code):
        z = next((z for z in cfg.get("zones", []) if z["id"] == zid), None)
        if not z or not (z.get("router") or {}).get("host"):
            return
        with open_router(z) as api:
            for a in api.query("/ip/hotspot/active/print", f"?user={code}"):
                api.query("/ip/hotspot/active/remove", f"=.id={a['.id']}")
    roaming.kicker = kick

    def kick_many(zid, names):
        """Une seule connexion au routeur, une seule liste des connectés, pour couper tous les noms donnés."""
        z = next((z for z in cfg.get("zones", []) if z["id"] == zid), None)
        if not z or not (z.get("router") or {}).get("host"):
            return
        want = {str(n).upper() for n in names}
        with open_router(z, timeout=6) as api:
            for a in api.query("/ip/hotspot/active/print", "=.proplist=.id,user"):
                if str(a.get("user", "")).upper() in want:
                    api.query("/ip/hotspot/active/remove", f"=.id={a['.id']}")
    roaming.kicker_many = kick_many

    parent = os.getppid()
    mtime = [Path(config_path).stat().st_mtime if Path(config_path).exists() else 0]

    def sync():
        n = 0
        while True:
            time.sleep(2)
            n += 1
            try:  # ce fil ne doit JAMAIS s'arrêter : il donne signe de vie et surveille le reste
                sync_once(n)
            except Exception as e:
                print(f"ADER Roaming : suivi {e!r}", flush=True)

    def copy(fn, fallback):
        for _ in range(5):  # un autre fil peut modifier la liste pendant la copie : on réessaie
            try:
                return fn()
            except RuntimeError:
                time.sleep(0.01)
        return fallback

    def sync_once(n):
        if True:
            if os.getppid() != parent:  # le programme principal s'est arrêté : on s'arrête aussi
                os._exit(0)
            # ce que le programme principal nous dit
            main = _read_json(data_dir / MAIN_STATE) or {}
            roaming.shared.clear()
            roaming.shared.update(main.get("shared") or {})
            snap.state = main.get("state") or {}
            # configuration modifiée (nouveau routeur, mot de passe…) : relue
            if n % 15 == 0:
                try:
                    m = Path(config_path).stat().st_mtime
                    if m != mtime[0]:
                        mtime[0] = m
                        new = load_config()
                        cfg.clear()
                        cfg.update(new)
                        roaming._forget_cache()
                except Exception as e:
                    print(f"ADER Roaming : configuration non relue ({e})", flush=True)
            # coincé (des demandes attendent, rien n'est traité depuis 60 s) : on s'arrête, le principal relance aussitôt
            if roaming.stuck():
                print("ADER Roaming : programme coincé (aucune demande traitée depuis 60 s) : redémarrage", flush=True)
                os._exit(4)
            # ce que nous disons au programme principal
            try:
                _write_json(data_dir / CHILD_STATE, {
                    "at": int(time.time()), "pid": os.getpid(), "listening": roaming.listening,
                    "seen": copy(lambda: dict(roaming.seen), {}),
                    "ignored": copy(lambda: {k: list(v) for k, v in list(roaming.ignored.items())}, {}),
                    "journal": copy(lambda: [list(x) for x in list(roaming.journal)], []),
                    "flow": copy(lambda: {k: dict(v) for k, v in list(roaming.flow.items())}, {}),
                    "hot": copy(lambda: (collections.Counter(dict(hot["now"])) + collections.Counter(dict(hot["prev"]))).most_common(8), []),
                    "threads": threading.active_count(), "writes": roaming._writes.qsize()})
            except OSError as e:
                print(f"ADER Roaming : état non écrit ({e})", flush=True)

    hot = {"now": collections.Counter(), "prev": collections.Counter(), "since": time.time()}

    def sampler():
        """Où le programme Roaming passe son temps (affiché dans « État du serveur ») : 2 coups d'œil par seconde."""
        idle = ("recvfrom", "sleep", "get", "_worker", "wait", "select")
        me = threading.get_ident()
        while True:
            time.sleep(0.5)
            try:
                for tid, f in sys._current_frames().items():
                    if tid == me:
                        continue
                    top = f
                    while f and "/app/" not in f.f_code.co_filename:
                        f = f.f_back
                    if f is None or (top.f_code.co_name in idle and f.f_code.co_name in ("_loop", "_writer", "_kicker_loop", "loop", "sync", "_worker")):
                        continue
                    if top is f and f.f_code.co_name in ("_loop", "_writer", "_kicker_loop", "loop", "sync", "sampler", "run"):
                        continue
                    # chemin complet (3 étapes du programme) : qui appelle qui, pour savoir QUI attend
                    chain, g = [], f
                    while g and len(chain) < 3:
                        if "/app/" in g.f_code.co_filename:
                            chain.append(f"{g.f_code.co_filename.rsplit('/', 1)[-1].replace('.py', '')}:{g.f_code.co_name}:{g.f_lineno}")
                        g = g.f_back
                    hot["now"][" < ".join(chain) + f" ({top.f_code.co_name}) [{threading._active.get(tid).name if threading._active.get(tid) else '?'}]"] += 1
                if time.time() - hot["since"] > 60:
                    hot["prev"], hot["now"], hot["since"] = hot["now"], collections.Counter(), time.time()
            except Exception:
                pass

    roaming.start()
    threading.Thread(target=sampler, daemon=True, name="roaming-sampler").start()
    print("ADER Roaming : programme séparé démarré", flush=True)
    threading.Thread(target=sync, daemon=True, name="roaming-sync").start()
    while True:
        time.sleep(3600)


# --- côté programme principal ----------------------------------------------------------------
class RoamingProcess:
    """Lance le programme Roaming, le relance s'il s'arrête, et échange l'état avec lui."""

    def __init__(self, roaming, collector, data_dir, log=print):
        self.roaming = roaming
        self.collector = collector
        self.data_dir = Path(data_dir)
        self.log = log
        self.proc = None
        self.restarts = 0

    def _spawn(self):
        env = dict(os.environ)
        root = Path(__file__).resolve().parent.parent
        self._started = time.time()
        self.proc = subprocess.Popen([sys.executable, "-m", "app", "radius"], cwd=str(root), env=env)
        self.log(f"ADER Roaming : programme séparé lancé (n° {self.proc.pid})")

    def start(self):
        self._spawn()
        threading.Thread(target=self._watch, daemon=True, name="roaming-watch").start()

    def _watch(self):
        while True:
            time.sleep(2)
            # vivant mais muet depuis 90 s (bloqué) : arrêté, puis relancé juste en dessous
            child_at = (_read_json(self.data_dir / CHILD_STATE) or {}).get("at") or 0
            if self.proc and self.proc.poll() is None and time.time() - self._started > 90 and time.time() - child_at > 90:
                self.log("ADER Roaming : programme muet depuis 90 s : relancé")
                self.proc.kill()
                try:
                    self.proc.wait(10)
                except Exception:
                    pass
            if self.proc and self.proc.poll() is not None:
                self.restarts += 1
                self.log(f"ADER Roaming : programme séparé arrêté (code {self.proc.returncode}), relancé")
                time.sleep(3)
                self._spawn()
            # ce que le programme Roaming doit savoir
            try:
                snap = self.collector.snapshot() if self.collector else {}
                _write_json(self.data_dir / MAIN_STATE, {
                    "shared": self.roaming.shared,
                    "state": {zid: {"identity": (s or {}).get("identity")} for zid, s in snap.items()}})
            except Exception as e:
                self.log(f"ADER Roaming : état non écrit ({e})")
            # ce que le programme Roaming a vu (journal, dernier contact) : pour l'écran Roaming
            child = _read_json(self.data_dir / CHILD_STATE)
            if child:
                r = self.roaming
                r.seen.update({k: v for k, v in (child.get("seen") or {}).items()})
                r.ignored.clear()
                r.ignored.update({k: tuple(v) for k, v in (child.get("ignored") or {}).items()})
                j = child.get("journal") or []
                r.journal.clear()
                r.journal.extend(tuple(x) for x in j)
                r.listening.update(child.get("listening") or {})
