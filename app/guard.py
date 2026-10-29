"""Sécurité d'ADER LINK : sauvegarde automatique chaque nuit et alertes Telegram.

- Chaque nuit (3 h, heure locale) : une sauvegarde complète dans data/backups, 14 jours gardés,
  et une copie envoyée sur Telegram si c'est relié (copie hors du serveur).
- Alertes Telegram : routeur hors ligne (après 1 heure, pour ignorer les petites coupures),
  routeur de retour, et à minuit le résumé des ventes de la journée qui vient de finir.
"""
import io
import json
import re
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

KEEP_DAYS = 14
OFFLINE_DELAY = 3600   # une alerte seulement si le routeur reste coupé 1 heure (pas de messages pour les petites coupures)
BACKUP_HOUR, SUMMARY_HOUR = 3, 0  # résumé à minuit : la journée entière
NAME_RE = re.compile(r"aderlink-auto-\d{4}-\d{2}-\d{2}\.zip")


class GuardError(Exception):
    pass


def make_backup(config, store, light=False):
    """Sauvegarde complète : config.json + copie cohérente de la base.
    light=True : sans l'historique des graphiques (relevés et journal) — pour Telegram, limité à 50 Mo."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.json", json.dumps(config, ensure_ascii=False, indent=2))
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "aderlink.db"
            dst = sqlite3.connect(copy)
            with store.lock:
                store.db.backup(dst)
            if light:
                for table in ("samples", "events"):
                    try:
                        dst.execute(f"DELETE FROM {table}")
                    except sqlite3.Error:
                        pass
                dst.commit()
                dst.execute("VACUUM")
            dst.close()
            zf.write(copy, "aderlink.db")
        zf.writestr("LISEZMOI.txt", "Sauvegarde ADER LINK. A restaurer dans Parametres > Restaurer une sauvegarde.\n")
    return buf.getvalue()


class Guard:
    def __init__(self, config, store, data_dir=None, save=None, tickets=None, demo=False, log=print):
        self.config = config
        self.store = store
        self.dir = Path(data_dir) / "backups" if data_dir else None
        self.save = save or (lambda c: None)
        self.tickets = tickets
        self.demo = demo
        self.log = log
        self.last_event = int(time.time())
        self.pending = {}   # zone_id -> heure de la coupure, en attente de confirmation
        self.alerted = set()
        self._load_state()
        self.done = {}      # tâches du jour déjà faites : {"backup": "2026-10-23", ...}

    # --- outils ------------------------------------------------------------------------------------
    def offset(self):
        return int(float(self.config.get("utc_offset_hours", 1)) * 3600)

    def today(self, now=None):
        return time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) + self.offset()))

    def tg(self):
        return self.config.setdefault("telegram", {})

    def _api(self, method, data=None, files=None, timeout=20):
        token = self.tg().get("token")
        if not token:
            raise GuardError("Telegram n'est pas relié")
        url = f"https://api.telegram.org/bot{token}/{method}"
        if files:
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in (data or {}).items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
            for k, (fname, content) in files.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                         "Content-Type: application/zip\r\n\r\n").encode() + content + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(url, urllib.parse.urlencode(data or {}).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read())
        except Exception as e:  # réseau, jeton refusé…
            raise GuardError(f"Telegram ne répond pas ({e})") from e
        if not res.get("ok"):
            raise GuardError(res.get("description") or "Telegram a refusé")
        return res["result"]

    def send(self, text):
        t = self.tg()
        if self.demo or not (t.get("token") and t.get("chat_id")):
            return False
        try:
            self._api("sendMessage", {"chat_id": t["chat_id"], "text": text, "parse_mode": "HTML",
                                      "disable_web_page_preview": "true"})
            return True
        except GuardError as e:
            self.log(f"Alerte Telegram non envoyée : {e}")
            return False

    # --- Telegram : réglage en 2 temps (jeton du bot, puis « /start » envoyé au bot) ----------------
    def set_token(self, token):
        token = (token or "").strip()
        # on accepte aussi le message entier de BotFather collé tel quel : on y retrouve le jeton
        found = re.search(r"\d{5,12}:[A-Za-z0-9_-]{30,50}", token)
        token = found.group(0) if found else token
        if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,50}", token):
            raise GuardError("Ce jeton ne ressemble pas à celui de BotFather (ex : 123456789:AAH…)")
        old = dict(self.tg())
        self.config["telegram"] = {"token": token}
        try:
            me = self._api("getMe")
        except GuardError:
            self.config["telegram"] = old
            raise
        self.config["telegram"]["bot"] = me.get("username", "")
        self.save(self.config)
        return self.status()

    def link(self):
        """Trouve la conversation où vous avez écrit au bot, et la retient."""
        updates = self._api("getUpdates", {"timeout": 0})
        chats = [u.get("message", {}).get("chat") for u in updates if u.get("message")]
        chats = [c for c in chats if c and c.get("type") == "private"]
        if not chats:
            raise GuardError("Ouvrez votre bot dans Telegram, appuyez sur DÉMARRER (ou écrivez /start), puis réessayez.")
        c = chats[-1]
        self.tg()["chat_id"] = c["id"]
        self.tg()["who"] = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
        self.save(self.config)
        self.send("✅ <b>ADER LINK est relié à ce Telegram.</b>\nVous recevrez ici les alertes de vos routeurs, "
                  "le résumé des ventes de la journée à minuit et la sauvegarde de chaque nuit.")
        return self.status()

    def unlink(self):
        self.config["telegram"] = {}
        self.save(self.config)
        return self.status()

    def test(self):
        if not self.send("🔔 Test ADER LINK : les alertes arrivent bien ici."):
            raise GuardError("Message non envoyé : reliez d'abord Telegram")
        return {"ok": True}

    # --- sauvegardes -------------------------------------------------------------------------------
    def backups(self):
        if not self.dir or not self.dir.exists():
            return []
        out = []
        for f in sorted(self.dir.glob("aderlink-auto-*.zip"), reverse=True):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "ts": int(st.st_mtime)})
        return out

    def backup_path(self, name):
        if not self.dir or not NAME_RE.fullmatch(name or ""):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def backup_now(self, send=True):
        if self.demo or not self.dir:
            raise GuardError("Indisponible en mode démonstration")
        self.dir.mkdir(parents=True, exist_ok=True)
        data = make_backup(self.config, self.store)
        name = f"aderlink-auto-{self.today()}.zip"
        (self.dir / name).write_bytes(data)
        for f in sorted(self.dir.glob("aderlink-auto-*.zip"), reverse=True)[KEEP_DAYS:]:
            f.unlink()
        sent = False
        t = self.tg()
        if send and t.get("token") and t.get("chat_id"):
            doc, caption = data, f"Sauvegarde ADER LINK du {self.today()}"
            if len(doc) >= 45_000_000:  # Telegram refuse plus de 50 Mo : on envoie tout sauf l'historique des graphiques
                doc = make_backup(self.config, self.store, light=True)
                caption += " (sans l'historique des graphiques)"
            try:
                if len(doc) >= 45_000_000:
                    raise GuardError("sauvegarde trop grosse pour Telegram")
                self._api("sendDocument", {"chat_id": t["chat_id"], "caption": caption},
                          {"document": (name, doc)}, timeout=180)
                sent = True
            except GuardError as e:
                self.log(f"Sauvegarde non envoyée sur Telegram : {e}")
        return {"name": name, "size": len(data), "telegram": sent}

    # --- alertes -----------------------------------------------------------------------------------
    # Les coupures en cours sont gardées dans la base : une mise à jour (redémarrage) ne fait ni oublier
    # une coupure, ni renvoyer une alerte déjà envoyée.
    def _load_state(self):
        try:
            with self.store.lock:
                self.store.db.execute("CREATE TABLE IF NOT EXISTS guard_state (zone_id TEXT PRIMARY KEY, since INTEGER, alerted INTEGER)")
                self.store.db.commit()
                rows = self.store.db.execute("SELECT zone_id, since, alerted FROM guard_state").fetchall()
                last = self.store.db.execute("SELECT value FROM guard_meta WHERE key = 'last_event'").fetchone() \
                    if self.store.db.execute("SELECT name FROM sqlite_master WHERE name = 'guard_meta'").fetchone() else None
        except Exception:
            return
        for zid, since, alerted in rows:
            if alerted:
                self.alerted.add(zid)
            else:
                self.pending[zid] = since
        if last:
            self.last_event = max(int(last[0]), self.last_event - 6 * 3600)

    def _save_state(self):
        try:
            with self.store.lock:
                db = self.store.db
                db.execute("CREATE TABLE IF NOT EXISTS guard_meta (key TEXT PRIMARY KEY, value TEXT)")
                db.execute("DELETE FROM guard_state")
                db.executemany("INSERT INTO guard_state VALUES (?,?,0)", list(self.pending.items()))
                db.executemany("INSERT INTO guard_state VALUES (?,0,1)", [(z,) for z in self.alerted])
                db.execute("INSERT OR REPLACE INTO guard_meta VALUES ('last_event', ?)", (str(self.last_event),))
                db.commit()
        except Exception:
            pass

    def check_events(self, now=None):
        now = now or int(time.time())
        with self.store.lock:
            rows = self.store.db.execute("SELECT ts, zone_id, kind, message FROM events WHERE ts > ? ORDER BY ts",
                                         (self.last_event,)).fetchall()
        for ts, zid, kind, msg in rows:
            self.last_event = max(self.last_event, ts)
            if kind == "offline" and "Ticket" not in (msg or ""):
                self.pending.setdefault(zid, ts)
            elif kind == "online":
                self.pending.pop(zid, None)
                if zid in self.alerted:
                    self.alerted.discard(zid)
                    self.send(f"🟢 <b>{_esc(self._name(zid))}</b> est de nouveau en ligne.")
        changed = False
        for zid, since in list(self.pending.items()):
            if now - since >= OFFLINE_DELAY:
                changed = True
                self.pending.pop(zid)
                self.alerted.add(zid)
                self.send(f"🔴 <b>{_esc(self._name(zid))}</b> est hors ligne depuis "
                          f"{time.strftime('%H:%M', time.gmtime(since + self.offset()))}.\n"
                          "Vérifiez le courant et la connexion Internet du routeur.")
        if rows or changed:
            self._save_state()

    def _name(self, zid):
        z = next((z for z in self.config.get("zones", []) if z["id"] == zid), None)
        return z["name"] if z else zid

    def summary(self, now=None):
        """Résumé de la journée : envoyé à minuit, il porte sur la journée qui vient de finir (0 h → 24 h)."""
        now = now or int(time.time())
        midnight = (now + self.offset()) // 86400 * 86400 - self.offset()
        start, end = (midnight - 86400, midnight) if SUMMARY_HOUR == 0 else (midnight, now)
        n = amount = 0
        by_zone = []
        if self.tickets:
            try:
                rep = self.tickets.report(start, end, self.offset())
                n, amount = rep["totals"]["tickets"], rep["totals"]["amount"]
                by_zone = [z for z in rep.get("by_zone", []) if z.get("tickets")]
            except Exception:  # base incomplète (tests) : on retombe sur le calcul simple
                sales = [s for s in self.tickets.sales(start)]
                n, amount = sum(x["tickets"] for x in sales), sum(x["amount"] for x in sales)
        nf = lambda x: f"{int(x):,}".replace(",", " ")
        lines = [f"📊 <b>Ventes du {time.strftime('%d/%m', time.gmtime(start + self.offset()))}</b>",
                 f"<b>Total : {nf(amount)} F CFA</b> · {nf(n)} ticket{'s' if n > 1 else ''}"]
        if by_zone:
            lines.append("")
            lines.append("<b>Par routeur :</b>")
            for z in sorted(by_zone, key=lambda z: -z["amount"]):
                lines.append(f"• {_esc(z['label'])} : {nf(z['amount'])} F ({nf(z['tickets'])})")
            silent = len(self.config.get("zones", [])) - len(by_zone)
            if silent > 0:
                lines.append(f"Sans vente : {silent} routeur{'s' if silent > 1 else ''}")
        if self.alerted:
            lines.append("")
            lines.append("🔴 Hors ligne : " + ", ".join(_esc(self._name(z)) for z in self.alerted))
        return "\n".join(lines)

    def send_long(self, text, limit=3800):
        """Telegram limite un message à 4096 caractères : on coupe entre deux lignes."""
        part = ""
        for line in text.split("\n"):
            if part and len(part) + len(line) + 1 > limit:
                self.send(part)
                part = ""
            part += ("\n" if part else "") + line
        if part:
            self.send(part)

    def tick(self, now=None):
        now = now or int(time.time())
        self.check_events(now)
        local_h = time.gmtime(now + self.offset()).tm_hour
        day = self.today(now)
        if local_h == BACKUP_HOUR and self.done.get("backup") != day:
            self.done["backup"] = day
            try:
                self.backup_now()
            except Exception as e:
                self.log(f"Sauvegarde automatique : {e}")
                self.send(f"⚠️ La sauvegarde automatique a échoué : {_esc(e)}")
        if local_h == SUMMARY_HOUR and self.done.get("summary") != day:
            self.done["summary"] = day
            self.send_long(self.summary(now))

    def start(self):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:  # la surveillance ne doit jamais s'arrêter
                    self.log(f"Surveillance : {e!r}")
                time.sleep(30)
        threading.Thread(target=loop, daemon=True, name="guard").start()

    def status(self):
        t = self.tg()
        b = self.backups()
        return {"telegram": {"token": bool(t.get("token")), "bot": t.get("bot", ""), "linked": bool(t.get("chat_id")),
                             "who": t.get("who", "")},
                "backups": b, "keep_days": KEEP_DAYS, "backup_hour": BACKUP_HOUR, "summary_hour": SUMMARY_HOUR,
                "available": bool(self.dir) and not self.demo}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
