/* ADER LINK — paramètres : mise à jour automatique */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
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
        .then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur"); return d; });
    });
  }
  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "jamais"; }
  var LABEL = { idle: "Pas encore configurée", checking: "Vérification en cours…", uptodate: "À jour", available: "Nouvelle version disponible",
    installing: "Installation en cours…", restarting: "Redémarrage…", error: "Erreur" };
  function show(u, demo) {
    $("ver").textContent = u.current;
    if (document.activeElement !== $("repo")) $("repo").value = u.repo || "";
    $("auto").checked = u.auto;
    $("upState").innerHTML = "<strong>" + esc(LABEL[u.status] || u.status) + "</strong>" + (u.message && u.status !== "uptodate" ? " — " + esc(u.message) : "") +
      "<br>Version installée : " + esc(u.current) + (u.latest ? " · dernière sur GitHub : " + esc(u.latest) : "") +
      "<br>Dernière vérification : " + when(u.checked_at) +
      (u.status === "available" ? "<br><button class='btn btn-sm' type='button' id='installNow'>Installer maintenant</button>" : "");
    var b = $("installNow");
    if (b) b.addEventListener("click", function () { run(true); });
    $("demoBanner").hidden = !demo;
  }
  function note(kind, text) { var n = $("upMsg"); n.className = "notice show notice-" + kind; n.textContent = text; }
  function load() {
    return api("/api/settings").then(function (d) {
      show(d.update, d.demo);
      if (!$("ctName").dataset.loaded) {
        $("ctName").value = (d.contact || {}).name || ""; $("ctPhone").value = (d.contact || {}).phone || "";
        $("ctName").dataset.loaded = "1";
      }
      var host = location.hostname === "127.0.0.1" || location.hostname === "localhost" ? null : "http://" + location.host;
      $("phoneUrl").textContent = host || d.phone_url || "Adresse introuvable : tapez ipconfig sur le PC.";
    });
  }
  $("ctSave").addEventListener("click", function () {
    var m = $("ctMsg");
    api("/api/settings/contact", { name: $("ctName").value, phone: $("ctPhone").value }).then(function () {
      m.className = "notice show notice-ok"; m.textContent = "Contact enregistré.";
    }).catch(function (e) { m.className = "notice show notice-err"; m.textContent = e.message; });
  });
  // Restauration : remplace tout par le contenu du fichier, puis ADER LINK redémarre
  $("bkFile").addEventListener("change", function () {
    var f = this.files && this.files[0], m = $("bkMsg"), input = this;
    if (!f) return;
    var say = function (kind, text) { m.className = "notice show notice-" + kind; m.textContent = text; };
    if (!/\.zip$/i.test(f.name)) { say("err", "Choisissez le fichier .zip téléchargé avec « Télécharger une sauvegarde »."); return; }
    if (!window.confirm("Tout remplacer par cette sauvegarde (" + f.name + ") ?\nLes données actuelles de cet ADER LINK seront effacées.")) { input.value = ""; return; }
    say("ok", "Envoi de la sauvegarde…");
    fetch("/api/restore", { method: "POST", credentials: "same-origin", headers: { "X-AderLink": "1", "Content-Type": "application/zip" }, body: f })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur"); return d; }); })
      .then(function (d) {
        say("ok", d.message + " La page va se recharger, reconnectez-vous avec le mot de passe de la sauvegarde.");
        setTimeout(function () { location.href = "/login"; }, 6000);
      })
      .catch(function (e) { say("err", e.message); input.value = ""; });
  });
  function run(install) {
    note("ok", install ? "Installation… la page se rechargera toute seule." : "Vérification…");
    api("/api/settings/check", { install: install }).then(function (d) {
      show(d.update);
      if (d.update.status === "error") note("err", d.update.message);
      else if (!install) note("ok", LABEL[d.update.status] || d.update.message);
    }).catch(function (e) { note("err", e.message); });
  }
  $("saveBtn").addEventListener("click", function () {
    api("/api/settings/update", { repo: $("repo").value, auto: $("auto").checked }).then(function (d) {
      show(d.update);
      note("ok", d.update.repo ? "Enregistré. Vérification du dépôt…" : "Mise à jour automatique désactivée.");
      if (d.update.repo) run(false);
    }).catch(function (e) { note("err", e.message); });
  });
  $("checkBtn").addEventListener("click", function () { run(false); });
  load();
  setInterval(load, 15000);
})();

