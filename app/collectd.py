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
ERRORS = "collect-errors.log"


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
    import sqlite3
    for attempt in range(24):  # base occupée au démarrage : on attend et on réessaie au lieu de s'arrêter
        try:
            store = Store(str(data_dir / "aderlink.db"))
            collector = Collector(cfg, store)
            tickets = Tickets(cfg, store, maintenance=False)
            tickets.background = True  # entretien des routeurs à côté du relevé
            roaming = Roaming(cfg, store, tickets, collector, log=log)  # seulement pour le réglage des routeurs
            break
        except sqlite3.OperationalError as e:
            log(f"[collecte] base occupée au démarrage ({e}), nouvel essai dans 5 s")
            time.sleep(5)
    else:
        raise RuntimeError("base de données occupée depuis plus de 2 minutes au démarrage")
    tickets.roaming = roaming

    def wg():
        w = cfg.get("wireguard") or {}
        return w if w.get("enabled") and w.get("pubkey") and w.get("endpoint") else None
    roaming.wg_conf = wg
    collector.ticket_hook = tickets.on_poll

    parent = os.getppid()
    started = time.time()
    seen = {"config": _mtime(config_path), "poll": _mtime(data_dir / POLL_REQ)}

    def copy(fn, fallback):
        for _ in range(5):  # un autre fil peut modifier la liste pendant la copie : on réessaie
            try:
                return fn()
            except RuntimeError:
                time.sleep(0.01)
        return fallback

    def write_state():
        if collector.last_cycle is None:
            # premier relevé pas encore fini : on garde le dernier état connu, on donne juste signe de vie
            old = _read_json(data_dir / STATE) or {}
            old.update({"at": int(time.time()), "pid": os.getpid()})
            _write_json(data_dir / STATE, old)
            return
        with collector.lock:
            state = {k: dict(v) for k, v in collector.state.items()}
        _write_json(data_dir / STATE, {
            "at": int(time.time()), "pid": os.getpid(), "state": state,
            "agg": copy(lambda: [list(v) for v in list(collector._agg.values())], []),
            "last_cycle": list(collector.last_cycle) if collector.last_cycle else None,
            "shared": copy(lambda: dict(roaming.shared), {})})

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
            try:
                last = step(n, last)
            except Exception as e:  # ce fil ne doit jamais s'arrêter
                log(f"[collecte] {e!r}")

    def step(n, last):
        if True:
            if os.getppid() != parent:  # le programme principal s'est arrêté : on s'arrête aussi
                os._exit(0)
            # aucun relevé complet depuis 15 minutes (un routeur coincé sur la ligne…) : on repart proprement
            done = collector.last_cycle[0] if collector.last_cycle else started
            if time.time() - done > 900:
                log("[collecte] aucun relevé complet depuis 15 min : redémarrage du programme des relevés")
                os._exit(3)
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
            # état des routeurs (et signe de vie) : après chaque relevé, et au moins toutes les 10 s,
            # même pendant un relevé long. Aucune erreur ne doit arrêter ce fil.
            if collector.last_cycle != last or n % 10 == 0:
                last = collector.last_cycle
                try:
                    write_state()
                except Exception as e:
                    log(f"[collecte] état non écrit ({e!r})")
            return last

    # le signe de vie part AVANT le premier relevé : un premier relevé long ne fait plus croire à un blocage
    # (avant : relancé toutes les 3 minutes, en boucle, sans jamais finir son premier relevé)
    threading.Thread(target=sync, daemon=True, name="collect-sync").start()
    log("[collecte] programme des relevés démarré")
    collector.start()
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
        self._r_agg = {}
        self._r_last_cycle = None
        self.roaming = None  # pour recevoir les routeurs qui ont un autre système de tickets
        self.proc = None
        self.restarts = 0
        self.updated = None  # heure du dernier état écrit par le programme des relevés
        self.local = None      # relevés repris par le programme principal si le séparé ne tient pas
        self.fallback = None   # fonction qui crée ce collecteur de secours
        self.quick_fails = 0
        self.last_error = None
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
            self._r_agg = {v[1]: v for v in (d.get("agg") or []) if len(v) >= 6}
            lc = d.get("last_cycle")
            self._r_last_cycle = tuple(lc) if lc else None
            self.updated = d.get("at")
        if self.roaming is not None:
            self.roaming.shared.update(d.get("shared") or {})

    def snapshot(self):
        if self.local:
            return self.local.snapshot()
        self._refresh()
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}

    def poll_all(self):
        """Relevé immédiat : demandé au programme des relevés."""
        if self.local:
            return self.local.poll_all()
        p = self.data_dir / POLL_REQ
        try:
            p.write_text(str(time.time()))
        except OSError as e:
            self.log(f"[collecte] relevé immédiat non demandé ({e})")

    def add_tickets(self, zone_id, n):
        if self.local:
            return self.local.add_tickets(zone_id, n)
        with self.lock:
            st = self.state.get(zone_id)
            if st and st.get("tickets") is not None:
                st["tickets"] += n

    def _spawn(self):
        root = Path(__file__).resolve().parent.parent
        self._started = time.time()
        # les messages d'erreur du programme sont gardés dans un fichier, affichés dans « État du serveur »
        err = open(self.data_dir / ERRORS, "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "app", "collect"], cwd=str(root), env=dict(os.environ),
                                     stderr=err)
        err.close()
        self.log(f"[collecte] programme des relevés lancé (n° {self.proc.pid})")

    def _save_error(self):
        try:
            lines = [l.rstrip() for l in (self.data_dir / ERRORS).read_text(errors="replace").splitlines() if l.strip()]
            self.last_error = " | ".join(lines[-6:])[-900:] or f"arrêt sans message (code {self.proc.returncode})"
        except OSError:
            self.last_error = f"arrêt sans message (code {self.proc.returncode})"
        self.log(f"[collecte] arrêt : {self.last_error}")

    @property
    def last_cycle(self):
        return self.local.last_cycle if self.local else self._r_last_cycle

    @property
    def _agg(self):
        return self.local._agg if self.local else self._r_agg

    @property
    def mode(self):
        return "principal" if self.local else "séparé"

    def start(self):
        self._spawn()
        threading.Thread(target=self._watch, daemon=True, name="collect-watch").start()

    def _watch(self):
        while True:
            time.sleep(2)
            # bloqué (vivant mais n'écrit plus rien depuis 5 minutes) : arrêté, puis relancé ci-dessous
            if self.proc and self.proc.poll() is None and self.updated and time.time() - self.updated > 300 \
                    and time.time() - self._started > 300:
                self.log("[collecte] programme des relevés bloqué : relancé")
                self.proc.kill()
                self.proc.wait(10)
            if self.proc and self.proc.poll() is not None and time.time() - self._started < 90:
                self.quick_fails += 1
                self._save_error()
                if self.quick_fails >= 3 and self.fallback:
                    # s'arrête dès le démarrage, 3 fois de suite : relevés faits par le programme principal,
                    # pour que les routeurs ne restent jamais sans relevés
                    self.log("[collecte] programme séparé arrêté 3 fois au démarrage : relevés dans le programme principal")
                    self.local = self.fallback()
                    self.local.start()
                    return
            elif self.proc and self.proc.poll() is None and time.time() - self._started > 120:
                self.quick_fails = 0
            if self.proc and self.proc.poll() is not None:
                self.restarts += 1
                self.log(f"[collecte] programme des relevés arrêté (code {self.proc.returncode}), relancé")
                time.sleep(3)
                self._spawn()
            try:
                self._refresh()
            except Exception as e:
                self.log(f"[collecte] état non lu ({e})")
