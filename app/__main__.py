"""Point d'entrée en ligne de commande d'ADER LINK.

    python3 -m app setup              # crée config.json (compte administrateur)
    python3 -m app password           # change le mot de passe administrateur
    python3 -m app test-router        # teste la connexion à chaque routeur
    python3 -m app set-ssid "NOM"     # nom du Wi-Fi imprimé sur les cartes
    python3 -m app serve              # lance le serveur
    python3 -m app serve --demo       # lance avec des données fictives
"""
import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

from .collector import Collector, Store
from .routeros import RouterOS, RouterOSError
from .server import App, hash_password, serve, verify_password
from .users import Users
from .tickets import Tickets
from .routers import Routers
from .updater import Updater
import time

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("ADERLINK_CONFIG", ROOT / "config.json"))
DATA = Path(os.environ.get("ADERLINK_DATA", ROOT / "data"))

DEMO_CONFIG = {
    "admin": {"username": "admin", "password_hash": hash_password("aderlink")},
    "poll_seconds": 15,
    "zones": [
        {"id": "marche", "name": "Zone Grand Marché", "ssid": "ADER LINK Marché", "location": "Niamey", "demo_capacity": 55},
        {"id": "gare", "name": "Zone Gare routière", "location": "Niamey", "demo_capacity": 35,
         "demo_board": "hAP ax²"},
        {"id": "campus", "name": "Zone Campus", "ssid": "ADER LINK Campus", "location": "Niamey", "demo_capacity": 70,
         "demo_board": "RB4011"},
        {"id": "quartier", "name": "Zone Plateau", "location": "Niamey", "demo_capacity": 20,
         "demo_offline": True},
    ],
}


def load_config():
    if not CONFIG.exists():
        sys.exit(f"Fichier de configuration introuvable : {CONFIG}\n"
                 "Lance d'abord :  python3 -m app setup")
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)  # écriture atomique : config.json n'est jamais à moitié écrit  # contient les mots de passe des routeurs


def ask_password():
    while True:
        p1 = getpass.getpass("Mot de passe administrateur (10 caractères min.) : ")
        if len(p1) < 10:
            print("Trop court.")
            continue
        if p1 != getpass.getpass("Confirmer : "):
            print("Les deux saisies diffèrent.")
            continue
        return p1


def cmd_setup(_args):
    if CONFIG.exists():
        sys.exit(f"{CONFIG} existe déjà. Utilise « password » pour changer le mot de passe.")
    username = input("Nom d'utilisateur administrateur [admin] : ").strip() or "admin"
    cfg = {
        "admin": {"username": username, "password_hash": hash_password(ask_password())},
        "secret_key": secrets.token_hex(32),
        "poll_seconds": 30,
        "secure_cookies": True,
        "behind_proxy": True,
        "zones": [
            {
                "id": "zone1",
                "name": "Ma première zone",
                "location": "",
                "router": {
                    "host": "10.66.0.2",
                    "port": 8728,
                    "ssl": False,
                    "username": "aderlink",
                    "password": "A_REMPLACER",
                    "wan_interface": "ether1",
                },
            }
        ],
    }
    save_config(cfg)
    print(f"Configuration créée : {CONFIG}\nAjoute tes zones dans ce fichier, puis lance « test-router ».")


def cmd_init_vps(_args):
    """Installation sur un serveur : configuration créée sans question, mot de passe affiché une fois."""
    if CONFIG.exists():
        print(f"{CONFIG} existe déjà : rien à faire.")
        return
    pwd = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(14))
    cfg = {
        "admin": {"username": "admin", "password_hash": hash_password(pwd)},
        "secret_key": secrets.token_hex(32),
        "poll_seconds": 30,
        "secure_cookies": False,  # passe à true quand le site a un nom de domaine en HTTPS
        "behind_proxy": True,
        "zones": [],
    }
    DATA.mkdir(parents=True, exist_ok=True)
    save_config(cfg)
    print("ADMIN_PASSWORD=" + pwd)


def cmd_password(_args):
    cfg = load_config()
    cfg["admin"]["password_hash"] = hash_password(ask_password())
    cfg["secret_key"] = secrets.token_hex(32)  # déconnecte toutes les sessions ouvertes
    save_config(cfg)
    print("Mot de passe modifié. Les sessions ouvertes sont déconnectées.")


