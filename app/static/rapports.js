/* ADER LINK — Rapport des ventes */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var R = null;
  var preset = "7";

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

  // --- dates (jours locaux, au format AAAA-MM-JJ) -------------------------------------
  function iso(d) { return d.toISOString().slice(0, 10); }
  function parse(s) { return new Date(s + "T00:00:00Z"); }
  function addDays(s, n) { var d = parse(s); d.setUTCDate(d.getUTCDate() + n); return iso(d); }
  function longDate(s) {
    return parse(s).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  }
  function fullDate(s) {
    return parse(s).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  }
  function range(p, today) {
    var d = parse(today);
    if (p === "today") return [today, today];
    if (p === "yesterday") return [addDays(today, -1), addDays(today, -1)];
    if (p === "month") return [today.slice(0, 8) + "01", today];
    if (p === "lastmonth") {
      var first = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, 1));
      var last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 0));
      return [iso(first), iso(last)];
    }
    return [addDays(today, -(+p - 1)), today];
  }

  // --- chargement ------------------------------------------------------------------------
  function load(from, to) {
    var q = from ? "?from=" + from + "&to=" + to : "";
    return api("/api/report" + q).then(function (d) {
      R = d;
      $("demoBanner").hidden = !d.demo;
      $("dFrom").value = d.from; $("dTo").value = d.to;
      $("dFrom").max = $("dTo").max = d.today;
      render();
      try { localStorage.setItem("aderlink-report", preset); } catch (e) { /* ignoré */ }
    }).catch(function (e) { $("scope").textContent = e.message; });
  }
  function choose(p) {
    preset = p;
    Array.prototype.forEach.call(document.querySelectorAll("#presets [data-p]"), function (b) {
      b.setAttribute("aria-pressed", b.dataset.p === p);
    });
    if (!R) return;
    var r = range(p, R.today);
    load(r[0], r[1]);
  }
  $("presets").addEventListener("click", function (e) {
    var b = e.target.closest("[data-p]");
    if (b) choose(b.dataset.p);
  });
  function custom() {
    if (!$("dFrom").value || !$("dTo").value) return;
    preset = "";
    Array.prototype.forEach.call(document.querySelectorAll("#presets [data-p]"), function (b) { b.setAttribute("aria-pressed", "false"); });
    load($("dFrom").value, $("dTo").value);
  }
  $("dFrom").addEventListener("change", custom);
  $("dTo").addEventListener("change", custom);

  // --- affichage -------------------------------------------------------------------------
  function render() {
    var t = R.totals, p = R.previous, n = R.by_day.length;
    var periodTxt = R.from === R.to ? fullDate(R.from) : "du " + fullDate(R.from) + " au " + fullDate(R.to);
    var who = R.me.role === "revendeur" ? "Mes ventes" : R.zones.length === 1 ? R.zones[0] : R.zones.length + " routeurs";
    $("scope").textContent = who + " · " + periodTxt;
    $("printPeriod").textContent = periodTxt + " · " + who;
    $("printFoot").textContent = "Imprimé le " + new Date().toLocaleString("fr-FR") + " · ADER LINK";

    $("kAmount").textContent = nf.format(t.amount);
    var trend = $("kTrend");
    if (!p.amount && !t.amount) trend.textContent = "Aucune vente";
    else if (!p.amount) trend.textContent = "Rien sur les " + n + " jours d'avant";
    else {
      var pct = Math.round((t.amount - p.amount) / p.amount * 100);
      trend.textContent = (pct >= 0 ? "▲ +" : "▼ ") + pct + " % par rapport aux " + n + " jours d'avant";
      trend.className = "kpi-sub " + (pct >= 0 ? "trend-up" : "trend-down");
    }
    $("kTickets").textContent = nf.format(t.tickets);
    $("kAvg").textContent = t.tickets ? "Moyenne " + money(t.amount / n) + " par jour" : "–";
    $("kGen").textContent = nf.format(t.generated);
    $("kGenSub").textContent = t.generated ? "valeur " + money(t.generated_value) : "aucun lot sur la période";
    if (R.me.role === "revendeur") {
      $("kLastLabel").textContent = "Ma commission (" + nf.format(R.me.commission) + " %)";
      $("kLast").textContent = nf.format(R.commission || 0) + " F";
      $("kLastSub").textContent = "sur les ventes de la période";
    } else {
      $("kLast").textContent = nf.format(t.stock);
    }

    bars();
    table("byZone", R.by_zone, t);
    table("byForfait", R.by_forfait, t);
    table("byPos", R.by_pos, t);
    table("bySeller", R.by_seller, t);

    $("dayRows").innerHTML = R.by_day.slice().reverse().map(function (d) {
      var day = d.date;
      return "<tr" + (d.tickets ? "" : " class='muted'") + "><td>" + esc(longDate(day)) + "</td><td class='r num'>" + nf.format(d.tickets) +
        "</td><td class='r num'>" + money(d.amount) + "</td></tr>";
    }).join("");
    $("fT").textContent = nf.format(t.tickets);
    $("fA").textContent = money(t.amount);
  }

  function table(id, rows, t) {
    if (!rows.length) { $(id).innerHTML = "<p class='pane-sub'>Aucune vente sur la période.</p>"; return; }
    var max = rows[0].amount || 1;
    $(id).innerHTML = "<ul class='rp-list'>" + rows.map(function (r) {
      var share = t.amount ? Math.round(r.amount / t.amount * 100) : 0;
      return "<li><div class='rp-row'><span class='rp-name'>" + esc(r.label) + "</span><span class='rp-amt num'>" + money(r.amount) + "</span></div>" +
        "<div class='rp-meter'><span data-w='" + (r.amount / max * 100).toFixed(1) + "'></span></div>" +
        "<div class='rp-sub'>" + nf.format(r.tickets) + " ticket" + (r.tickets > 1 ? "s" : "") + " · " + share + " %</div></li>";
    }).join("") + "</ul>";
    // largeur des barres par le CSSOM (la politique de sécurité interdit les styles écrits dans le HTML)
    Array.prototype.forEach.call($(id).querySelectorAll("[data-w]"), function (s) { s.style.width = s.dataset.w + "%"; });
  }

  // Barres : une par jour (regroupées par semaine au-delà de 62 jours)
  function bars() {
    var data = R.by_day;
    if (data.length > 62) {
      var weeks = [];
      data.forEach(function (d, i) {
        if (i % 7 === 0) weeks.push({ date: d.date, tickets: 0, amount: 0 });
        var w = weeks[weeks.length - 1]; w.tickets += d.tickets; w.amount += d.amount;
      });
      data = weeks;
    }
    var W = Math.max(300, $("bars").clientWidth || 720), H = 230, L = 40, B = 26, T = 10;
    var max = Math.max.apply(null, data.map(function (d) { return d.amount; }).concat([1]));
    var step = Math.pow(10, Math.floor(Math.log10(max)));
    var top = Math.ceil(max / step) * step;
    if (top / step <= 2) step /= 2;
    var bw = (W - L) / data.length;
    var y = function (v) { return T + (H - T - B) * (1 - v / top); };
    var svg = "<svg viewBox='0 0 " + W + " " + H + "' role='img' aria-label='Montant des ventes par jour'>";
    for (var v = 0; v <= top + 0.001; v += step) {
      svg += "<line class='gridline' x1='" + L + "' x2='" + W + "' y1='" + y(v) + "' y2='" + y(v) + "'/>" +
        "<text class='axis' x='" + (L - 6) + "' y='" + (y(v) + 4) + "' text-anchor='end'>" + (v >= 1000 ? nf.format(v / 1000) + "k" : nf.format(v)) + "</text>";
    }
    var every = Math.ceil(data.length / 8);
    var best = null;
    data.forEach(function (d, i) {
      var h = (H - T - B) * d.amount / top;
      var x = L + i * bw + bw * 0.15;
      var label = d.date;
      if (!best || d.amount > best.amount) best = { amount: d.amount, label: label };
      svg += "<rect class='bar' x='" + x.toFixed(1) + "' y='" + (H - B - h).toFixed(1) + "' width='" + (bw * 0.7).toFixed(1) + "' height='" + Math.max(h, d.amount ? 2 : 0).toFixed(1) + "' rx='3'>" +
        "<title>" + longDate(label) + " : " + money(d.amount) + " · " + nf.format(d.tickets) + " tickets</title></rect>";
      if (i % every === 0) {
        svg += "<text class='axis' x='" + (L + i * bw + bw / 2).toFixed(1) + "' y='" + (H - 8) + "' text-anchor='middle'>" +
          parse(label).toLocaleDateString("fr-FR", { day: "numeric", month: "short", timeZone: "UTC" }) + "</text>";
      }
    });
    $("bars").innerHTML = svg + "</svg>";
    $("best").textContent = best && best.amount ? "Meilleur " + (R.by_day.length > 62 ? "semaine" : "jour") + " : " + longDate(best.label) + " · " + money(best.amount) : "";
  }

  var rz;
  window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(function () { if (R) bars(); }, 150); });

  // --- export et impression -----------------------------------------------------------------
  $("printBtn").addEventListener("click", function () { window.print(); });
  $("csvBtn").addEventListener("click", function () {
    if (!R) return;
    var lines = [["Rapport des ventes ADER LINK", R.from + " au " + R.to], [],
      ["Jour", "Tickets vendus", "Montant (F CFA)"]];
    R.by_day.forEach(function (d) { lines.push([d.date, d.tickets, d.amount]); });
    lines.push(["Total", R.totals.tickets, R.totals.amount]);
    [["Routeur", R.by_zone], ["Profil", R.by_forfait], ["Point de vente", R.by_pos], ["Vendeur", R.by_seller]].forEach(function (g) {
      if (g[0] === "Vendeur" && R.me.role === "revendeur") return;
      lines.push([], [g[0], "Tickets vendus", "Montant (F CFA)"]);
      g[1].forEach(function (r) { lines.push([r.label, r.tickets, r.amount]); });
    });
    // point-virgule : Excel en français ouvre le fichier directement en colonnes
    var csv = "﻿" + lines.map(function (l) {
      return l.map(function (c) { c = String(c); return /[;"\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c; }).join(";");
    }).join("\r\n");
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    a.download = "ventes-aderlink-" + R.from + "-au-" + R.to + ".csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  });

  // Première ouverture : la dernière période choisie (7 jours par défaut)
  try { preset = localStorage.getItem("aderlink-report") || "7"; } catch (e) { preset = "7"; }
  api("/api/report").then(function (d) {
    R = d;
    choose(preset || "7");
  }).catch(function (e) { $("scope").textContent = e.message; });
})();
