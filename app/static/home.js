/* ADER LINK — accueil : évolution des ventes, totaux et derniers lots */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var days = 30, geom = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function get(path) {
    return fetch(path, { credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error(); }
      return r.json();
    });
  }
  function short(v) {
    if (v >= 1e6) return nf.format(Math.round(v / 1e5) / 10) + " M";
    if (v >= 1e3) return nf.format(Math.round(v / 100) / 10) + " k";
    return nf.format(v);
  }
  function dayLabel(ts, long) {
    return new Date(ts * 1000).toLocaleDateString("fr-FR", long ? { weekday: "long", day: "numeric", month: "long" } : { day: "numeric", month: "short" });
  }
  function niceMax(v) {
    if (v <= 0) return 1000;
    var p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }

  // --- ventes -------------------------------------------------------------------------
  function loadSales() {
    get("/api/sales?days=" + days).then(function (d) {
      var all = d.all_time;
      $("kCa").innerHTML = nf.format(all.amount) + " <small>F CFA</small>";
      $("kSold").textContent = nf.format(all.tickets);
      $("kSoldSub").textContent = all.generated
        ? Math.round(100 * all.tickets / all.generated) + " % des " + nf.format(all.generated) + " générés"
        : "Aucun ticket généré";
      $("salesAmount").textContent = nf.format(d.period.amount) + " F CFA";
      var today = d.series[d.series.length - 1] || { amount: 0, tickets: 0 };
      $("salesSub").innerHTML = nf.format(d.period.tickets) + " tickets vendus en " + days + " jours · <strong>Aujourd'hui</strong> : " +
        nf.format(today.amount) + " F, " + nf.format(today.tickets) + " ticket" + (today.tickets > 1 ? "s" : "");
      draw(d.series);
    });
  }
  function draw(series) {
    var svg = $("salesSvg");
    var W = svg.clientWidth || 800, H = svg.clientHeight || 220;
    var m = { l: 8, r: 46, t: 12, b: 26 };
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    var max = niceMax(series.reduce(function (a, p) { return Math.max(a, p.amount); }, 0));
    var n = series.length, iw = W - m.l - m.r, slot = iw / n;
    var bw = Math.max(2, Math.min(28, slot - 2));
    var Y = function (v) { return H - m.b - v / max * (H - m.t - m.b); };
    var out = "";
    [0, 0.5, 1].forEach(function (f) {
      var y = Y(max * f);
      out += '<line class="gridline' + (f ? " dashed" : "") + '" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y + '" y2="' + y + '"/>';
      if (f) out += '<text class="axis" x="' + (W - m.r + 6) + '" y="' + (y + 4) + '">' + short(max * f) + "</text>";
    });
    series.forEach(function (p, i) {
      var x = m.l + i * slot + (slot - bw) / 2, y = Y(p.amount), h = H - m.b - y;
      var cls = i === n - 1 ? "bar bar-today" : "bar";
      if (h > 0) {
        var r = Math.min(4, bw / 2, h);
        out += '<path class="' + cls + '" d="M' + x + "," + (H - m.b) + " V" + (y + r) + " Q" + x + "," + y + " " + (x + r) + "," + y +
          " H" + (x + bw - r) + " Q" + (x + bw) + "," + y + " " + (x + bw) + "," + (y + r) + " V" + (H - m.b) + ' Z"/>';
      }
    });
    var labels = [0, Math.floor((n - 1) / 2), n - 1];
    labels.forEach(function (i, k) {
      var x = m.l + i * slot + slot / 2;
      out += '<text class="axis" x="' + x + '" y="' + (H - 6) + '" text-anchor="' + (k === 0 ? "start" : k === 2 ? "end" : "middle") + '">' +
        (k === 2 ? "Aujourd'hui" : dayLabel(series[i].day)) + "</text>";
    });
    out += '<rect class="bar-hover" id="salesHover" y="' + m.t + '" height="' + (H - m.t - m.b) + '" width="' + slot + '" visibility="hidden"/>';
    out += '<rect id="salesHit" x="' + m.l + '" y="0" width="' + iw + '" height="' + H + '" fill="transparent"/>';
    svg.innerHTML = out;
    geom = { series: series, slot: slot, m: m, W: W, H: H, Y: Y };
  }
  function move(ev) {
    if (!geom) return;
    var svg = $("salesSvg"), rect = svg.getBoundingClientRect();
    var px = (ev.clientX - rect.left) * (geom.W / rect.width);
    var i = Math.max(0, Math.min(geom.series.length - 1, Math.floor((px - geom.m.l) / geom.slot)));
    var p = geom.series[i];
    var hov = $("salesHover");
    hov.setAttribute("x", geom.m.l + i * geom.slot); hov.setAttribute("visibility", "visible");
    var tip = $("salesTip");
    tip.innerHTML = "<strong>" + nf.format(p.amount) + " F</strong> · " + nf.format(p.tickets) + " ticket" + (p.tickets > 1 ? "s" : "") +
      "<br>" + esc(i === geom.series.length - 1 ? "Aujourd'hui" : dayLabel(p.day, true));
    var sx = (geom.m.l + (i + 0.5) * geom.slot) * rect.width / geom.W;
    tip.style.left = Math.min(Math.max(sx, 80), rect.width - 80) + "px";
    tip.style.top = (geom.Y(p.amount) * rect.height / geom.H - 10) + "px";
    tip.style.opacity = 1;
  }
  function leave() {
    var h = $("salesHover"); if (h) h.setAttribute("visibility", "hidden");
    $("salesTip").style.opacity = 0;
  }
  $("salesSvg").addEventListener("pointermove", move);
  $("salesSvg").addEventListener("pointerdown", move);
  $("salesSvg").addEventListener("pointerleave", leave);
  document.querySelectorAll("#salesRange button").forEach(function (b) {
    b.addEventListener("click", function () {
      days = +b.dataset.days;
      document.querySelectorAll("#salesRange button").forEach(function (o) { o.setAttribute("aria-pressed", o === b ? "true" : "false"); });
      loadSales();
    });
  });
  var rt;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { if (geom) draw(geom.series); }, 150); });

  // --- derniers lots ----------------------------------------------------------------------
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 3600) return "il y a " + Math.max(1, Math.floor(s / 60)) + " min";
    if (s < 86400) return "il y a " + Math.floor(s / 3600) + " h";
    if (s < 30 * 86400) return "il y a " + Math.floor(s / 86400) + " j";
    return new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
  }
  function loadLots() {
    get("/api/batches").then(function (d) {
      var list = (d.batches || []).slice(0, 5);
      $("recentLots").innerHTML = list.length ? list.map(function (b) {
        var pct = b.count ? Math.round(100 * b.used / b.count) : 0;
        return "<li><a href='/lots#lot-" + b.id + "'><span class='lot-ico' aria-hidden='true'>" + window.AderShell.icon("ticket") + "</span>" +
          "<span class='lot-main'><strong>" + nf.format(b.count) + " tickets « " + esc(b.note || b.profile) + " »</strong>" +
          "<span class='zone-meta'>" + esc(b.zone_name) + ", " + ago(b.created_at) + "</span></span>" +
          "<span class='lot-pct'><strong class='num'>" + pct + " %</strong><span class='zone-meta'>vendus</span></span></a></li>";
      }).join("") : "<li class='empty'>Aucun lot pour l'instant. <a class='link' href='/tickets'>Créer des tickets</a></li>";
    });
  }

  loadSales();
  loadLots();
  setInterval(function () { if (!document.hidden) { loadSales(); loadLots(); } }, 60000);
})();
