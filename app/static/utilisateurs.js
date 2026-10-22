/* ADER LINK — page Utilisateurs (administrateur) */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var nf = new Intl.NumberFormat("fr-FR");
  var ic = window.AderShell.icon;
  var S = { users: [], zones: [] };

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
  function money(n) { return nf.format(Math.round(n || 0)) + " F"; }
  function initials(name) {
    var w = String(name).split(/\s+/).filter(Boolean);
    return ((w[0] || "?")[0] + (w[1] ? w[1][0] : (w[0] || "")[1] || "")).toUpperCase();
  }
  function when(ts) {
    if (!ts) return "jamais";
    return new Date(ts * 1000).toLocaleString("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  // Abonnement : texte, couleur et temps restant
  function left(sec) {
    if (sec <= 0) return "terminé";
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.max(1, Math.floor(sec % 3600 / 60));
    if (d >= 60) return Math.round(d / 30.4) + " mois";
    if (d >= 1) return d + " j" + (h ? " " + h + " h" : "");
    return h ? h + " h " + m + " min" : m + " min";
  }
  function fullWhen(ts) {
    return new Date(ts * 1000).toLocaleString("fr-FR", { weekday: "short", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function sub(u) {
    if (!u.expires_at) return { cls: "sub-free", title: "Sans limite", text: "Aucune date de fin" };
    var rest = u.expires_at - Math.floor(Date.now() / 1000);
    if (rest <= 0) return { cls: "sub-over", title: "Abonnement terminé", text: "depuis le " + fullWhen(u.expires_at) };
    return { cls: rest < 3 * 86400 ? "sub-soon" : "sub-ok", title: "Reste " + left(rest), text: "jusqu'au " + fullWhen(u.expires_at) };
  }
  function byId(id) { return S.users.find(function (u) { return u.id === id; }); }

  // --- liste ---------------------------------------------------------------------------
  function load() {
    return api("/api/users").then(function (d) {
      S.users = d.users; S.zones = d.zones;
      api("/api/me").then(function (m) { $("demoBanner").hidden = !m.demo; });
      render();
    }).catch(function (e) { $("list").innerHTML = "<div class='card empty'>" + esc(e.message) + "</div>"; });
  }
  function render() {
    $("list").innerHTML = S.users.length ? S.users.map(function (u) {
      var st = u.active ? "<span class='pill " + (u.role === "gerant" ? "pill-role" : "pill-online") + "'>" + esc(u.role_label) + "</span>"
        : "<span class='pill pill-offline'>Désactivé</span>";
      var act = function (a, label, icon) { return "<button type='button' class='act' data-act='" + a + "' data-id='" + u.id + "'>" + ic(icon) + "<span>" + label + "</span></button>"; };
      var s = u.stats;
      var kpi = function (k, v, cls) { return "<div class='u-kpi" + (cls ? " " + cls : "") + "'><span>" + k + "</span><strong class='num'>" + v + "</strong></div>"; };
      return "<article class='card router-card" + (u.active ? "" : " is-off") + "'>" +
        "<div class='rc-head'><span class='avatar " + (u.role === "gerant" ? "av-navy" : "av-orange") + "'>" + esc(initials(u.name)) + "</span>" +
        "<div class='rc-main'><div class='rc-name'>" + esc(u.name) + "</div>" +
        "<div class='rc-meta'><span class='mono'>" + esc(u.username) + "</span>" + (u.phone ? " · " + esc(u.phone) : "") + "</div>" +
        "<div class='rc-meta'>" + ic("router") + " " + esc(u.zone_names.join(", ") || "Aucun routeur") + "</div></div>" + st + "</div>" +
        "<div class='u-kpis'>" + kpi("Vendus ce mois", nf.format(s.sold)) + kpi("Montant", money(s.amount)) +
        (u.role === "revendeur" ? kpi("Commission " + nf.format(u.commission) + " %", money(s.commission_due), "u-kpi-orange") : kpi("Lots créés", nf.format(s.lots))) + "</div>" +
        (function () { var x = sub(u); return "<button type='button' class='sub-bar " + x.cls + "' data-act='subscription' data-id='" + u.id + "'>" + ic("clock") +
          "<span><strong>" + esc(x.title) + "</strong><small>" + esc(x.text) + "</small></span><em>Gérer</em></button>"; })() +
        "<div class='rc-meta u-last'>Dernière connexion : " + when(u.last_login) + "</div>" +
        "<div class='act-grid act-grid-4'>" + act("subscription", "Abonnement", "clock") + act("edit", "Modifier", "pencil") + act("password", "Mot de passe", "key") +
        act("toggle", u.active ? "Désactiver" : "Activer", u.active ? "x" : "check") + "</div></article>";
    }).join("") : "<div class='card empty'>Aucun utilisateur pour l'instant. Cliquez sur <strong>+ Ajouter</strong> pour créer le compte d'un gérant ou d'un revendeur.</div>";
  }
  $("list").addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (!b) return;
    var u = byId(+b.dataset.id);
    ({ edit: edit, password: password, toggle: toggle, delete: remove, subscription: subscription })[b.dataset.act](u);
  });
  $("addBtn").addEventListener("click", function () { edit(null); });

  // --- feuille ------------------------------------------------------------------------
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
  function msg(kind, text) {
    var n = $("bsMsg");
    if (n) { n.className = "notice show notice-" + kind; n.innerHTML = text; n.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }

  // Le mot de passe n'est montré qu'une seule fois : on propose de le copier
  function showPassword(u, pwd, title) {
    sheet(title, "key",
      "<p class='pane-sub'>Donnez ces informations à <strong>" + esc(u.name) + "</strong>. Le mot de passe ne sera plus affiché ensuite.</p>" +
      "<dl class='check-dl'><div><dt>Adresse</dt><dd class='mono'>" + esc(location.origin) + "</dd></div>" +
      "<div><dt>Identifiant</dt><dd class='mono'>" + esc(u.username) + "</dd></div>" +
      "<div><dt>Mot de passe</dt><dd class='mono pw-big'>" + esc(pwd) + "</dd></div></dl>",
      "<button class='btn-prev' type='button' data-close>Fermer</button><button class='btn btn-big' type='button' id='cp'>" + ic("copy") + " Copier</button>");
    $("cp").addEventListener("click", function () {
      var text = "ADER LINK\nAdresse : " + location.origin + "\nIdentifiant : " + u.username + "\nMot de passe : " + pwd;
      var b = $("cp");
      var done = function () { b.innerHTML = ic("check") + " Copié !"; setTimeout(function () { b.innerHTML = ic("copy") + " Copier"; }, 2000); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
      else {
        var t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
        try { document.execCommand("copy"); done(); } catch (e) { /* ignoré */ }
        t.remove();
      }
    });
  }

  // --- ajouter / modifier --------------------------------------------------------------
  function edit(u) {
    var v = u || { name: "", username: "", phone: "", role: "revendeur", zones: S.zones.length === 1 ? [S.zones[0].id] : [], commission: 0 };
    var f = function (id, label, value, attrs, hint) {
      return "<div class='f'><label for='" + id + "'>" + label + "</label><input id='" + id + "' value='" + esc(value) + "' " + (attrs || "") + ">" +
        (hint ? "<div class='hint'>" + hint + "</div>" : "") + "</div>";
    };
    var role = function (r, label, sub) {
      return "<label class='role-opt'><input type='radio' name='uRole' value='" + r + "'" + (v.role === r ? " checked" : "") + "><span><strong>" + label + "</strong><small>" + sub + "</small></span></label>";
    };
    var body = "<form id='uForm' class='form-grid form-grid-2' autocomplete='off' novalidate>" +
      f("uName", "Nom complet <span class='req'>*</span>", v.name, "maxlength='60' placeholder='ex : Moussa Issa' required") +
      f("uPhone", "Téléphone", v.phone || "", "inputmode='tel' maxlength='20' placeholder='ex : +227 90 00 00 00'") +
      f("uLogin", "Identifiant de connexion <span class='req'>*</span>", v.username, "maxlength='32' autocapitalize='off' spellcheck='false' placeholder='ex : moussa'", "Lettres minuscules et chiffres, sans espace") +
      (u ? "" : f("uPwd", "Mot de passe", "", "type='text' autocapitalize='off' spellcheck='false' placeholder='Laisser vide = créé automatiquement'", "6 caractères minimum")) +
      "<fieldset class='span2 role-pick'><legend>Rôle <span class='req'>*</span></legend>" +
      role("revendeur", "Revendeur", "Crée et imprime ses tickets, voit ses ventes") +
      role("gerant", "Gérant", "Tickets, profils et lots de ses routeurs") + "</fieldset>" +
      "<fieldset class='span2 zone-pick'><legend>Routeurs autorisés <span class='req'>*</span></legend>" +
      (S.zones.length ? S.zones.map(function (z) {
        return "<label class='check-opt'><input type='checkbox' name='uZone' value='" + esc(z.id) + "'" + (v.zones.indexOf(z.id) >= 0 ? " checked" : "") + "><span>" + esc(z.name) + "</span></label>";
      }).join("") : "<p class='pane-sub'>Ajoutez d'abord un routeur.</p>") + "</fieldset>" +
      "<div class='f' id='uComBox'><label for='uCom'>Commission du revendeur</label><div class='unit-field'><input id='uCom' type='number' min='0' max='100' step='any' inputmode='decimal' value='" + esc(v.commission || "") + "' placeholder='0'><span class='unit'>%</span></div>" +
      "<div class='hint'>Calculée sur les tickets vendus</div></div>" +
      (u ? "" : "<fieldset class='span2 sub-pick'><legend>Abonnement de départ</legend>" +
        "<div class='f'><div class='sub-add'><input id='uSubN' type='number' min='1' step='1' inputmode='numeric' value='1' aria-label='Durée'>" +
        "<select id='uSubU' aria-label='Unité'><option value='heure'>heure(s)</option><option value='jour'>jour(s)</option><option value='mois' selected>mois</option><option value='an'>an(s)</option></select></div></div>" +
        "<label class='check-opt'><input type='checkbox' id='uSubFree'><span>Sans limite (pas de date de fin)</span></label></fieldset>") +
      "</form><div class='notice' id='bsMsg' role='status'></div>";
    sheet(u ? "Modifier " + u.name : "Nouvel utilisateur", u ? "pencil" : "users", body,
      "<button class='btn-prev' type='button' data-close>Annuler</button>" + (u ? "<button class='btn btn-danger' type='button' id='uDel'>Supprimer</button>" : "") +
      "<button class='btn btn-big' type='button' id='uSave'>Enregistrer</button>");
    if (u) $("uDel").addEventListener("click", function () { remove(u); });
    else $("uSubFree").addEventListener("change", function () { $("uSubN").disabled = $("uSubU").disabled = this.checked; });
    function syncRole() {
      var r = document.querySelector("input[name=uRole]:checked").value;
      $("uComBox").hidden = r !== "revendeur";
    }
    Array.prototype.forEach.call(document.querySelectorAll("input[name=uRole]"), function (el) { el.addEventListener("change", syncRole); });
    syncRole();
    $("uName").addEventListener("input", function () {
      if (u || $("uLogin").dataset.touched) return;
      $("uLogin").value = $("uName").value.split(/\s+/)[0].toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9._-]/g, "");
    });
    $("uLogin").addEventListener("input", function () { $("uLogin").dataset.touched = "1"; });
    function save() {
      var data = {
        name: $("uName").value, phone: $("uPhone").value, username: $("uLogin").value,
        role: document.querySelector("input[name=uRole]:checked").value,
        zones: Array.prototype.map.call(document.querySelectorAll("input[name=uZone]:checked"), function (el) { return el.value; }),
        commission: +$("uCom").value || 0
      };
      if (!u) {
        data.password = $("uPwd").value;
        data.subscription = $("uSubFree").checked ? { unlimited: true } : { unlimited: false, n: $("uSubN").value, unit: $("uSubU").value };
      }
      var btn = $("uSave"); btn.disabled = true;
      api(u ? "/api/users/" + u.id + "/update" : "/api/users", data).then(function (res) {
        return load().then(function () {
          if (!u) showPassword(res, res.password, "Compte créé");
          else close();
        });
      }).catch(function (err) { msg("err", esc(err.message)); btn.disabled = false; });
    }
    $("uSave").addEventListener("click", save);
    $("uForm").addEventListener("submit", function (e) { e.preventDefault(); save(); });
  }

  // --- abonnement --------------------------------------------------------------------------
  var UNIT_S = { heure: 3600, jour: 86400 };
  function previewEnd(u, n, unit) {
    var now = Math.floor(Date.now() / 1000);
    var start = u.expires_at && u.expires_at > now ? u.expires_at : now;
    if (!(n >= 1)) return null;
    if (UNIT_S[unit]) return start + n * UNIT_S[unit];
    var d = new Date(start * 1000), months = unit === "an" ? 12 * n : n;
    var day = d.getDate();
    d.setDate(1); d.setMonth(d.getMonth() + months);
    d.setDate(Math.min(day, new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()));
    return Math.floor(d.getTime() / 1000);
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function localInput(ts) {
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function subscription(u) {
    var x = sub(u);
    var body =
      "<div class='sub-state " + x.cls + "'>" + ic("clock") + "<div><strong>" + esc(x.title) + "</strong><span>" + esc(x.text) + "</span></div></div>" +
      "<form id='sForm' autocomplete='off' novalidate><h3 class='sub-h'>Ajouter du temps</h3>" +
      "<div class='f'><div class='sub-add'><input id='sN' type='number' min='1' step='1' inputmode='numeric' placeholder='Nombre' aria-label='Nombre'>" +
      "<select id='sU' aria-label='Unité'><option value='heure'>heure(s)</option><option value='jour' selected>jour(s)</option><option value='mois'>mois</option><option value='an'>an(s)</option></select></div></div>" +
      "<div class='chips' id='sChips'><button type='button' data-n='1' data-u='jour'>+1 jour</button><button type='button' data-n='7' data-u='jour'>+7 jours</button>" +
      "<button type='button' data-n='1' data-u='mois'>+1 mois</button><button type='button' data-n='3' data-u='mois'>+3 mois</button><button type='button' data-n='1' data-u='an'>+1 an</button></div>" +
      "<div class='f sub-note'><label for='sNote'>Note (facultatif)</label><input id='sNote' maxlength='80' placeholder='ex : payé 5 000 F en espèces'></div>" +
      "<div class='sub-preview' id='sPrev'>Saisissez un nombre pour voir la nouvelle date de fin.</div>" +
      "<button class='btn btn-big btn-block' type='submit' id='sAdd' disabled>Ajouter</button></form>" +
      "<details class='adv sub-more'><summary>Autres réglages</summary>" +
      "<div class='f'><label for='sUntil'>Fixer la date et l'heure de fin</label><div class='sub-add'><input id='sUntil' type='datetime-local'>" +
      "<button class='btn btn-outline' type='button' id='sSet'>Fixer</button></div></div>" +
      "<div class='sub-btns'><button class='btn btn-outline' type='button' id='sFree'>Sans limite</button>" +
      "<button class='btn btn-danger' type='button' id='sStop'>Arrêter maintenant</button></div></details>" +
      "<div class='notice' id='bsMsg' role='status'></div>" +
      "<h3 class='sub-h'>Historique</h3><ul class='sub-hist' id='sHist'><li class='pane-sub'>Chargement…</li></ul>";
    sheet("Abonnement de " + u.name, "clock", body);
    var now = Math.floor(Date.now() / 1000);
    $("sUntil").value = localInput(u.expires_at && u.expires_at > now ? u.expires_at : now + 30 * 86400);
    function update() {
      var end = previewEnd(u, +$("sN").value, $("sU").value);
      $("sAdd").disabled = !end || +$("sN").value % 1 !== 0;
      $("sPrev").innerHTML = end ? "Nouvelle fin : <strong>" + esc(fullWhen(end)) + "</strong>" : "Saisissez un nombre pour voir la nouvelle date de fin.";
      $("sAdd").textContent = end ? "Ajouter " + $("sN").value + " " + $("sU").selectedOptions[0].textContent : "Ajouter";
    }
    $("sN").addEventListener("input", update);
    $("sU").addEventListener("change", update);
    $("sChips").addEventListener("click", function (e) {
      var b = e.target.closest("[data-n]");
      if (!b) return;
      $("sN").value = b.dataset.n; $("sU").value = b.dataset.u; update(); $("sN").focus();
    });
    function send(data, btn) {
      if (btn) btn.disabled = true;
      data.note = $("sNote").value;
      return api("/api/users/" + u.id + "/subscription", data).then(function (res) {
        return load().then(function () {
          subscription(byId(u.id));
          msg("ok", "Enregistré : " + esc(sub(res).title.toLowerCase()) + " " + esc(sub(res).text) + ".");
        });
      }).catch(function (err) { msg("err", esc(err.message)); if (btn) btn.disabled = false; });
    }
    $("sForm").addEventListener("submit", function (e) {
      e.preventDefault();
      if (!$("sAdd").disabled) send({ action: "add", n: $("sN").value, unit: $("sU").value }, $("sAdd"));
    });
    $("sSet").addEventListener("click", function () {
      var t = new Date($("sUntil").value);
      if (isNaN(t)) return msg("err", "Choisissez une date et une heure");
      send({ action: "set", until: Math.floor(t.getTime() / 1000) }, $("sSet"));
    });
    $("sFree").addEventListener("click", function () { send({ action: "unlimited" }, $("sFree")); });
    $("sStop").addEventListener("click", function () {
      var b = $("sStop");
      if (b.dataset.confirm !== "1") { b.dataset.confirm = "1"; b.textContent = "Confirmer l'arrêt ?"; return; }
      send({ action: "stop" }, b);
    });
    api("/api/users/" + u.id + "/history").then(function (d) {
      $("sHist").innerHTML = d.history.length ? d.history.map(function (h) {
        return "<li><div class='rp-row'><span>" + esc(h.label) + "</span><span class='rp-sub'>" + esc(when(h.at)) + "</span></div>" +
          "<div class='rp-sub'>" + (h.new_expires ? "Fin : " + esc(fullWhen(h.new_expires)) : "Sans date de fin") + (h.note ? " · " + esc(h.note) : "") + "</div></li>";
      }).join("") : "<li class='pane-sub'>Rien pour l'instant.</li>";
    });
  }

  // --- mot de passe ----------------------------------------------------------------------
  function password(u) {
    sheet("Nouveau mot de passe", "key",
      "<p class='pane-sub'>Pour <strong>" + esc(u.name) + "</strong> (" + esc(u.username) + "). L'ancien ne marchera plus.</p>" +
      "<form id='pForm' class='form-grid' autocomplete='off' novalidate><div class='f'><label for='pNew'>Nouveau mot de passe</label>" +
      "<input id='pNew' type='text' autocapitalize='off' spellcheck='false' placeholder='Laisser vide = créé automatiquement'><div class='hint'>6 caractères minimum</div></div></form>" +
      "<div class='notice' id='bsMsg' role='status'></div>",
      "<button class='btn-prev' type='button' data-close>Annuler</button><button class='btn btn-big' type='button' id='pSave'>Changer</button>");
    function go() {
      api("/api/users/" + u.id + "/password", { password: $("pNew").value }).then(function (res) {
        showPassword(u, res.password, "Mot de passe changé");
      }).catch(function (err) { msg("err", esc(err.message)); });
    }
    $("pSave").addEventListener("click", go);
    $("pForm").addEventListener("submit", function (e) { e.preventDefault(); go(); });
  }

  // --- activer / désactiver / supprimer -------------------------------------------------------
  function toggle(u) {
    api("/api/users/" + u.id + "/" + (u.active ? "disable" : "enable"), {}).then(load)
      .catch(function (err) { alertSheet(err.message); });
  }
  function remove(u) {
    sheet("Supprimer " + u.name + " ?", "trash",
      "<p class='pane-sub'>Le compte <strong>" + esc(u.username) + "</strong> ne pourra plus se connecter. Ses lots et ses ventes restent dans l'historique." +
      (u.role === "revendeur" ? " Son point de vente sera retiré." : "") + "</p><p class='pane-sub'>Pour couper l'accès juste un moment, utilisez plutôt <strong>Désactiver</strong>.</p>" +
      "<div class='notice' id='bsMsg' role='status'></div>",
      "<button class='btn-prev' type='button' data-close>Annuler</button><button class='btn btn-danger' type='button' id='dGo'>Supprimer</button>");
    $("dGo").addEventListener("click", function () {
      api("/api/users/" + u.id + "/delete", {}).then(function () { close(); load(); })
        .catch(function (err) { msg("err", esc(err.message)); });
    });
  }
  function alertSheet(text) {
    sheet("Impossible", "info", "<p class='pane-sub'>" + esc(text) + "</p>");
  }

  load();
})();
