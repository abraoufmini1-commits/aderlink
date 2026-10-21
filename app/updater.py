"""Mise à jour automatique d'ADER LINK depuis un dépôt GitHub.

Toutes les 10 minutes, l'application lit le fichier VERSION du dépôt. S'il a changé,
elle télécharge le code, remplace ses fichiers (jamais config.json ni data/),
puis redémarre (code de sortie 3 : ADER-LINK.bat et systemd la relancent).
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
RESTART_CODE = 3
KEEP = {"config.json", "data", ".git"}           # jamais remplacés
COPY = ("app", "deploy", "tests", "README.md", "VERSION", "config.example.json")  # pas le .bat : il tourne pendant la mise à jour


def local_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "ADERLINK-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Updater:
    def __init__(self, config, on_restart=None):
        self.config = config
        self.on_restart = on_restart or (lambda: os._exit(RESTART_CODE))
        self.state = {"current": local_version(), "latest": None, "checked_at": None,
                      "status": "idle", "message": "", "updated_at": None}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def repo(self):
        return (self.config.get("update") or {}).get("repo", "").strip()

    @property
    def branch(self):
        return (self.config.get("update") or {}).get("branch", "main").strip() or "main"

    def info(self):
        with self.lock:
            return dict(self.state, repo=self.repo, branch=self.branch,
                        auto=bool((self.config.get("update") or {}).get("auto", True)))

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="aderlink-updater").start()

    def _loop(self):
        time.sleep(20)
        while not self._stop.is_set():
            if self.repo and (self.config.get("update") or {}).get("auto", True):
                try:
                    self.check(install=True)
                except Exception as e:  # jamais bloquant
                    self._set(status="error", message=str(e))
            self._stop.wait(600)

    def check(self, install=False):
        if not self.repo:
            self._set(status="idle", message="Aucun dépôt configuré")
            return self.info()
        self._set(status="checking", message="Vérification…")
        try:
            latest = _get(f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/VERSION?t={int(time.time())}").decode().strip()
        except Exception as e:
            self._set(status="error", message=f"Dépôt injoignable : {e}", checked_at=int(time.time()))
            return self.info()
        self._set(latest=latest, checked_at=int(time.time()))
        if latest == self.state["current"]:
            self._set(status="uptodate", message="À jour")
            return self.info()
        if not install:
            self._set(status="available", message=f"Version {latest} disponible")
            return self.info()
        self.install(latest)
        return self.info()

    def install(self, latest):
        self._set(status="installing", message=f"Installation de la version {latest}…")
        data = _get(f"https://codeload.github.com/{self.repo}/zip/refs/heads/{self.branch}", timeout=120)
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for member in zf.namelist():  # protection contre les chemins dangereux
                    if member.startswith("/") or ".." in Path(member).parts:
                        raise RuntimeError("Archive invalide")
                zf.extractall(tmp)
            top = next(Path(tmp).iterdir())
            src = top / "aderlink" if (top / "aderlink" / "app").is_dir() else top
            if not (src / "app" / "__main__.py").is_file() or not (src / "VERSION").is_file():
                raise RuntimeError("Le dépôt ne contient pas ADER LINK")
            for name in COPY:
                s, d = src / name, ROOT / name
                if name in KEEP or not s.exists():
                    continue
                if s.is_dir():
                    # copie fichier par fichier (Windows refuse de renommer un dossier en cours d'usage)
                    shutil.copytree(s, d, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(s, d)
        self._set(status="restarting", message=f"Version {latest} installée, redémarrage…",
                  current=latest, updated_at=int(time.time()))
        threading.Timer(1.5, self.on_restart).start()
