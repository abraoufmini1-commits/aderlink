/* ADER LINK — tableau de bord */
(function () {
  "use strict";

  var state = { range: "24h", data: null, tableView: false, sheetZone: null, sessions: [] };
  var REFRESH_MS = 15000;
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var nf1 = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });

  // --- formats -----------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function bps(v) {
    v = v || 0;
    if (v >= 1e9) return nf1.format(v / 1e9) + " Gb/s";
    if (v >= 1e6) return nf1.format(v / 1e6) + " Mb/s";
    if (v >= 1e3) return nf.format(Math.round(v / 1e3)) + " kb/s";
    return nf.format(v) + " b/s";
  }
  function bytes(v) {
    v = v || 0;
    if (v >= 1e9) return nf1.format(v / 1e9) + " Go";
    if (v >= 1e6) return nf1.format(v / 1e6) + " Mo";
    if (v >= 1e3) return nf.format(Math.round(v / 1e3)) + " ko";
    return nf.format(v) + " o";
  }
  function duration(s) {
    if (s == null) return "–";
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    if (d) return d + " j " + h + " h";
    if (h) return h + " h " + String(m).padStart(2, "0");
    return m + " min";
  }
  function ago(ts) {
    if (!ts) return "–";
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return "à l'instant";
    if (s < 3600) return "il y a " + Math.floor(s / 60) + " min";
    if (s < 86400) return "il y a " + Math.floor(s / 3600) + " h";
    return "il y a " + Math.floor(s / 86400) + " j";
  }
  function clock(ts, withDay) {
    var d = new Date(ts * 1000);
    var t = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    return withDay ? d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" }) + " " + t : t;
  }

  var ICON_OK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var ICON_KO = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  var ICON_UNK = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.5" fill="currentColor"/></svg>';

  // --- chargement --------------------------------------------------------------------
  function load() {
    return fetch("/api/overview?range=" + state.range, { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
        return r.json();
      })
      .then(function (d) {
        state.data = d;
        render();
        if (state.sheetZone) loadSessions(state.sheetZone, true);
      })
      .catch(function (e) {
        if (e.message !== "auth") $("updated").textContent = "Serveur injoignable — nouvel essai…";
      });
  }

  function render() {
    var d = state.data, t = d.totals;
    $("demoBanner").hidden = !d.demo;
    $("updated").textContent = "Mis à jour à " + clock(d.generated_at);
    $("kActive").textContent = nf.format(t.active);
    $("kActiveSub").textContent = "Sur tous les routeurs";
    $("kZones").innerHTML = nf.format(t.zones_online) + " <small>/ " + nf.format(t.zones) + "</small>";
    var off = t.zones - t.zones_online;
    $("kZonesSub").textContent = off ? off + " routeur" + (off > 1 ? "s" : "") + " hors ligne" : "Tous en ligne";
    var st = d.sales_today || { tickets: 0, amount: 0 };
    $("heroAmount").textContent = nf.format(st.amount);
    var today = new Date();
    $("stubDay").innerHTML = "<b>" + today.getDate() + "</b>" + today.toLocaleDateString("fr-FR", { month: "short" }).replace(".", "");
    $("heroTickets").textContent = nf.format(st.tickets) + " ticket" + (st.tickets > 1 ? "s" : "") + " vendu" + (st.tickets > 1 ? "s" : "") + " aujourd'hui";
    $("pollInfo").textContent = "Relevé toutes les " + d.poll_seconds + " s";
    renderZones(d.zones);
    personal(d);
    renderEvents(d.events, d.zones);
    renderChart(d.history);
  }

  // Espace personnel : un client, un gérant ou un revendeur voit à qui appartient ce tableau de bord
  function personal(d) {
    var u = d.me || {};
    // Accueil : « Bienvenue, Prénom » pour tout le monde
    var first = String(u.name || u.username || "").trim().split(/\s+/)[0];
    // administrateur : le nom de l'entreprise ; les autres : leur prénom
    $("dashTitle").textContent = u.role === "admin" ? "Bienvenue, ADER TECHNOLOGIE" : first ? "Bienvenue, " + first : "Bienvenue";
    if (!u.role || u.role === "admin") return;
    $("dashSub").textContent = u.role === "client" ? "Vos routeurs, vos tickets et vos ventes" : "Vos tickets et vos ventes";
    var empty = u.role === "client" && !d.zones.length;
    $("welcome").hidden = !empty;
    if (empty) {
      $("welcomeTitle").textContent = "Bienvenue, " + u.name + " !";
      $("welcomeIco").innerHTML = window.AderShell.icon("router");
    }
  }

  // --- zones -------------------------------------------------------------------------
  function model(z) {
    return String(z.board || "").replace(/\^2/g, "²").replace(/\^3/g, "³");
  }
  function initials(name) {
    var w = String(name).replace(/^(zone|wifi)\s+/i, "").split(/\s+/).filter(Boolean);
    return ((w[0] || "?")[0] + (w[1] ? w[1][0] : (w[0] || "")[1] || "")).toUpperCase();
  }
  var STATUS = { online: ["En ligne", "is-online"], offline: ["Hors ligne", "is-offline"], pending: ["À installer", "is-pending"], unknown: ["…", "is-unknown"] };
  function renderZones(zones) {
    var html = zones.map(function (z) {
      var st = STATUS[z.status] || STATUS.unknown, online = z.status === "online";
      var avatar = z.has_logo ? '<span class="ravatar ravatar-logo"><img alt="" src="/api/routers/' + encodeURIComponent(z.id) + '/logo"></span>'
        : '<span class="ravatar">' + esc(initials(z.name)) + "</span>";
      var head = '<span class="rtop">' + avatar + '<span class="rid"><span class="rname" title="' + esc(z.name) + '">' + esc(z.name) + "</span>" +
        '<span class="rmodel">' + esc(model(z) || (z.status === "pending" ? "Pas encore installé" : "MikroTik")) + "</span></span>" +
        '<span class="rstate"><i aria-hidden="true"></i>' + st[0] + "</span></span>";
      var tile = function (cls, n, label, sub) {
        return '<span class="rtile ' + cls + '"><span class="n num">' + n + '</span><span class="l">' + label + "</span>" +
          (sub ? '<span class="rtile-sub">' + sub + "</span>" : "") + "</span>";
      };
      var tiles = '<span class="rtiles">' +
        tile("rtile-blue", online ? nf.format(z.active) : "–", "Connecté" + (online && z.active > 1 ? "s" : ""),
          z.peak ? "Max connectés : " + nf.format(z.peak) : "") +
        tile("rtile-orange", z.tickets != null ? nf.format(z.tickets) : "–", "Tickets dans le MikroTik") + "</span>";
      var inner = head + tiles;
      if (online) return '<button type="button" class="card rcard ' + st[1] + '" data-zone="' + esc(z.id) + '" aria-label="' + esc(z.name) + " : " + z.active + ' connectés — voir la liste">' + inner + "</button>";
      return '<a class="card rcard ' + st[1] + '" href="/routeurs">' + inner + "</a>";
    }).join("");
    var box = $("zones");
    box.innerHTML = html || '<div class="card empty">Aucun routeur pour l\'instant. <a href="/routeurs">Ajouter un routeur</a></div>';
  }

  // --- activité ----------------------------------------------------------------------
  function renderEvents(events) {
    var ul = $("events");
    if (!events.length) { ul.innerHTML = '<li class="empty">Aucun événement pour le moment.</li>'; return; }
    ul.innerHTML = events.map(function (e) {
      var ok = e.kind === "online";
      return '<li><span class="ico ' + (ok ? "online" : "offline") + '">' + (ok ? ICON_OK : ICON_KO) +
        "</span><span>" + esc(e.message) + '</span><time datetime="' + new Date(e.ts * 1000).toISOString() +
        '" title="' + esc(clock(e.ts, true)) + '">' + ago(e.ts) + "</time></li>";
    }).join("");
  }

  // --- graphique ---------------------------------------------------------------------
  var chartGeom = null;
  function niceMax(v) {
    if (v <= 5) return 5;
    var p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function renderChart(hist) {
    var svg = $("chartSvg");
    var W = svg.clientWidth || 800, H = svg.clientHeight || 240;
    var m = { l: 36, r: 12, t: 12, b: 26 };
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    if (!hist.length) {
      svg.innerHTML = '<text class="axis" x="' + W / 2 + '" y="' + H / 2 + '" text-anchor="middle">Pas encore de données</text>';
      chartGeom = null; renderTable(hist); return;
    }
    if (hist.length === 1) hist = [hist[0], { ts: state.data.generated_at, active: hist[0].active, rx_bps: hist[0].rx_bps, tx_bps: hist[0].tx_bps }];
    var x0 = hist[0].ts, x1 = hist[hist.length - 1].ts || x0 + 1;
    if (x1 === x0) x1 = x0 + 1;
    var ymax = niceMax(hist.reduce(function (a, p) { return Math.max(a, p.active); }, 0));
    var X = function (t) { return m.l + (t - x0) / (x1 - x0) * (W - m.l - m.r); };
    var Y = function (v) { return H - m.b - v / ymax * (H - m.t - m.b); };
    var out = "";
    for (var i = 0; i <= 4; i++) {
      var v = ymax * i / 4, y = Y(v);
      out += '<line class="gridline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y + '" y2="' + y + '"/>';
      out += '<text class="axis" x="' + (m.l - 8) + '" y="' + (y + 4) + '" text-anchor="end">' + nf.format(v) + "</text>";
    }
    var span = x1 - x0, step = span > 3 * 86400 ? 86400 : span > 12 * 3600 ? 4 * 3600 : 3600;
    var ticks = Math.max(1, Math.round((W - m.l - m.r) / 90));
    step = Math.max(step, Math.ceil(span / ticks / step) * step);
    var first = Math.ceil((x0 + 3600) / step) * step - 3600; // aligné sur l'heure locale UTC+1
    for (var t = first; t <= x1; t += step) {
      out += '<text class="axis" x="' + X(t) + '" y="' + (H - 6) + '" text-anchor="middle">' +
        (step >= 86400 ? new Date(t * 1000).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" }) : clock(t)) + "</text>";
    }
    var pts = hist.map(function (p) { return X(p.ts).toFixed(1) + "," + Y(p.active).toFixed(1); });
    out += '<path class="area" d="M' + X(x0) + "," + Y(0) + " L" + pts.join(" L") + " L" + X(x1) + "," + Y(0) + ' Z"/>';
    out += '<path class="line" d="M' + pts.join(" L") + '"/>';
    out += '<line class="cross" id="cross" y1="' + m.t + '" y2="' + (H - m.b) + '" visibility="hidden"/>';
    out += '<circle class="dot" id="dot" r="5" visibility="hidden"/>';
    out += '<rect id="hit" x="' + m.l + '" y="0" width="' + (W - m.l - m.r) + '" height="' + H + '" fill="transparent"/>';
    svg.innerHTML = out;
    chartGeom = { hist: hist, X: X, Y: Y, W: W };
    renderTable(hist);
  }
  function onMove(ev) {
    if (!chartGeom) return;
    var svg = $("chartSvg"), rect = svg.getBoundingClientRect();
    var px = (ev.clientX - rect.left) * (chartGeom.W / rect.width);
    var h = chartGeom.hist, best = h[0], bd = Infinity;
    h.forEach(function (p) { var dd = Math.abs(chartGeom.X(p.ts) - px); if (dd < bd) { bd = dd; best = p; } });
    var x = chartGeom.X(best.ts), y = chartGeom.Y(best.active);
    var cross = $("cross"), dot = $("dot"), tip = $("tooltip");
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("visibility", "visible");
    tip.innerHTML = "<strong>" + nf.format(best.active) + "</strong> connecté" + (best.active > 1 ? "s" : "") +
      "<br>" + esc(clock(best.ts, state.range === "7j")) + " · ↓ " + bps(best.rx_bps);
    var sx = x * rect.width / chartGeom.W, sy = y * rect.height / svg.viewBox.baseVal.height;
    tip.style.left = Math.min(Math.max(sx, 70), rect.width - 70) + "px";
    tip.style.top = (sy - 12) + "px";
    tip.style.opacity = 1;
  }
  function onLeave() {
    var c = $("cross"), d = $("dot");
    if (c) c.setAttribute("visibility", "hidden");
    if (d) d.setAttribute("visibility", "hidden");
    $("tooltip").style.opacity = 0;
  }
  function renderTable(hist) {
    var rows = hist.slice().reverse().map(function (p) {
      return "<tr><td class='num'>" + esc(clock(p.ts, true)) + "</td><td class='r num'>" + nf.format(p.active) +
        "</td><td class='r num'>" + bps(p.rx_bps) + "</td></tr>";
    }).join("");
    $("chartTable").innerHTML = "<table class='chart-table'><thead><tr><th>Heure</th><th class='r'>Connectés</th><th class='r'>Débit ↓</th></tr></thead><tbody>" + rows + "</tbody></table>";
  }

  // --- panneau des connectés --------------------------------------------------------------
  function loadSessions(zoneId, silent) {
    return fetch("/api/zones/" + encodeURIComponent(zoneId) + "/sessions", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        state.sessions = d.sessions || [];
        $("sheetTitle").textContent = d.zone ? d.zone.name : "Connectés";
        $("sheetSub").textContent = nf.format(state.sessions.length) + " connecté" + (state.sessions.length > 1 ? "s" : "");
        renderSessions();
        if (!silent) $("sheetSearch").focus();
      });
  }
  function renderSessions() {
    var q = $("sheetSearch").value.trim().toLowerCase();
    var list = state.sessions.filter(function (s) {
      return !q || [s.user, s.ip, s.mac].join(" ").toLowerCase().indexOf(q) >= 0;
    }).sort(function (a, b) { return b.bytes_in - a.bytes_in; });
    if (!list.length) { $("sheetBody").innerHTML = '<div class="empty">' + (q ? "Aucun résultat." : "Personne n'est connecté.") + "</div>"; return; }
    $("sheetBody").innerHTML = "<table><thead><tr><th>Ticket</th><th class='hide-sm'>Adresse</th><th class='r'>Durée</th><th class='r'>Consommé</th></tr></thead><tbody>" +
      list.map(function (s) {
        return "<tr><td><span class='mono'>" + esc(s.user) + "</span><div class='zone-meta mono'>" + esc(s.mac) + "</div></td>" +
          "<td class='hide-sm mono'>" + esc(s.ip) + "</td><td class='r num'>" + duration(s.uptime_s) + "</td>" +
          "<td class='r num'>" + bytes(s.bytes_in + s.bytes_out) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }
  var lastFocus = null;
  function openSheet(zoneId) {
    lastFocus = document.activeElement;
    state.sheetZone = zoneId;
    $("sheetSearch").value = "";
    $("sheetBody").innerHTML = '<div class="empty">Chargement…</div>';
    $("sheet").classList.add("open"); $("backdrop").classList.add("open");
    loadSessions(zoneId);
  }
  function closeSheet() {
    state.sheetZone = null;
    $("sheet").classList.remove("open"); $("backdrop").classList.remove("open");
    if (lastFocus) lastFocus.focus();
  }

  // --- événements ----------------------------------------------------------------------
  document.querySelectorAll(".seg button").forEach(function (b) {
    b.addEventListener("click", function () {
      state.range = b.dataset.range;
      document.querySelectorAll(".seg button").forEach(function (o) { o.setAttribute("aria-pressed", o === b ? "true" : "false"); });
      load();
    });
  });
  $("toggleTable").addEventListener("click", function () {
    state.tableView = !state.tableView;
    $("chartTable").hidden = !state.tableView;
    $("chart").hidden = state.tableView;
    this.textContent = state.tableView ? "Voir le graphique" : "Voir en tableau";
    if (!state.tableView && state.data) renderChart(state.data.history);
  });
  $("zones").addEventListener("click", function (e) {
    var b = e.target.closest("[data-zone]");
    if (b) openSheet(b.dataset.zone);
  });
  $("sheetClose").addEventListener("click", closeSheet);
  $("backdrop").addEventListener("click", closeSheet);
  $("sheetSearch").addEventListener("input", renderSessions);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && state.sheetZone) closeSheet(); });
  var svg = $("chartSvg");
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerdown", onMove);
  svg.addEventListener("pointerleave", onLeave);
  var rt;
  window.addEventListener("resize", function () {
    clearTimeout(rt);
    rt = setTimeout(function () { if (state.data) renderChart(state.data.history); }, 150);
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) load(); });

  load();
  setInterval(function () { if (!document.hidden) load(); }, REFRESH_MS);
})();

