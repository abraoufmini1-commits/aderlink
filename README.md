# ADER LINK — Tableau de bord des zones Wi-Fi

Application web pour surveiller à distance des zones Wi-Fi équipées de routeurs **MikroTik** (hotspot).
Version 5 : tableau de bord, création de tickets en 4 étapes (routeur, point de vente, forfait,
quantité), lots, contrôle des tickets, **gestion des routeurs depuis l'application** (ajout,
installation en une ligne Winbox, points de vente) et **mise à jour automatique** depuis GitHub.

## Mise à jour automatique

Dans **Paramètres**, indiquez le dépôt GitHub (ex. `votre-nom/aderlink`). Toutes les 10 minutes,
ADER LINK lit le fichier `VERSION` du dépôt ; s'il a changé, il télécharge le code, remplace ses
fichiers (jamais `config.json` ni `data/`) et redémarre (code de sortie 3 : `ADER-LINK.bat` et
systemd le relancent). Les pages ouvertes se rechargent toutes seules.

- Utilisateurs connectés, toutes zones et par zone, avec courbe sur 6 h / 24 h / 7 jours
- État de chaque routeur : en ligne / hors ligne, processeur, mémoire, débit, durée d'allumage, modèle et version
- Liste des connectés d'une zone (ticket, adresse MAC, IP, durée, consommation) avec recherche
- Journal des coupures et des retours en ligne
- **Tickets** : création de lots (quantité, prix en F CFA, durée, données, validité après la
  1re connexion), envoi direct au routeur, impression des cartes 85 × 54 mm (10 par page A4,
  avec repères de découpe), contrôle d'un code, suppression des tickets non utilisés
