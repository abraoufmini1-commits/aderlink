"""Page de connexion Wi-Fi (portail captif MikroTik) aux couleurs ADER LINK.

Les fichiers login.html, status.html et logout.html sont générés pour chaque routeur
(nom, logo, forfaits, contact) puis téléchargés par le MikroTik dans le dossier HTML
de son hotspot. Le routeur remplace lui-même les variables $(...) à l'affichage.
Tout est dans le fichier (styles, logo) : le client n'a pas encore Internet.
"""
import base64
import html
import re

ADER_MARK = (
    '<svg viewBox="0 0 100 100" width="56" height="56" aria-hidden="true"><rect width="100" height="100" rx="24" fill="#0d4fa8"/>'
    '<g fill="none" stroke="#fff" stroke-width="8" stroke-linecap="round"><path d="M19.5 39.5A43 43 0 0 1 80.5 39.5"/>'
    '<path d="M29 49A29.7 29.7 0 0 1 71 49"/><path d="M38.5 58.5A16.3 16.3 0 0 1 61.5 58.5"/></g>'
    '<circle cx="50" cy="70" r="7.5" fill="#f08a1c"/></svg>'
)

CSS = """
*{box-sizing:border-box}
html,body{margin:0}
body{min-height:100vh;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#0f1d33;
background:linear-gradient(160deg,#0a3f8a 0%,#0d4fa8 55%,#0a3f8a 100%);display:flex;align-items:center;justify-content:center;padding:18px}
.card{width:100%;max-width:400px;background:#fff;border-radius:22px;padding:24px 20px 20px;box-shadow:0 18px 50px rgba(0,0,0,.28)}
.head{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.head img,.head svg{width:56px;height:56px;border-radius:16px;flex:none;object-fit:cover}
.net{font-weight:800;font-size:20px;line-height:1.2;overflow-wrap:anywhere}
.by{font-size:12px;color:#4a5a73;margin-top:3px;letter-spacing:.04em}
.by b{color:#0a3f8a}.by b span{color:#f08a1c}
label{display:block;font-weight:700;font-size:14px;color:#4a5a73;margin-bottom:8px}
.code{width:100%;height:58px;border:2px solid #d6dfec;border-radius:14px;font-size:26px;font-weight:800;text-align:center;
letter-spacing:.12em;color:#0f1d33;background:#f5f8fc;outline:none}
.code:focus{border-color:#0d4fa8;background:#fff}
.btn{display:block;width:100%;height:54px;margin-top:14px;border:0;border-radius:14px;background:#f08a1c;color:#fff;
font-size:18px;font-weight:800;cursor:pointer;text-align:center;line-height:54px;text-decoration:none}
.btn:active{background:#d97606}
.btn-out{background:#fff;color:#0a3f8a;border:2px solid #0d4fa8;line-height:50px}
.err{background:#fdecec;color:#b42318;border-radius:12px;padding:12px 14px;font-weight:650;font-size:14px;margin-bottom:14px}
.hint{font-size:13px;color:#4a5a73;text-align:center;margin-top:10px}
.prices{margin-top:20px;border-top:1px solid #e3e9f2;padding-top:14px}
.prices h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#4a5a73;margin:0 0 10px}
.price{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;border-radius:12px;background:#f5f8fc;margin-bottom:6px;font-size:14px}
.price b{font-weight:750}.price span{color:#4a5a73;font-size:12.5px}
.price i{font-style:normal;font-weight:800;color:#0a3f8a;white-space:nowrap}
.contact{display:block;margin-top:14px;text-align:center;font-size:14px;font-weight:700;color:#0a3f8a;text-decoration:none}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:6px 0 4px}
.stat{background:#f5f8fc;border-radius:14px;padding:12px}
.stat small{display:block;font-size:12px;color:#4a5a73;font-weight:650}
.stat strong{display:block;font-size:20px;margin-top:2px}
.stat.big{grid-column:1/-1;background:#0d4fa8;color:#fff}.stat.big small{color:#d6e3f5}
.ok{background:#e6f4ec;color:#0b5e35;border-radius:12px;padding:12px 14px;font-weight:700;font-size:15px;margin-bottom:14px;text-align:center}
.foot{text-align:center;color:#d6e3f5;font-size:11px;margin-top:14px;letter-spacing:.05em}
"""

