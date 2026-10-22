/* ADER LINK — création de tickets : 1. routeur → 2. profil → 3. quantité → terminé */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var S = { zones: [], status: {}, zone: null, pv: null, forfaits: [], forfait: null, result: null, editing: null };
  var PARENT = { "pv": "routeur", "forfait": "pv", "forfait/nouveau": "forfait", "forfait/modifier": "forfait", "quantite": "forfait", "fini": "routeur" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function api(path, body) {
    var opt = { credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers["Content-Type"] = "application/json";
      opt.headers["X-AderLink"] = "1";
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("Non connecté"); }
      return r.json().catch(function () { throw new Error("Redémarrez ADER LINK (ancienne version en cours)"); }).then(function (d) {
        if (!r.ok) throw new Error(d.error || "Erreur " + r.status);
        return d;
      });
    });
  }
  function num(id) {
    var v = $(id).value.trim().replace(",", ".");
    return v === "" ? 0 : Math.max(0, parseFloat(v) || 0);
  }
  function dur(s) {
    s = Math.round(+s);
    if (!s) return "Illimitée";
    if (s % 86400 === 0) return s / 86400 + (s === 86400 ? " jour" : " jours");
    if (s % 3600 === 0) return s / 3600 + (s === 3600 ? " heure" : " heures");
    if (s >= 3600) return Math.floor(s / 3600) + " h " + String(Math.round(s % 3600 / 60)).padStart(2, "0");
    return Math.round(s / 60) + " min";
  }
  function data(mb) {
    mb = +mb;
    if (!mb) return "Illimitées";
    return mb >= 1024 ? nf.format(Math.round(mb / 1024 * 100) / 100) + " Go" : nf.format(mb) + " Mo";
  }
  function notice(id, kind, html) { var el = $(id); el.className = "notice show notice-" + kind; el.innerHTML = html; }
  function hideNotice(id) { $(id).className = "notice"; }
  function zoneObj() { return S.zones.find(function (z) { return z.id === S.zone; }) || {}; }
  function initials(name) {
    var w = String(name).replace(/^zone\s+/i, "").split(/\s+/).filter(Boolean);
    return ((w[0] || "?")[0] + (w[1] ? w[1][0] : (w[0] || "")[1] || "")).toUpperCase();
  }

  // --- navigation (bouton Précédent + flèche retour du navigateur) -------------------------
  var trail = [], lastView = location.hash.slice(1) || "routeur";
  function go(view) {
    var cur = location.hash.slice(1) || "routeur";
    if (view === cur) { show(view); return; }
    if (trail.length && trail[trail.length - 1] === view) { history.back(); return; }
    location.hash = view;
  }
  window.addEventListener("hashchange", function () {
    var v = location.hash.slice(1) || "routeur";
    if (trail.length && trail[trail.length - 1] === v) trail.pop(); else trail.push(lastView);
    lastView = v;
    show(v);
  });
  function show(view) {
    if (view !== "routeur" && !S.zone) view = "routeur";
    if ((view === "forfait" || view === "forfait/nouveau" || view === "forfait/modifier" || view === "quantite") && !S.pv) view = "pv";
    if (view === "forfait/modifier" && !S.editing) view = "forfait";
    if (view === "forfait/nouveau" && S.editing) { S.editing = null; resetProfileForm(); }
    if (view === "quantite" && !S.forfait) view = "forfait";
    if (view === "fini" && !S.result) view = "routeur";
    var pane = { "routeur": "routeur", "pv": "pv", "forfait": "forfait", "forfait/nouveau": "nouveau", "forfait/modifier": "nouveau", "quantite": "quantite", "fini": "fini" }[view] || "routeur";
    ["routeur", "pv", "forfait", "nouveau", "quantite", "fini"].forEach(function (p) { $("pane-" + p).hidden = p !== pane; });
    var order = ["routeur", "pv", "forfait", "quantite"];
    var cur = view === "fini" ? 4 : order.indexOf(view.indexOf("forfait/") === 0 ? "forfait" : view);
    if (pane === "pv") renderPv();
    document.querySelectorAll("#stepper li").forEach(function (li, i) {
      li.className = i < cur ? "is-done" : i === cur ? "is-current" : "";
      li.querySelector(".dot-n").innerHTML = i < cur ? "✓" : String(i + 1);
    });
    if (pane === "quantite") renderQuantite();
    if (pane === "nouveau") { profileMode(); pSummary(); setTimeout(function () { $("pName").focus(); }, 60); }
    if (pane === "routeur") setTimeout(function () { $("routerSearch").focus({ preventScroll: true }); }, 60);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  document.addEventListener("click", function (e) {
    var g = e.target.closest("[data-go]");
    if (g) { go(g.dataset.go); return; }
    var b = e.target.closest("[data-back]");
    if (b) go(PARENT[location.hash.slice(1)] || "routeur");
  });

  // --- 1. routeur -------------------------------------------------------------------------
  function renderRouters() {
    var q = $("routerSearch").value.trim().toLowerCase();
    var list = S.zones.filter(function (z) { return !q || (z.name + " " + z.ssid).toLowerCase().indexOf(q) >= 0; });
    $("routers").innerHTML = list.map(function (z) {
      var s = S.status[z.id] || {};
      var on = s.status === "online";
      return "<button type='button' class='pick' role='radio' aria-checked='" + (S.zone === z.id) + "' data-zone='" + esc(z.id) + "'>" +
        "<span class='avatar'>" + esc(initials(z.name)) + "</span>" +
        "<span class='pick-main'><span class='pick-title'>" + esc(z.name) + "</span>" +
        "<span class='pick-sub'>Wi-Fi " + esc(z.ssid) + " · <span class='" + (on ? "st-on" : "st-off") + "'>● " +
        (on ? "En ligne" : s.status === "offline" ? "Hors ligne" : "En attente") + "</span></span></span>" +
        "<span class='radio' aria-hidden='true'></span></button>";
    }).join("") || "<p class='pane-sub'>" + (S.zones.length ? "Aucun routeur ne correspond." : "Aucun routeur configuré dans config.json.") + "</p>";
  }
  $("routerSearch").addEventListener("input", renderRouters);
  $("routers").addEventListener("click", function (e) {
    var b = e.target.closest("[data-zone]");
    if (!b) return;
    if (S.zone !== b.dataset.zone) { S.forfait = null; S.pv = null; }
    S.zone = b.dataset.zone;
    try { localStorage.setItem("aderlink-zone", S.zone); } catch (err) { /* ignoré */ }
    renderRouters();
    loadForfaits().then(function () { go("pv"); });
  });

  // --- 2. point de vente ----------------------------------------------------------------------
  function renderPv() {
    var z = zoneObj(), list = z.points_vente || [];
    $("ctxRouterPv").textContent = z.name || "";
    $("pvs").innerHTML = list.length ? list.map(function (p) {
      return "<button type='button' class='pick' role='radio' aria-checked='" + (S.pv && S.pv.id === p.id) + "' data-pv='" + p.id + "'>" +
        "<span class='avatar av-orange' aria-hidden='true'>" + window.AderShell.icon("store") + "</span>" +
        "<span class='pick-main'><span class='pick-title'>" + esc(p.name) + "</span><span class='pick-sub mono'>" + esc(p.code) + "</span></span>" +
        "<span class='radio' aria-hidden='true'></span></button>";
    }).join("") : "<div class='empty-step'>Aucun point de vente : ajoutez-en un dans la page Routeurs.</div>";
  }
  $("pvs").addEventListener("click", function (e) {
    var b = e.target.closest("[data-pv]");
    if (!b) return;
    S.pv = (zoneObj().points_vente || []).find(function (p) { return p.id === +b.dataset.pv; });
    renderPv();
    go("forfait");
  });

  // --- 2. forfait -------------------------------------------------------------------------
  function loadForfaits() {
    $("ctxRouter").textContent = zoneObj().name || "";
    $("ctxRouter2").textContent = zoneObj().name || "";
    return api("/api/zones/" + encodeURIComponent(S.zone) + "/forfaits").then(function (d) {
      S.forfaits = d.forfaits;
      if (S.forfait) S.forfait = S.forfaits.find(function (f) { return f.id === S.forfait.id; }) || null;
      renderForfaits();
    }).catch(function (e) { $("forfaits").innerHTML = "<p class='pane-sub'>" + esc(e.message) + "</p>"; });
  }
  function renderForfaits() {
    $("forfaits").innerHTML = S.forfaits.length ? S.forfaits.map(function (f) {
      var bits = [f.uptime_s ? dur(f.uptime_s) : null, f.data_mb ? data(f.data_mb) : null,
        f.validity_s ? "valable " + dur(f.validity_s) : null].filter(Boolean).join(" · ") || "Illimité";
      return "<div class='pick-row'><button type='button' class='pick' role='radio' aria-checked='" + (S.forfait && S.forfait.id === f.id) + "' data-forfait='" + f.id + "'>" +
        "<span class='avatar avatar-ticket c-" + esc(f.color || "bleu") + "' aria-hidden='true'>" + window.AderShell.icon("ticket") + "</span>" +
        "<span class='pick-main'><span class='pick-title'>" + esc(f.name) + "</span><span class='pick-sub'>" + esc(bits) + "</span>" +
        "<span class='pick-stock'>" + nf.format(f.stock) + " en stock" + (f.down_mbps ? " · " + nf.format(f.down_mbps) + " Mb/s" : "") + "</span></span>" +
        "<span class='pick-price num'>" + nf.format(f.price) + " F</span><span class='radio' aria-hidden='true'></span></button>" +
        "<button type='button' class='row-edit need-gerant' data-edit='" + f.id + "' aria-label='Modifier le profil " + esc(f.name) + "'>" + window.AderShell.icon("pencil") + "<span>Modifier</span></button>" +
        "<button type='button' class='row-del' data-archive='" + f.id + "' aria-label='Retirer le profil " + esc(f.name) + "'>Retirer</button></div>";
    }).join("") : "<div class='empty-step'>Aucun profil sur ce routeur. Créez le premier, par exemple <strong>1 heure à 200 F</strong>.</div>";
    $("manage").hidden = !S.forfaits.length;
  }
  $("manage").addEventListener("click", function () {
    var on = $("forfaits").classList.toggle("managing");
    $("manage").textContent = on ? "Terminer" : "Retirer un profil";
    $("manage").setAttribute("aria-pressed", on);
  });
  $("forfaits").addEventListener("click", function (e) {
    var ed = e.target.closest("[data-edit]");
    if (ed) {
      S.editing = S.forfaits.find(function (f) { return f.id === +ed.dataset.edit; }) || null;
      if (S.editing) { fillProfileForm(S.editing); go("forfait/modifier"); }
      return;
    }
    var b = e.target.closest("[data-forfait]");
    if (b) {
      S.forfait = S.forfaits.find(function (f) { return f.id === +b.dataset.forfait; });
      renderForfaits();
      go("quantite");
      return;
    }
    var a = e.target.closest("[data-archive]");
    if (!a) return;
    if (a.dataset.confirm !== "1") {
      a.dataset.confirm = "1"; a.textContent = "Confirmer ?";
      setTimeout(function () { if (a.isConnected) { a.dataset.confirm = ""; a.textContent = "Retirer"; } }, 4000);
      return;
    }
    api("/api/forfaits/" + a.dataset.archive + "/archive", {}).then(function () {
      if (S.forfait && S.forfait.id === +a.dataset.archive) S.forfait = null;
      loadForfaits();
    });
  });

  // --- 2 bis. nouveau forfait --------------------------------------------------------------
  function profileValues() {
    var down = num("pDown");
    var up = num("pUp") || (down ? Math.max(0.5, Math.round(down / 2 * 10) / 10) : 0);
    return {
      zone_id: S.zone, name: $("pName").value.trim(), price: Math.round(num("pPrice")),
      uptime_s: Math.round(num("pUptimeN") * +$("pUptimeU").value),
      data_mb: Math.round(num("pDataN") * +$("pDataU").value),
      validity_s: Math.round(num("pValN") * +$("pValU").value),
      down_mbps: down, up_mbps: up, shared_users: Math.round(num("pShared")) || 1, color: pColor()
    };
  }
  // Remplit le formulaire avec un profil existant (modification)
  function unitSet(nId, uId, value, units) {
    var u = units.filter(function (x) { return value && value % x === 0; }).pop() || units[0];
    $(nId).value = value ? Math.round(value / u * 100) / 100 : "";
    $(uId).value = String(u);
  }
  function fillProfileForm(f) {
    $("pane-nouveau").reset();
    $("pName").value = f.name;
    $("pPrice").value = f.price;
    unitSet("pDataN", "pDataU", f.data_mb, [1, 1024]);
    unitSet("pValN", "pValU", f.validity_s, [3600, 86400]);
    unitSet("pUptimeN", "pUptimeU", f.uptime_s, [60, 3600, 86400]);
    if (!f.uptime_s) $("pUptimeU").value = "3600";
    $("pShared").value = f.shared_users || 1;
    $("pDown").value = f.down_mbps || "";
    $("pUp").value = f.up_mbps || "";
    var c = document.querySelector("input[name=pColor][value='" + (f.color || "bleu") + "']");
    if (c) c.checked = true;
    var more = !!(f.uptime_s || f.down_mbps || f.up_mbps || (f.shared_users || 1) > 1);
    $("pMore").checked = more;
    $("pMoreBox").hidden = !more;
  }
  function resetProfileForm() {
    $("pane-nouveau").reset();
    $("pMoreBox").hidden = true;
  }
  function profileMode() {
    var ed = !!S.editing;
    $("npTitle").textContent = ed ? "Modifier le profil" : "Nouveau profil";
    $("npVerb").textContent = ed ? "Sur" : "Créé sur";
    $("pEditHint").hidden = !ed;
    $("pSave").textContent = ed ? "✓ Enregistrer les modifications" : "✓ Enregistrer";
    hideNotice("pNotice");
  }
  function pColor() {
    var c = document.querySelector("input[name=pColor]:checked");
    return c ? c.value : "bleu";
  }
  function pSummary() {
    var v = profileValues();
    $("miniCard").className = "mini-card c-" + v.color;
    $("miniCard").querySelector("img").src = "/static/" + (v.color === "orange" ? "wordmark-mono.svg" : "wordmark-white.svg");
    $("mcPrice").textContent = nf.format(v.price) + " F";
    $("mcSpecs").textContent = [v.data_mb ? data(v.data_mb) : "Données illimitées", v.validity_s ? dur(v.validity_s) : null,
      v.uptime_s ? dur(v.uptime_s) + " de connexion" : null].filter(Boolean).join(" · ");
  }
  $("pMore").addEventListener("change", function () { $("pMoreBox").hidden = !this.checked; });
  $("pane-nouveau").addEventListener("input", pSummary);
  $("pane-nouveau").addEventListener("change", pSummary);
  $("pane-nouveau").addEventListener("submit", function (e) {
    e.preventDefault();
    var v = profileValues();
    if (!v.name) { notice("pNotice", "err", "Donnez un nom au profil."); $("pName").focus(); return; }
    if ($("pPrice").value.trim() === "") { notice("pNotice", "err", "Indiquez le prix du ticket."); $("pPrice").focus(); return; }
    if (v.shared_users > 10) { notice("pNotice", "err", "10 appareils maximum par ticket."); $("pShared").focus(); return; }
    var btn = $("pSave");
    btn.disabled = true; btn.textContent = "Envoi au routeur…";
    hideNotice("pNotice");
    var editing = S.editing;
    api(editing ? "/api/forfaits/" + editing.id : "/api/forfaits", v).then(function (f) {
      S.editing = null;
      resetProfileForm();
      return loadForfaits().then(function () {
        S.forfait = S.forfaits.find(function (x) { return x.id === f.id; }) || null;
        renderForfaits();
        go("forfait");
      });
    }).catch(function (err) { notice("pNotice", "err", esc(err.message)); })
      .then(function () { btn.disabled = false; profileMode(); });
  });

  // --- 3. quantité ---------------------------------------------------------------------------
  function renderQuantite() {
    var f = S.forfait;
    $("crumbs").innerHTML = "<span>" + esc(zoneObj().name) + "</span><span aria-hidden='true'>›</span><span>" + esc(S.pv ? S.pv.name : "") +
      "</span><span aria-hidden='true'>›</span><span>" + esc(f.name) + "</span>";
    if (!$("codeLen").dataset.touched) $("codeLen").value = zoneObj().code_length || 8;
    lenUi();
    var tile = function (cls, label, value) { return "<div class='tile " + cls + "'><strong>" + value + "</strong><span>" + label + "</span></div>"; };
    $("tiles").innerHTML = tile("t-price", "Prix unité", nf.format(f.price) + " F") +
      (f.uptime_s ? tile("t-time", "Durée", esc(dur(f.uptime_s))) : "") +
      tile("t-val", "Validité", esc(f.validity_s ? dur(f.validity_s) : "Sans limite")) +
      tile("t-data", "Données", esc(data(f.data_mb)));
    var ro = zoneObj().roaming;
    $("roBox").hidden = !ro;
    if (ro) $("roText").textContent = "Valable sur les " + ro.routers + " routeurs du réseau « " + ro.name + " »";
    hideNotice("createNotice");
    total();
  }
  function qty() { return Math.round(num("count")); }
  function setQty(n) { $("count").value = Math.max(1, Math.min(1000, n)); total(); }
  function total() {
    var n = qty();
    $("total").textContent = S.forfait ? nf.format(n * S.forfait.price) + " F CFA" : "–";
    document.querySelectorAll("#chips button").forEach(function (b) { b.setAttribute("aria-pressed", +b.dataset.n === n ? "true" : "false"); });
  }
  $("count").addEventListener("input", total);
  function lenUi() {
    var n = Math.round(num("codeLen"));
    document.querySelectorAll("#lenChips button").forEach(function (b) { b.setAttribute("aria-pressed", +b.dataset.len === n ? "true" : "false"); });
    var digits = "1234567890123".slice(0, Math.max(0, Math.min(12, n)));
    $("codeSample").textContent = n >= 8 ? digits.replace(/(\d{4})(?=\d)/g, "$1 ") : digits.replace(/(\d{3})(?=\d)/g, "$1 ");
  }
  $("codeLen").addEventListener("input", function () { this.dataset.touched = "1"; lenUi(); });
  $("lenChips").addEventListener("click", function (e) {
    var b = e.target.closest("[data-len]"); if (!b) return;
    $("codeLen").value = b.dataset.len; $("codeLen").dataset.touched = "1"; lenUi();
  });
  $("minus").addEventListener("click", function () { setQty(qty() - (qty() > 10 ? 10 : 1)); });
  $("plus").addEventListener("click", function () { setQty(qty() + (qty() >= 10 ? 10 : 1)); });
  $("chips").addEventListener("click", function (e) { var b = e.target.closest("[data-n]"); if (b) setQty(+b.dataset.n); });
  $("pane-quantite").addEventListener("submit", function (e) {
    e.preventDefault();
    var n = qty(), len = Math.round(num("codeLen"));
    if (n < 1 || n > 1000) { notice("createNotice", "err", "Quantité : entre 1 et 1000 tickets."); $("count").focus(); return; }
    if (len < 4 || len > 12) { notice("createNotice", "err", "Longueur du code : entre 4 et 12 chiffres."); $("codeLen").focus(); return; }
    var btn = $("createBtn");
    btn.disabled = true; btn.textContent = "Envoi au routeur… (" + n + " tickets)";
    hideNotice("createNotice");
    api("/api/batches", { forfait_id: S.forfait.id, pos_id: S.pv ? S.pv.id : null, count: n, code_length: len, password_mode: $("pwMode").value, note: $("note").value,
      roaming: !!zoneObj().roaming && $("roOn").checked })
      .then(function (d) {
        S.result = d;
        $("doneTitle").textContent = "Lot n° " + d.id + " généré";
        $("doneSub").innerHTML = "<strong>" + nf.format(d.created) + " ticket" + (d.created > 1 ? "s" : "") + " « " + esc(S.forfait.name) + " »</strong> ajouté" +
          (d.created > 1 ? "s" : "") + (d.roaming ? " · <span class='ro-badge'>ADER Roaming</span> valables sur tout le réseau « " + esc(zoneObj().roaming.name) + " »" : " sur " + esc(zoneObj().name)) + " · valeur " + nf.format(d.created * S.forfait.price) + " F CFA" +
          (d.error ? "<br><span class='err-text'>Arrêt après une erreur du routeur : " + esc(d.error) + "</span>" : "");
        $("donePrint").href = "/imprimer?lot=" + d.id;
        $("note").value = "";
        loadForfaits();
        go("fini");
      })
      .catch(function (err) { notice("createNotice", "err", esc(err.message)); })
      .then(function () { btn.disabled = false; btn.textContent = "✓ Générer le lot"; });
  });
  $("again").addEventListener("click", function () { S.result = null; go("quantite"); });

  // --- démarrage --------------------------------------------------------------------------------
  Promise.all([api("/api/zones"), api("/api/overview?range=6h").catch(function () { return { zones: [] }; })]).then(function (r) {
    $("demoBanner").hidden = !r[0].demo;
    S.zones = r[0].zones;
    (r[1].zones || []).forEach(function (z) { S.status[z.id] = z; });
    var saved = null;
    try { saved = localStorage.getItem("aderlink-zone"); } catch (e) { /* ignoré */ }
    var start = location.hash.slice(1);
    var keep = S.zones.find(function (z) { return z.id === saved; });
    renderRouters();
    if (keep && start && start !== "routeur") {
      S.zone = keep.id;
      if ((keep.points_vente || []).length === 1) S.pv = keep.points_vente[0];
      renderRouters();
      loadForfaits().then(function () { show(start); });
    } else {
      if (start) history.replaceState(null, "", location.pathname);
      lastView = "routeur";
      show("routeur");
    }
  });
})();
