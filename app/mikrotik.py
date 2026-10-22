"""Scripts RouterOS d'ADER LINK.

Le routeur doit fonctionner seul, même quand ADER LINK est éteint :
- chaque profil hotspot « AL-… » lance, à la 1re connexion d'un ticket, un compte à rebours
  (une tâche du Scheduler « AL-EXP-<code> ») ; à la fin de la validité, le routeur coupe la
  session et supprime le ticket, puis efface la tâche ;
- le Scheduler « ADER LINK » se signale toutes les 5 minutes à l'application, qui retient
  ainsi l'adresse du routeur même si elle change.

Compatible RouterOS 6 et 7.
"""
import re

# Script appelé par le profil hotspot à chaque connexion : $u = code du ticket, $v = validité.
LOGIN_SOURCE = (
    ':if ([:len [/system scheduler find name=("AL-EXP-" . $u)]] = 0) do={ '
    '/system scheduler add name=("AL-EXP-" . $u) '
    'start-date=[/system clock get date] start-time=[/system clock get time] interval=[:totime $v] '
    'comment="ADER LINK : fin du ticket" '
    'on-event=("/ip hotspot active remove [find user=\\"" . $u . "\\"]; '
    '/ip hotspot user remove [find name=\\"" . $u . "\\"]; '
    '/system scheduler remove [find name=\\"AL-EXP-" . $u . "\\"]") '
    '}'
)


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
    return (':local f [:parse [/system script get [find name="aderlink-login"] source]]; '
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
        '/system script add name="aderlink-login" dont-require-permissions=yes policy=read,write,policy,test '
        f'comment="ADER LINK : expiration des tickets" source={q(LOGIN_SOURCE)}',
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
                     f'rate-limit={q(p["rate"])} on-login={q(on_login(p["validity_s"]))}')
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