def cmd_test(_args):
    cfg = load_config()
    ok = True
    for zone in cfg.get("zones", []):
        r = zone["router"]
        label = f"{zone['name']} ({r['host']})"
        try:
            with RouterOS(r["host"], r["username"], r["password"],
                          port=r.get("port", 8729 if r.get("ssl") else 8728),
                          use_ssl=r.get("ssl", False)) as api:
                res = api.query("/system/resource/print")[0]
                active = len(api.query("/ip/hotspot/active/print"))
            print(f"  OK   {label} — {res.get('board-name')} RouterOS {res.get('version')}, "
                  f"{active} connecté(s)")
        except RouterOSError as e:
            ok = False
            print(f"  ÉCHEC {label} — {e}")
    sys.exit(0 if ok else 1)


def cmd_set_ssid(args):
    cfg = load_config()
    zones = cfg.get("zones", [])
    zone = next((z for z in zones if z["id"] == args.zone), None) if args.zone else (zones[0] if zones else None)
    if not zone:
        sys.exit("Zone introuvable.")
    zone["ssid"] = args.ssid
    save_config(cfg)
    print(f"Nom du Wi-Fi de « {zone['name']} » : {args.ssid}")


def cmd_serve(args):
    DATA.mkdir(parents=True, exist_ok=True)
    if args.demo:
        cfg = json.loads(json.dumps(DEMO_CONFIG))
        cfg["secret_key"] = secrets.token_hex(32)
        for i, z in enumerate(cfg["zones"]):
            z.setdefault("router", {"host": f"192.168.{88 + i}.1", "port": 8728, "username": "aderlink",
                                    "password": "demo", "wan_interface": "ether1"})
        store = Store(":memory:")
        print("Mode démonstration — identifiants : admin / aderlink")
    else:
        cfg = load_config()
        store = Store(str(DATA / "aderlink.db"))
    collector = Collector(cfg, store, demo=args.demo)
    tickets = Tickets(cfg, store, demo=args.demo)
    routers = Routers(cfg, store, collector, save=save_config, demo=args.demo)
    for z in cfg.get("zones", []):
        routers.ensure_default_pv(z["id"])
    users = Users(cfg, store, routers, hash_password, verify_password)
    updater = Updater(cfg)
    collector.ticket_hook = tickets.on_poll
    from .roaming import Roaming
    roaming = Roaming(cfg, store, tickets, collector)
    tickets.roaming = roaming
    if args.demo:
        _demo_batches(tickets)
        zids = [z["id"] for z in cfg.get("zones", [])]
        roaming.save_group({"name": "Réseau Niamey", "zones": zids[:2]})
        fs = tickets.list_forfaits(zids[0])
        if fs:
            tickets.create_batch({"forfait_id": fs[-1]["id"], "count": 20, "roaming": True, "note": "Pass Roaming"})
        _demo_users(cfg, users, tickets)
        _demo_samples(cfg, store)
    collector.poll_all()  # premier état avant d'ouvrir le tableau de bord
    collector.start()
    if not args.demo:
        updater.start()
        if (cfg.get("radius") or {}).get("enabled", True):
            roaming.start()
    print(f"ADER LINK version {updater.state['current']}")
    serve(App(cfg, collector, store, demo=args.demo, tickets=tickets, routers=routers, updater=updater,
              users=users, save_config=save_config, roaming=roaming,
              paths={"config": CONFIG, "db": None if args.demo else DATA / "aderlink.db"}),
          args.host, args.port)


