/* ADER LINK — liste des lots */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var all = [], zones = [], filter = "";

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
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur"); return d; });
    });
  }
  function dur(s) {
    s = Math.round(+s);
    if (!s) return "durée illimitée";
    if (s % 86400 === 0) return s / 86400 + (s === 86400 ? " jour" : " jours");
    if (s % 3600 === 0) return s / 3600 + " h";
    return Math.round(s / 60) + " min";
  }
  function data(mb) { mb = +mb; return !mb ? "données illimitées" : mb >= 1024 ? nf.format(Math.round(mb / 1024 * 100) / 100) + " Go" : nf.format(mb) + " Mo"; }
  function date(ts) { return new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }

  function render() {
    $("filter").innerHTML = zones.length > 1 ? ["<button type='button' data-z='' aria-pressed='" + (!filter) + "'>Tous</button>"].concat(zones.map(function (z) {
      return "<button type='button' data-z='" + esc(z.id) + "' aria-pressed='" + (filter === z.id) + "'>" + esc(z.name) + "</button>";
    })).join("") : "";
    var list = all.filter(function (b) { return !filter || b.zone_id === filter; });
    var sold = list.reduce(function (a, b) { return a + b.used * b.price; }, 0);
    $("lotsSub").textContent = nf.format(list.length) + " lot" + (list.length > 1 ? "s" : "") + " · " + nf.format(sold) + " F CFA vendus";
    $("lots").innerHTML = list.length ? list.map(function (b) {
      var left = b.count - b.used - b.removed;
      var pct = b.count ? Math.round(100 * b.used / b.count) : 0;
      return "<article class='card lot-card' id='lot-" + b.id + "'>" +
        "<div class='lot-head'><span class='lot-ico' aria-hidden='true'>" + window.AderShell.icon("ticket") + "</span>" +
        "<div class='lot-main'><strong>" + nf.format(b.count) + " tickets « " + esc(b.note || b.profile) + " »</strong>" +
        "<span class='zone-meta'>Lot n° " + b.id + " · " + esc(b.zone_name) + (b.pos_name ? " · " + esc(b.pos_name) : "") + " · " + date(b.created_at) + "</span></div>" +
        "<div class='lot-pct'><strong class='num'>" + pct + " %</strong><span class='zone-meta'>vendus</span></div></div>" +
        "<div class='progress'><span data-w='" + pct + "'></span></div>" +
        "<div class='lot-facts'><span><b class='num'>" + nf.format(b.price) + " F</b> l'unité</span><span>" + dur(b.uptime_s) + " · " + data(b.data_mb) + "</span>" +
        "<span><b class='num'>" + nf.format(b.used) + "</b> vendus · <b class='num'>" + nf.format(left) + "</b> en stock" + (b.removed ? " · " + b.removed + " supprimés" : "") + "</span>" +
        "<span>Vendu : <b class='num'>" + nf.format(b.used * b.price) + " F</b></span></div>" +
        "<div class='lot-actions'><a class='btn btn-sm' href='/imprimer?lot=" + b.id + "' target='_blank'>Imprimer les cartes</a>" +
        (left > 0 ? "<button class='btn btn-sm btn-outline' type='button' data-remove='" + b.id + "' data-left='" + left + "'>Supprimer " + left + " non utilisés</button>" : "") +
        "</div></article>";
    }).join("") : "<div class='card empty'>Aucun lot. <a class='link' href='/tickets'>Créer des tickets</a></div>";
    $("lots").querySelectorAll(".progress span").forEach(function (s) { s.style.width = s.dataset.w + "%"; });
    var target = location.hash && document.querySelector(location.hash);
    if (target) { target.classList.add("is-target"); target.scrollIntoView({ block: "center" }); }
  }
  function load() {
    return Promise.all([api("/api/batches"), api("/api/zones")]).then(function (r) {
      $("demoBanner").hidden = !r[0].demo;
      all = r[0].batches; zones = r[1].zones;
      render();
    });
  }
  $("filter").addEventListener("click", function (e) {
    var b = e.target.closest("[data-z]"); if (!b) return;
    filter = b.dataset.z; history.replaceState(null, "", location.pathname); render();
  });
  $("lots").addEventListener("click", function (e) {
    var b = e.target.closest("[data-remove]");
    if (!b) return;
    if (b.dataset.confirm !== "1") {
      b.dataset.confirm = "1"; b.textContent = "Confirmer la suppression ?"; b.classList.remove("btn-outline");
      setTimeout(function () { if (b.isConnected) { b.dataset.confirm = ""; b.classList.add("btn-outline"); b.textContent = "Supprimer " + b.dataset.left + " non utilisés"; } }, 4000);
      return;
    }
    b.disabled = true; b.textContent = "Suppression…";
    api("/api/batches/" + b.dataset.remove + "/remove-unused", {}).then(load).catch(function (err) { b.disabled = false; b.textContent = "Erreur : " + err.message; });
  });
  load();
  setInterval(function () { if (!document.hidden) load(); }, 60000);
})();
