/* ADER LINK — état d'un ticket : ce qui reste (Go, temps, validité), connecté ou non, fini ou non */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function dur(s) {
    s = Math.round(+s);
    if (!s) return "Illimitée";
    if (s % 86400 === 0) return s / 86400 + (s === 86400 ? " jour" : " jours");
    if (s % 3600 === 0) return s / 3600 + (s === 3600 ? " heure" : " heures");
    return Math.round(s / 60) + " min";
  }
  function data(mb) { mb = +mb; return !mb ? "Illimitées" : mb >= 1024 ? nf.format(Math.round(mb / 1024 * 100) / 100) + " Go" : nf.format(mb) + " Mo"; }
  function date(ts) { return new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }); }

  fetch("/api/zones", { credentials: "same-origin" }).then(function (r) {
    if (r.status === 401) { location.href = "/login"; return null; }
    return r.json();
  }).then(function (d) { if (d) $("demoBanner").hidden = !d.demo; });

  function size(b) {
    b = +b || 0;
    if (b >= 1073741824) return nf.format(Math.round(b / 1073741824 * 100) / 100) + " Go";
    if (b >= 1048576) return nf.format(Math.round(b / 1048576)) + " Mo";
    return nf.format(Math.round(b / 1024)) + " Ko";
  }
  function hms(s) {
    s = Math.max(0, Math.round(+s || 0));
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    var out = (d ? d + " j " : "") + (h ? h + " h " : "") + (m || (!d && !h) ? m + " min" : "");
    return out.trim();
  }
  // Barre « reste X sur Y »
  function meter(label, left, total, fmt) {
    var pct = total ? Math.max(0, Math.min(100, Math.round(left / total * 100))) : 0;
    return "<div class='st-meter'><div class='st-meter-head'><span>" + label + "</span><strong>Reste " + fmt(left) +
      "</strong><small>sur " + fmt(total) + "</small></div><div class='st-bar'><span data-w='" + pct + "'" +
      (pct <= 15 ? " class='low'" : "") + "></span></div></div>";
  }
  var WHY = {
    data: ["state-exp", "Fini : données épuisées"],
    uptime: ["state-exp", "Fini : temps de connexion épuisé"],
    validity: ["state-exp", "Fini : validité terminée"],
    disabled: ["state-exp", "Fini : désactivé sur le routeur"],
    purged: ["state-off", "Fini et supprimé du routeur"],
    removed: ["state-off", "Supprimé (jamais vendu)"]
  };
  function card(t) {
    var b = t.batch, L = t.live || {};
    var st = L.finished ? WHY[L.finished] : t.first_used ? ["state-used", L.online && L.online.length ? "Connecté en ce moment" : "En cours (pas connecté)"]
      : ["state-new", "Neuf, jamais utilisé"];
    var html = "<div class='check-card " + st[0] + "'><div class='state'>" + st[1] + "</div>" +
      "<div class='check-code num'>" + esc(t.code) + "</div>";
    if (t.first_used && !L.finished) {
      html += "<div class='st-meters'>" +
        (+b.data_mb ? meter("Données", L.data_left, b.data_mb * 1048576, size) : "") +
        (+b.uptime_s ? meter("Temps de connexion", L.uptime_left, +b.uptime_s, hms) : "") +
        (t.ends_at ? meter("Validité", Math.max(0, t.ends_at - Date.now() / 1000), +b.validity_s, hms) : "") + "</div>";
    }
    if (L.online && L.online.length) {
      html += "<div class='st-online'>" + L.online.map(function (o) {
        return "<span>📶 Sur <strong>" + esc(o.zone) + "</strong>" + (o.ip ? " · " + esc(o.ip) : "") + (o.mac ? " · " + esc(o.mac) : "") + "</span>";
      }).join("") + "</div>";
    }
    if (L.source === "offline") html += "<p class='st-warn'>Le routeur ne répond pas : la consommation affichée n'est peut-être pas à jour.</p>";
    html += "<dl class='check-dl'>" +
      "<div><dt>" + (b.roaming_id ? "Réseau Roaming" : "Routeur") + "</dt><dd>" + esc(b.roaming_id ? b.roaming_name : b.zone_name) + "</dd></div>" +
      "<div><dt>Profil</dt><dd>" + esc(b.note || b.profile) + " · " + nf.format(b.price) + " F</dd></div>" +
      "<div><dt>Données utilisées</dt><dd>" + (t.first_used ? size(L.bytes) : "–") + (+b.data_mb ? " sur " + data(b.data_mb) : "") + "</dd></div>" +
      "<div><dt>Temps utilisé</dt><dd>" + (t.first_used ? hms(L.uptime) : "–") + (+b.uptime_s ? " sur " + dur(b.uptime_s) : "") + "</dd></div>" +
      "<div><dt>1re connexion</dt><dd>" + (t.first_used ? date(t.first_used) : "–") + "</dd></div>" +
      "<div><dt>Fin de validité</dt><dd>" + (t.ends_at ? date(t.ends_at) : b.validity_s ? dur(b.validity_s) + " après la 1re connexion" : "Sans limite") + "</dd></div>" +
      (L.delete_at && L.finished === "validity" ? "<div><dt>Supprimé du routeur le</dt><dd>" + date(L.delete_at) + "</dd></div>" : "") +
      (L.finished && L.finished !== "validity" && L.finished !== "removed" && L.finished !== "purged" && !b.roaming_id ? "<div><dt>Suppression</dt><dd>7 jours après la fin</dd></div>" : "") +
      "<div><dt>Lot</dt><dd><a class='link' href='/lots#lot-" + b.id + "'>n° " + b.id + "</a></dd></div>" +
      "</dl></div>";
    return html;
  }

  $("checkForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var code = $("checkCode").value.replace(/\s/g, "");
    if (!code) return;
    fetch("/api/tickets/find?code=" + encodeURIComponent(code), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      var box = $("checkResult");
      if (!d.results || !d.results.length) {
        box.innerHTML = "<div class='check-card state-unknown'><div class='state'>Code inconnu</div><p>Aucun ticket ADER LINK ne porte le code <strong>" + esc(code) + "</strong>. Vérifiez la saisie.</p></div>";
        return;
      }
      box.innerHTML = d.results.map(card).join("");
      Array.prototype.forEach.call(box.querySelectorAll("[data-w]"), function (el) { el.style.width = el.dataset.w + "%"; });
    });
  });
})();
