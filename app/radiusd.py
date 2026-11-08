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

    def kick(zid, code):
        z = next((z for z in cfg.get("zones", []) if z["id"] == zid), None)
        if not z or not (z.get("router") or {}).get("host"):
            return
        with open_router(z) as api:
            for a in api.query("/ip/hotspot/active/print", f"?user={code}"):
                api.query("/ip/hotspot/active/remove", f"=.id={a['.id']}")
    roaming.kicker = kick

    parent = os.getppid()
    mtime = [Path(config_path).stat().st_mtime if Path(config_path).exists() else 0]

    def sync():
        n = 0
        while True:
            time.sleep(2)
            n += 1
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
            # ce que nous disons au programme principal
            try:
                _write_json(data_dir / CHILD_STATE, {
                    "at": int(time.time()), "pid": os.getpid(), "listening": roaming.listening,
                    "seen": roaming.seen, "ignored": {k: list(v) for k, v in roaming.ignored.items()},
                    "journal": [list(x) for x in roaming.journal]})
            except OSError as e:
                print(f"ADER Roaming : état non écrit ({e})", flush=True)

    roaming.start()
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
        self.proc = subprocess.Popen([sys.executable, "-m", "app", "radius"], cwd=str(root), env=env)
        self.log(f"ADER Roaming : programme séparé lancé (n° {self.proc.pid})")

    def start(self):
        self._spawn()
        threading.Thread(target=self._watch, daemon=True, name="roaming-watch").start()

    def _watch(self):
        while True:
            time.sleep(2)
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