# Messages MikroTik (anglais) -> français
ERRORS_JS = r"""
var M=[[/invalid username or password|invalid password|no such user/i,"Code incorrect. Vérifiez les chiffres de votre ticket."],
[/user .* has reached uptime limit|uptime limit/i,"Le temps de ce ticket est épuisé."],
[/traffic limit|bytes limit/i,"Les données de ce ticket sont épuisées."],
[/no more sessions are allowed|already logged in|simultaneous/i,"Ce ticket est déjà utilisé sur un autre appareil."],
[/disabled|expired/i,"Ce ticket a expiré."],
[/radius|server is not responding/i,"Le serveur ne répond pas, réessayez dans un instant."]];
var e=document.getElementById("err");
if(e){var t=e.textContent;for(var i=0;i<M.length;i++){if(M[i][0].test(t)){e.textContent=M[i][1];break;}}}
"""


def _esc(s):
    return html.escape(str(s or ""), quote=True).replace("$(", "$&#40;")  # jamais de variable MikroTik injectée


def _duration(sec):
    sec = int(sec or 0)
    if not sec:
        return ""
    for size, one, many in ((86400, "jour", "jours"), (3600, "heure", "heures"), (60, "minute", "minutes")):
        if sec % size == 0:
            n = sec // size
            return f"{n} {one if n == 1 else many}"
    return f"{round(sec / 60)} minutes"


def _data(mb):
    mb = int(mb or 0)
    if not mb:
        return ""
    return f"{mb / 1024:g} Go".replace(".", ",") if mb >= 1024 else f"{mb} Mo"


def _logo(logo):
    if logo:
        mime, raw = logo
        return f'<img src="data:{mime};base64,{base64.b64encode(bytes(raw)).decode()}" alt="">'
    return ADER_MARK


def _page(title, body, script=""):
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="pragma" content="no-cache"><meta http-equiv="expires" content="-1">'
        f"<title>{title}</title><style>{CSS}</style></head><body>"
        f'<main>{body}<div class="foot">ADER LINK · Gestion de zones Wi-Fi</div></main>{script}</body></html>'
    )


def _head(name, logo):
    return (f'<div class="head">{_logo(logo)}<div><div class="net">{_esc(name)}</div>'
            '<div class="by">Wi-Fi géré par <b>ADER <span>LINK</span></b></div></div></div>')


def login_html(zone, forfaits, logo=None, contact=None):
    name = zone.get("ssid") or zone.get("name") or "Wi-Fi"
    cur = zone.get("currency") or "FCFA"
    prices = ""
    if forfaits:
        rows = []
        for f in forfaits:
            bits = [b for b in (_duration(f.get("uptime_s")), _data(f.get("data_mb")),
                                ("valable " + _duration(f.get("validity_s"))) if f.get("validity_s") else "") if b]
            rows.append(f'<div class="price"><div><b>{_esc(f["name"])}</b><br><span>{_esc(" · ".join(bits) or "Illimité")}</span></div>'
                        f'<i>{int(f.get("price") or 0):,} {_esc(cur)}</i></div>'.replace(",", " "))
        prices = '<div class="prices"><h2>Nos forfaits</h2>' + "".join(rows) + "</div>"
    phone = (contact or {}).get("phone")
    contact_html = ""
    if phone:
        digits = re.sub(r"\D", "", phone)
        contact_html = (f'<a class="contact" href="https://wa.me/{digits}">Acheter un ticket : {_esc(phone)}</a>')
    body = (
        '<div class="card">' + _head(name, logo) +
        '$(if error)<div class="err" id="err">$(error)</div>$(endif)'
        '<form name="login" action="$(link-login-only)" method="post" onsubmit="return go()">'
        '<input type="hidden" name="dst" value="$(link-orig)"><input type="hidden" name="popup" value="true">'
        '<input type="hidden" name="username" value=""><input type="hidden" name="password" value="">'
        '<label for="code">Code du ticket</label>'
        '<input class="code" id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="1234 5678" autofocus>'
        '<button class="btn" type="submit">Se connecter</button>'
        '<div class="hint">Tapez les chiffres imprimés sur votre ticket.</div></form>'
        + prices + contact_html + "</div>"
    )
    script = (
        '<script src="/md5.js"></script><script>'
        "function go(){var f=document.login;var c=document.getElementById('code').value.replace(/[^0-9A-Za-z]/g,'');"
        "if(!c){document.getElementById('code').focus();return false;}f.username.value=c;"
        "$(if chap-id)if(window.hexMD5){f.password.value=hexMD5('$(chap-id)'+c+'$(chap-challenge)');return true;}$(endif)"
        "f.password.value=c;return true;}" + ERRORS_JS + "</script>"
    )
    return _page(f"{_esc(name)} — Connexion", body, script)


