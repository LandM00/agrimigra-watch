// Service worker minimo: cache dell'app shell per l'installazione come PWA
// e per un caricamento rapido. I dati (bandi) arrivano sempre live da Firestore,
// non vengono mai serviti dalla cache.
const CACHE_NAME = "grantscout-shell-v3";
const SHELL_FILES = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Non intercettare mai chiamate a Firestore/Google APIs: devono sempre andare in rete.
  if (url.hostname.includes("googleapis.com") || url.hostname.includes("firebaseio.com")) {
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});