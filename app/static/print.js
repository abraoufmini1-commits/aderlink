/* ADER LINK — impression des cartes */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var PER_PAGE = 10;
  var batch = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function dur(s) {
    s = +s;
    if (!s) return "Illimitée";
    if (s % 86400 === 0) return s / 86400 + " j";
    if (s % 3600 === 0) return s / 3600 + " h";
    return Math.round(s / 60) + " min";
  }
  function data(mb) {
    mb = +mb;
    if (!mb) return "Illimitées";
    return mb >= 1024 ? nf.format(mb / 1024) + " Go" : nf.format(mb) + " Mo";
  }
  function spaced(code) {
    return code.length >= 8 ? code.replace(/(\d{4})(?=\d)/g, "$1 ") : code.replace(/(\d{3})(?=\d)/g, "$1 ");
  }

  function card(code, b) {
    return '<div class="card c-' + esc(b.color || "bleu") + '">' +
      '<div class="band"><div class="brand"><img class="mark" src="/static/logo-blue.svg" alt="">' +
      '<div><div class="brand-name"><img class="word" src="/static/' + (b.color === "orange" ? "wordmark-mono.svg" : "wordmark-white.svg") + '" alt="ADER LINK">' + (b.demo ? '<span class="demo">Démo</span>' : "") + "</div>" +
      '<div class="ssid">Wi-Fi : ' + esc(b.ssid) + "</div></div></div>" +
      '<div class="price">' + nf.format(b.price) + " <small>F CFA</small></div></div>" +
      '<div class="body"><div class="code-box"><div class="code-label">Code d\'accès</div>' +
      '<div class="code' + (code.length > 8 ? " long" : "") + '">' + esc(spaced(code)) + "</div></div>" +
      '<div class="specs">' +
      '<div class="spec"><div class="spec-l">Durée</div><div class="spec-v">' + dur(b.uptime_s) + "</div></div>" +
      '<div class="spec"><div class="spec-l">Données</div><div class="spec-v">' + data(b.data_mb) + "</div></div>" +
      '<div class="spec"><div class="spec-l">Validité</div><div class="spec-v">' + (b.validity_s ? dur(b.validity_s) : "Illimitée") + "</div></div>" +
      "</div>" +
      '<div class="howto">Connectez-vous au Wi-Fi <b>' + esc(b.ssid) + '</b>, ouvrez le navigateur et saisissez ce code.</div>' +
      '<div class="foot"><span>' + (b.validity_s ? "Valable " + dur(b.validity_s) + " après la 1re connexion" : "Sans date limite") + "</span><span>Lot " + b.id + "</span></div>" +
      "</div></div>";
  }

  function render() {
    var only = $("onlyNew").checked;
    var codes = batch.tickets.filter(function (t) { return !t.removed && (!only || !t.first_used); })
      .map(function (t) { return t.code; });
    $("sub").textContent = nf.format(codes.length) + " carte" + (codes.length > 1 ? "s" : "") + " · " +
      Math.max(1, Math.ceil(codes.length / PER_PAGE)) + " page" + (codes.length > PER_PAGE ? "s" : "") + " A4 · " +
      nf.format(batch.price) + " F CFA l'unité";
    if (!codes.length) {
      $("sheets").innerHTML = '<div class="empty-msg">Aucune carte à imprimer : tous les tickets de ce lot ont été utilisés ou supprimés.</div>';
      $("printBtn").disabled = true;
      return;
    }
    $("printBtn").disabled = false;
    var html = "";
    for (var i = 0; i < codes.length; i += PER_PAGE) {
      html += '<section class="sheet"><div class="grid">' +
        codes.slice(i, i + PER_PAGE).map(function (c) { return card(c, batch); }).join("") + "</div></section>";
    }
    $("sheets").innerHTML = html;
  }

  var id = new URLSearchParams(location.search).get("lot");
  fetch("/api/batches/" + encodeURIComponent(id), { credentials: "same-origin" })
    .then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error(); }
      return r.json();
    })
    .then(function (b) {
      if (b.error) { $("title").textContent = b.error; return; }
      batch = b;
      document.title = "Lot " + b.id + " — impression — ADER LINK";
      $("title").textContent = "Lot n° " + b.id + " — " + (b.note || b.zone_name);
      render();
    });
  $("onlyNew").addEventListener("change", function () { if (batch) render(); });
  $("printBtn").addEventListener("click", function () { window.print(); });
})();