def status_html(zone, logo=None):
    name = zone.get("ssid") or zone.get("name") or "Wi-Fi"
    body = (
        '<div class="card">' + _head(name, logo) +
        '<div class="ok">Vous êtes connecté à Internet</div>'
        '<div class="stats">'
        '$(if session-time-left)<div class="stat big"><small>Temps restant</small><strong>$(session-time-left)</strong></div>$(endif)'
        '<div class="stat"><small>Ticket</small><strong>$(username)</strong></div>'
        '<div class="stat"><small>Connecté depuis</small><strong>$(uptime)</strong></div>'
        '<div class="stat"><small>Reçu</small><strong>$(bytes-out-nice)</strong></div>'
        '<div class="stat"><small>Envoyé</small><strong>$(bytes-in-nice)</strong></div>'
        "</div>"
        '<a class="btn" href="$(link-orig)">Continuer</a>'
        '<a class="btn btn-out" href="$(link-logout)">Se déconnecter</a></div>'
    )
    return _page(f"{_esc(name)} — Connecté", body)


def logout_html(zone, logo=None):
    name = zone.get("ssid") or zone.get("name") or "Wi-Fi"
    body = (
        '<div class="card">' + _head(name, logo) +
        '<div class="ok">Vous êtes déconnecté. Merci et à bientôt !</div>'
        '<div class="stats"><div class="stat"><small>Ticket</small><strong>$(username)</strong></div>'
        '<div class="stat"><small>Durée</small><strong>$(uptime)</strong></div></div>'
        '<a class="btn" href="$(link-login)">Se reconnecter</a></div>'
    )
    return _page(f"{_esc(name)} — Déconnecté", body)


FILES = ("login.html", "status.html", "logout.html")


def render(name, zone, forfaits, logo, contact):
    if name == "login.html":
        return login_html(zone, forfaits, logo, contact)
    if name == "status.html":
        return status_html(zone, logo)
    if name == "logout.html":
        return logout_html(zone, logo)
    return None


SAMPLE = {"error": "", "link-login-only": "#", "link-orig": "#", "link-logout": "#", "link-login": "#",
          "username": "1234 5678", "uptime": "12m30s", "session-time-left": "47m30s",
          "bytes-in-nice": "3.2 MiB", "bytes-out-nice": "48.7 MiB", "chap-id": "", "chap-challenge": ""}


def preview(page):
    """Aperçu dans le navigateur : on remplace les variables comme le ferait le routeur."""
    page = re.sub(r"\$\(if error\).*?\$\(endif\)", "", page, flags=re.S)
    page = re.sub(r"\$\(if chap-id\).*?\$\(endif\)", "", page, flags=re.S)
    page = re.sub(r"\$\(if [a-z-]+\)|\$\(endif\)", "", page)
    return re.sub(r"\$\(([a-z-]+)\)", lambda m: SAMPLE.get(m.group(1), ""), page)


def install_rsc(base, token):
    """Script : chaque hotspot du routeur reçoit les 3 pages dans son dossier HTML."""
    lines = [
        "# ADER LINK - page de connexion Wi-Fi",
        ':if ([:len [/ip hotspot find]] = 0) do={ :put "ATTENTION : aucun hotspot sur ce routeur. '
        'Creez-le dans IP > Hotspot > Hotspot Setup."; :error "pas de hotspot" }',
        ":foreach h in=[/ip hotspot find] do={",
        "  :local prof [/ip hotspot get $h profile]",
        "  :local dir [/ip hotspot profile get [find name=$prof] html-directory]",
        '  :foreach f in={"login.html";"status.html";"logout.html"} do={',
        f'    /tool fetch url=("http://{base}/portail/{token}/" . $f) dst-path=($dir . "/" . $f)',
        "  }",
        "}",
        ':log info "ADER LINK : page de connexion installee"',
        ':put "ADER LINK : page de connexion installee. Testez avec un telephone sur le Wi-Fi."',
        "",
    ]
    return "\r\n".join(lines)
