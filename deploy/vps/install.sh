#!/usr/bin/env bash
# ADER LINK — installation sur un VPS Ubuntu 22.04 / 24.04, en une ligne (en root) :
#   curl -fsSL https://raw.githubusercontent.com/abraoufmini1-commits/aderlink/main/deploy/vps/install.sh | bash
# Peut être relancé sans danger : la configuration et les données sont gardées.
set -euo pipefail

REPO="${ADERLINK_REPO:-abraoufmini1-commits/aderlink}"
BRANCH="${ADERLINK_BRANCH:-main}"
DEST=/opt/aderlink
export DEBIAN_FRONTEND=noninteractive

if [ "$(id -u)" -ne 0 ]; then echo "Lancez ce script en root (ou avec sudo)."; exit 1; fi
say() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }

say "1/7 Paquets système"
apt-get update -qq
apt-get install -y -qq python3 curl unzip ufw wireguard-tools openvpn openssl nftables debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null

say "2/7 Caddy (serveur web)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

say "3/7 Application ADER LINK (depuis GitHub)"
id aderlink >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin aderlink
mkdir -p "$DEST/data"
TMP=$(mktemp -d)
curl -fsSL "https://codeload.github.com/$REPO/zip/refs/heads/$BRANCH" -o "$TMP/src.zip"
unzip -q "$TMP/src.zip" -d "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
cp -r "$SRC/app" "$SRC/deploy" "$SRC/VERSION" "$SRC/README.md" "$DEST/"
cp "$SRC/config.example.json" "$DEST/" 2>/dev/null || true
rm -rf "$TMP"
FIRST=0
if [ ! -f "$DEST/config.json" ]; then
  FIRST=1
  ADMIN_PW=$(cd "$DEST" && ADERLINK_CONFIG="$DEST/config.json" ADERLINK_DATA="$DEST/data" python3 -m app init-vps | sed -n 's/^ADMIN_PASSWORD=//p')
fi
chown -R aderlink:aderlink "$DEST"
chmod 600 "$DEST/config.json"

say "4/7 Démarrage automatique"
cat > /etc/systemd/system/aderlink.service <<UNIT
[Unit]
Description=ADER LINK - gestion des zones Wi-Fi
After=network-online.target
Wants=network-online.target

[Service]
User=aderlink
Group=aderlink
WorkingDirectory=$DEST
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m app serve --host 127.0.0.1 --port 8080
# code 3 = mise à jour installée : on redémarre aussitôt
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DEST

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now aderlink >/dev/null
systemctl restart aderlink

