"""Sécurité d'ADER LINK : sauvegarde automatique chaque nuit et alertes Telegram.

- Chaque nuit (3 h, heure locale) : une sauvegarde complète dans data/backups, 14 jours gardés,
  et une copie envoyée sur Telegram si c'est relié (copie hors du serveur).
- Alertes Telegram : routeur hors ligne (après 12 heures, pour ignorer les petites coupures),
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
OFFLINE_DELAY = 12 * 3600   # une alerte seulement si le routeur reste coupé 12 heures (pas de messages pour les petites coupures)
BACKUP_HOUR, SUMMARY_HOUR = 3, 0  # résumé à minuit : la journée entière
NAME_RE = re.compile(r"aderlink-auto-\d{4}-\d{2}-\d{2}\.zip")


class GuardError(Exception):
    pass


KEY_FILES = ("vps.key", "vps.pub", "ca.crt", "ca.key", "server.crt", "server.key", "dh.pem")


def make_backup(config, store, light=False, keys_dir=None):
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
        # clés des tunnels du serveur (WireGuard / OpenVPN) : un nouveau serveur garde les mêmes, les routeurs se reconnectent seuls
        if keys_dir and Path(keys_dir).is_dir():
            for name in KEY_FILES:
                f = Path(keys_dir) / name
                if f.is_file():
                    zf.write(f, f"server-keys/{name}")
        zf.writestr("LISEZMOI.txt", "Sauvegarde ADER LINK. A restaurer dans Parametres > Restaurer une sauvegarde.\n")
    return buf.getvalue()


class Guard:
    def __init__(self, config, store, data_dir=None, save=None, tickets=None, demo=False, log=print):
        self.config = config
        self.store = store
        self.dir = Path(data_dir) / "backups" if data_dir else None
        self.keys_dir = Path(data_dir) / "server-keys" if data_dir else None
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
        others = {p["chat_id"] for p in self.tg().get("extra", [])}
        chats = [c for c in chats if c and c.get("type") == "private" and c.get("id") not in others]
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
        data = make_backup(self.config, self.store, keys_dir=self.keys_dir)
        name = f"aderlink-auto-{self.today()}.zip"
        (self.dir / name).write_bytes(data)
        for f in sorted(self.dir.glob("aderlink-auto-*.zip"), reverse=True)[KEEP_DAYS:]:
            f.unlink()
        sent = False
        t = self.tg()
        if send and t.get("token") and t.get("chat_id"):
            doc, caption = data, f"Sauvegarde ADER LINK du {self.today()}"
            if len(doc) >= 45_000_000:  # Telegram refuse plus de 50 Mo : on envoie tout sauf l'historique des graphiques
                doc = make_backup(self.config, self.store, light=True, keys_dir=self.keys_dir)
                caption += " (sans l'historique des graphiques)"
            try:
                if len(doc) >= 45_000_000:
                    raise GuardError("sauvegarde trop grosse pour Telegram")
                self._api("sendDocument", {"chat_id": t["chat_id"], "caption": caption},
                          {"document": (name, doc)}, timeout=300)
                sent = True
            except GuardError as e:
                self.log(f"Sauvegarde non envoyée sur Telegram : {e}")
        return {"name": name, "size": len(data), "telegram": sent}

    def resend_backup(self, day):
        """Renvoie sur Telegram la sauvegarde du jour déjà faite sur le serveur."""
        f = self.dir / f"aderlink-auto-{day}.zip" if self.dir else None
        t = self.tg()
        if not (f and f.is_file() and t.get("chat_id")):
            return False
        doc, caption = f.read_bytes(), f"Sauvegarde ADER LINK du {day}"
        if len(doc) >= 45_000_000:
            doc = make_backup(self.config, self.store, light=True, keys_dir=self.keys_dir)
            caption += " (sans l'historique des graphiques)"
        self._api("sendDocument", {"chat_id": t["chat_id"], "caption": caption}, {"document": (f.name, doc)}, timeout=300)
        return True

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
                    self.notify(f"🟢 <b>{_esc(self._name(zid))}</b> est de nouveau en ligne.", "alert")
        changed = False
        for zid, since in list(self.pending.items()):
            if now - since >= OFFLINE_DELAY:
                changed = True
                self.pending.pop(zid)
                self.alerted.add(zid)
                self.notify(f"🔴 <b>{_esc(self._name(zid))}</b> est hors ligne depuis "
                          f"{time.strftime('%H:%M', time.gmtime(since + self.offset()))}.\n"
                          "Vérifiez le courant et la connexion Internet du routeur.", "alert")
        if rows or changed:
            self._save_state()

    def _name(self, zid):
        z = next((z for z in self.config.get("zones", []) if z["id"] == zid), None)
        return z["name"] if z else zid

    MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
              "octobre", "novembre", "décembre")

    def summary(self, now=None):
        """Résumé de la journée, envoyé à minuit, sur la journée qui vient de finir (0 h → 24 h) :
        pour chaque point de vente, les tickets par forfait, le total du jour et le total du mois."""
        now = now or int(time.time())
        off = self.offset()
        midnight = (now + off) // 86400 * 86400 - off
        start, end = (midnight - 86400, midnight) if SUMMARY_HOUR == 0 else (midnight, now)
        day = time.gmtime(start + off)
        month_start = int(time.mktime((day.tm_year, day.tm_mon, 1, 0, 0, 0, 0, 0, 0)) - time.timezone) - off
        nf = lambda x: f"{int(x):,}".replace(",", " ")
        tk = lambda n: f"{nf(n)} ticket{'s' if n > 1 else ''}"
        pos = []
        if self.tickets:
            try:
                pos = self.tickets.sales_by_pos(start, end, month_start)
            except Exception as e:  # base incomplète (tests) : résumé sans détail
                self.log(f"Résumé : détail indisponible ({e})")
        day_n, day_a = sum(p["day"][0] for p in pos), sum(p["day"][1] for p in pos)
        mon_n, mon_a = sum(p["month"][0] for p in pos), sum(p["month"][1] for p in pos)
        month = self.MONTHS[day.tm_mon - 1]
        lines = [f"📊 <b>Ventes du {time.strftime('%d/%m/%Y', day)}</b>",
                 f"Jour : <b>{nf(day_a)} F</b> · {tk(day_n)}",
                 f"Mois de {month} : <b>{nf(mon_a)} F</b> · {tk(mon_n)}"]
        def pv_lines(p, indent="   ", title=None):
            out = [f"{indent[:-3]}📍 <b>{_esc(title or p['name'])}</b>"]
            for l in p["lines"]:
                out.append(f"{indent}{_esc(l['label'])} ({nf(l['price'])} F) : {tk(l['tickets'])} = {nf(l['amount'])} F")
            out.append(f"{indent}<b>Jour : {nf(p['day'][1])} F</b> ({nf(p['day'][0])}) · Mois : {nf(p['month'][1])} F ({nf(p['month'][0])})")
            return out

        # réseaux ADER Roaming : le réseau, puis ses points de vente juste en dessous
        nets = {}
        for p in pos:
            if p.get("roaming") and p["day"][0]:
                nets.setdefault(p["roaming"], []).append(p)
        blocks = []
        for name, ps in nets.items():
            day = [sum(p["day"][0] for p in ps), sum(p["day"][1] for p in ps)]
            mon = [sum(p["month"][0] for p in ps), sum(p["month"][1] for p in ps)]
            b = ["", f"🌍 <b>{_esc(name)}</b> (Roaming)", f"<b>Jour : {nf(day[1])} F</b> ({nf(day[0])}) · Mois : {nf(mon[1])} F ({nf(mon[0])})"]
            for p in ps:
                b += pv_lines(p, "      ", p.get("short"))
            blocks.append((day[1], b))
        # routeurs simples : un par un, comme avant
        for p in pos:
            if not p.get("roaming") and p["day"][0]:
                blocks.append((p["day"][1], [""] + pv_lines(p)))
        for _, b in sorted(blocks, key=lambda x: -x[0]):
            lines += b
        quiet = [p for p in pos if not p["day"][0]]
        if quiet:
            lines.append("")
            lines.append(f"Sans vente aujourd'hui ({len(quiet)}) :")
            for p in quiet:
                lines.append(f"   {_esc(p['name'])} · mois : {nf(p['month'][1])} F")
        if self.alerted:
            lines.append("")
            lines.append("🔴 Hors ligne : " + ", ".join(_esc(self._name(z)) for z in self.alerted))
        return "\n".join(lines)

    def send_long(self, text, limit=3800, kind="report"):
        """Telegram limite un message à 4096 caractères : on coupe entre deux lignes."""
        part = ""
        for line in text.split("\n"):
            if part and len(part) + len(line) + 1 > limit:
                self.notify(part, kind)
                part = ""
            part += ("\n" if part else "") + line
        if part:
            self.notify(part, kind)

    # --- autres personnes qui reçoivent les messages ---------------------------------------------
    # Elles reçoivent le rapport des ventes, et les alertes des routeurs si c'est coché.
    # Jamais la sauvegarde (elle contient les mots de passe des routeurs) : elle va seulement à l'administrateur.
    MAX_EXTRA = 10

    def extras(self):
        return self.tg().setdefault("extra", [])

    def notify(self, text, kind="alert"):
        ok = self.send(text)
        t = self.tg()
        if self.demo or not t.get("token"):
            return ok
        for p in list(self.extras()):
            if kind == "report" or p.get("alerts"):
                try:
                    self._api("sendMessage", {"chat_id": p["chat_id"], "text": text, "parse_mode": "HTML",
                                              "disable_web_page_preview": "true"})
                except GuardError as e:
                    self.log(f"Message Telegram non envoyé à {p.get('who')} : {e}")
        return ok

    def add_person(self):
        """La personne a appuyé sur DÉMARRER dans le bot : on la retrouve et on l'ajoute."""
        if not self.tg().get("chat_id"):
            raise GuardError("Reliez d'abord votre propre Telegram")
        known = {self.tg().get("chat_id")} | {p["chat_id"] for p in self.extras()}
        updates = self._api("getUpdates", {"timeout": 0})
        chats = [u.get("message", {}).get("chat") for u in updates if u.get("message")]
        new = [c for c in chats if c and c.get("type") == "private" and c.get("id") not in known]
        if not new:
            raise GuardError("Personne de nouveau : la personne doit ouvrir le bot dans Telegram, appuyer sur DÉMARRER "
                             "(ou écrire /start), puis vous réessayez.")
        if len(self.extras()) >= self.MAX_EXTRA:
            raise GuardError(f"{self.MAX_EXTRA} personnes au plus")
        c = new[-1]
        who = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or (c.get("username") or "Sans nom")
        self.extras().append({"chat_id": c["id"], "who": who, "alerts": True})
        self.save(self.config)
        try:
            self._api("sendMessage", {"chat_id": c["id"], "parse_mode": "HTML",
                                      "text": "✅ <b>Vous recevrez ici les messages d'ADER LINK</b> : le rapport des ventes chaque nuit "
                                              "à minuit, et les alertes des routeurs."})
        except GuardError:
            pass
        return self.status()

    def set_person(self, chat_id, alerts=None, remove=False):
        people = self.extras()
        p = next((x for x in people if str(x["chat_id"]) == str(chat_id)), None)
        if not p:
            raise GuardError("Personne introuvable")
        if remove:
            people.remove(p)
        elif alerts is not None:
            p["alerts"] = bool(alerts)
        self.save(self.config)
        return self.status()

    def tick(self, now=None):
        now = now or int(time.time())
        self.check_events(now)
        local_h = time.gmtime(now + self.offset()).tm_hour
        day = self.today(now)
        if local_h == BACKUP_HOUR and self.done.get("backup") != day:
            self.done["backup"] = day
            try:
                res = self.backup_now()
                if self.tg().get("chat_id") and not res.get("telegram"):
                    self.done["backup_retry"] = (day, now + 3600, 1)  # Telegram n'a pas pris le fichier : on réessaie
            except Exception as e:
                self.log(f"Sauvegarde automatique : {e}")
                self.send(f"⚠️ La sauvegarde automatique a échoué : {_esc(e)}")
        # nouvel essai d'envoi sur Telegram, toutes les heures, 5 fois au plus
        retry = self.done.get("backup_retry")
        if retry and retry[0] == day and now >= retry[1]:
            try:
                ok = self.resend_backup(day)
            except Exception as e:
                ok = False
                self.log(f"Sauvegarde : nouvel essai Telegram : {e}")
            if ok:
                self.done.pop("backup_retry", None)
            elif retry[2] >= 5:
                self.done.pop("backup_retry", None)
                self.send("⚠️ La sauvegarde de cette nuit est faite sur le serveur, mais Telegram n'a pas pu la recevoir. "
                          "Elle sera renvoyée la nuit prochaine ; vous pouvez aussi la télécharger dans Paramètres.")
            else:
                self.done["backup_retry"] = (day, now + 3600, retry[2] + 1)
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
                             "who": t.get("who", ""),
                             "people": [{"id": str(p["chat_id"]), "who": p.get("who", ""), "alerts": bool(p.get("alerts"))}
                                        for p in t.get("extra", [])]},
                "backups": b, "keep_days": KEEP_DAYS, "backup_hour": BACKUP_HOUR, "summary_hour": SUMMARY_HOUR,
                "available": bool(self.dir) and not self.demo}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
