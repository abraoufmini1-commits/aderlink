"""Scripts RouterOS d'ADER LINK.

Le routeur doit fonctionner seul, même quand ADER LINK est éteint :
- chaque profil hotspot « AL-… » lance, à la 1re connexion d'un ticket, un compte à rebours
  (une tâche du Scheduler « AL-EXP-<code> ») ; à la fin de la validité — ou dès que les Go
  ou le temps sont consommés — le routeur coupe la session et DÉSACTIVE le ticket ; il le
  supprime 7 jours plus tard ;
- le Scheduler « ADER LINK » se signale toutes les 5 minutes à l'application, qui retient
  ainsi l'adresse du routeur même si elle change.

Compatible RouterOS 6 et 7.
"""
import re

KEEP_DAYS = 7  # un ticket fini reste visible (désactivé) une semaine, puis il est supprimé

# Tâche du Scheduler qui appelle « aderlink-end » pour le ticket $u.
_END_CALL = ('(":local f [:parse [/system script get [find name=\\"aderlink-end\\"] source]]; '
             '\\$f u=\\"" . $u . "\\"")')

# Script appelé par le profil hotspot à chaque connexion : $u = code du ticket, $v = validité.
# Il lance le compte à rebours « AL-EXP-<code> » ; à la fin, « aderlink-end » prend le relais.
LOGIN_SOURCE = (
    ':if ([:len [/system scheduler find name=("AL-EXP-" . $u)]] = 0) do={ '
    '/system scheduler add name=("AL-EXP-" . $u) '
    'start-date=[/system clock get date] start-time=[/system clock get time] interval=[:totime $v] '
    'comment="ADER LINK : fin du ticket" '
    f'on-event={_END_CALL} '
    '}'
)

# Fin d'un ticket (validité écoulée, ou Go / temps consommés), en 2 temps :
#  1. le client est coupé et le ticket DÉSACTIVÉ (il reste visible dans Winbox) ;
#     la tâche « AL-EXP-<code> » est reprogrammée dans 7 jours ;
#  2. 7 jours plus tard, le ticket et sa tâche sont supprimés.
END_SOURCE = (
    ':local n ("AL-EXP-" . $u); '
    ':local s [/system scheduler find name=$n]; '
    ':local last false; '
    ':if ([:len $s] > 0) do={ :if ([/system scheduler get $s comment] = "ADER LINK : a supprimer") do={ :set last true } }; '
    '/ip hotspot active remove [find user=$u]; '
    ':if ($last) do={ /ip hotspot user remove [find name=$u]; /system scheduler remove $s } else={ '
    '/ip hotspot user set [find name=$u] disabled=yes; '
    ':if ([:len $s] > 0) do={ '
    '/system scheduler set $s comment="ADER LINK : a supprimer" start-date=[/system clock get date] '
    f'start-time=[/system clock get time] interval={KEEP_DAYS}d '
    '} else={ '
    '/system scheduler add name=$n comment="ADER LINK : a supprimer" start-date=[/system clock get date] '
    f'start-time=[/system clock get time] interval={KEEP_DAYS}d on-event={_END_CALL} '
    '} }'
)

# Script appelé à chaque déconnexion : si le ticket a tout consommé (Go ou temps de connexion),
# le ticket est fini tout de suite (désactivé, puis supprimé 7 jours après).
LOGOUT_SOURCE = (
    ':local i [/ip hotspot user find name=$u]; '
    ':if ([:len $i] > 0) do={ '
    ':local fin false; '
    ':local l [/ip hotspot user get $i limit-bytes-total]; '
    ':if ([:typeof $l] = "num" && $l > 0) do={ '
    ':if (([/ip hotspot user get $i bytes-in] + [/ip hotspot user get $i bytes-out]) >= $l) do={ :set fin true } }; '
    ':local t [/ip hotspot user get $i limit-uptime]; '
    ':if ([:typeof $t] = "time" && $t > 0s) do={ :if ([/ip hotspot user get $i uptime] >= $t) do={ :set fin true } }; '
    ':if ($fin) do={ :local e [:parse [/system script get [find name="aderlink-end"] source]]; $e u=$u } '
    '}'
)
ON_LOGOUT = ':local f [:parse [/system script get [find name="aderlink-logout"] source]]; $f u=$user'


