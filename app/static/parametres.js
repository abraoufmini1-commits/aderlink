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
      var host = location.hostname === "127.0.0.1" || location.hostname === "localhost" ? null : "http://" + location.host;
      $("phoneUrl").textContent = host || d.phone_url || "Adresse introuvable : tapez ipconfig sur le PC.";
    });
  }
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
