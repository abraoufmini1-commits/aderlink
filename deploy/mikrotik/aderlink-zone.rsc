# ============================================================================
# ADER LINK — préparation d'un routeur MikroTik (RouterOS 7.x)
#
# À coller dans le Terminal du routeur (Winbox > New Terminal) APRÈS avoir
# remplacé les 4 valeurs ci-dessous. À faire une fois par zone.
# Colle TOUT le bloc d'un coup (les accolades gardent les variables).
#
#   ZONEIP    : adresse du routeur dans le tunnel (10.66.0.2, puis .3, .4…)
#   VPSIP     : adresse IP publique de ton VPS
#   VPSPUBKEY : clé publique WireGuard du VPS (cat /etc/wireguard/vps.pub)
#   APIPASS   : mot de passe du compte « aderlink » (long et unique)
# ============================================================================

{
:local ZONEIP "10.66.0.2"
:local VPSIP "203.0.113.10"
:local VPSPUBKEY "COLLER_ICI_LA_CLE_PUBLIQUE_DU_VPS"
:local APIPASS "CHANGER_CE_MOT_DE_PASSE"

# 1) Tunnel WireGuard sortant vers le VPS (passe derrière la box 4G / la NAT)
/interface wireguard add name=wg-aderlink listen-port=13231 comment="ADER LINK"
/interface wireguard peers add interface=wg-aderlink public-key=$VPSPUBKEY \
    endpoint-address=$VPSIP endpoint-port=51820 allowed-address=10.66.0.1/32 \
    persistent-keepalive=25s comment="VPS ADER LINK"
/ip address add address=($ZONEIP . "/24") interface=wg-aderlink comment="ADER LINK"

# 2) Compte de l'application : lecture + écriture (création des tickets),
#    sans accès à Winbox, SSH, FTP ni aux réglages de sécurité
/user group add name=aderlink-api policy=read,write,api \
    comment="ADER LINK tableau de bord + tickets"
/user add name=aderlink group=aderlink-api password=$APIPASS address=10.66.0.1/32 \
    comment="ADER LINK"

# 3) Service API accessible uniquement depuis le VPS, à travers le tunnel
/ip service set api disabled=no address=10.66.0.1/32
/ip firewall filter add chain=input in-interface=wg-aderlink src-address=10.66.0.1 \
    protocol=tcp dst-port=8728 action=accept place-before=0 comment="ADER LINK API"
/ip firewall filter add chain=input in-interface=wg-aderlink protocol=icmp \
    action=accept place-before=0 comment="ADER LINK ping"

# 4) Affiche la clé publique du routeur : à copier dans /etc/wireguard/wg0.conf du VPS
:put "------------------------------------------------------------"
:put ("Cle publique de ce routeur : " . [/interface wireguard get [find name=wg-aderlink] public-key])
:put ("Adresse dans le tunnel     : " . $ZONEIP)
:put "------------------------------------------------------------"
}