- Ventes du jour (tickets utilisés pour la première fois aujourd'hui × prix)
- Connexion protégée par mot de passe, utilisable depuis un téléphone
- Aucune dépendance à installer : uniquement Python 3.8 ou plus récent

## Essayer tout de suite (données fictives)

```bash
python3 -m app serve --demo
```

Ouvre http://127.0.0.1:8080 — identifiant `admin`, mot de passe `aderlink`.
Pour l'ouvrir depuis un téléphone sur le même réseau : `python3 -m app serve --demo --host 0.0.0.0`,
puis tape `http://ADRESSE-IP-DU-PC:8080` dans le navigateur du téléphone.

Sous Windows, double-cliquez sur **ADER-LINK.bat** pour lancer le vrai serveur
(accessible depuis les téléphones du réseau local).

## Comment ça marche

```
 Téléphone / PC ──HTTPS──▶  VPS (ADER LINK + Caddy)  ◀──tunnel WireGuard──  MikroTik zone 1
                                10.66.0.1                                     10.66.0.2
                                                      ◀──tunnel WireGuard──  MikroTik zone 2
                                                                              10.66.0.3
```

Les routeurs sont souvent derrière une box 4G ou une NAT : on ne peut pas les joindre directement.
Chaque MikroTik ouvre donc lui-même un **tunnel WireGuard** vers le VPS. L'application interroge
ensuite chaque routeur par l'API RouterOS à travers ce tunnel, avec un **compte limité à l'API** (pas de Winbox, SSH ni FTP).
L'API n'est jamais exposée sur Internet.

## Installation sur le VPS

Prérequis : un VPS Ubuntu 22.04+ ou Debian 12+, un nom de domaine qui pointe vers son adresse IP,
des routeurs sous **RouterOS 7** (WireGuard n'existe pas en v6).

1. Copie ce dossier sur le VPS, puis :
   ```bash
   sudo bash deploy/vps/install.sh
   ```
   Le script installe Python, WireGuard et Caddy, crée l'utilisateur système, ouvre le pare-feu
   et affiche la **clé publique du VPS**.

2. Crée le compte administrateur :
   ```bash
   cd /opt/aderlink && sudo -u aderlink python3 -m app setup
   ```

3. Pour **chaque zone**, ouvre `deploy/mikrotik/aderlink-zone.rsc`, remplace les 4 valeurs
   en haut (adresse dans le tunnel, IP du VPS, clé du VPS, mot de passe), puis colle-le dans
   le Terminal Winbox du routeur. Il affiche la clé publique du routeur.

4. Ajoute chaque routeur dans `/etc/wireguard/wg0.conf` (bloc `[Peer]`, voir
   `deploy/vps/wg0.conf.example`), puis :
   ```bash
   sudo systemctl enable --now wg-quick@wg0     # la première fois
   sudo systemctl restart wg-quick@wg0          # après chaque nouveau routeur
   ping 10.66.0.2                               # doit répondre
   ```

5. Déclare les zones dans `/opt/aderlink/config.json` (modèle : `config.example.json`) et teste :
   ```bash
   cd /opt/aderlink && sudo -u aderlink python3 -m app test-router
   ```
   `wan_interface` est l'interface reliée à Internet (`ether1`, `lte1`, `pppoe-out1`…) ;
   elle sert à mesurer le débit.

6. Mets ton domaine dans `/etc/caddy/Caddyfile` (modèle : `deploy/vps/Caddyfile`), puis :
   ```bash
   sudo systemctl enable --now aderlink
   sudo systemctl reload caddy
   ```
   Ouvre `https://ton-domaine` : c'est prêt.

## Commandes utiles

| Action | Commande (dans /opt/aderlink) |
|---|---|
| Changer le mot de passe admin | `sudo -u aderlink python3 -m app password` |
| Tester les routeurs | `sudo -u aderlink python3 -m app test-router` |
| Redémarrer après modification de config.json | `sudo systemctl restart aderlink` |
| Voir les journaux | `journalctl -u aderlink -f` |
| Nom du Wi-Fi sur les cartes | `python3 -m app set-ssid "ADER TECHNOLOGIE"` |
| Lancer les tests | `python3 -m unittest discover -s tests -t .` |

## Sécurité

- Compte MikroTik `aderlink` limité à l'API (lecture + écriture pour les tickets),
  sans accès Winbox/SSH/FTP ni aux mots de passe, et limité à l'adresse du VPS (10.66.0.1).
- Service API du routeur accessible uniquement à travers le tunnel.
- `config.json` contient les mots de passe des routeurs : il est créé avec les droits 600.
- Mot de passe admin haché (PBKDF2), session signée de 12 h, cookie `HttpOnly` + `Secure`,
  blocage de 15 min après 5 échecs de connexion.
- HTTPS obligatoire via Caddy ; l'application n'écoute que sur 127.0.0.1.

## Tickets : bon à savoir

- Chaque ticket est un utilisateur du hotspot MikroTik : **nom = code**, mot de passe identique
  au code (ou vide, au choix), commentaire `ADERLINK-<n° du lot>`.
- La **validité après 1re connexion** est surveillée par ADER LINK : à l'échéance, le ticket est
  désactivé et la session coupée. L'application doit donc tourner en continu (c'est le rôle du VPS).
- Le nom du Wi-Fi imprimé sur les cartes vient du champ `"ssid"` de la zone dans `config.json`
  (à défaut, le nom de la zone).
- Impression : format A4, échelle **100 %**, marges par défaut.

## Prochains modules prévus

Déconnexion d'un utilisateur, revendeurs et rapport des ventes détaillé, alertes SMS /
WhatsApp en cas de coupure, paiement mobile.

## Structure

```
app/
  __main__.py     commandes (setup, password, test-router, serve)
  routeros.py     client API RouterOS (sans dépendance)
  collector.py    relevés périodiques, historique SQLite, mode démo
  tickets.py      lots de tickets, envoi au routeur, validité, ventes
  server.py       serveur web, connexion, API JSON
  static/         pages, styles et script du tableau de bord
deploy/
  vps/            install.sh, service systemd, Caddyfile, modèle WireGuard
  mikrotik/       script RouterOS à coller dans chaque routeur
tests/            tests automatiques (faux routeur MikroTik inclus)
```

## Utilisateurs et revendeurs (5.3)

Menu → **Utilisateurs** (administrateur seulement) :

- **Gérant** : crée des tickets et des profils, voit les lots et les ventes des routeurs qu'on lui donne.
- **Revendeur** : crée et imprime des tickets avec son propre point de vente ; il ne voit que ses lots et ses ventes. Commission en % calculée sur les tickets vendus.

Chacun change son mot de passe dans **Mon profil**. Un compte désactivé est déconnecté tout de suite.
