/* ADER LINK — service worker : rend l'application installable. Toujours en ligne (aucune donnée gardée en cache). */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", function (e) {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(function () {
    return new Response("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>ADER LINK</title>" +
      "<body style='font-family:system-ui;padding:40px 20px;text-align:center;color:#0a3f8a'><h1>ADER LINK</h1>" +
      "<p>Pas de connexion Internet. Réessayez dans un instant.</p></body>", { headers: { "Content-Type": "text/html; charset=utf-8" } });
  }));
});
