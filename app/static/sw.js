/* ADER LINK — service worker : rend l'application installable et garde les fichiers de l'application
   (style, scripts, images) sur l'appareil pour que les pages s'ouvrent tout de suite.
   Les données (tickets, ventes…) ne sont jamais gardées : elles viennent toujours du serveur. */
var CACHE = "aderlink-static-v1";
// pages de l'application : affichées tout de suite depuis l'appareil, puis relues sur le serveur à côté.
// Le nom change à chaque mise à jour (empreinte des fichiers) : les anciennes pages sont alors effacées.
var PAGES = "aderlink-pages-__BUILD__";
var PAGE_PATHS = ["/", "/tickets", "/lots", "/controle", "/routeurs", "/parametres", "/utilisateurs", "/rapports",
                  "/analytique", "/profil", "/roaming", "/formules", "/abonnement"];
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf("aderlink-pages-") === 0 && k !== PAGES; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
function offline() {
  return new Response("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>ADER LINK</title>" +
    "<body style='font-family:system-ui;padding:40px 20px;text-align:center;color:#0a3f8a'><h1>ADER LINK</h1>" +
    "<p>Pas de connexion Internet. Réessayez dans un instant.</p></body>", { headers: { "Content-Type": "text/html; charset=utf-8" } });
}
self.addEventListener("fetch", function (e) {
  var req = e.request, url = new URL(req.url);
  // fichiers de l'application avec leur empreinte (?v=…) : une mise à jour change l'adresse, donc on peut les garder
  if (req.method === "GET" && url.origin === location.origin && url.pathname.indexOf("/static/") === 0 && url.searchParams.has("v")) {
    e.respondWith(caches.open(CACHE).then(function (c) {
      return c.match(req).then(function (hit) {
        if (hit) return hit;
        return fetch(req).then(function (res) {
          if (res.ok) {
            c.put(req, res.clone());
            // on retire les anciennes versions du même fichier
            c.keys().then(function (keys) {
              keys.forEach(function (k) {
                var u = new URL(k.url);
                if (u.pathname === url.pathname && u.search !== url.search) c.delete(k);
              });
            });
          }
          return res;
        });
      });
    }));
    return;
  }
  if (req.mode !== "navigate") return;
  if (req.method === "GET" && url.origin === location.origin && !url.search && PAGE_PATHS.indexOf(url.pathname) >= 0) {
    e.respondWith(caches.open(PAGES).then(function (c) {
      return c.match(url.pathname).then(function (hit) {
        var net = fetch(req).then(function (res) {
          var html = (res.headers.get("Content-Type") || "").indexOf("text/html") === 0;
          if (res.ok && res.type === "basic" && html) c.put(url.pathname, res.clone());
          else c.delete(url.pathname);  // déconnecté, plus le droit… : la page n'est plus gardée
          return res;
        });
        if (hit) {
          e.waitUntil(net.catch(function () {}));
          return hit;  // tout de suite, sans attendre le réseau
        }
        return net.catch(offline);
      });
    }));
    return;
  }
  e.respondWith(fetch(req).catch(offline));
});