def duration(seconds):
    """Secondes -> durée RouterOS (ex. 1d02:30:00)."""
    seconds = int(seconds)
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return (f"{d}d" if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def on_login(validity_s):
    """Script « on-login » d'un profil : vide si le forfait n'a pas de validité."""
    if not validity_s:
        return ""
    return (':local f [:parse [/system script get [find name="aderlink-start"] source]]; '
            f'$f u=$user v="{duration(validity_s)}"')


def ping_source(ok_url):
    """Le routeur se signale à l'application. « keep-result » (RouterOS 6) et « output » (RouterOS 7)
    ne s'écrivent pas pareil : la bonne commande n'est lue qu'au moment de l'exécution (:parse)."""
    v6 = f'/tool fetch url=\\"{ok_url}\\" keep-result=no'
    v7 = f'/tool fetch url=\\"{ok_url}\\" output=none'
    return (':local c; :if ([:pick [/system resource get version] 0 1] = "6") do={ '
            f':set c [:parse "{v6}"] }} else={{ :set c [:parse "{v7}"] }}; :do {{ $c }} on-error={{}}')


def q(text):
    """Chaîne entre guillemets pour un fichier .rsc ($ et " protégés)."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def tunnel_lines(wg, n, report_base, vpn_user, vpn_password):
    """Tunnel vers le serveur ADER LINK, quelle que soit la version de RouterOS.

    - RouterOS 7 : WireGuard (10.66.0.n). Le routeur crée sa clé et l'envoie au serveur.
    - RouterOS 6 : OpenVPN en TCP (10.67.0.n), avec un identifiant propre à ce routeur.
    Les commandes sont lues au moment de l'exécution (:parse) : chaque version ne lit que les siennes.
    """
    pub = re.sub(r"[^A-Za-z0-9+/=]", "", wg["pubkey"])
    endpoint = re.sub(r"[^A-Za-z0-9.\-:]", "", wg["endpoint"])
    server = wg.get("server", "10.66.0.1")
    lan = (':if (([:len [/interface list find name="LAN"]] > 0) && ([:len [/interface list member find interface="{i}"]] = 0)) do={{ '
           '/interface list member add list=LAN interface="{i}" comment="ADER LINK" }}; ')
    v7 = (
        ':if ([:len [/interface wireguard find name="wg-aderlink"]] = 0) do={ '
        '/interface wireguard add name="wg-aderlink" listen-port=13231 comment="ADER LINK" }; '
        '/interface wireguard peers remove [find comment="ADER LINK"]; '
        f'/interface wireguard peers add interface="wg-aderlink" public-key="{pub}" endpoint-address={endpoint} '
        f'endpoint-port={int(wg.get("port", 51820))} allowed-address={server}/32 persistent-keepalive=25s comment="ADER LINK"; '
        '/ip address remove [find comment="ADER LINK tunnel"]; '
        f'/ip address add address={wg.get("net", "10.66.0")}.{n}/24 interface="wg-aderlink" comment="ADER LINK tunnel"; '
        + lan.format(i="wg-aderlink") +
        f'/tool fetch url=("{report_base}/wg?pub=" . [/interface wireguard get [find name="wg-aderlink"] public-key]) output=none; '
        ':put "Tunnel ADER LINK (WireGuard) : OK"'
    )
    user = re.sub(r"[^A-Za-z0-9]", "", vpn_user)
    pwd = re.sub(r"[^A-Za-z0-9]", "", vpn_password)
    v6 = (
        '/interface ovpn-client remove [find name="ovpn-aderlink"]; '
        f'/interface ovpn-client add name="ovpn-aderlink" connect-to={endpoint} port={int(wg.get("ovpn_port", 1194))} '
        f'mode=ip user="{user}" password="{pwd}" cipher=aes256 auth=sha1 add-default-route=no comment="ADER LINK"; '
        + lan.format(i="ovpn-aderlink") +
        f'/tool fetch url="{report_base}/ovpn" keep-result=no; '
        ':put "Tunnel ADER LINK (OpenVPN) : OK"'
    )
    return [
        "# 6. Tunnel securise vers le serveur ADER LINK (WireGuard en RouterOS 7, OpenVPN en RouterOS 6)",
        ':if ([:pick [/system resource get version] 0 1] = "7") do={ :local t [:parse ' + q(v7) + ']; $t } '
        'else={ :local t [:parse ' + q(v6) + ']; $t }',
    ]


def install_script(name, password, server_ip, api_port, ok_url, profiles, username="aderlink", extra=(), api_from=None):
    """Script complet, importé par le routeur avec la ligne « /tool fetch … /import … »."""
    name = re.sub(r"[^A-Za-z0-9 \-]", "", name)
    srv = api_from or server_ip  # adresse d'où ADER LINK se connecte à l'API (le tunnel, s'il existe)
    user = re.sub(r"[^A-Za-z0-9._\-]", "", username) or "aderlink"
    lines = [
        f"# ADER LINK - installation automatique : {name}",
        "# 1. Acces de l application (compte aderlink, API ouverte seulement pour le serveur)",
        ':if ([:len [/user group find name="aderlink-api"]] = 0) do={ /user group add name="aderlink-api" '
        'policy=read,write,api comment="ADER LINK" } else={ /user group set [find name="aderlink-api"] '
        'policy=read,write,api }',
        f':if ([:len [/user find name="{user}"]] = 0) do={{ /user add name="{user}" group="aderlink-api" '
        f'password={q(password)} address={srv}/32 comment="ADER LINK" }} else={{ /user set [find name="{user}"] '
        f'group="aderlink-api" password={q(password)} address={srv}/32 }}',
        f"/ip service set api disabled=no port={int(api_port)} address={srv}/32",
        "# 2. Heure exacte (apres une coupure de courant, l horloge du routeur repart a zero :",
        "#    sans la bonne heure, les tickets n expireraient pas a l heure prevue)",
        ':do { /ip cloud set update-time=yes } on-error={}',
        ':do { :local n [:parse "/system ntp client set enabled=yes servers=pool.ntp.org"]; $n } on-error={ '
        ':do { :local n [:parse "/system ntp client set enabled=yes server-dns-names=pool.ntp.org"]; $n } on-error={} }',
        "# 3. Expiration des tickets par le routeur lui-meme",
        '/system script remove [find name="aderlink-login"]',
        '/system script remove [find name="aderlink-start"]',
        '/system script add name="aderlink-start" dont-require-permissions=yes policy=read,write '
        f'comment="ADER LINK : expiration des tickets" source={q(LOGIN_SOURCE)}',
        '/system script remove [find name="aderlink-end"]',
        '/system script add name="aderlink-end" dont-require-permissions=yes policy=read,write '
        f'comment="ADER LINK : ticket fini = desactive, supprime apres {KEEP_DAYS} jours" source={q(END_SOURCE)}',
        '/system script remove [find name="aderlink-logout"]',
        '/system script add name="aderlink-logout" dont-require-permissions=yes policy=read,write '
        f'comment="ADER LINK : Go ou temps consommes = ticket fini" source={q(LOGOUT_SOURCE)}',
        "# 4. Liaison avec l application toutes les 5 minutes",
        '/system script remove [find name="aderlink-ping"]',
        '/system script add name="aderlink-ping" dont-require-permissions=yes policy=read,write,policy,test '
        f'comment="ADER LINK : liaison avec l application" source={q(ping_source(ok_url))}',
        '/system scheduler remove [find name="ADER LINK"]',
        '/system scheduler add name="ADER LINK" interval=5m start-time=startup '
        'on-event="/system script run aderlink-ping" comment="ADER LINK : liaison avec l application"',
        "# 5. Profils des forfaits (vitesse, appareils, validite)",
    ]
    for p in profiles:
        prof = re.sub(r"[^A-Za-z0-9\-]", "", p["profile"])
        lines.append(f':if ([:len [/ip hotspot user profile find name="{prof}"]] = 0) do={{ '
                     f'/ip hotspot user profile add name="{prof}" }}')
        lines.append(f'/ip hotspot user profile set [find name="{prof}"] shared-users={int(p["shared"])} '
                     f'rate-limit={q(p["rate"])} on-login={q(on_login(p["validity_s"]))} on-logout={q(ON_LOGOUT)}')
    lines += list(extra)
    lines += [
        ':if ([:len [/ip hotspot find]] = 0) do={ :put "ATTENTION : aucun serveur hotspot sur ce routeur. '
        'Creez-le dans IP > Hotspot > Hotspot Setup." }',
        "/system script run aderlink-ping",
        ':log info "ADER LINK installe"',
        f':put "ADER LINK : installation terminee ({name}). Voir System > Scheduler et System > Scripts."',
        "",
    ]
    return "\r\n".join(lines)