// Carte « Installer ADER LINK » de l'accueil
(function () {
  var card = document.getElementById("installCard");
  if (!card) return;
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone;
  if (standalone) return;  // déjà ouvert comme application
  card.hidden = false;
  var help = document.getElementById("installHelp");
  var ua = navigator.userAgent;
  function say(html) { help.innerHTML = html; help.hidden = false; }
  window.addEventListener("appinstalled", function () { card.hidden = true; });
  document.getElementById("installBtn").addEventListener("click", function () {
    var h = location.hostname;
    if (!window.isSecureContext) {
      // l'installation demande une adresse https : on passe sur le même serveur en https
      var to = /^\d{1,3}(\.\d{1,3}){3}$/.test(h) ? "https://" + h + ".sslip.io" + location.pathname : "https://" + location.host + location.pathname;
      say("L'installation demande l'adresse sécurisée. Ouverture de <strong>" + to.replace(/</g, "") + "</strong>… Connectez-vous puis touchez encore <strong>Installer</strong>.");
      setTimeout(function () { location.href = to; }, 1800);
      return;
    }
    var p = window.AderInstallPrompt;
    if (p) {
      p.prompt();
      p.userChoice.then(function (c) { if (c && c.outcome === "accepted") card.hidden = true; });
      window.AderInstallPrompt = null;
      return;
    }
    if (/iphone|ipad|ipod/i.test(ua)) say("Sur iPhone, ouvrez cette page dans <strong>Safari</strong>, touchez <strong>Partager</strong> ⬆️ puis <strong>Sur l'écran d'accueil</strong>.");
    else if (/android/i.test(ua)) say("Dans Chrome, touchez le menu <strong>⋮</strong> en haut à droite, puis <strong>Installer l'application</strong> (ou <strong>Ajouter à l'écran d'accueil</strong>).");
    else say("Dans Chrome ou Edge, cliquez sur l'icône <strong>Installer</strong> à droite de la barre d'adresse, ou menu <strong>⋮</strong> → <strong>Installer ADER LINK</strong>. Si ADER LINK est déjà installé, il est dans le menu Démarrer.");
  });
})();
