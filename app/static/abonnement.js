/* ADER LINK — Mon abonnement (gérant) : sa formule, ce qui est compris, et les autres formules */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var FEATS = ["remote", "antennas", "roaming", "excel", "support"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function money(n) { return nf.format(Math.round(n || 0)) + " F"; }
  function date(ts) { return new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" }); }
  function wa(contact, text) {
    var phone = String(contact.phone || "").replace(/[^0-9]/g, "");
    if (phone && phone.length <= 8) phone = "227" + phone;  // numéro du Niger sans indicatif
    return phone ? "https://wa.me/" + phone + "?text=" + encodeURIComponent(text) : null;
  }

  fetch("/api/subscription", { credentials: "same-origin" }).then(function (r) {
    if (r.status === 401) { location.href = "/login"; throw new Error("Non connecté"); }
    return r.json();
  }).then(render).catch(function (e) { $("head").innerHTML = "<div class='card empty'>" + esc(e.message) + "</div>"; });

  function render(d) {
    if (d.none) { $("head").innerHTML = "<div class='card empty'>Pas d'abonnement sur ce compte.</div>"; return; }
    var lim = d.limits, plan = d.plan, labels = d.labels;
    var days = d.days_left, end = d.expires_at;
    var endCls = end == null ? "" : days <= 0 ? "over" : days <= 7 ? "warn" : "";
    var tile = function (k, v, small, cls) { return "<div class='card ab-tile " + (cls || "") + "'><span>" + k + "</span><strong class='num'>" + v + "</strong>" + (small ? "<small>" + small + "</small>" : "") + "</div>"; };
    var roam = lim.roaming_routers === -1 ? "sans limite" : nf.format(lim.roaming_routers);
    $("head").innerHTML =
      tile("Formule", esc(plan ? plan.name : "Sans formule"), plan ? money(plan.price) + " par mois" + (d.extra_routers ? " + " + d.extra_routers + " routeur(s) en plus" : "") : "Demandez une formule à votre fournisseur") +
      tile("Fin de l'abonnement", end == null ? "Sans limite" : date(end), end == null ? "" : days <= 0 ? "Terminé" : "Encore " + nf.format(days) + " jour" + (days > 1 ? "s" : ""), endCls) +
      tile("Routeurs", nf.format(d.used.routers) + " / " + nf.format(lim.max_routers), d.used.routers >= lim.max_routers ? "Limite atteinte" : nf.format(lim.max_routers - d.used.routers) + " encore possible(s)", d.used.routers >= lim.max_routers ? "warn" : "") +
      (lim.features.roaming ? tile("Routeurs en Roaming", nf.format(d.used.roaming) + " / " + roam) : "");
    $("feats").innerHTML = ["<li><span class='y'>✓</span>Tickets, impression, lots, rapports, alertes Telegram, sauvegarde</li>"].concat(FEATS.map(function (f) {
      var on = lim.features[f];
      return "<li class='" + (on ? "" : "off") + "'><span class='" + (on ? "y" : "n") + "'>" + (on ? "✓" : "—") + "</span>" + esc(labels[f]) + "</li>";
    })).join("");

    var b = d.billing;
    $("plans").innerHTML = d.plans.map(function (p) {
      var current = plan && plan.id === p.id;
      var items = ["<li><span class='y'>✓</span>De 1 à " + nf.format(p.routers) + " routeurs</li>"].concat(FEATS.map(function (f) {
        var on = p.features.indexOf(f) >= 0;
        var t = f === "roaming" && on ? (p.roaming_routers === -1 ? "ADER Roaming sans limite" : "ADER Roaming : " + p.roaming_routers + " routeurs") : labels[f];
        return "<li class='" + (on ? "" : "off") + "'><span class='" + (on ? "y" : "n") + "'>" + (on ? "✓" : "—") + "</span>" + esc(t) + "</li>";
      }));
      var quotes = p.quotes.map(function (q) {
        return "<div>" + q.months + " mois<b class='num'>" + money(q.total) + "</b>" + (q.discount ? "<em>−" + nf.format(q.discount) + " %</em>" : "") + "</div>";
      }).join("");
      var text = (current ? "Bonjour, je veux renouveler ma formule " : "Bonjour, je veux passer à la formule ") + p.name + " sur ADER LINK (compte : " + d.name + "). Combien de mois : ";
      var link = wa(d.contact, text);
      return "<article class='card pl-card" + (current ? " is-current" : p.highlight ? " is-hl" : "") + "'>" +
        (current ? "<span class='pl-badge'>Votre formule</span>" : p.highlight ? "<span class='pl-badge'>Conseillée</span>" : "") +
        "<div class='pl-name'>" + esc(p.name) + "</div><div class='pl-price num'>" + money(p.price) + " <small>/ mois</small></div>" +
        "<ul class='pl-list'>" + items.join("") + "</ul><div class='pl-quotes'>" + quotes + "</div>" +
        (link ? "<a class='btn btn-block" + (current ? "" : " btn-outline") + "' href='" + esc(link) + "' target='_blank' rel='noopener'>" +
          (current ? "Renouveler" : "Choisir " + esc(p.name)) + "</a>" :
          "<p class='pl-meta'>Contactez " + esc(d.contact.name || "votre fournisseur") + " pour " + (current ? "renouveler" : "changer de formule") + ".</p>") + "</article>";
    }).join("");
    if (b.extra_router_price) $("plans").insertAdjacentHTML("afterend", "<p class='pane-sub'>Besoin d'un ou deux routeurs de plus sans changer de formule ? " +
      money(b.extra_router_price) + " par routeur et par mois.</p>");

    if (d.history && d.history.length) {
      $("histCard").hidden = false;
      $("hist").innerHTML = d.history.map(function (h) {
        return "<li><strong>" + esc(h.label) + "</strong>" + (h.amount ? " · " + money(h.amount) : "") + "<small> — " + date(h.at) + (h.new_expires ? " · fin le " + date(h.new_expires) : "") + "</small></li>";
      }).join("");
    }
  }
})();