/* Sécurité : alertes Telegram et sauvegardes automatiques */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  if (!$("secCard")) return;
  var ic = window.AderShell.icon;
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
      return r.json().catch(function () { throw new Error("Redémarrez ADER LINK"); })
        .then(function (d) { if (!r.ok) throw new Error(d.error || "Erreur"); return d; });
    });
  }
  function msg(kind, text) { var n = $("secMsg"); n.className = "notice show notice-" + kind; n.textContent = text; }
  function size(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " Mo" : Math.max(1, Math.round(n / 1024)) + " Ko"; }
  function when(ts) { return new Date(ts * 1000).toLocaleString("fr-FR", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }

  function render(s) {
    var t = s.telegram, box = $("tgBox");
    if (t.linked) {
      box.innerHTML = "<div class='sub-state sub-ok'>" + ic("check") + "<div><strong>Relié à Telegram" + (t.who ? " (" + esc(t.who) + ")" : "") + "</strong>" +
        "<span>Vous recevez : routeur hors ligne (après 3 min) et de retour, " + (s.summary_hour === 0 ? "ventes de la journée à minuit" : "ventes du jour à " + s.summary_hour + " h") + ", sauvegarde de la nuit.</span></div></div>" +
        "<div class='bk-actions'><button class='btn btn-outline' type='button' id='tgTest'>Envoyer un message de test</button>" +
        "<button class='btn-prev' type='button' id='tgOff'>Délier</button></div>";
      $("tgTest").onclick = function () { api("/api/security/test", {}).then(function () { msg("ok", "Message envoyé : regardez Telegram."); }).catch(function (e) { msg("err", e.message); }); };
      $("tgOff").onclick = function () { api("/api/security/unlink", {}).then(render); };
    } else if (t.token) {
      box.innerHTML = "<ol class='wb-steps'><li>Ouvrez votre bot <strong>@" + esc(t.bot) + "</strong> dans Telegram : " +
        "<a class='link' href='https://t.me/" + encodeURIComponent(t.bot) + "' target='_blank' rel='noopener'>t.me/" + esc(t.bot) + "</a></li>" +
        "<li>Appuyez sur <strong>DÉMARRER</strong> (ou écrivez <strong>/start</strong>).</li><li>Revenez ici et cliquez :</li></ol>" +
        "<div class='bk-actions'><button class='btn' type='button' id='tgLink'>J'ai écrit au bot : relier</button>" +
        "<button class='btn-prev' type='button' id='tgOff'>Changer de bot</button></div>";
      $("tgLink").onclick = function () { api("/api/security/link", {}).then(function (d) { render(d); msg("ok", "Relié ! Un message de bienvenue est arrivé sur Telegram."); }).catch(function (e) { msg("err", e.message); }); };
      $("tgOff").onclick = function () { api("/api/security/unlink", {}).then(render); };
    } else {
      box.innerHTML = "<ol class='wb-steps'><li>Dans Telegram, ouvrez <a class='link' href='https://t.me/BotFather' target='_blank' rel='noopener'>@BotFather</a> et envoyez <strong>/newbot</strong>.</li>" +
        "<li>Donnez un nom (ex : <em>ADER LINK Alertes</em>) puis un identifiant finissant par <em>bot</em> (ex : <em>aderlink_alertes_bot</em>).</li>" +
        "<li>BotFather vous donne un <strong>jeton</strong> (ex : <span class='mono'>123456789:AAH…</span>). Collez-le ici :</li></ol>" +
        "<div class='dev-form tg-form'><div class='f'><label for='tgToken'>Jeton du bot</label><input id='tgToken' autocomplete='off' spellcheck='false' placeholder='123456789:AA…'></div>" +
        "<button class='btn' type='button' id='tgSave'>Valider</button></div>";
      $("tgSave").onclick = function () {
        var b = this; b.disabled = true;
        api("/api/security/telegram", { token: $("tgToken").value }).then(function (d) { render(d); $("secMsg").className = "notice"; })
          .catch(function (e) { msg("err", e.message); }).then(function () { b.disabled = false; });
      };
    }
    $("bkList").innerHTML = s.backups.length ? s.backups.map(function (b) {
      return "<div class='ra-row'><div class='ra-main'><div class='ra-label'>" + esc(when(b.ts)) + "</div><div class='ra-value mono'>" + esc(b.name) + " · " + size(b.size) + "</div></div>" +
        "<a class='btn btn-sm btn-outline' href='/api/backups/" + encodeURIComponent(b.name) + "' download>" + ic("download") + " Télécharger</a></div>";
    }).join("") : "<p class='pane-sub'>Aucune sauvegarde automatique pour l'instant : la première sera faite cette nuit, ou cliquez sur « Sauvegarder maintenant ».</p>";
    // la liste reste fermée : un bouton l'ouvre (pas besoin de tout faire défiler)
    var last = s.backups[0];
    $("bkToggle").hidden = !s.backups.length;
    $("bkToggle").textContent = ($("bkList").hidden ? "Voir les sauvegardes" : "Masquer les sauvegardes") +
      " (" + s.backups.length + ")" + (last && $("bkList").hidden ? " · dernière : " + when(last.ts) : "");
    $("bkToggle").onclick = function () {
      $("bkList").hidden = !$("bkList").hidden;
      this.setAttribute("aria-expanded", String(!$("bkList").hidden));
      render(s);
    };
    if (!s.backups.length) $("bkList").hidden = false;  // message « aucune sauvegarde » visible
    $("bkNow").disabled = !s.available;
    if (!s.available) $("bkAutoText").textContent = "La sauvegarde automatique fonctionne sur le serveur en ligne (pas en démonstration).";
  }
  function load() { api("/api/security").then(render).catch(function (e) { msg("err", e.message); }); }
  $("bkNow").addEventListener("click", function () {
    var b = this; b.disabled = true; b.textContent = "Sauvegarde…";
    api("/api/security/backup", {}).then(function (d) {
      render(d); msg("ok", "Sauvegarde faite" + (d.telegram ? " et envoyée sur Telegram." : "."));
    }).catch(function (e) { msg("err", e.message); }).then(function () { b.disabled = false; b.textContent = "Sauvegarder maintenant"; });
  });
  load();
})();
