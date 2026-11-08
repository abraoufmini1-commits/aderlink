"""Retour automatique à la version d'avant quand une mise à jour se passe mal.

À chaque mise à jour, l'ancienne version est gardée dans data/previous/ et un fichier
data/update-pending.json est écrit. Pendant les 15 minutes qui suivent :
- si le serveur n'arrive pas à démarrer (3 démarrages ratés), ou
- si la surveillance (health.py) voit les relevés des routeurs ou ADER Roaming arrêtés,
l'ancienne version est remise, la nouvelle est mise de côté (plus jamais installée
automatiquement, jusqu'à la version suivante) et un message Telegram explique pourquoi.

Ce module n'utilise que la bibliothèque standard : il doit marcher même si la nouvelle
version a un fichier cassé.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PENDING = "update-pending.json"
BAD = "update-bad.json"
PREV = "previous"
NOTE = "rollback-note.json"
WATCH_S = 15 * 60       # durée de surveillance après une mise à jour
MAX_STARTS = 3          # démarrages permis pendant cette surveillance


def data_dir():
    return Path(os.environ.get("ADERLINK_DATA", ROOT / "data"))


def _read(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(p, obj):
    p = Path(p)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, p)


def backup(names, ddir=None):
    """Avant une mise à jour : copie de la version actuelle dans data/previous/."""
    ddir = Path(ddir or data_dir())
    prev = ddir / PREV
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir(parents=True)
    for name in names:
        s = ROOT / name
        if s.is_dir():
            shutil.copytree(s, prev / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif s.is_file():
            shutil.copy2(s, prev / name)


def mark_pending(frm, to, ddir=None):
    _write(Path(ddir or data_dir()) / PENDING, {"from": frm, "to": to, "at": int(time.time()), "starts": 0})


def pending(ddir=None):
    return _read(Path(ddir or data_dir()) / PENDING)


def confirm(ddir=None):
    """15 minutes sans problème après la mise à jour : elle est gardée."""
    try:
        (Path(ddir or data_dir()) / PENDING).unlink()
    except OSError:
        pass


def bad_versions(ddir=None):
    return set(_read(Path(ddir or data_dir()) / BAD, []) or [])


def rollback(reason, ddir=None):
    """Remet la version gardée dans data/previous/. Retourne True si c'est fait."""
    ddir = Path(ddir or data_dir())
    p = pending(ddir) or {}
    prev = ddir / PREV
    if not (prev / "app" / "__main__.py").is_file():
        confirm(ddir)
        return False
    for item in prev.iterdir():
        d = ROOT / item.name
        if item.is_dir():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(item, d)
        else:
            shutil.copy2(item, d)
    bad = bad_versions(ddir)
    if p.get("to"):
        bad.add(p["to"])
    _write(ddir / BAD, sorted(bad))
    _write(ddir / NOTE, {"bad": p.get("to"), "back": p.get("from"), "reason": reason, "at": int(time.time())})
    confirm(ddir)
    print(f"[mise à jour] version {p.get('to')} annulée ({reason}) : retour à {p.get('from')}", flush=True)
    return True


def early():
    """Tout au début du démarrage du serveur : compte les démarrages après une mise à jour.
    Trop de démarrages ratés : retour à l'ancienne version, puis redémarrage."""
    if "serve" not in sys.argv[1:2]:
        return
    ddir = data_dir()
    p = pending(ddir)
    if not p:
        return
    if time.time() - p.get("at", 0) > WATCH_S * 2:
        confirm(ddir)
        return
    p["starts"] = p.get("starts", 0) + 1
    _write(ddir / PENDING, p)
    if p["starts"] > MAX_STARTS:
        if rollback("le serveur n'arrivait plus à démarrer", ddir):
            os._exit(3)  # systemd relance le serveur avec l'ancienne version
