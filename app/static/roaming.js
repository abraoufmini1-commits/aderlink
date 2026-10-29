/* ADER LINK — ADER Roaming : réseaux de routeurs qui partagent les mêmes tickets */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var ic = window.AderShell.icon;
  var S = { groups: [], zones: [] };

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

  function load() {
    return api("/api/roaming").then(function (d) {
      S.groups = d.groups; S.zones = d.zones;
      var on = d.listening && d.listening.auth;
      var m = $("srvMsg");
      if (!on) {
        m.className = "notice show notice-err";
        m.textContent = "Le serveur ADER Roaming n'est pas en marche sur cette installation (ports UDP " + d.ports.join(" et ") +
          "). Il démarre avec ADER LINK sur le serveur en ligne (VPS).";
      } else if (d.ignored && d.ignored.length) {
        m.className = "notice show notice-err";
        m.innerHTML = "Des routeurs non reconnus essaient d'utiliser ADER Roaming : " + d.ignored.map(function (x) {
          return "<b>" + esc(x.ip) + "</b>" + (x.name ? " (" + esc(x.name) + ")" : "") + " " + ago(x.ago);
        }).join(", ") + ". Vérifiez qu'ils sont bien cochés dans un réseau.";
      } else m.className = "notice";
      render();
    }).catch(function (e) { $("list").innerHTML = "<div class='card empty'>" + esc(e.message) + "</div>"; });
  }
  api("/api/me").then(function (me) {
    $("demoBanner").hidden = !me.demo;
    if (me.features && !me.features.roaming) {  // pas compris dans la formule
      var m = $("srvMsg");
      m.className = "notice show notice-err";
      m.innerHTML = "ADER Roaming n'est pas compris dans votre formule" + (me.plan ? " " + esc(me.plan.name) : "") +
        ". <a class='link' href='/abonnement'>Voir les formules</a>";
      $("addBtn").hidden = true;
    }
  }).catch(function () {});

  function ago(sec) {
    if (sec < 90) return "à l'instant";
    if (sec < 3600) return "il y a " + Math.round(sec / 60) + " min";
    if (sec < 86400) return "il y a " + Math.round(sec / 3600) + " h";
    return "il y a " + Math.round(sec / 86400) + " j";
  }
  // Chaque routeur du réseau : réglé ou non, et dernier contact avec le serveur Roaming
  function zoneLine(z) {
    var setup = z.setup_ok === true ? "<span class='ok'>✓ Réglé</span>" :
      z.setup_ok === false ? "<span class='bad'>✗ " + esc(z.setup_msg) + "</span>" : "<span class='wait'>… " + esc(z.setup_msg) + "</span>";
    var seen = z.seen_ago != null ? "<span class='ok'>Contact " + ago(z.seen_ago) + "</span>" :
      "<span class='wait'>Aucun contact depuis le démarrage</span>";
    return "<li>" + ic("router") + "<b>" + esc(z.name) + "</b>" + setup + seen + "</li>";
  }
  function stat(cls, n, label) { return "<div class='lstat " + cls + "'><strong class='num'>" + nf.format(n) + "</strong><span>" + label + "</span></div>"; }
  function render() {
    $("list").innerHTML = S.groups.length ? S.groups.map(function (g) {
      return "<article class='card ro-card'>" +
        "<div class='ro-head'><span class='avatar av-blue' aria-hidden='true'>" + ic("zones") + "</span>" +
        "<div class='ro-main'><div class='ro-name'>" + esc(g.name) + "</div>" +
        "<div class='ro-sub'>" + nf.format(g.zones.length) + " routeur" + (g.zones.length > 1 ? "s" : "") + " · " +
        nf.format(g.connected) + " connecté" + (g.connected > 1 ? "s" : "") + " en Roaming</div></div></div>" +
        "<ul class='ro-status'>" + g.zones.map(zoneLine).join("") + "</ul>" +
        "<div class='lstats lstats-3'>" + stat("ls-total", g.tickets, "Tickets Roaming") + stat("ls-left", g.left, "Restants") + stat("ls-live", g.used, "Utilisés") + "</div>" +
        "<button type='button' class='btn btn-outline btn-block ro-check' data-act='check' data-id='" + g.id + "'>Vérifier les routeurs</button>" +
        "<div class='act-grid act-grid-3'>" +
        "<button type='button' class='act' data-act='script' data-id='" + g.id + "'>" + ic("terminal") + "<span>Script</span></button>" +
        "<button type='button' class='act' data-act='edit' data-id='" + g.id + "'>" + ic("pencil") + "<span>Modifier</span></button>" +
        "<button type='button' class='act' data-act='del' data-id='" + g.id + "'>" + ic("trash") + "<span>Supprimer</span></button></div></article>";
    }).join("") : "<div class='card empty'>Aucun réseau pour l'instant. Cliquez sur <strong>+ Nouveau réseau</strong> et cochez au moins 2 routeurs.</div>";
  }
  $("list").addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (!b) return;
    var g = S.groups.find(function (x) { return x.id === +b.dataset.id; });
    ({ script: script, edit: edit, del: remove, check: check })[b.dataset.act](g);
  });
  $("addBtn").addEventListener("click", function () { edit(null); });

  // --- feuille -----------------------------------------------------------------------------
  var lastFocus = null;
  function sheet(title, icon, body, foot) {
    lastFocus = document.activeElement;
    $("bsTitle").textContent = title;
    $("bsIco").innerHTML = ic(icon);
    $("bsClose").innerHTML = ic("x");
    $("bsBody").innerHTML = body;
    $("bsFoot").innerHTML = foot || "<button class='btn-prev' type='button' data-close>Fermer</button>";
    $("bs").hidden = false; $("bsShade").classList.add("open");
    document.body.classList.add("no-scroll");
    var f = $("bs").querySelector("input, .btn, button[data-close]");
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
  function msg(kind, text) { var n = $("bsMsg"); if (n) { n.className = "notice show notice-" + kind; n.innerHTML = text; } }

  // --- créer / modifier ------------------------------------------------------------------------
  function edit(g) {
    var mine = g ? g.zones.map(function (z) { return z.id; }) : [];
    var other = function (z) { return z.group && (!g || z.group !== g.id) ? S.groups.find(function (x) { return x.id === z.group; }) : null; };
    var body = "<form id='gForm' class='form-grid form-grid-2' autocomplete='off' novalidate>" +
      "<div class='f span2'><label for='gName'>Nom du réseau <span class='req'>*</span></label><input id='gName' maxlength='40' value='" + esc(g ? g.name : "") +
      "' placeholder='Ex : Réseau ADER Tahoua'></div>" +
      "<fieldset class='span2 zone-pick'><legend>Routeurs du réseau <span class='req'>*</span> (2 ou plus)</legend>" +
      (S.zones.length ? S.zones.map(function (z) {
        var o = other(z);
        return "<label class='check-opt'><input type='checkbox' name='gZone' value='" + esc(z.id) + "'" + (mine.indexOf(z.id) >= 0 ? " checked" : "") + "><span>" + esc(z.name) +
          (o ? "<small class='ro-warn'>Quittera le réseau « " + esc(o.name) + " »</small>" : "") + "</span></label>";
      }).join("") : "<p class='pane-sub'>Ajoutez d'abord vos routeurs.</p>") + "</fieldset></form>" +
      "<div class='notice' id='bsMsg' role='status'></div>";
    sheet(g ? "Modifier " + g.name : "Nouveau réseau ADER Roaming", "zones", body,
      "<button class='btn-prev' type='button' data-close>Annuler</button><button class='btn btn-big' type='button' id='gSave'>" +
      (g ? "Enregistrer" : ic("check") + " Créer le réseau") + "</button>");
    function save() {
      var data = { name: $("gName").value, zones: Array.prototype.map.call(document.querySelectorAll("input[name=gZone]:checked"), function (el) { return el.value; }) };
      if (!data.name.trim()) { msg("err", "Donnez un nom au réseau."); $("gName").focus(); return; }
      if (data.zones.length < 2) { msg("err", "Cochez au moins 2 routeurs."); return; }
      $("gSave").disabled = true;
      api(g ? "/api/roaming/" + g.id : "/api/roaming", data).then(function (res) {
        return load().then(function () { script(res, !g); });
      }).catch(function (e) { msg("err", esc(e.message)); $("gSave").disabled = false; });
    }
    $("gSave").addEventListener("click", save);
    $("gForm").addEventListener("submit", function (e) { e.preventDefault(); save(); });
  }

  // --- script à coller dans chaque routeur -------------------------------------------------------
  function script(g, fresh) {
    sheet((fresh ? "Réseau créé — " : "Script — ") + g.name, "terminal", "<p class='pane-sub'>Préparation du script…</p>");
    api("/api/roaming/" + g.id + "/script").then(function (d) {
      $("bsBody").innerHTML =
        "<div class='info-box ok-box'><strong>ADER LINK règle maintenant les routeurs tout seul</strong> (en 1 à 2 minutes, s'ils sont en ligne). Ce script ne sert qu'en secours, si un routeur reste en rouge.</div>" +
        "<div class='info-box'><strong>En secours : collez ce script dans le routeur</strong> (" +
        g.zones.map(function (z) { return esc(z.name); }).join(", ") + ").<br>Winbox → <strong>New Terminal</strong> → coller → <strong>Entrée</strong>.</div>" +
        "<label class='q-label' for='script'>Script ADER Roaming</label>" +
        "<textarea id='script' class='script' readonly rows='6'>" + esc(d.script) + "</textarea>" +
        "<div class='hint'>Les routeurs demanderont à ADER LINK (" + esc(d.server) + ") si un code est bon. " +
        "Le script est aussi ajouté automatiquement au script d'installation de ces routeurs.</div>" +
        "<div class='info-box ok-box'><strong>Bon à savoir</strong><ul>" +
        "<li>Les tickets normaux de chaque routeur continuent de marcher comme avant.</li>" +
        "<li>Le serveur ADER LINK doit être joignable par les routeurs : c'est le rôle du serveur en ligne (VPS).</li>" +
        "<li>Si ADER LINK est arrêté, les nouveaux tickets Roaming ne peuvent pas se connecter ; les clients déjà connectés continuent.</li></ul></div>";
      $("bsFoot").innerHTML = "<button class='btn-prev' type='button' data-close>Fermer</button><button class='btn btn-big' type='button' id='copy'>" + ic("copy") + " Copier le script</button>";
      $("script").addEventListener("focus", function () { this.select(); });
      $("copy").addEventListener("click", function () {
        var t = $("script"), b = $("copy");
        var done = function () { b.innerHTML = ic("check") + " Copié !"; setTimeout(function () { b.innerHTML = ic("copy") + " Copier le script"; }, 2000); };
        if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(t.value).then(done, function () { t.select(); });
        else { t.select(); try { document.execCommand("copy"); done(); } catch (e) { /* ignoré */ } }
      });
    }).catch(function (e) { $("bsBody").innerHTML = "<div class='notice show notice-err'>" + esc(e.message) + "</div>"; });
  }

  // --- vérifier : ce que chaque routeur a vraiment (serveurs RADIUS, compteurs, profils) ----------
  function check(g) {
    sheet("Vérification — " + g.name, "zones", "<p class='pane-sub'>Lecture des routeurs… (quelques secondes)</p>");
    api("/api/roaming/" + g.id + "/check", {}).then(function (d) {
      $("bsBody").innerHTML = d.routers.map(function (r) {
        var lines = [];
        if (r.error) lines.push("<li class='bad'>Routeur injoignable : " + esc(r.error) + "</li>");
        var ours = r.radius.filter(function (x) { return x.ours; });
        var others = r.radius.filter(function (x) { return !x.ours && !x.disabled && /hotspot/.test(x.service); });
        if (!r.error && !ours.length) lines.push("<li class='bad'>Réglage ADER Roaming absent</li>");
        ours.forEach(function (x) {
          var good = x.address === r.expected;
          lines.push("<li class='" + (good ? "ok" : "bad") + "'>Serveur ADER Roaming : " + esc(x.address) + (good ? " ✓" : " (attendu " + esc(r.expected || "?") + ")") +
            " · demandes " + esc(x.requests || "0") + ", acceptées " + esc(x.accepts || "0") + ", refusées " + esc(x.rejects || "0") +
            ", <b>sans réponse " + esc(x.timeouts || "0") + "</b></li>");
        });
        var first = r.radius.filter(function (x) { return !x.disabled && /hotspot/.test(x.service); })[0];
        var oursFirst = first && first.ours;
        others.forEach(function (x) {
          lines.push(oursFirst ?
            "<li class='ok'>Autre système de tickets : " + esc(x.address) + " — ADER Roaming passe en premier, les deux marchent ✓</li>" :
            "<li class='bad'>Autre système de tickets : " + esc(x.address) + " passe avant ADER Roaming (correction automatique en cours)</li>");
        });
        r.profiles.forEach(function (p) {
          lines.push("<li class='" + (p.use_radius ? "ok" : "bad") + "'>Profil " + esc(p.name) + " : Use RADIUS " + (p.use_radius ? "coché ✓" : "décoché") + "</li>");
        });
        lines.push("<li class='" + (r.seen_ago != null ? "ok" : "wait") + "'>" + (r.seen_ago != null ? "Dernier contact avec le serveur : " + ago(r.seen_ago) : "Aucun contact avec le serveur depuis le démarrage") + "</li>");
        return "<div class='ro-diag'><b>" + esc(r.name) + "</b><ul>" + lines.join("") + "</ul></div>";
      }).join("") + "<p class='hint'>Envoyez une photo de cette page à votre support si un routeur a des lignes en rouge.</p>";
    }).catch(function (e) { $("bsBody").innerHTML = "<div class='notice show notice-err'>" + esc(e.message) + "</div>"; });
  }

  // --- supprimer -------------------------------------------------------------------------------
  function remove(g) {
    sheet("Supprimer " + g.name + " ?", "trash",
      "<p class='pane-sub'>Les routeurs quittent le réseau. Les <strong>" + nf.format(g.left) + " tickets Roaming non utilisés</strong> " +
      "ne pourront plus se connecter. Les ventes restent dans l'historique.</p><div class='notice' id='bsMsg' role='status'></div>",
      "<button class='btn-prev' type='button' data-close>Annuler</button><button class='btn btn-danger' type='button' id='gDel'>Supprimer le réseau</button>");
    $("gDel").addEventListener("click", function () {
      api("/api/roaming/" + g.id + "/delete", {}).then(function () { close(); load(); }).catch(function (e) { msg("err", esc(e.message)); });
    });
  }

  load();
})();
