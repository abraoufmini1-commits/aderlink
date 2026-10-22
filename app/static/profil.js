/* ADER LINK — page Mon profil */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function api(path, body) {
    var opt = { credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.headers["X-AderLink"] = "1";
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("Non connecté"); }
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur " + r.status); return d; });
    });
  }
  function money(n) { return nf.format(Math.round(n || 0)) + " F"; }
  function msg(kind, text) { var n = $("pwMsg"); n.className = "notice show notice-" + kind; n.textContent = text; }

  api("/api/me").then(function (me) {
    $("demoBanner").hidden = !me.demo;
    $("who").innerHTML = "<strong>" + esc(me.name) + "</strong> · " + esc(me.role_label) + " · <span class='mono'>" + esc(me.username) + "</span>";
    if (me.role !== "admin") {
      var box = $("subState");
      if (!me.expires_at) { box.className = "sub-state sub-free"; box.innerHTML = "<div><strong>Sans limite</strong><span>Votre accès n'a pas de date de fin.</span></div>"; }
      else {
        var rest = me.expires_at - me.now, d = Math.floor(rest / 86400), h = Math.floor(rest % 86400 / 3600);
        var end = new Date(me.expires_at * 1000).toLocaleString("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
        box.className = "sub-state " + (rest < 3 * 86400 ? "sub-soon" : "sub-ok");
        box.innerHTML = "<div><strong>Reste " + (d ? d + " jour" + (d > 1 ? "s" : "") + " " : "") + h + " h</strong><span>Jusqu'au " + esc(end) +
          ". Pour prolonger, contactez votre fournisseur.</span></div>";
      }
      $("subCard").hidden = false;
    }
    if (me.role === "admin") $("pwHint").textContent = "Administrateur : 10 caractères minimum";
    if (me.role === "revendeur") {
      api("/api/sales?days=30").then(function (s) {
        var row = function (k, v) { return "<div><dt>" + k + "</dt><dd class='num'>" + v + "</dd></div>"; };
        $("statList").innerHTML =
          row("Vendus (30 jours)", nf.format(s.period.tickets)) + row("Montant (30 jours)", money(s.period.amount)) +
          row("Ma commission (" + nf.format(me.commission) + " %)", money(s.period.amount * me.commission / 100)) +
          row("Tickets créés au total", nf.format(s.all_time.generated));
        $("stats").hidden = false;
      });
    }
  });

  $("pwForm").addEventListener("submit", function (e) {
    e.preventDefault();
    if ($("new1").value !== $("new2").value) return msg("err", "Les deux nouveaux mots de passe ne sont pas identiques");
    $("pwSave").disabled = true;
    api("/api/me/password", { old: $("old").value, new: $("new1").value }).then(function () {
      msg("ok", "Mot de passe changé. Utilisez-le à votre prochaine connexion.");
      $("pwForm").reset();
    }).catch(function (err) { msg("err", err.message); }).then(function () { $("pwSave").disabled = false; });
  });
})();