say "5/7 Adresse web (port 80, et HTTPS avec un nom de domaine)"
# Plusieurs noms possibles : un nouveau nom s'AJOUTE aux anciens (les anciens liens continuent de marcher)
NEW=$(printf '%s' "${ADERLINK_DOMAIN:-}" | tr 'A-Z,' 'a-z ' | tr -cd 'a-z0-9. -')
OLD=""
[ -f "$DEST/domain" ] && OLD=$(tr 'A-Z,' 'a-z ' < "$DEST/domain" | tr -cd 'a-z0-9. -')
DOMAINS=$(printf '%s\n' $NEW $OLD | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ *$//')
DOMAIN=$(printf '%s\n' $DOMAINS | head -n1)   # le plus récent : adresse principale
if [ -n "$DOMAINS" ]; then
  echo "$DOMAINS" > "$DEST/domain"
  SITES=$(printf '%s' "$DOMAINS" | sed 's/ /, /g')
  # les noms de domaine en HTTPS (certificats automatiques) ; l'adresse IP reste en http pour les routeurs
  cat > /etc/caddy/Caddyfile <<CADDY
# ADER LINK
$SITES {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
:80 {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
CADDY
elif ! grep -q "ADER LINK" /etc/caddy/Caddyfile 2>/dev/null; then  # on ne touche pas à un Caddyfile déjà réglé
  cat > /etc/caddy/Caddyfile <<'CADDY'
# ADER LINK. Avec un nom de domaine : relancez l'installation avec ADERLINK_DOMAIN=votre.domaine
:80 {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
CADDY
fi
systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

say "6/7 Tunnel des routeurs (WireGuard pour RouterOS 7, OpenVPN pour RouterOS 6)"
umask 077
mkdir -p /etc/wireguard
[ -f /etc/wireguard/vps.key ] || wg genkey | tee /etc/wireguard/vps.key | wg pubkey > /etc/wireguard/vps.pub
umask 022
OV=/etc/openvpn/aderlink
mkdir -p "$OV/ccd"
if [ ! -f "$OV/server.crt" ]; then  # certificats créés une seule fois (les routeurs ne les vérifient pas)
  openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -subj "/CN=ADER LINK CA" -keyout "$OV/ca.key" -out "$OV/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=aderlink-server" -keyout "$OV/server.key" -out "$OV/server.csr" 2>/dev/null
  printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' > "$OV/ext.cnf"
  openssl x509 -req -in "$OV/server.csr" -CA "$OV/ca.crt" -CAkey "$OV/ca.key" -CAcreateserial -days 7300 -extfile "$OV/ext.cnf" -out "$OV/server.crt" 2>/dev/null
  chmod 600 "$OV/ca.key" "$OV/server.key"
fi
[ -f "$OV/dh.pem" ] || openssl dhparam -out "$OV/dh.pem" 2048 2>/dev/null
touch "$OV/users"; chgrp nogroup "$OV/users"; chmod 640 "$OV/users"
cat > /usr/local/sbin/aderlink-ovpn-auth <<'AUTH'
#!/bin/sh
# OpenVPN donne un fichier : 1re ligne = identifiant, 2e = mot de passe
u=$(sed -n 1p "$1"); p=$(sed -n 2p "$1")
case "$u$p" in *[!A-Za-z0-9]*|"") exit 1 ;; esac
grep -qx "$u $p" /etc/openvpn/aderlink/users
AUTH
chmod 755 /usr/local/sbin/aderlink-ovpn-auth
mkdir -p /etc/openvpn/server
cat > /etc/openvpn/server/aderlink.conf <<CONF
# ADER LINK : tunnel des routeurs RouterOS 6
port 1194
proto tcp-server
dev tun-aderlink
dev-type tun
mode server
tls-server
topology subnet
ifconfig 10.67.0.1 255.255.255.0
ca $OV/ca.crt
cert $OV/server.crt
key $OV/server.key
dh $OV/dh.pem
verify-client-cert none
username-as-common-name
script-security 2
auth-user-pass-verify /usr/local/sbin/aderlink-ovpn-auth via-file
client-config-dir $OV/ccd
ccd-exclusive
data-ciphers AES-256-CBC:AES-128-CBC
data-ciphers-fallback AES-256-CBC
auth SHA1
tls-cipher DEFAULT:@SECLEVEL=0
keepalive 10 60
persist-key
persist-tun
user nobody
group nogroup
verb 2
CONF
# Aide système : l'application (sans droits) écrit la liste des routeurs dans data/wg-peers.conf,
# cette aide (root) la vérifie ligne par ligne puis met le tunnel à jour.
cat > /usr/local/sbin/aderlink-wg-sync <<'HELPER'
#!/usr/bin/env bash
set -euo pipefail
PEERS=/opt/aderlink/data/wg-peers.conf
CONF=/etc/wireguard/wg0.conf
umask 077
{
  printf '[Interface]\nAddress = 10.66.0.1/24\nListenPort = 51820\nPrivateKey = %s\n\n' "$(cat /etc/wireguard/vps.key)"
  # seules ces lignes sont acceptées : rien d'autre ne peut entrer dans la configuration root
  [ -f "$PEERS" ] && grep -E '^(\[Peer\]|PublicKey = [A-Za-z0-9+/]{43}=|AllowedIPs = 10\.66\.0\.[0-9]{1,3}/32|)$' "$PEERS" || true
} > "$CONF.new"
mv "$CONF.new" "$CONF"
# OpenVPN (RouterOS 6) : « identifiant mot-de-passe 10.67.0.n », vérifié ligne par ligne
OVU=/opt/aderlink/data/ovpn-users.conf
OV=/etc/openvpn/aderlink
grep -E '^al[0-9]{1,3} [A-Za-z0-9]{8,64} 10\.67\.0\.[0-9]{1,3}$' "$OVU" 2>/dev/null > "$OV/users.new" || true
rm -f "$OV"/ccd/al*
while read -r u p a; do printf 'ifconfig-push %s 255.255.255.0\n' "$a" > "$OV/ccd/$u"; done < "$OV/users.new"
cut -d' ' -f1,2 "$OV/users.new" > "$OV/users.tmp"; chgrp nogroup "$OV/users.tmp"; chmod 640 "$OV/users.tmp"
mv "$OV/users.tmp" "$OV/users"; rm -f "$OV/users.new"
chmod 755 "$OV/ccd"; chmod 644 "$OV"/ccd/* 2>/dev/null || true
# Accès à distance : port public du serveur -> Winbox (8291) / WebFig (80) du routeur, par le tunnel
FW=/opt/aderlink/data/forwards.conf
RULES=""
while read -r port addr dport; do
  RULES="$RULES    tcp dport $port dnat to $addr:$dport
"
done < <(grep -E '^(5[0-2][0-9]{3} 10\.6[67]\.0\.[0-9]{1,3} (8291|80)|53[0-9]{3} 10\.6[67]\.0\.[0-9]{1,3} 53[0-9]{3})$' "$FW" 2>/dev/null || true)
nft -f - <<NFT || true
table ip aderlink
delete table ip aderlink
table ip aderlink {
  chain pre {
    type nat hook prerouting priority -100;
$RULES  }
  chain post {
    type nat hook postrouting priority 100;
    ip daddr 10.66.0.0/23 ct status dnat masquerade
  }
}
NFT
if ip link show wg0 >/dev/null 2>&1; then
  wg syncconf wg0 <(wg-quick strip wg0)
else
  systemctl start wg-quick@wg0
fi
HELPER
chmod 755 /usr/local/sbin/aderlink-wg-sync
cat > /etc/systemd/system/aderlink-wg.service <<'UNIT'
[Unit]
Description=ADER LINK - mise a jour du tunnel des routeurs
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/aderlink-wg-sync
UNIT
cat > /etc/systemd/system/aderlink-wg.path <<'UNIT'
[Unit]
Description=ADER LINK - surveille la liste des routeurs du tunnel
[Path]
PathChanged=/opt/aderlink/data/wg-peers.conf
PathChanged=/opt/aderlink/data/ovpn-users.conf
PathChanged=/opt/aderlink/data/forwards.conf
[Install]
WantedBy=multi-user.target
UNIT
touch "$DEST/data/wg-peers.conf" "$DEST/data/ovpn-users.conf" "$DEST/data/forwards.conf"
chown aderlink:aderlink "$DEST/data/wg-peers.conf" "$DEST/data/ovpn-users.conf" "$DEST/data/forwards.conf"
# le serveur relaie Winbox / WebFig vers les routeurs par le tunnel
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-aderlink.conf
sysctl -q -p /etc/sysctl.d/99-aderlink.conf
chmod 600 "$DEST/data/ovpn-users.conf"
IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
systemctl stop aderlink  # l'application ne doit pas réécrire la configuration pendant ce réglage
(cd "$DEST" && ADERLINK_CONFIG="$DEST/config.json" ADERLINK_DATA="$DEST/data" \
  python3 -m app wg-setup --endpoint "$IP" --pubkey "$(cat /etc/wireguard/vps.pub)" >/dev/null)
chown aderlink:aderlink "$DEST/config.json"
chmod 600 "$DEST/config.json"
systemctl daemon-reload
systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
/usr/local/sbin/aderlink-wg-sync || true
systemctl enable --now aderlink-wg.path >/dev/null
systemctl enable openvpn-server@aderlink >/dev/null 2>&1 || true
systemctl restart openvpn-server@aderlink || echo "  (OpenVPN : voir journalctl -u openvpn-server@aderlink)"
systemctl restart aderlink

say "7/7 Pare-feu"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 51820/udp >/dev/null
ufw allow 1194/tcp >/dev/null      # tunnel des routeurs RouterOS 6
ufw allow 1812:1813/udp >/dev/null
ufw route allow proto tcp to 10.66.0.0/23 port 8291 >/dev/null   # Winbox à distance (par le tunnel)
ufw route allow proto tcp to 10.66.0.0/23 port 80 >/dev/null     # WebFig à distance (par le tunnel)
ufw route allow proto tcp to 10.66.0.0/23 port 53000:53999 >/dev/null  # appareils derrière les routeurs (antennes…)   # ADER Roaming (RADIUS : tickets valables sur plusieurs routeurs)
# routeurs RouterOS 6 (tunnel OpenVPN 10.67.0.x) : mêmes accès à distance
ufw route allow proto tcp to 10.67.0.0/24 port 8291 >/dev/null
ufw route allow proto tcp to 10.67.0.0/24 port 80 >/dev/null
ufw route allow proto tcp to 10.67.0.0/24 port 53000:53999 >/dev/null
ufw --force enable >/dev/null

# Protection contre ceux qui essaient de deviner le mot de passe root (SSH) :
# 5 essais ratés en 10 minutes -> l'adresse est bloquée 1 heure (2 jours si elle recommence)
apt-get install -y -qq fail2ban >/dev/null
cat > /etc/fail2ban/jail.d/aderlink.local <<'F2B'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
bantime.increment = true
bantime.maxtime = 2d

[sshd]
enabled = true
backend = systemd
F2B
systemctl enable --now fail2ban >/dev/null 2>&1
systemctl restart fail2ban >/dev/null 2>&1 || true

sleep 2
if systemctl is-active --quiet aderlink; then STATE="EN MARCHE"; else STATE="PROBLEME (journalctl -u aderlink)"; fi
echo
echo "================================================================"
echo "  ADER LINK est installé : $STATE"
if systemctl is-active --quiet fail2ban; then echo "  Protection SSH (fail2ban) : active"; fi
if [ -n "$DOMAIN" ]; then echo "  Ouvrez :  https://$DOMAIN   (installable comme une application)"; else echo "  Ouvrez :  http://$IP"; fi
if [ "$FIRST" = 1 ]; then
  echo "  Identifiant : admin"
  echo "  Mot de passe : $ADMIN_PW"
  echo "  (notez-le maintenant, il ne sera plus affiché)"
else
  echo "  Mise à jour faite : vos comptes et vos données sont gardés."
fi
echo "================================================================"
