/* ADER LINK — en-tête, menu latéral et barre du bas, communs à toutes les pages */
(function () {
  "use strict";
  var page = document.body.dataset.page || "";
  var I = {
    home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    ticket: '<path d="M3 8a2 2 0 0 0 0 4v0a2 2 0 0 1 0 4v2h18v-2a2 2 0 0 1 0-4 2 2 0 0 0 0-4V6H3z"/><path d="M9 9h6M9 15h6"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    scan: '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><circle cx="12" cy="12" r="3"/>',
    router: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01M8.5 9.5a5 5 0 0 1 7 0M6 7a8.5 8.5 0 0 1 12 0"/>',
    zones: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H14a4 4 0 0 1 0 8h-4a4 4 0 0 0 0 8h0"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    trend: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H3"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    pencil: '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    store: '<path d="M4 9.5 5.5 4h13L20 9.5M4 9.5a2.7 2.7 0 0 0 5.3 0 2.7 2.7 0 0 0 5.4 0 2.7 2.7 0 0 0 5.3 0M5 10.5V20h14v-9.5"/><path d="M10 20v-5h4v5"/>',
    wifi: '<path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M2 9a14.5 14.5 0 0 1 20 0"/><path d="M12 19.5h.01"/>',
    terminal: '<path d="m5 7 5 5-5 5M12 18h7"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
    upload: '<path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    coins: '<circle cx="9" cy="9" r="6"/><path d="M15.5 9.5a6 6 0 1 1-6 6"/>',
    download: '<path d="M12 4v12M7 11l5 5 5-5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    winbox: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01M8 14l2 2-2 2M13 18h3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M15 8l2 2"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    card: '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19M6.5 15h4"/>',
    phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>',
  };
  function icon(name) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + I[name] + "</svg>";
  }
  var main = [
    { id: "accueil", href: "/", label: "Accueil", icon: "home" },
    { id: "creer", href: "/tickets", label: "Créer des tickets", icon: "ticket" },
    { id: "lots", href: "/lots", label: "Lots", icon: "layers" },
    { id: "controle", href: "/controle", label: "État du ticket", icon: "scan" },
    { id: "rapports", href: "/rapports", label: "Rapport des ventes", icon: "chart" },
    { id: "analytique", href: "/analytique", label: "Analytique", icon: "trend" },
    { id: "routeurs", href: "/routeurs", label: "Routeurs", icon: "router", need: "gerant" },
    { id: "roaming", href: "/roaming", label: "ADER Roaming", icon: "zones", roles: "admin client" },
    { id: "utilisateurs", href: "/utilisateurs", label: "Utilisateurs", icon: "users", roles: "admin client" },
    { id: "formules", href: "/formules", label: "Formules", icon: "card", need: "admin" },
    { id: "abonnement", href: "/abonnement", label: "Mon abonnement", icon: "card", roles: "client" },
    { id: "parametres", href: "/parametres", label: "Paramètres", icon: "gear", need: "admin" },
    { id: "profil", href: "/profil", label: "Mon profil", icon: "user" }
  ];
  var soon = [];
  function need(m) { return m.need ? ' data-need="' + m.need + '"' : m.roles ? ' data-roles="' + m.roles + '"' : ""; }
  function link(m) {
    return '<a href="' + m.href + '"' + need(m) + (m.id === page ? ' aria-current="page"' : "") + ">" + icon(m.icon) + "<span>" + m.label + "</span></a>";
  }

  // En-tête
  var header = document.createElement("header");
  header.className = "topbar";
  header.innerHTML =
    '<div class="topbar-inner">' +
    '<a class="brand" href="/"><span class="brand-mark"><img src="/static/mark-blue.svg" alt="" width="22" height="22"></span>' +
    '<span class="brand-text"><img class="brand-word" src="/static/wordmark-white.svg" alt="ADER LINK" width="106" height="15">' +
    '<span class="brand-tag">Gestion de zones Wi-Fi</span></span></a>' +
    '<nav class="nav" aria-label="Menu principal">' + main.filter(function (m) { return m.id !== "analytique"; }).slice(0, 6).map(function (m) {
      return '<a href="' + m.href + '"' + need(m) + (m.id === page ? ' aria-current="page"' : "") + ">" + m.label + "</a>";
    }).join("") + "</nav>" +
    '<div class="topbar-right"><span class="updated num" id="updated" aria-live="polite"></span>' +
    '<a class="me-chip" href="/profil" id="meChip" hidden><span class="me-av" id="meAv"></span><span class="me-txt"><b id="meChipName"></b><small id="meChipRole"></small></span></a>' +
    '<button class="icon-btn" type="button" id="menuBtn" aria-label="Ouvrir le menu" aria-expanded="false" aria-controls="drawer">' + icon("menu") + "</button></div></div>";
  var banner = document.getElementById("demoBanner");
  document.body.insertBefore(header, banner ? banner.nextSibling : document.body.firstChild);

  // Menu latéral
  var shade = document.createElement("div");
  shade.className = "drawer-shade";
  var drawer = document.createElement("aside");
  drawer.className = "drawer";
  drawer.id = "drawer";
  drawer.setAttribute("aria-label", "Menu");
  drawer.innerHTML =
    '<div class="drawer-head"><div><span class="drawer-title" id="meName">Menu</span><span class="drawer-role" id="meRole"></span></div><button class="icon-btn icon-btn-plain" type="button" id="menuClose" aria-label="Fermer le menu">' + icon("x") + "</button></div>" +
    '<nav class="drawer-nav">' + main.map(link).join("") +
    '<div class="drawer-sep"></div>' +
    (soon.length ? soon.map(function (m) { return '<span class="is-soon" aria-disabled="true">' + icon(m.icon) + "<span>" + m.label + '</span><em>Bientôt</em></span>'; }).join("") +
      '<div class="drawer-sep"></div>' : "") +
    '<a href="/logout">' + icon("logout") + "<span>Déconnexion</span></a></nav>" +
    '<div class="drawer-foot">ADER LINK<br>Gestion de zones Wi-Fi</div>';
  document.body.appendChild(shade);
  document.body.appendChild(drawer);

  // Barre du bas (téléphone)
  var bar = document.createElement("nav");
  bar.className = "bottombar";
  bar.setAttribute("aria-label", "Raccourcis");
  function tab(id, href, label, ic) {
    return '<a href="' + href + '"' + (id === page ? ' aria-current="page"' : "") + ">" + icon(ic) + "<span>" + label + "</span></a>";
  }
  bar.innerHTML = tab("accueil", "/", "Accueil", "home") +
    '<span class="bb-slot" data-need="gerant">' + tab("routeurs", "/routeurs", "Routeurs", "router") + "</span>" +
    '<span class="bb-slot" data-only="revendeur" hidden>' + tab("controle", "/controle", "Contrôle", "scan") + "</span>" +
    '<a class="fab" href="/tickets" aria-label="Créer des tickets"' + (page === "creer" ? ' aria-current="page"' : "") + ">" + icon("plus") + "</a>" +
    tab("lots", "/lots", "Lots", "layers") +
    '<button type="button" id="menuBtn2">' + icon("menu") + "<span>Menu</span></button>";
  document.body.appendChild(bar);
  document.body.classList.add("has-bottombar");

  var lastFocus = null;
  function open() {
    lastFocus = document.activeElement;
    drawer.classList.add("open"); shade.classList.add("open");
    document.getElementById("menuBtn").setAttribute("aria-expanded", "true");
    document.getElementById("menuClose").focus();
  }
  function close() {
    drawer.classList.remove("open"); shade.classList.remove("open");
    document.getElementById("menuBtn").setAttribute("aria-expanded", "false");
    if (lastFocus) lastFocus.focus();
  }
  document.getElementById("menuBtn").addEventListener("click", open);
  document.getElementById("menuBtn2").addEventListener("click", open);
  document.getElementById("menuClose").addEventListener("click", close);
  shade.addEventListener("click", close);
  drawer.addEventListener("click", function (e) { if (e.target.closest("a")) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && drawer.classList.contains("open")) close(); });

  // Mise à jour automatique : quand le serveur change de version, la page se recharge toute seule
  var version = null;
  function toast(msg) {
    var t = document.createElement("div");
    t.className = "toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
  }
  function checkVersion() {
    fetch("/api/version", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      if (version && d.version !== version) {
        toast("Nouvelle version " + d.version + " installée — actualisation…");
        setTimeout(function () { location.reload(); }, 2500);
      }
      version = d.version;
    }).catch(function () { /* serveur en train de redémarrer */ });
  }
  checkVersion();
  setInterval(checkVersion, 30000);

  // Compte connecté : on masque ce que son rôle ne permet pas
  var rank = { revendeur: 1, gerant: 2, client: 2, admin: 3 };
  var me = fetch("/api/me", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  me.then(function (u) {
    if (!u) return;
    document.body.dataset.role = u.role;
    document.getElementById("meName").textContent = u.name;
    document.getElementById("meRole").textContent = u.role_label;
    var initials = String(u.name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map(function (w) { return w[0]; }).join("").toUpperCase();
    document.getElementById("meAv").textContent = initials;
    document.getElementById("meChipName").textContent = u.name;
    document.getElementById("meChipRole").textContent = u.role_label;
    document.getElementById("meChip").hidden = false;
    Array.prototype.forEach.call(document.querySelectorAll("[data-need]"), function (el) {
      if (rank[u.role] < rank[el.dataset.need]) el.hidden = true;
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-roles]"), function (el) {
      if (el.dataset.roles.split(" ").indexOf(u.role) < 0) el.hidden = true;
      if (u.role === "client" && el.getAttribute("href") === "/utilisateurs") el.querySelector("span") ? el.querySelector("span").textContent = "Mon équipe" : el.textContent = "Mon équipe";
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-only]"), function (el) {
      el.hidden = el.dataset.only !== u.role;
    });
    if (u.expires_at) {
      var rest = u.expires_at - u.now, warn = document.createElement("div");
      if (rest < 3 * 86400) {
        var d = Math.floor(rest / 86400), h = Math.floor(rest % 86400 / 3600);
        warn.className = "sub-warn";
        warn.setAttribute("role", "status");
        warn.textContent = "Votre abonnement se termine dans " + (d ? d + " jour" + (d > 1 ? "s" : "") + " " : "") + h + " h. Contactez votre fournisseur pour le renouveler.";
        header.parentNode.insertBefore(warn, header.nextSibling);
      }
      // déconnexion automatique à l'heure de fin
      setTimeout(function () { location.href = "/login"; }, Math.min(rest * 1000 + 2000, 2147000000));
    }
  });

  window.AderShell = { icon: icon, me: me };
})();
// Bandeau « Installer ADER LINK » : une vraie application sur le téléphone ou le PC (adresse https uniquement)
(function () {
  var KEY = "aderlink-install-hide";
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone;
  window.addEventListener("beforeinstallprompt", function (e) { e.preventDefault(); window.AderInstallPrompt = e; });
  if (standalone || !window.isSecureContext || location.pathname === "/login" || location.pathname.indexOf("/print") === 0) return;
  if (location.pathname === "/") return;  // l'accueil a sa propre carte « Installer ADER LINK »
  try { if (localStorage.getItem(KEY) === "1") return; } catch (e) { /* ignoré */ }
  function banner(text, button, onClick) {
    if (document.getElementById("installBar")) return;
    var d = document.createElement("div");
    d.id = "installBar"; d.className = "install-bar no-print"; d.setAttribute("role", "region"); d.setAttribute("aria-label", "Installer l'application");
    d.innerHTML = "<img src='/static/icon-192.png' alt='' width='36' height='36'><span></span>" +
      (button ? "<button type='button' class='btn btn-sm' data-i='go'></button>" : "") +
      "<button type='button' class='icon-btn' data-i='no' aria-label='Fermer'>✕</button>";
    d.querySelector("span").innerHTML = text;
    if (button) d.querySelector("[data-i=go]").textContent = button;
    d.addEventListener("click", function (e) {
      var b = e.target.closest("[data-i]");
      if (!b) return;
      if (b.dataset.i === "go") onClick();
      else { try { localStorage.setItem(KEY, "1"); } catch (err) { /* ignoré */ } }
      d.remove();
    });
    document.body.appendChild(d);
  }
  var prompt = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault(); prompt = e; window.AderInstallPrompt = e;
    banner("<strong>Installer ADER LINK</strong> sur cet appareil : une icône comme une vraie application.", "Installer", function () {
      prompt.prompt();
      prompt.userChoice.then(function () { prompt = null; });
    });
  });
  window.addEventListener("appinstalled", function () {
    try { localStorage.setItem(KEY, "1"); } catch (e) { /* ignoré */ }
    var b = document.getElementById("installBar"); if (b) b.remove();
  });
  // iPhone / iPad : pas de bouton automatique, on explique le geste
  if (/iphone|ipad|ipod/i.test(navigator.userAgent)) {
    setTimeout(function () {
      banner("<strong>Installer ADER LINK :</strong> touchez <strong>Partager</strong> ⬆️ puis <strong>Sur l'écran d'accueil</strong>.", "", null);
    }, 1500);
  }
})();
(function () {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    try { navigator.serviceWorker.register("/sw.js").catch(function () {}); } catch (e) { /* ignoré */ }
  }
})();
// Dans l'application installée : les pages d'ADER LINK (impression…) s'ouvrent dans la même fenêtre,
// pas dans le navigateur à côté (qui demanderait de se reconnecter)
(function () {
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone;
  if (!standalone) return;
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[target=_blank]");
    if (!a || !a.href) return;
    try { if (new URL(a.href).origin !== location.origin) return; } catch (err) { return; }
    e.preventDefault();
    location.href = a.href;
  });
})();
