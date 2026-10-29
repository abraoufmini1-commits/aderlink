/* ADER LINK — Formules (administrateur) : les offres vendues aux gérants */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var ic = window.AderShell.icon;
  var S = { plans: [], billing: {}, labels: {}, clients: {} };
  var FEATS = ["remote", "antennas", "roaming", "excel", "support"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function money(n) { return nf.format(Math.round(n || 0)) + " F"; }
  function api(path, body) {
    var opt = { credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.headers["X-AderLink"] = "1";
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("Non connecté"); }
      return r.json().catch(function () { throw new Error("Redémarrez ADER LINK (ancienne version en cours)"); })
        .then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur " + r.status); return d; });
    });
  }
  // seule la plus petite formule (Découverte) s'écrit « De 1 à 3 routeurs »
  function routersText(p, all) {
    var min = Math.min.apply(null, all.map(function (x) { return x.routers; }));
    return (p.routers === min ? "De 1 à " : "") + nf.format(p.routers) + " routeurs";
  }
  function roamText(p) {
    if (p.features.indexOf("roaming") < 0) return null;
    return p.roaming_routers === -1 ? "ADER Roaming sans limite" : "ADER Roaming : " + p.roaming_routers + " routeur" + (p.roaming_routers > 1 ? "s" : "");
  }

  function load() {
    return api("/api/plans").then(function (d) {
      S.plans = d.plans; S.billing = d.billing; S.labels = d.labels; S.clients = d.clients || {};
      render(); fillBilling();
    }).catch(function (e) { $("list").innerHTML = "<div class='card empty'>" + esc(e.message) + "</div>"; });
  }
  api("/api/me").then(function (me) { $("demoBanner").hidden = !me.demo; }).catch(function () {});

  function render() {
    var plansList = S.plans;
    $("list").innerHTML = S.plans.map(function (p) {
      var n = S.clients[p.id] || 0;
      var items = ["<li><span class='y'>✓</span>" + routersText(p, plansList) + "</li>",
        "<li><span class='y'>✓</span>Tickets, lots, rapports, alertes Telegram, sauvegarde</li>"].concat(FEATS.map(function (f) {
        if (f === "roaming") {
          var t = roamText(p);
          return "<li class='" + (t ? "" : "off") + "'><span class='" + (t ? "y" : "n") + "'>" + (t ? "✓" : "—") + "</span>" + esc(t || S.labels.roaming) + "</li>";
        }
        var on = p.features.indexOf(f) >= 0;
        return "<li class='" + (on ? "" : "off") + "'><span class='" + (on ? "y" : "n") + "'>" + (on ? "✓" : "—") + "</span>" + esc(S.labels[f]) + "</li>";
      }));
      return "<article class='card pl-card" + (p.highlight ? " is-hl" : "") + "'>" + (p.highlight ? "<span class='pl-badge'>Conseillée</span>" : "") +
        "<div class='pl-name'>" + esc(p.name) + "</div><div class='pl-price num'>" + money(p.price) + " <small>/ mois</small></div>" +
        "<ul class='pl-list'>" + items.join("") + "</ul>" +
        "<div class='pl-meta'>" + nf.format(n) + " gérant" + (n > 1 ? "s" : "") + " avec cette formule</div>" +
        "<div class='pl-acts'><button type='button' class='btn btn-sm' data-act='edit' data-id='" + p.id + "'>" + ic("pencil") + " Modifier</button>" +
        "<button type='button' class='btn btn-sm btn-outline' data-act='archive' data-id='" + p.id + "'>Retirer</button></div></article>";
    }).join("") || "<div class='card empty'>Aucune formule. Cliquez sur <strong>+ Nouvelle formule</strong>.</div>";
  }
  $("list").addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (!b) return;
    var p = S.plans.find(function (x) { return x.id === +b.dataset.id; });
    if (b.dataset.act === "edit") edit(p);
    else if (b.dataset.confirm !== "1") { b.dataset.confirm = "1"; b.textContent = "Confirmer ?"; }
    else api("/api/plans/" + p.id + "/archive", {}).then(load).catch(function (err) { b.textContent = err.message; });
  });
  $("addBtn").addEventListener("click", function () { edit(null); });

  // --- feuille ---------------------------------------------------------------------------
  var lastFocus = null;
  function sheet(title, icon, body, foot) {
    lastFocus = document.activeElement;
    $("bsTitle").textContent = title; $("bsIco").innerHTML = ic(icon); $("bsClose").innerHTML = ic("x");
    $("bsBody").innerHTML = body;
    $("bsFoot").innerHTML = foot || "<button class='btn-prev' type='button' data-close>Fermer</button>";
    $("bs").hidden = false; $("bsShade").classList.add("open"); document.body.classList.add("no-scroll");
    setTimeout(function () { var f = $("bs").querySelector("input"); (f || $("bsClose")).focus(); }, 50);
  }
  function close() {
    $("bs").hidden = true; $("bsShade").classList.remove("open"); document.body.classList.remove("no-scroll");
    if (lastFocus) lastFocus.focus();
  }
  $("bsClose").addEventListener("click", close);
  $("bsShade").addEventListener("click", close);
  $("bs").addEventListener("click", function (e) { if (e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("bs").hidden) close(); });
  function msg(kind, text) { var n = $("bsMsg"); if (n) { n.className = "notice show notice-" + kind; n.innerHTML = text; } }

  function edit(p) {
    var v = p || { name: "", price: "", routers: "", roaming_routers: 0, features: [], highlight: false };
    var body = "<form id='pForm' class='form-grid form-grid-2' autocomplete='off' novalidate>" +
      "<div class='f span2'><label for='fName'>Nom de la formule</label><input id='fName' maxlength='30' value='" + esc(v.name) + "' placeholder='Ex : Pro'></div>" +
      "<div class='f'><label for='fPrice'>Prix par mois (F CFA)</label><input id='fPrice' type='number' min='0' step='500' inputmode='numeric' value='" + esc(v.price) + "'></div>" +
      "<div class='f'><label for='fRouters'>Nombre de routeurs</label><input id='fRouters' type='number' min='1' step='1' inputmode='numeric' value='" + esc(v.routers) + "'></div>" +
      "<fieldset class='span2'><legend>Options comprises</legend><div class='feat-check'>" + FEATS.map(function (f) {
        return "<label class='check-opt'><input type='checkbox' name='feat' value='" + f + "'" + (v.features.indexOf(f) >= 0 ? " checked" : "") + "><span>" + esc(S.labels[f]) + "</span></label>";
      }).join("") + "</div></fieldset>" +
      "<div class='f' id='fRoamBox'><label for='fRoam'>Routeurs en Roaming</label><input id='fRoam' type='number' min='-1' step='1' inputmode='numeric' value='" + esc(v.roaming_routers) + "'>" +
      "<small class='hint'>−1 = sans limite</small></div>" +
      "<label class='check-opt span2'><input type='checkbox' id='fHl'" + (v.highlight ? " checked" : "") + "><span>Mettre en avant (« Conseillée »)</span></label>" +
      "</form><div class='notice' id='bsMsg' role='status'></div>";
    sheet(p ? "Modifier " + p.name : "Nouvelle formule", "layers", body,
      "<button class='btn-prev' type='button' data-close>Annuler</button><button class='btn btn-big' type='button' id='fSave'>" + ic("check") + " Enregistrer</button>");
    function roamVis() { $("fRoamBox").hidden = !document.querySelector("input[name=feat][value=roaming]").checked; }
    document.querySelector("input[name=feat][value=roaming]").addEventListener("change", roamVis); roamVis();
    $("fSave").addEventListener("click", function () {
      var data = { name: $("fName").value, price: $("fPrice").value, routers: $("fRouters").value, roaming_routers: $("fRoam").value,
        highlight: $("fHl").checked,
        features: Array.prototype.map.call(document.querySelectorAll("input[name=feat]:checked"), function (el) { return el.value; }) };
      var btn = this; btn.disabled = true;
      api(p ? "/api/plans/" + p.id : "/api/plans", data).then(function () { close(); load(); })
        .catch(function (e) { msg("err", esc(e.message)); btn.disabled = false; });
    });
  }

  // --- réglages communs ------------------------------------------------------------------
  function fillBilling() {
    var b = S.billing;
    $("d3").value = b.discounts["3"] || 0; $("d6").value = b.discounts["6"] || 0; $("d12").value = b.discounts["12"] || 0;
    $("xr").value = b.extra_router_price; $("td").value = b.trial_days; $("gd").value = b.grace_days;
    $("tp").innerHTML = S.plans.map(function (p) { return "<option" + (p.name === b.trial_plan ? " selected" : "") + ">" + esc(p.name) + "</option>"; }).join("");
  }
  $("bSave").addEventListener("click", function () {
    var btn = this, m = $("bMsg"); btn.disabled = true;
    api("/api/billing", { discounts: { "3": $("d3").value, "6": $("d6").value, "12": $("d12").value }, extra_router_price: $("xr").value,
      trial_days: $("td").value, trial_plan: $("tp").value, grace_days: $("gd").value })
      .then(function (d) { S.billing = d.billing; m.className = "notice show notice-ok"; m.textContent = "Réglages enregistrés."; })
      .catch(function (e) { m.className = "notice show notice-err"; m.textContent = e.message; })
      .then(function () { btn.disabled = false; });
  });

  load();
})();
