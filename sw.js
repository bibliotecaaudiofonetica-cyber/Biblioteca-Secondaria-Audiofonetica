// Service worker minimale: serve solo a rendere l'app installabile
// (icona in home, apertura a schermo intero) e a velocizzare i
// caricamenti successivi della pagina statica. NON mette in cache le
// chiamate API (dati libri/prestiti/alunni): quelle vanno sempre e solo
// in rete, altrimenti si rischierebbe di mostrare dati vecchi/sbagliati.

const CACHE_NAME = 'biblioteca-shell-v1';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .catch(() => {}) // non bloccare l'installazione se qualche asset non si carica
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Solo richieste GET dello stesso dominio (l'app statica): il backend
  // API vive su un altro dominio (Render) e non va mai intercettato qui.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached); // offline: usa la copia in cache se c'è
      return cached || network;
    })
  );
});
