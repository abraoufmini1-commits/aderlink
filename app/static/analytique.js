/* ADER LINK — Analytique */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var ic = window.AderShell.icon;
  var A = null, days = 30;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function api(path) {
    return fetch(path, { credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("Non connecté"); }
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur " + r.status); return d; });
    });
  }
  function money(n) { return nf.format(Math.round(n || 0)) + " F"; }
  function hh(h) { return h + " h – " + ((h + 1) % 24) + " h"; }

  function load() {
    try { localStorage.setItem("aderlink-an-days", days); } catch (e) { /* ignoré */ }
    Array.prototype.forEach.call(document.querySelectorAll("#periods [data-d]"), function (b) { b.setAttribute("aria-pressed", +b.dataset.d === days); });
    api("/api/analytique?days=" + days).then(function (d) {
      A = d;
      $("demoBanner").hidden = !d.demo;
      render();
    }).catch(function (e) { $("scope").textContent = e.message; });
  }
  $("periods").addEventListener("click", function (e) {
    var b = e.target.closest("[data-d]");
    if (b) { days = +b.dataset.d; load(); }
  });

  function render() {
    var i = A.insights;
    var tile = function (icon, label, value, sub) {
      return "<div class='card insight'><span class='ins-ico'>" + ic(icon) + "</span><div><div class='ins-l'>" + label + "</div>" +
        "<div class='ins-v'>" + value + "</div><div class='ins-s'>" + sub + "</div></div></div>";
    };
    $("insights").innerHTML =
      tile("wifi", "Heure de pointe", i.peak_hour != null ? hh(i.peak_hour) : "–", i.peak_hour != null ? nf.format(Math.round(i.peak_avg)) + " connectés en moyenne" : "Pas encore de relevés") +
      tile("ticket", "On achète surtout", i.sales_hour != null ? hh(i.sales_hour) : "–", nf.format(i.tickets) + " tickets vendus sur " + days + " jours") +
      tile("chart", "Meilleur jour", i.best_day || "–", i.best_day ? money(i.best_day_avg) + " en moyenne" : "Aucune vente") +
      tile("layers", "Forfait n° 1", esc(i.top_forfait || "–"), i.avg_delay != null ? "Un ticket se vend en " + nf.format(i.avg_delay) + " j en moyenne" : "Ticket moyen : " + money(i.avg_ticket));

    bars("hoursChart", A.hours.map(function (x) { return { v: x.avg, label: x.hour + "h", tip: hh(x.hour) + " : " + nf.format(Math.round(x.avg)) + " connectés en moyenne, pic " + nf.format(x.peak) }; }), 3, i.peak_hour);
    bars("salesHourChart", A.sales_hour.map(function (x) { return { v: x.tickets, label: x.hour + "h", tip: hh(x.hour) + " : " + nf.format(x.tickets) + " tickets · " + money(x.amount) }; }), 4, i.sales_hour);
    var bestIdx = A.weekdays.reduce(function (b, x, k) { return x.avg_amount > A.weekdays[b].avg_amount ? k : b; }, 0);
    bars("weekChart", A.weekdays.map(function (x) { return { v: x.avg_amount, label: x.day.slice(0, 3), tip: x.day + " : " + money(x.avg_amount) + " en moyenne · " + nf.format(x.tickets) + " tickets" }; }), 1, i.best_day ? bestIdx : null);

    $("fRows").innerHTML = A.forfaits.length ? A.forfaits.map(function (f) {
      return "<tr><td><strong>" + esc(f.name) + "</strong></td><td class='r num'>" + nf.format(f.sold) + "</td><td class='r num'>" + money(f.amount) + "</td>" +
        "<td><div class='an-rate'><div class='rp-meter'><span data-w='" + f.sell_rate + "'></span></div><span class='num'>" + f.sell_rate + " %</span></div></td>" +
        "<td class='r num'>" + (f.delay_days != null ? nf.format(f.delay_days) + " j" : "–") + "</td><td class='r num'>" + nf.format(f.stock) + "</td></tr>";
    }).join("") : "<tr><td colspan='6' class='pane-sub'>Aucun forfait pour l'instant.</td></tr>";
    Array.prototype.forEach.call(document.querySelectorAll("#fRows [data-w]"), function (s) { s.style.width = s.dataset.w + "%"; });
  }

  // Barres simples ; la meilleure en orange
  function bars(id, data, every, best) {
    var box = $(id);
    var W = Math.max(280, box.clientWidth || 600), H = 200, L = 34, B = 24, T = 8;
    var max = Math.max.apply(null, data.map(function (d) { return d.v; }).concat([1]));
    var step = Math.pow(10, Math.floor(Math.log10(max)));
    var top = Math.ceil(max / step) * step;
    if (top / step <= 2) step /= 2;
    var bw = (W - L) / data.length;
    var y = function (v) { return T + (H - T - B) * (1 - v / top); };
    var svg = "<svg viewBox='0 0 " + W + " " + H + "' role='img' aria-label='" + esc(box.closest("section").querySelector("h2").textContent) + "'>";
    for (var v = 0; v <= top + 1e-9; v += step) {
      svg += "<line class='gridline' x1='" + L + "' x2='" + W + "' y1='" + y(v) + "' y2='" + y(v) + "'/>" +
        "<text class='axis' x='" + (L - 6) + "' y='" + (y(v) + 4) + "' text-anchor='end'>" + (v >= 1000 ? nf.format(v / 1000) + "k" : nf.format(Math.round(v * 10) / 10)) + "</text>";
    }
    data.forEach(function (d, k) {
      var h = (H - T - B) * d.v / top;
      svg += "<rect class='bar" + (k === best ? " bar-best" : "") + "' x='" + (L + k * bw + bw * 0.15).toFixed(1) + "' y='" + (H - B - h).toFixed(1) +
        "' width='" + (bw * 0.7).toFixed(1) + "' height='" + Math.max(h, d.v ? 2 : 0).toFixed(1) + "' rx='2'><title>" + esc(d.tip) + "</title></rect>";
      if (k % every === 0) svg += "<text class='axis' x='" + (L + k * bw + bw / 2).toFixed(1) + "' y='" + (H - 7) + "' text-anchor='middle'>" + esc(d.label) + "</text>";
    });
    box.innerHTML = svg + "</svg>";
  }
  var rz;
  window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(function () { if (A) render(); }, 150); });

  try { days = +localStorage.getItem("aderlink-an-days") || 30; } catch (e) { days = 30; }
  if ([7, 30, 90].indexOf(days) < 0) days = 30;
  load();
})();
