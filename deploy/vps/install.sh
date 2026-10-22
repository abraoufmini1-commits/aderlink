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
apt-get install -y -qq python3 curl unzip ufw wireguard-tools debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null

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

say "5/7 Adresse web (port 80)"
if ! grep -q "ADER LINK" /etc/caddy/Caddyfile 2>/dev/null; then  # on ne touche pas à un Caddyfile déjà réglé
  cat > /etc/caddy/Caddyfile <<'CADDY'
# ADER LINK. Avec un nom de domaine : remplacez « :80 » par le domaine (ex. app.aderlink.com),
# puis « systemctl reload caddy » : le certificat HTTPS est obtenu automatiquement.
:80 {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
CADDY
fi
systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

say "6/7 Tunnel des routeurs (WireGuard)"
umask 077
mkdir -p /etc/wireguard
[ -f /etc/wireguard/vps.key ] || wg genkey | tee /etc/wireguard/vps.key | wg pubkey > /etc/wireguard/vps.pub
if [ ! -f /etc/wireguard/wg0.conf ]; then
  printf '[Interface]\nAddress = 10.66.0.1/24\nListenPort = 51820\nPrivateKey = %s\n' "$(cat /etc/wireguard/vps.key)" > /etc/wireguard/wg0.conf
fi
systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || true
umask 022

say "7/7 Pare-feu"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 51820/udp >/dev/null
ufw --force enable >/dev/null

IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
sleep 2
if systemctl is-active --quiet aderlink; then STATE="EN MARCHE"; else STATE="PROBLEME (journalctl -u aderlink)"; fi
echo
echo "================================================================"
echo "  ADER LINK est installé : $STATE"
echo "  Ouvrez :  http://$IP"
if [ "$FIRST" = 1 ]; then
  echo "  Identifiant : admin"
  echo "  Mot de passe : $ADMIN_PW"
  echo "  (notez-le maintenant, il ne sera plus affiché)"
else
  echo "  Mise à jour faite : vos comptes et vos données sont gardés."
fi
echo "================================================================"
