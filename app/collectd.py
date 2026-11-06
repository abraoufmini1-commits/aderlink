"""Relevés des routeurs dans un programme à part.

Trois programmes tournent sur le serveur, chacun sur son processeur :
- le principal : seulement les pages de l'application (toujours rapides) ;
- ADER Roaming : les demandes de connexion des tickets Roaming (radiusd.py) ;
- celui-ci : les relevés des routeurs toutes les 30 s, le suivi et le nettoyage des tickets sur les routeurs,
  le réglage automatique d'ADER Roaming sur chaque routeur, et le nettoyage de l'historique la nuit.

Ils partagent la même base (SQLite, mode WAL). Ce programme écrit l'état des routeurs dans
data/collect-state.json (lu par les pages). Le principal demande un relevé immédiat (routeur
qui vient d'être installé) en touchant data/collect-poll.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .radiusd import _read_json, _write_json

STATE = "collect-state.json"
POLL_REQ = "collect-poll"


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


# --- côté programme des relevés ------------------------------------------------------------
def run(config_path, data_dir, load_config):
    from .collector import Collector, Store
    from .roaming import Roaming
    from .tickets import Tickets

    data_dir = Path(data_dir)
    log = lambda m: print(m, flush=True)
    cfg = load_config()
    store = Store(str(data_dir / "aderlink.db"))
    collector = Collector(cfg, store)
    tickets = Tickets(cfg, store)
    roaming = Roaming(cfg, store, tickets, collector, log=log)  # seulement pour le réglage des routeurs (ensure)
    tickets.roaming = roaming

    def wg():
        w = cfg.get("wireguard") or {}
        return w if w.get("enabled") and w.get("pubkey") and w.get("endpoint") else None
    roaming.wg_conf = wg
    collector.ticket_hook = tickets.on_poll

    parent = os.getppid()
    seen = {"config": _mtime(config_path), "poll": _mtime(data_dir / POLL_REQ)}

    def write_state():
        with collector.lock:
            state = {k: dict(v) for k, v in collector.state.items()}
        _write_json(data_dir / STATE, {
            "at": int(time.time()), "pid": os.getpid(), "state": state,
            "agg": [list(v) for v in list(collector._agg.values())],
            "last_cycle": list(collector.last_cycle) if collector.last_cycle else None,
            "shared": dict(roaming.shared)})

    def poll_now():
        try:
            collector.poll_all()
            write_state()
        except Exception as e:
            log(f"[collecte] relevé demandé : {e!r}")

    def sync():
        last = None
        n = 0
        while True:
            time.sleep(1)
            n += 1
            if os.getppid() != parent:  # le programme principal s'est arrêté : on s'arrête aussi
                os._exit(0)
            # configuration modifiée (routeur ajouté, mot de passe…) : relue
            m = _mtime(config_path)
            if m != seen["config"]:
                seen["config"] = m
                try:
                    new = load_config()
                    cfg.clear()
                    cfg.update(new)
                    roaming._forget_cache()
                except Exception as e:
                    log(f"[collecte] configuration non relue ({e})")
            # relevé immédiat demandé par le principal (routeur qui vient d'être installé)
            m = _mtime(data_dir / POLL_REQ)
            if m != seen["poll"]:
                seen["poll"] = m
                threading.Thread(target=poll_now, daemon=True).start()
            # état des routeurs : après chaque relevé, et au moins toutes les 10 s
            if collector.last_cycle != last or n % 10 == 0:
                last = collector.last_cycle
                try:
                    write_state()
                except (OSError, TypeError, ValueError) as e:
                    log(f"[collecte] état non écrit ({e})")

    collector.poll_all()  # premier état
    write_state()
    collector.start()
    log("[collecte] programme des relevés démarré")
    threading.Thread(target=sync, daemon=True, name="collect-sync").start()
    while True:
        time.sleep(3600)


# --- côté programme principal --------------------------------------------------------------
class RemoteCollector:
    """Remplace le collecteur dans le programme principal : lance le programme des relevés,
    le relance s'il s'arrête, et lit l'état des routeurs qu'il écrit."""

    def __init__(self, config, data_dir, log=print):
        self.config = config
        self.data_dir = Path(data_dir)
        self.log = log
        self.poll_seconds = max(10, int(config.get("poll_seconds", 30)))
        self.lock = threading.Lock()
        self.state = {}
        self._agg = {}
        self.last_cycle = None
        self.roaming = None  # pour recevoir les routeurs qui ont un autre système de tickets
        self.proc = None
        self.restarts = 0
        self.updated = None  # heure du dernier état écrit par le programme des relevés
        self._mtime = None
        self._refresh()      # dernier état connu (avant le redémarrage) en attendant le premier relevé

    def _refresh(self):
        p = self.data_dir / STATE
        m = _mtime(p)
        if m is None or m == self._mtime:
            return
        d = _read_json(p)
        if not d:
            return
        self._mtime = m
        with self.lock:
            self.state = d.get("state") or {}
            self._agg = {v[1]: v for v in (d.get("agg") or []) if len(v) >= 6}
            lc = d.get("last_cycle")
            self.last_cycle = tuple(lc) if lc else None
            self.updated = d.get("at")
        if self.roaming is not None:
            self.roaming.shared.update(d.get("shared") or {})

    def snapshot(self):
        self._refresh()
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}

    def poll_all(self):
        """Relevé immédiat : demandé au programme des relevés."""
        p = self.data_dir / POLL_REQ
        try:
            p.write_text(str(time.time()))
        except OSError as e:
            self.log(f"[collecte] relevé immédiat non demandé ({e})")

    def add_tickets(self, zone_id, n):
        with self.lock:
            st = self.state.get(zone_id)
            if st and st.get("tickets") is not None:
                st["tickets"] += n

    def _spawn(self):
        root = Path(__file__).resolve().parent.parent
        self._started = time.time()
        self.proc = subprocess.Popen([sys.executable, "-m", "app", "collect"], cwd=str(root), env=dict(os.environ))
        self.log(f"[collecte] programme des relevés lancé (n° {self.proc.pid})")

    def start(self):
        self._spawn()
        threading.Thread(target=self._watch, daemon=True, name="collect-watch").start()

    def _watch(self):
        while True:
            time.sleep(2)
            # bloqué (vivant mais n'écrit plus rien depuis 3 minutes) : arrêté, puis relancé ci-dessous
            if self.proc and self.proc.poll() is None and self.updated and time.time() - self.updated > 180 \
                    and time.time() - self._started > 180:
                self.log("[collecte] programme des relevés bloqué : relancé")
                self.proc.kill()
                self.proc.wait(10)
            if self.proc and self.proc.poll() is not None:
                self.restarts += 1
                self.log(f"[collecte] programme des relevés arrêté (code {self.proc.returncode}), relancé")
                time.sleep(3)
                self._spawn()
            try:
                self._refresh()
            except Exception as e:
                self.log(f"[collecte] état non lu ({e})")
