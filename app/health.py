"""Surveillance de la santé du serveur, jour et nuit, même quand personne n'ouvre l'application.

Toutes les minutes : relevés des routeurs, programme ADER Roaming, pages lentes, place sur le disque.
Un problème vu deux fois de suite : message Telegram « ⚠️ ». Quand il disparaît : « ✅ ».
Juste après une mise à jour (15 min), un problème grave (relevés ou Roaming arrêtés) fait
revenir automatiquement à la version d'avant (rollback.py).
"""
import os
import shutil
import threading
import time

from . import rollback
from .radiusd import CHILD_STATE, _read_json

EVERY = 60
LIVE_PATHS = ("/check", "/test")
LABELS = {"releves-secours": "programme des relevés", "releves": "relevés des routeurs",
          "roaming": "programme ADER Roaming", "lent": "vitesse de l'application", "disque": "place sur le disque"}  # boutons qui attendent les routeurs en direct : lenteur normale


class Health:
    def __init__(self, app, collector, roaming_proc, guard, data_dir, log=print):
        self.app = app
        self.collector = collector
        self.roaming_proc = roaming_proc
        self.guard = guard
        self.data_dir = data_dir
        self.log = log
        self.started = time.time()
        self.seen = {}      # problème -> nombre de vérifications de suite où il est présent
        self.alerted = {}   # problème -> message envoyé
        self._roam_restarts = []  # heures des relances du programme Roaming
        self._last_restarts = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="health").start()

    def _loop(self):
        time.sleep(30)
        self._send_rollback_note()
        while True:
            try:
                self.check()
            except Exception as e:  # la surveillance ne doit jamais faire tomber le serveur
                self.log(f"[santé] {e!r}")
            time.sleep(EVERY)

    def _send(self, text):
        try:
            if self.guard:
                self.guard.notify(text)
        except Exception as e:
            self.log(f"[santé] message non envoyé ({e!r})")

    def _send_rollback_note(self):
        p = self.data_dir / rollback.NOTE
        note = _read_json(p)
        if note:
            self._send(f"↩️ <b>ADER LINK</b> : la mise à jour {note.get('bad')} a été annulée automatiquement "
                       f"({note.get('reason')}). La version {note.get('back')} a été remise : tout remarche.")
            try:
                p.unlink()
            except OSError:
                pass

    # --- ce qui est vérifié ---------------------------------------------------------------
    def problems(self):
        """{clé: (grave, message)}"""
        now = time.time()
        up = now - self.started
        out = {}
        c = self.collector
        if getattr(c, "mode", None) == "principal":
            err = (getattr(c, "last_error", "") or "")[-200:]
            out["releves-secours"] = (True, "le programme des relevés s'arrête : relevés faits en secours par le "
                                            "programme principal (l'application peut être plus lente)."
                                            + (f"\nErreur : {err}" if err else ""))
        lc = c.last_cycle if c else None
        if up > 300 and (not lc or now - lc[0] > 300):
            mins = int((now - lc[0]) / 60) if lc else int(up / 60)
            out["releves"] = (True, f"aucun relevé des routeurs depuis {mins} minutes "
                                    "(état des routeurs et alertes hors ligne arrêtés).")
        rp = self.roaming_proc
        if rp:
            if rp.restarts != self._last_restarts:
                self._roam_restarts += [now] * (rp.restarts - self._last_restarts)
                self._last_restarts = rp.restarts
            self._roam_restarts = [t for t in self._roam_restarts if now - t < 600]
            child = _read_json(self.data_dir / CHILD_STATE) or {}
            if up > 120 and now - (child.get("at") or 0) > 120:
                out["roaming"] = (True, "le programme ADER Roaming ne répond plus : les tickets Roaming "
                                        "peuvent être refusés.")
            elif len(self._roam_restarts) >= 3:
                out["roaming"] = (True, f"le programme ADER Roaming s'est arrêté {len(self._roam_restarts)} fois "
                                        "en 10 minutes.")
        if up > 600 and self.app is not None:
            # sans les actions qui attendent un routeur (création de lot, Vérifier, Tester…) : lenteur normale
            slow = [s for s in list(self.app.slow) if now - s[0] < 600 and not s[1].endswith(LIVE_PATHS)
                    and (len(s) < 4 or s[3] != "POST")]
            if len(slow) >= 5:
                worst = max(slow, key=lambda s: s[2])
                out["lent"] = (False, f"application lente : {len(slow)} pages lentes en 10 minutes "
                                      f"(la plus lente : {worst[1]}, {worst[2]} s).")
        try:
            free = shutil.disk_usage(self.data_dir).free
            if free < 1e9:
                out["disque"] = (False, f"plus que {free / 1e9:.1f} Go de place libre sur le disque du serveur.")
        except OSError:
            pass
        return out

    def check(self):
        now = time.time()
        found = self.problems()
        for k in list(self.seen):
            if k not in found:
                del self.seen[k]
        for k in found:
            self.seen[k] = self.seen.get(k, 0) + 1
        confirmed = {k: v for k, v in found.items() if self.seen[k] >= 2}  # vu 2 fois de suite : pas un hasard
        # juste après une mise à jour : un problème grave fait revenir à la version d'avant
        p = rollback.pending(self.data_dir)
        if p:
            graves = [msg for g, msg in confirmed.values() if g]
            if graves and now - self.started > 180:
                if rollback.rollback(graves[0], self.data_dir):
                    os._exit(3)  # relancé par systemd avec l'ancienne version ; message envoyé au démarrage
            elif now - p.get("at", now) > rollback.WATCH_S and now - self.started > rollback.WATCH_S:
                rollback.confirm(self.data_dir)
                self.log(f"[mise à jour] version {p.get('to')} gardée : 15 minutes sans problème")
        # messages Telegram : début et fin de chaque problème
        for k, (grave, msg) in confirmed.items():
            if k not in self.alerted:
                self.alerted[k] = msg
                self._send(f"⚠️ <b>ADER LINK</b> : {msg}")
        for k in list(self.alerted):
            if k not in found:
                self.alerted.pop(k)
                self._send(f"✅ <b>ADER LINK</b> : revenu à la normale — {LABELS.get(k, k)}.")
