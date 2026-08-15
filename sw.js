// Service worker minimale: serve solo a rendere l'app installabile
// (icona in home, apertura a schermo intero) e a garantire un minimo
// di funzionamento offline per la sola shell statica. NON mette in
// cache le chiamate API (dati libri/prestiti/alunni): quelle vanno
// sempre e solo in rete, altrimenti si rischierebbe di mostrare dati
// vecchi/sbagliati.
//
// Strategia "network-first": si prova sempre a scaricare la versione
// più recente dalla rete; solo se la rete non risponde (offline) si
// usa la copia salvata in cache. Così, appena pubblichi un nuovo
// index.html sul server, gli utenti lo vedono al primo caricamento
// successivo, senza restare bloccati su una versione vecchia in cache.
//
// IMPORTANTE: cambia CACHE_NAME (es. v2 -> v3) ogni volta che vuoi
// forzare l'invalidazione totale delle cache vecchie sui dispositivi
// degli utenti (utile la prima volta dopo aver introdotto questo fix,
// per ripulire le cache "v1" bloccate sulla strategia precedente).
const CACHE_NAME = 'biblioteca-shell-v2';
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
    fetch(event.request)
      .then((response) => {
        // Rete disponibile: usa sempre la risposta fresca e aggiorna
        // la cache per il fallback offline.
        if (response && response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() =>
        // Offline: usa la copia in cache se esiste.
        caches.match(event.request)
      )
  );
});
