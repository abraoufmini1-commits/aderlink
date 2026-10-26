/* ADER LINK — impression des cartes */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var FORMATS = { nb100: 100, mini: 70, petits: 33, cartes: 10 };
  var format = "nb100";
  try { format = localStorage.getItem("aderlink-print-format3") || "nb100"; } catch (e) { /* ignoré */ }
  if (!FORMATS[format]) format = "nb100";
  var batch = null;
  var WIFI = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M2 9a14.5 14.5 0 0 1 20 0"/><path d="M12 19.5h.01"/></svg>';
  var ROUTER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01M8.5 9.5a5 5 0 0 1 7 0M6 7a8.5 8.5 0 0 1 12 0"/></svg>';

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

  function longDur(sec) {
    var n, u;
    if (sec % 86400 === 0) { n = sec / 86400; u = "jour"; }
    else if (sec % 3600 === 0) { n = sec / 3600; u = "heure"; }
    else { n = Math.round(sec / 60); u = "minute"; }
    return n + " " + u + (n > 1 ? "s" : "");
  }
  function money(b) { return nf.format(b.price) + " " + (b.currency || "FCFA"); }
  function pairs(code) { return code.replace(/(\d{2})(?=\d)/g, "$1 "); }

  // 100 par page : le même en-tête (nom du Wi-Fi, prix), mais le code en noir dans un cadre en pointillés
  function bw(code, b) {
    return '<div class="bw">' + small(code, b).replace('class="tk-code', 'class="tk-code tk-code-bw') + "</div>";
  }

  // Petit ticket : l'en-tête (nom du Wi-Fi, prix) — sans « ADER LINK », le code, puis données et validité
  function small(code, b) {
    var line = [+b.data_mb ? data(b.data_mb) : "", b.validity_s ? "Validité " + longDur(b.validity_s) : "Sans limite de validité"].filter(Boolean).join(" • ");
    return '<div class="tk c-' + esc(b.color || "bleu") + '">' +
      '<div class="tk-band"><img class="tk-mark" src="/static/logo-blue.svg" alt="">' +
      '<div class="tk-brand">' +
      '<div class="tk-ssid">' + esc(b.roaming_id ? "Roaming · " + b.roaming_name : b.ssid) + "</div></div>" +
      '<div class="tk-price">' + nf.format(b.price) + "<small>" + esc(b.currency === "FCFA" ? "F" : (b.currency || "F")) + "</small></div></div>" +
      '<div class="tk-code' + (code.length > 10 ? " long" : "") + '">' + esc(pairs(code)) + "</div>" +
      '<div class="tk-line">' + esc(line) + "</div></div>";
  }

  function card(code, b) {
    return '<div class="card c-' + esc(b.color || "bleu") + '">' +
      '<div class="band"><div class="brand"><img class="mark" src="/static/logo-blue.svg" alt="">' +
      '<div>' + (b.demo ? '<div class="brand-name"><span class="demo">Démo</span></div>' : "") +
      '<div class="ssid ssid-big">' + esc(b.roaming_id ? "ADER Roaming : " + b.roaming_name : "Wi-Fi : " + b.ssid) + "</div></div></div>" +
      '<div class="price">' + nf.format(b.price) + " <small>" + esc(b.currency === "FCFA" ? "F CFA" : (b.currency || "F CFA")) + "</small></div></div>" +
      '<div class="body"><div class="code-box"><div class="code-label">Code d\'accès</div>' +
      '<div class="code' + (code.length > 8 ? " long" : "") + '">' + esc(spaced(code)) + "</div></div>" +
      '<div class="specs">' +
      (+b.data_mb ? '<div class="spec"><div class="spec-l">Données</div><div class="spec-v">' + data(b.data_mb) + "</div></div>" : "") +
      '<div class="spec"><div class="spec-l">Validité</div><div class="spec-v">' + (b.validity_s ? longDur(b.validity_s) : "Illimitée") + "</div></div>" +
      "</div>" +
      "</div></div>";
  }

  function render() {
    var only = $("onlyNew").checked;
    var codes = batch.tickets.filter(function (t) { return !t.removed && (!only || !t.first_used); })
      .map(function (t) { return t.code; });
    var PER_PAGE = FORMATS[format];
    $("sub").textContent = nf.format(codes.length) + (format !== "cartes" ? " ticket" : " carte") + (codes.length > 1 ? "s" : "") + " · " +
      Math.max(1, Math.ceil(codes.length / PER_PAGE)) + " page" + (codes.length > PER_PAGE ? "s" : "") + " A4 · " +
      nf.format(batch.price) + " " + (batch.currency === "FCFA" ? "F CFA" : (batch.currency || "F CFA")) + " l'unité";
    if (!codes.length) {
      $("sheets").innerHTML = '<div class="empty-msg">Aucune carte à imprimer : tous les tickets de ce lot ont été utilisés ou supprimés.</div>';
      $("printBtn").disabled = true;
      return;
    }
    $("printBtn").disabled = false;
    var html = "";
    for (var i = 0; i < codes.length; i += PER_PAGE) {
      var cls = { nb100: ["sheet-bw", "grid-bw"], mini: ["sheet-small sheet-mini", "grid-small grid-mini"], petits: ["sheet-small", "grid-small"], cartes: ["", "grid"] }[format];
      html += '<section class="sheet ' + cls[0] + '"><div class="' + cls[1] + '">' +
        codes.slice(i, i + PER_PAGE).map(function (c) { return format === "cartes" ? card(c, batch) : format === "nb100" ? bw(c, batch) : small(c, batch); }).join("") + "</div></section>";
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
  $("format").value = format;
  $("format").addEventListener("change", function () {
    format = this.value;
    try { localStorage.setItem("aderlink-print-format3", format); } catch (e) { /* ignoré */ }
    if (batch) render();
  });
  $("printBtn").addEventListener("click", function () { window.print(); });
  // Nouveau lot : on revient à la création, sur le même routeur, au choix du profil
  $("newBtn").addEventListener("click", function (e) {
    if (!batch) return;
    e.preventDefault();
    try {
      localStorage.setItem("aderlink-zone", batch.zone_id);
      if (batch.roaming_id) localStorage.setItem("aderlink-roam", String(batch.roaming_id));  // lot Roaming -> nouveau lot Roaming
      else localStorage.removeItem("aderlink-roam");
    } catch (err) { /* ignoré */ }
    location.href = "/tickets#forfait";
  });

  // Nouvelle version d'ADER LINK installée : la page se recharge toute seule
  var version = null;
  setInterval(function () {
    fetch("/api/version", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      if (version && d.version !== version) location.reload();
      version = d.version;
    }).catch(function () { /* serveur en train de redémarrer */ });
  }, 30000);
})();
