# AgriMigra Watch

App web (installabile come icona su telefono, Android e iOS) che monitora
bandi di finanziamento e reti di collaborazione su **migrazione e lavoro
in agricoltura**, per un progetto di ricerca legato a Eurac Research.

Completamente separata da Claude: gira su servizi gratuiti —

- **GitHub Pages** ospita l'app (cartella `docs/`).
- **GitHub Actions** esegue ogni 6 ore (gratis) uno scraper Python che
  cerca bandi/opportunità in base a parole chiave e aggiorna i dati.
- **Firebase Firestore** (piano gratuito) fa da database condiviso: ci
  scrive lo scraper, e da lì l'app legge e mostra i risultati. È anche
  dove l'app salva le impostazioni (parole chiave, frequenza) quando le
  modifichi dal telefono.

Nessun costo, nessuna chiave API di modelli linguistici: la ricerca è per
parola chiave e rilevamento di cambiamenti di pagina, non un giudizio
"intelligente" — vedi i dettagli in `scripts/scan.py`.

## Per iniziare

Segui **SETUP.md** passo passo: richiede circa 20-30 minuti la prima
volta (creare un progetto Firebase gratuito, un repository GitHub, e
collegare i due).

## Struttura del progetto

```
docs/                  L'app web (HTML/CSS/JS), pubblicata via GitHub Pages
  index.html           Pagina principale
  firebase-config.js   Configurazione del TUO progetto Firebase (da compilare)
  manifest.json        Rende l'app installabile come icona sul telefono
  service-worker.js    Cache minima per l'installazione
scripts/
  scan.py              Lo scraper che gira su GitHub Actions
  requirements.txt     Dipendenze Python dello scraper
.github/workflows/
  scan.yml             Pianificazione dello scraper (ogni 6 ore + avvio manuale)
firestore.rules         Regole di sicurezza del database (chi può leggere/scrivere cosa)
```