def _demo_samples(cfg, store):
    """30 jours de relevés fictifs (toutes les 15 min) : peu de monde la nuit, pointe le soir."""
    import math
    import random as _r
    now = int(time.time())
    curve = [3, 2, 1, 1, 1, 2, 5, 12, 20, 24, 26, 28, 33, 30, 26, 25, 28, 34, 42, 52, 55, 46, 30, 12]
    rows = []
    for z in cfg.get("zones", []):
        scale = _r.uniform(0.6, 1.4)
        for ts in range(now - 30 * 86400, now, 900):
            h = ((ts + 3600) // 3600) % 24
            weekend = ((ts + 3600) // 86400 + 3) % 7 >= 5
            n = max(0, int(curve[h] * scale * (1.2 if weekend else 1) + _r.uniform(-3, 3)))
            rows.append((ts, z["id"], 1, n, n * 90_000, n * 15_000))
    with store.lock:
        store.db.executemany("INSERT INTO samples (ts, zone_id, online, active, rx_bps, tx_bps) VALUES (?,?,?,?,?,?)", rows)
        store.db.commit()


def _demo_users(cfg, users, tickets):
    zones = [z["id"] for z in cfg.get("zones", [])]
    users.create({"name": "Awa Gérante", "username": "gerant", "password": "aderlink", "role": "gerant",
                  "zones": zones[:2], "subscription": {"n": 1, "unit": "mois"}})
    rev = users.create({"name": "Moussa Revendeur", "username": "revendeur", "password": "aderlink",
                        "role": "revendeur", "zones": zones[:1], "commission": 10, "phone": "+227 90 00 00 00",
                        "subscription": {"n": 2, "unit": "jour"}})
    cfg["contact"] = {"name": "ADER TECHNOLOGIE", "phone": "+227 90 00 00 00"}
    # Clients : chacun possède ses routeurs, dans la limite que l'administrateur lui donne
    for name, login, n_max, own, sub in (("Wifi Tassi", "tassi", 3, zones[2:3], {"n": 3, "unit": "mois"}),
                                          ("Hamza Net", "hamza", 1, zones[3:4], {"n": 1, "unit": "jour"}),
                                          ("Zone Koira", "koira", 2, [], {"n": 1, "unit": "an"})):
        c = users.create({"name": name, "username": login, "password": "aderlink", "role": "client",
                          "max_routers": n_max, "phone": "+227 96 00 00 0" + str(n_max), "subscription": sub})
        for z in cfg["zones"]:
            if z["id"] in own:
                z["owner"] = login
    users.subscription(users._get("koira")["id"], {"action": "stop"})
    who = users.principal(rev["username"])
    f = next((f for f in tickets.list_forfaits(zones[0])), None) if zones else None
    if f:
        lot = tickets.create_batch({"forfait_id": f["id"], "count": 40, "pos_id": users.pv_for(who, zones[0]),
                                    "note": "Lot Moussa"}, created_by=who.username)
        now = int(time.time())
        rows, _ = tickets._q("SELECT code FROM tickets WHERE batch_id = ? LIMIT 14", (lot["id"],))
        for i, (code,) in enumerate(rows):  # quelques ventes réparties sur 10 jours
            tickets._q("UPDATE tickets SET first_used = ? WHERE batch_id = ? AND code = ?",
                       (now - (i % 10) * 86400 - 3600, lot["id"], code), commit=True)
    print("Comptes démo : gerant, revendeur, tassi (client) — mot de passe aderlink")


def _demo_batches(tickets):
    plans = [
        ("marche", "1 heure", 200, 3600, 500, 86400, 2, 1, 1, 20),
        ("marche", "Journée", 500, 86400, 2048, 86400, 3, 1, 1, 10),
        ("campus", "Pass journée", 1000, 0, 0, 86400, 5, 2, 2, 10),
        ("campus", "Semaine", 3000, 0, 10240, 604800, 5, 2, 2, 0),
    ]
    for zid, name, price, up_s, mb, val, down, upl, shared, count in plans:
        f = tickets.create_forfait({"zone_id": zid, "name": name, "price": price, "uptime_s": up_s,
                                    "data_mb": mb, "validity_s": val, "down_mbps": down, "up_mbps": upl,
                                    "shared_users": shared})
        if count:
            b = tickets.create_batch({"forfait_id": f["id"], "count": count})
            for t in tickets.get_batch(b["id"])["tickets"][: count // 3]:
                tickets._mark_used(zid, t["code"], int(time.time()) - 5000)
    # historique fictif sur 90 jours pour la courbe des ventes
    import random as _r
    rnd = _r.Random(7)
    hist = tickets.create_forfait({"zone_id": "gare", "name": "Ticket 100 F", "price": 100, "uptime_s": 0,
                                   "data_mb": 2048, "validity_s": 86400, "down_mbps": 2, "up_mbps": 1,
                                   "shared_users": 1})
    b = tickets.create_batch({"forfait_id": hist["id"], "count": 1000, "note": "Historique démo"})
    codes = [t["code"] for t in tickets.get_batch(b["id"])["tickets"]]
    now = int(time.time())
    i = 0
    for day in range(89, 0, -1):
        n = int(max(0, rnd.gauss(4 + (89 - day) * 0.12, 2)))
        for _ in range(n):
            if i >= len(codes) - 60:
                break
            tickets._mark_used("gare", codes[i], now - day * 86400 + rnd.randint(-20000, 20000))
            i += 1


def main():
    parser = argparse.ArgumentParser(prog="python3 -m app", description="ADER LINK")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(func=cmd_setup)
    sub.add_parser("password").set_defaults(func=cmd_password)
    sub.add_parser("init-vps").set_defaults(func=cmd_init_vps)
    sub.add_parser("test-router").set_defaults(func=cmd_test)
    w = sub.add_parser("set-ssid", help="nom du Wi-Fi imprimé sur les cartes")
    w.add_argument("ssid")
    w.add_argument("--zone", help="identifiant de la zone (par défaut : la première)")
    w.set_defaults(func=cmd_set_ssid)
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("ADERLINK_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("ADERLINK_PORT", 8080)))
    s.add_argument("--demo", action="store_true")
    s.set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
