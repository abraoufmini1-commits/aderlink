/* ADER LINK — contrôle d'un ticket */
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
      box.innerHTML = d.results.map(function (t) {
        var st = t.removed ? ["state-off", "Supprimé"] : t.expired_at ? ["state-exp", "Expiré"] : t.first_used ? ["state-used", "En cours d'utilisation"] : ["state-new", "Neuf, jamais utilisé"];
        var b = t.batch;
        return "<div class='check-card " + st[0] + "'><div class='state'>" + st[1] + "</div>" +
          "<div class='check-code num'>" + esc(t.code) + "</div>" +
          "<dl class='check-dl'>" +
          "<div><dt>Routeur</dt><dd>" + esc(b.zone_name) + "</dd></div>" +
          "<div><dt>Forfait</dt><dd>" + esc(b.note || b.profile) + " · " + nf.format(b.price) + " F</dd></div>" +
          "<div><dt>Durée / données</dt><dd>" + dur(b.uptime_s) + " · " + data(b.data_mb) + "</dd></div>" +
          "<div><dt>Lot</dt><dd><a class='link' href='/lots#lot-" + b.id + "'>n° " + b.id + "</a></dd></div>" +
          "<div><dt>1re connexion</dt><dd>" + (t.first_used ? date(t.first_used) : "–") + "</dd></div>" +
          "<div><dt>Fin de validité</dt><dd>" + (t.ends_at ? date(t.ends_at) : b.validity_s ? dur(b.validity_s) + " après la 1re connexion" : "Sans limite") + "</dd></div>" +
          "</dl></div>";
      }).join("");
    });
  });
})();
