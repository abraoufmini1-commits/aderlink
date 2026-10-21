#!/usr/bin/env bash
# Installation d'ADER LINK sur un VPS Ubuntu 22.04+ / Debian 12+.
# À lancer en root depuis le dossier du projet :  sudo bash deploy/vps/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
DEST=/opt/aderlink

echo "==> Paquets système"
apt-get update -qq
apt-get install -y -qq python3 wireguard ufw debian-keyring debian-archive-keyring apt-transport-https curl gnupg

echo "==> Caddy (HTTPS automatique)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi

echo "==> Utilisateur et fichiers"
id aderlink >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin aderlink
mkdir -p "$DEST/data"
cp -r "$SRC/app" "$DEST/"
cp "$SRC/config.example.json" "$DEST/"
chown -R aderlink:aderlink "$DEST"

echo "==> Clés WireGuard du VPS"
umask 077
if [ ! -f /etc/wireguard/vps.key ]; then
  wg genkey | tee /etc/wireguard/vps.key | wg pubkey > /etc/wireguard/vps.pub
fi
if [ ! -f /etc/wireguard/wg0.conf ]; then
  sed "s#CONTENU_DE_/etc/wireguard/vps.key#$(cat /etc/wireguard/vps.key)#" \
    "$SRC/deploy/vps/wg0.conf.example" > /etc/wireguard/wg0.conf
fi

echo "==> Pare-feu"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 51820/udp >/dev/null
ufw --force enable >/dev/null

echo "==> Service ADER LINK"
cp "$SRC/deploy/vps/aderlink.service" /etc/systemd/system/
systemctl daemon-reload

cat <<EOF

Installation terminée. Étapes suivantes :

  1. Clé publique du VPS (à mettre dans le script MikroTik) :
       $(cat /etc/wireguard/vps.pub)

  2. Crée le compte administrateur :
       cd $DEST && sudo -u aderlink python3 -m app setup

  3. Après chaque routeur préparé, ajoute sa clé dans /etc/wireguard/wg0.conf,
     puis :  systemctl enable --now wg-quick@wg0   (ou : systemctl restart wg-quick@wg0)

  4. Renseigne les zones dans $DEST/config.json, puis teste :
       cd $DEST && sudo -u aderlink python3 -m app test-router

  5. Mets ton domaine dans /etc/caddy/Caddyfile (modèle : deploy/vps/Caddyfile), puis :
       systemctl enable --now aderlink && systemctl reload caddy
EOF
