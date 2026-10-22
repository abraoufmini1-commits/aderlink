/* ADER LINK — page Routeurs */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var ic = window.AderShell.icon;
  var S = { routers: [], demo: false, canWrite: true, quota: null, role: "admin" };
  var COLORS = ["av-blue", "av-orange", "av-navy", "av-teal"];

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
      return r.json().catch(function () { throw new Error("Redémarrez ADER LINK (ancienne version en cours)"); })
        .then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur " + r.status); return d; });
    });
  }
  function initials(name) {
    var w = String(name).replace(/^(zone|wifi)\s+/i, "").split(/\s+/).filter(Boolean);
    return ((w[0] || "?")[0] + (w[1] ? w[1][0] : (w[0] || "")[1] || "")).toUpperCase();
  }
  function duration(s) {
    if (s == null) return "–";
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? d + " j " + h + " h" : h ? h + " h " + m + " min" : m + " min";
  }
  function byId(id) { return S.routers.find(function (r) { return r.id === id; }); }

  // --- liste ---------------------------------------------------------------------------
  function load() {
    return api("/api/routers").then(function (d) {
      S.routers = d.routers; S.demo = d.demo; S.canWrite = d.can_write; S.quota = d.quota || null; S.role = d.role;
      $("demoBanner").hidden = !d.demo;
      $("addBtn").hidden = !S.canWrite;
      var q = $("quota");
      if (S.quota) {
        var full = S.quota.used >= S.quota.max;
        q.className = "card quota" + (full ? " quota-full" : "");
        q.innerHTML = "<div>Mes routeurs : <strong class='num'>" + nf.format(S.quota.used) + " / " + nf.format(S.quota.max) + "</strong>" +
          "<div class='quota-sub'>" + (full ? "Limite atteinte : contactez votre fournisseur pour ajouter des routeurs." : "Vous pouvez encore en ajouter " + nf.format(S.quota.max - S.quota.used) + ".") + "</div></div>" +
          "<div class='quota-meter'><span data-w='" + Math.min(100, S.quota.max ? S.quota.used / S.quota.max * 100 : 100) + "'></span></div>";
        q.querySelector("[data-w]").style.width = q.querySelector("[data-w]").dataset.w + "%";
        q.hidden = false;
      } else q.hidden = true;
      render();
    });
  }
  function render() {
    var q = $("search").value.trim().toLowerCase();
    var list = S.routers.filter(function (r) { return !q || (r.name + " " + r.ssid + " " + r.location + " " + r.host).toLowerCase().indexOf(q) >= 0; });
    $("list").innerHTML = list.length ? list.map(function (r, i) {
      var st = r.status === "pending" ? "<span class='pill pill-pending'>● À installer</span>"
        : r.status === "online" ? "<span class='pill pill-online'>● En ligne</span>"
        : r.status === "offline" ? "<span class='pill pill-offline'>● Hors ligne</span>" : "<span class='pill pill-unknown'>● En attente</span>";
      var act = function (a, label, icon) { return "<button type='button' class='act' data-act='" + a + "' data-id='" + esc(r.id) + "'>" + ic(icon) + "<span>" + label + "</span></button>"; };
      return "<article class='card router-card'>" +
        "<div class='rc-head'>" + (r.has_logo ? "<span class='avatar avatar-logo'><img alt='' src='/api/routers/" + encodeURIComponent(r.id) + "/logo'></span>"
          : "<span class='avatar " + COLORS[S.routers.indexOf(r) % COLORS.length] + "'>" + esc(initials(r.name)) + "</span>") +
        "<div class='rc-main'><div class='rc-name'>" + esc(r.name) + "</div>" +
        "<div class='rc-meta'>Wi-Fi " + esc(r.ssid) + (r.location ? " · " + esc(r.location) : "") + "</div>" +
        (r.installed ? "<div class='rc-meta'>" + esc(r.board || "Modèle inconnu") + " · " + esc(r.host) + (r.status === "online" ? " · " + nf.format(r.active || 0) + " connectés" : "") + "</div>"
          : "<div class='rc-meta'>Pas encore installé : touchez <strong>Installer</strong></div>") + "</div>" +
        st + "</div>" +
        (r.owner_name ? "<div class='rc-meta rc-owner'>" + ic("user") + " Client : <strong>" + esc(r.owner_name) + "</strong></div>" : "") +
        "<div class='act-grid" + (S.canWrite ? "" : " act-grid-4") + "'>" + act("details", "Détails", "info") + (S.canWrite ? act("edit", "Modifier", "pencil") : "") + act("forfaits", "Profils", "ticket") +
        (S.canWrite ? act("pv", "Points de vente", "store") : "") + act("sessions", "Connectés", "wifi") + (S.canWrite ? act("install", "Installer", "terminal") + act("winbox", "Winbox", "winbox") : "") + "</div></article>";
    }).join("") : "<div class='card empty'>" + (S.routers.length ? "Aucun routeur ne correspond." : "Aucun routeur. Cliquez sur <strong>+ Ajouter</strong> pour enregistrer votre premier MikroTik.") + "</div>";
  }
  $("search").addEventListener("input", render);
  $("list").addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (!b) return;
    var r = byId(b.dataset.id);
    ({ details: details, edit: edit, forfaits: forfaits, pv: pointsVente, sessions: sessions, install: install, winbox: winbox })[b.dataset.act](r, b);
  });
  $("addBtn").addEventListener("click", function () {
    if (S.quota && S.quota.used >= S.quota.max) {
      sheet("Limite atteinte", "router", "<p class='pane-sub'>Votre abonnement permet <strong>" + nf.format(S.quota.max) + " routeur" + (S.quota.max > 1 ? "s" : "") +
        "</strong> et vous les utilisez tous. Pour en ajouter, contactez votre fournisseur : il peut augmenter votre limite en une minute.</p>");
      return;
    }
    edit(null);
  });

  // --- feuille (bas de l'écran sur téléphone, fenêtre au centre sur ordinateur) -------------------------
  var lastFocus = null;
  function sheet(title, icon, body, foot) {
    lastFocus = document.activeElement;
    $("bsTitle").textContent = title;
    $("bsIco").innerHTML = ic(icon);
    $("bsClose").innerHTML = ic("x");
    $("bsBody").innerHTML = body;
    $("bsBody").onclick = null;
    $("bsFoot").innerHTML = foot || "<button class='btn-prev' type='button' data-close>Fermer</button>";
    $("bs").hidden = false; $("bsShade").classList.add("open");
    document.body.classList.add("no-scroll");
    var f = $("bs").querySelector("input, textarea, .btn, button[data-close]");
    setTimeout(function () { (f || $("bsClose")).focus(); }, 50);
  }
  function close() {
    $("bs").hidden = true; $("bsShade").classList.remove("open");
    document.body.classList.remove("no-scroll");
    if (lastFocus) lastFocus.focus();
  }
  $("bsClose").addEventListener("click", close);
  $("bsShade").addEventListener("click", close);
  $("bs").addEventListener("click", function (e) { if (e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("bs").hidden) close(); });
  function msg(kind, text) {
    var n = $("bsMsg");
    if (!n) return;
    n.className = "notice show notice-" + kind; n.innerHTML = text;
  }

  // --- détails ------------------------------------------------------------------------------------
  function details(r) {
    var row = function (k, v) { return "<div><dt>" + k + "</dt><dd>" + v + "</dd></div>"; };
    sheet("Détails de " + r.name, "info", "<dl class='check-dl'>" +
      row("État", r.status === "online" ? "En ligne" : r.status === "offline" ? "Hors ligne" + (r.error ? " — " + esc(r.error) : "") : "En attente") +
      row("Modèle", esc(r.board || "–")) + row("RouterOS", esc(r.version || "–")) +
      row("Adresse IP", "<span class='mono'>" + esc(r.host) + ":" + r.port + "</span>") +
      row("Interface Internet", esc(r.wan_interface || "–")) + row("Nom du Wi-Fi", esc(r.ssid)) +
      row("Connectés", nf.format(r.active || 0)) + row("Processeur / mémoire", (r.cpu != null ? r.cpu + " %" : "–") + " / " + (r.memory != null ? r.memory + " %" : "–")) +
      row("Allumé depuis", duration(r.uptime_s)) + row("Comptes hotspot", r.tickets != null ? nf.format(r.tickets) : "–") +
      row("Longueur des codes", r.code_length + " chiffres") + row("Points de vente", nf.format(r.points_vente.length)) + "</dl>");
  }

  // --- ajouter / modifier : l'essentiel d'abord, le reste dans « Plus de paramètres » ---------------
  function cropSquare(file) {
    return new Promise(function (ok, ko) {
      if (!/^image\//.test(file.type)) return ko(new Error("Choisissez une image (PNG, JPG, GIF ou SVG)"));
      if (file.size > 8 * 1024 * 1024) return ko(new Error("Image trop lourde (8 Mo au plus)"));
      var img = new Image(), reader = new FileReader();
      img.onload = function () {
        var side = Math.min(img.naturalWidth || img.width, img.naturalHeight || img.height) || 256;
        var sx = ((img.naturalWidth || side) - side) / 2, sy = ((img.naturalHeight || side) - side) / 2;
        var c = document.createElement("canvas"); c.width = c.height = 256;
        var g = c.getContext("2d");
        g.fillStyle = "#ffffff"; g.fillRect(0, 0, 256, 256);
        g.drawImage(img, sx, sy, side, side, 0, 0, 256, 256);
        ok(c.toDataURL("image/png"));
      };
      img.onerror = function () { ko(new Error("Image illisible")); };
      reader.onload = function () { img.src = reader.result; };  // data: autorisé par la politique de sécurité
      reader.onerror = function () { ko(new Error("Image illisible")); };
      reader.readAsDataURL(file);
    });
  }

  function edit(r) {
    var v = r || { name: "", location: "", ssid: "", host: "", port: 8728, wan_interface: "ether1", code_length: 8, currency: "FCFA" };
    var logo = { changed: false, data: null };
    var lens = "";
    for (var n = 4; n <= 12; n++) lens += "<option value='" + n + "'" + (n === +v.code_length ? " selected" : "") + ">" + n + " chiffres</option>";
    var f = function (id, label, value, attrs, hint) {
      return "<div class='f'><label for='" + id + "'>" + label + "</label><input id='" + id + "' value='" + esc(value) + "' " + (attrs || "") + ">" +
        (hint ? "<div class='hint'>" + hint + "</div>" : "") + "</div>";
    };
    var body = "<form id='rForm' class='nr-form' autocomplete='off' novalidate>" +
      "<div class='f'><label for='rName'>" + ic("wifi") + " Nom du réseau <span class='req'>*</span></label>" +
      "<input id='rName' value='" + esc(v.name) + "' maxlength='40' placeholder='Ex : MON WIFIZONE' required></div>" +
      "<div class='f'><label for='rLen'># Longueur du code</label><select id='rLen'>" + lens + "</select></div>" +
      "<div class='f'><span class='lbl'>" + ic("image") + " Logo du réseau</span>" +
      "<label class='logo-drop' id='logoDrop'><input type='file' id='rLogo' accept='image/png,image/jpeg,image/gif,image/svg+xml,image/webp' class='sr-only'>" +
      "<span class='logo-prev' id='logoPrev'>" + (r && r.has_logo ? "<img alt='' src='/api/routers/" + encodeURIComponent(r.id) + "/logo?v=" + Date.now() + "'>" : ic("upload")) + "</span>" +
      "<span><strong id='logoTxt'>" + (r && r.has_logo ? "Changer l'image" : "Choisir une image") + "</strong><small>PNG, JPG, GIF, SVG — recadrage carré</small></span></label>" +
      (r && r.has_logo ? "<button type='button' class='link link-muted' id='logoDel'>Retirer le logo</button>" : "") + "</div>" +
      "<div class='f'><label for='rCur'>" + ic("coins") + " Devise</label><input id='rCur' value='" + esc(v.currency || "FCFA") + "' maxlength='8' placeholder='FCFA'></div>" +
      "<label class='more-toggle'><input type='checkbox' id='rMore'> <span>Plus de paramètres</span></label>" +
      "<div class='form-grid form-grid-2' id='rMoreBox' hidden>" +
      f("rSsid", "Nom du Wi-Fi imprimé sur les cartes", r ? v.ssid : "", "maxlength='32' placeholder='Par défaut : le nom du réseau'") +
      f("rLoc", "Quartier / adresse", v.location || "", "maxlength='60' placeholder='ex : Katako, Niamey'") +
      f("rHost", "Adresse IP du routeur", r && !r.auto_host ? v.host : "", "inputmode='decimal' placeholder='Automatique'", "Vide = détectée à l'installation" + (r && r.auto_host && r.host ? " (actuelle : " + esc(r.host) + ")" : "")) +
      f("rPort", "Port API", v.port, "type='number' min='1' max='65535' inputmode='numeric'") +
      f("rWinbox", "Adresse Winbox (à distance)", v.winbox || "", "maxlength='80' placeholder='Par défaut : l\'adresse du routeur'", "Ex. 41.203.10.5:8291 — sera remplie automatiquement avec le VPS") +
      f("rWan", "Interface Internet", v.wan_interface || "ether1", "maxlength='32' placeholder='ether1, lte1, pppoe-out1…'", "Sert à mesurer le débit") +
      "</div></form><div class='notice' id='bsMsg' role='status'></div>";
    var foot = "<button class='btn-prev' type='button' data-close>" + ic("x") + " Annuler</button>" +
      (r ? "<button class='btn btn-danger' type='button' id='rDel'>Supprimer</button>" : "") +
      "<button class='btn btn-big' type='button' id='rSave'>" + ic("check") + (r ? " Enregistrer" : " Créer le routeur") + "</button>";
    sheet(r ? "Modifier " + r.name : "Nouveau routeur", "router", body, foot);
    $("rMore").addEventListener("change", function () { $("rMoreBox").hidden = !this.checked; });
    $("rLogo").addEventListener("change", function () {
      var file = this.files && this.files[0];
      if (!file) return;
      cropSquare(file).then(function (data) {
        logo = { changed: true, data: data };
        $("logoPrev").innerHTML = "<img alt='' src='" + data + "'>";
        $("logoTxt").textContent = file.name.length > 28 ? file.name.slice(0, 25) + "…" : file.name;
      }).catch(function (err) { msg("err", esc(err.message)); });
    });
    if ($("logoDel")) $("logoDel").addEventListener("click", function () {
      logo = { changed: true, data: null };
      $("logoPrev").innerHTML = ic("upload"); $("logoTxt").textContent = "Choisir une image"; this.hidden = true;
    });
    function save() {
      if (!$("rName").value.trim()) { msg("err", "Donnez un nom au réseau"); $("rName").focus(); return; }
      var data = { name: $("rName").value, code_length: +$("rLen").value, currency: $("rCur").value,
        ssid: $("rSsid").value, location: $("rLoc").value, host: $("rHost").value,
        port: +$("rPort").value || 8728, wan_interface: $("rWan").value, winbox: $("rWinbox").value };
      var btn = $("rSave"); btn.disabled = true;
      api(r ? "/api/routers/" + encodeURIComponent(r.id) + "/update" : "/api/routers", data).then(function (res) {
        var id = r ? r.id : res.id;
        return (logo.changed ? api("/api/routers/" + encodeURIComponent(id) + "/logo", { image: logo.data }) : Promise.resolve())
          .then(function () { return load(); })
          .then(function () { if (r) close(); else install(byId(id), true); });
      }).catch(function (err) { msg("err", esc(err.message)); btn.disabled = false; });
    }
    $("rSave").addEventListener("click", save);
    $("rForm").addEventListener("submit", function (e) { e.preventDefault(); save(); });
    if (r) $("rDel").addEventListener("click", function () {
      var b = $("rDel");
      if (b.dataset.confirm !== "1") { b.dataset.confirm = "1"; b.textContent = "Confirmer ?"; return; }
      api("/api/routers/" + encodeURIComponent(r.id) + "/delete", {}).then(function () { close(); load(); })
        .catch(function (err) { msg("err", esc(err.message)); });
    });
  }

  // --- Winbox : ADER LINK l'ouvre sur ce PC, l'adresse du routeur déjà remplie ---------------------
  function copyOld(text) {
    var t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); } catch (e) { /* ignoré */ }
    t.remove();
  }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { copyOld(text); });
    }
    copyOld(text);
    return Promise.resolve();
  }
  function toast(text, ok) {
    var t = document.createElement("div");
    t.className = "toast" + (ok ? " toast-ok" : ""); t.setAttribute("role", "status"); t.textContent = text;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 6000);
  }
  function winboxReady() { try { return localStorage.getItem("aderlink-winbox") === "1"; } catch (e) { return false; } }
  function launch(addr) {
    location.href = "aderlink-winbox://" + encodeURIComponent(addr);
    toast("Ouverture de Winbox sur " + addr + "…", true);
  }
  function winbox(r, btn) {
    var addr = r.winbox || r.host;
    if (!addr) { toast("Ce routeur n'est pas encore installé."); return; }
    if (winboxReady()) { launch(addr); return; }
    if (btn) btn.disabled = true;
    api("/api/routers/" + encodeURIComponent(r.id) + "/winbox", {}).then(function (d) {
      if (d.ok) { toast(d.message, true); return; }
      copyText(addr);
      setupWinbox(addr);
    }).catch(function (err) { toast(err.message); })
      .then(function () { if (btn) btn.disabled = false; });
  }
  // Une seule fois par PC : on apprend à Windows à ouvrir Winbox depuis le navigateur
  function setupWinbox(addr) {
    sheet("Ouvrir Winbox depuis ADER LINK", "winbox",
      "<p class='pane-sub'>À faire <strong>une seule fois sur ce PC</strong> (Windows). Ensuite, le bouton Winbox ouvre Winbox directement, même à travers le lien d'ADER LINK.</p>" +
      "<ol class='wb-steps'><li><a class='btn btn-big btn-block' href='/winbox/activer-winbox.bat' download>" + ic("download") + " Télécharger le fichier d'activation</a></li>" +
      "<li>Ouvrez le fichier <strong>activer-winbox-aderlink.bat</strong>. Si Windows affiche « Windows a protégé votre ordinateur », cliquez sur <strong>Informations complémentaires</strong> puis <strong>Exécuter quand même</strong>.</li>" +
      "<li>Une fenêtre noire installe Winbox (s'il n'est pas déjà sur le PC) et affiche <strong>C'est fait</strong>. Fermez-la.</li>" +
      "<li>Cliquez ci-dessous sur <strong>C'est fait</strong>.</li></ol>" +
      "<div class='info-box'>En attendant, l'adresse du routeur est copiée : <strong class='mono'>" + esc(addr) + "</strong></div>",
      "<button class='btn-prev' type='button' data-close>Plus tard</button><button class='btn btn-big' type='button' id='wbDone'>" + ic("check") + " C'est fait</button>");
    $("wbDone").addEventListener("click", function () {
      try { localStorage.setItem("aderlink-winbox", "1"); } catch (e) { /* ignoré */ }
      close();
      launch(addr);
    });
  }

  // --- forfaits : on ouvre l'assistant de création sur ce routeur ------------------------------
  function forfaits(r) {
    try { localStorage.setItem("aderlink-zone", r.id); } catch (e) { /* ignoré */ }
    location.href = "/tickets#forfait";
  }

  // --- points de vente -------------------------------------------------------------------------
  function pointsVente(r) {
    var list = r.points_vente.map(function (p) {
      return "<div class='pv-row'><span class='avatar av-orange' aria-hidden='true'>" + ic("store") + "</span>" +
        "<div class='rc-main'><div class='rc-name'>" + esc(p.name) + "</div><div class='rc-meta mono'>" + esc(p.code) + "</div></div>" +
        "<span class='pill pill-online'>● Actif</span>" +
        (r.points_vente.length > 1 ? "<button class='row-del show' type='button' data-pv-del='" + p.id + "'>Retirer</button>" : "") + "</div>";
    }).join("");
    sheet("Points de vente de " + r.name, "store",
      "<div class='pv-list'>" + list + "</div>" +
      "<form id='pvForm' class='pv-add' hidden><div class='f'><label for='pvName'>Nom du point de vente</label><input id='pvName' maxlength='40' placeholder='ex : Revendeur Moussa' required></div></form>" +
      "<div class='notice' id='bsMsg' role='status'></div>",
      "<button class='btn-prev' type='button' data-close>Fermer</button><button class='btn btn-big' type='button' id='pvAdd'>+ Nouveau point</button>");
    $("pvAdd").addEventListener("click", function () {
      var form = $("pvForm");
      if (form.hidden) { form.hidden = false; $("pvAdd").textContent = "Enregistrer"; $("pvName").focus(); return; }
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit"));
    });
    $("pvForm").addEventListener("submit", function (e) {
      e.preventDefault();
      api("/api/routers/" + encodeURIComponent(r.id) + "/pv", { name: $("pvName").value }).then(function () {
        return load().then(function () { pointsVente(byId(r.id)); });
      }).catch(function (err) { msg("err", esc(err.message)); });
    });
    $("bsBody").onclick = function (e) {
      var d = e.target.closest("[data-pv-del]");
      if (!d) return;
      if (d.dataset.confirm !== "1") { d.dataset.confirm = "1"; d.textContent = "Confirmer ?"; return; }
      api("/api/pv/" + d.dataset.pvDel + "/archive", {}).then(function () { return load().then(function () { pointsVente(byId(r.id)); }); });
    };
  }

  // --- connectés -----------------------------------------------------------------------------------
  function sessions(r) {
    sheet("Connectés sur " + r.name, "wifi", "<p class='pane-sub'>Chargement…</p>");
    api("/api/zones/" + encodeURIComponent(r.id) + "/sessions").then(function (d) {
      var list = (d.sessions || []).slice().sort(function (a, b) { return b.bytes_in - a.bytes_in; });
      $("bsBody").innerHTML = !d.online ? "<p class='pane-sub'>Le routeur ne répond pas pour l'instant.</p>"
        : !list.length ? "<p class='pane-sub'>Personne n'est connecté.</p>"
          : "<p class='pane-sub'>" + nf.format(list.length) + " connecté" + (list.length > 1 ? "s" : "") + "</p><div class='table-scroll'><table><thead><tr><th>Ticket</th><th>Adresse</th><th class='r'>Durée</th><th class='r'>Consommé</th></tr></thead><tbody>" +
          list.map(function (s) {
            var mo = (s.bytes_in + s.bytes_out) / 1e6;
            return "<tr><td class='mono'>" + esc(s.user) + "</td><td class='mono'>" + esc(s.ip) + "</td><td class='r num'>" + duration(s.uptime_s) +
              "</td><td class='r num'>" + (mo >= 1000 ? nf.format(Math.round(mo / 100) / 10) + " Go" : nf.format(Math.round(mo)) + " Mo") + "</td></tr>";
          }).join("") + "</tbody></table></div>";
    });
  }

  // --- installer ---------------------------------------------------------------------------------
  var watch = null;
  function install(r, fresh) {
    sheet((fresh ? "Routeur créé — " : "Installer ") + r.name, "terminal", "<p class='pane-sub'>Préparation du script…</p>");
    api("/api/routers/" + encodeURIComponent(r.id) + "/install").then(function (d) {
      var warn = /^127\./.test(d.server_ip) && r.installed ? "<div class='notice show notice-err'>Ce PC n'a pas pu trouver son adresse sur le réseau du routeur. Vérifiez l'adresse IP du routeur.</div>" : "";
      $("bsBody").innerHTML =
        "<div class='info-box'><strong>Une seule ligne à coller.</strong><br>Le routeur télécharge sa configuration, crée le compte « aderlink » et ouvre l'API uniquement pour ce serveur (" + esc(d.server_ip) + ").</div>" + warn +
        "<label class='q-label' for='script'>Script d'installation</label>" +
        "<textarea id='script' class='script' readonly rows='4'>" + esc(d.line) + "</textarea>" +
        "<div class='hint'>Touchez le texte pour tout sélectionner, puis copiez.</div>" +
        "<button class='btn btn-big btn-block' type='button' id='copy'>" + ic("copy") + " Copier le script</button>" +
        "<div class='info-box'><strong>Comment faire</strong><ol><li>Ouvrir <strong>Winbox</strong> et se connecter au routeur</li><li>Aller dans <strong>New Terminal</strong></li>" +
        "<li>Coller la ligne, puis <strong>Entrée</strong></li><li>C'est tout : le routeur se signale tout seul et apparaît <strong>En ligne</strong> ici</li></ol></div>" +
        "<div class='wait-box' id='waitBox'>" + (r.installed ? "" : "<span class='spin' aria-hidden='true'></span> En attente du routeur…") + "</div>" +
        "<div class='info-box ok-box'><strong>Ce que le routeur installe</strong><ul>" +
        "<li><strong>L'accès d'ADER LINK</strong> : le compte « aderlink » (lecture, écriture, API — pas de Winbox ni SSH), l'API ouverte seulement pour " + esc(d.server_ip) + "</li>" +
        "<li><strong>Vos profils</strong> : un profil hotspot « AL-… » par profil ADER LINK, avec sa vitesse et ses appareils</li>" +
        "<li><strong>L'expiration des tickets</strong> : à la 1re connexion, le routeur lance le compte à rebours et supprime le ticket à la fin, même si ADER LINK est éteint</li>" +
        "<li><strong>La liaison</strong> : le routeur se signale à ADER LINK toutes les 5 minutes</li>" +
        (d.tunnel ? "<li><strong>Le tunnel sécurisé</strong> vers le serveur en ligne (adresse du routeur : " + esc(d.tunnel) + ") — RouterOS 7 nécessaire</li>" : "") + "</ul>" +
        "Dans Winbox, vous verrez <strong>ADER LINK</strong> dans <strong>System → Scheduler</strong> et <strong>aderlink-login</strong>, <strong>aderlink-ping</strong> dans <strong>System → Scripts</strong>.<br>" +
        "<small>Déjà installé ? Recollez la ligne après avoir ajouté des profils : c'est sans danger.</small></div>" +
        "<div class='notice' id='bsMsg' role='status'></div>";
      $("bsFoot").innerHTML = "<button class='btn-prev' type='button' data-close>Fermer</button><button class='btn btn-big' type='button' id='verify'>Vérifier la connexion</button>";
      clearInterval(watch);
      if (!r.installed) watch = setInterval(function () {
        if ($("bs").hidden || !$("waitBox")) { clearInterval(watch); return; }
        api("/api/routers").then(function (d) {
          var now = d.routers.find(function (x) { return x.id === r.id; });
          if (now && now.installed) {
            clearInterval(watch);
            $("waitBox").className = "wait-box ok";
            $("waitBox").innerHTML = ic("check") + " Routeur détecté (" + esc(now.host) + "). Installation réussie !";
            S.routers = d.routers; render();
          }
        });
      }, 3000);
      $("script").addEventListener("focus", function () { this.select(); });
      $("script").addEventListener("click", function () { this.select(); });
      $("copy").addEventListener("click", function () {
        var t = $("script"), b = this;
        var done = function () { b.innerHTML = ic("check") + " Copié !"; setTimeout(function () { b.innerHTML = ic("copy") + " Copier le script"; }, 2000); };
        if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(t.value).then(done);
        else { t.select(); try { document.execCommand("copy"); done(); } catch (e) { /* sélection manuelle */ } }
      });
      $("verify").addEventListener("click", function () {
        var b = this; b.disabled = true; b.textContent = "Vérification…";
        api("/api/routers/" + encodeURIComponent(r.id) + "/test", {}).then(function (t) {
          msg(t.ok ? "ok" : "err", esc(t.ok ? "✓ " + t.message : "Pas encore connecté : " + t.message));
          if (t.ok) load();
        }).catch(function (err) { msg("err", esc(err.message)); })
          .then(function () { b.disabled = false; b.textContent = "Vérifier"; });
      });
    }).catch(function (err) { $("bsBody").innerHTML = "<p class='pane-sub'>" + esc(err.message) + "</p>"; });
  }

  load();
  setInterval(function () { if (!document.hidden && $("bs").hidden) load(); }, 30000);
})();
