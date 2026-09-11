#!/usr/bin/env python3
"""
Scraper periodico per AgriMigra Watch.

Gira su GitHub Actions secondo lo schedule in .github/workflows/scan.yml
(di default ogni 6 ore). Ad ogni esecuzione:

  1. Legge da Firestore le impostazioni (config/main: keywords, frequency)
     e l'ultima esecuzione (meta/status: lastRun).
  2. Decide se è davvero il momento di fare una scansione completa,
     confrontando il tempo trascorso con la frequenza scelta dall'utente
     nell'app (weekly / biweekly / monthly). Questo permette di far girare
     il workflow spesso (per reagire in fretta a un cambio di impostazioni)
     senza sprecare tempo a fare scraping ad ogni esecuzione.
  3. Se è il momento: prova a interrogare l'API pubblica del portale
     Funding & Tenders (Horizon Europe) e controlla un elenco di pagine
     istituzionali note (COST, MUR/PRIN, Alto Adige, ecc.) cercando le
     parole chiave configurate o un cambiamento di contenuto.
  4. Scrive/aggiorna i risultati nella collection "calls" di Firestore,
     e aggiorna meta/status.

Non usa nessun modello linguistico: è ricerca per parola chiave e
rilevamento di cambiamenti di pagina, non un giudizio "intelligente" di
rilevanza. Le voci di tipo "watch" vanno sempre verificate a mano.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("Manca firebase-admin. Esegui: pip install -r scripts/requirements.txt")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


FREQ_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30}
DEFAULT_FREQUENCY = "biweekly"
HTTP_HEADERS = {
    "User-Agent": "AgriMigraWatch/1.0 (+strumento privato di monitoraggio bandi; uso non commerciale)"
}
HTTP_TIMEOUT = 20

# Pagine istituzionali da controllare quando non esiste un'API pubblica.
# Per ognuna: un id stabile, la fonte "leggibile", l'URL e la categoria.
WATCH_PAGES = [
    {
        "id": "cost-open-call",
        "funder": "COST Association",
        "url": "https://www.cost.eu/funding/open-call-a-simple-one-step-application-process/",
        "category": "network",
        "title": "COST Open Call — proposta di nuova COST Action",
    },
    {
        "id": "mur-prin",
        "funder": "MUR — Ministero dell'Università e della Ricerca",
        "url": "https://www.mur.gov.it/it/atti-e-normativa",
        "category": "funding",
        "title": "Bandi/decreti MUR (inclusi cicli PRIN)",
    },
    {
        "id": "alto-adige-ricerca",
        "funder": "Provincia Autonoma di Bolzano/Alto Adige",
        "url": "https://innovazione-ricerca.provincia.bz.it/it/agevolazioni-bandi",
        "category": "funding",
        "title": "Bandi Ricerca e Innovazione — Provincia di Bolzano",
    },
    {
        "id": "imiscoe-news",
        "funder": "IMISCOE",
        "url": "https://www.imiscoe.org/news-and-blog",
        "category": "network",
        "title": "Rete IMISCOE — news, call for papers e conferenze",
    },
]

# Dati iniziali (raccolti manualmente l'11/09/2026) inseriti una sola volta,
# così l'app non parte vuota mentre lo scraper automatico matura.
SEED_CALLS = [
    {
        "id": "horizon-cl2-2026-01-transfo-08",
        "title": "Support all'attuazione del Patto UE su Migrazione e Asilo / equità sanitaria e inclusione sociale per migranti e rifugiati",
        "funder": "Commissione Europea — Horizon Europe, Cluster 2 (HORIZON-CL2-2026-01-TRANSFO-08)",
        "amount": "€3-4 mln a progetto (bando totale €12 mln)",
        "category": "funding",
        "status": "closed",
        "deadlineDate": "2026-09-23",
        "tags": ["UE", "Horizon Europe", "migrazione", "asilo", "salute"],
        "summary": "Finanzia progetti che sostengono l'attuazione del Patto UE su Migrazione e Asilo o migliorano equità sanitaria e inclusione sociale di migranti e rifugiati. Utile soprattutto per capire il prossimo ciclo Cluster 2.",
        "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals?callIdentifier=HORIZON-CL2-2026-01",
    },
    {
        "id": "cost-open-call-2026-01",
        "title": "COST Open Call 2026 — proposta di nuova COST Action",
        "funder": "COST Association",
        "amount": "fino a ~€690.000 per rete su 4 anni",
        "category": "network",
        "status": "open",
        "deadlineDate": "2026-10-28",
        "tags": ["rete", "bottom-up", "UE", "COST Action"],
        "summary": "Meccanismo bottom-up per proporre una nuova rete di ricerca europea su qualsiasi tema, incluse le scienze sociali. Occasione per costruire una rete su lavoro migrante e agricoltura con partner europei.",
        "url": "https://www.cost.eu/funding/open-call-a-simple-one-step-application-process/",
    },
    {
        "id": "prin-2026",
        "title": "PRIN 2026 — Progetti di Ricerca di Rilevante Interesse Nazionale",
        "funder": "MUR — Ministero dell'Università e della Ricerca",
        "amount": "€260 mln complessivi, progetti triennali",
        "category": "funding",
        "status": "closed",
        "deadlineDate": "2026-06-01",
        "tags": ["Italia", "PRIN", "chiuso"],
        "summary": "Bando nazionale italiano già chiuso (domande dal 17 aprile al 1° giugno 2026). Utile per monitorare l'apertura del prossimo ciclo, atteso indicativamente nel 2027.",
        "url": "https://www.mur.gov.it/it/atti-e-normativa/decreto-direttoriale-n-2298-del-10-04-2026",
    },
    {
        "id": "alto-adige-ricerca-innovazione",
        "title": "Bandi Ricerca e Innovazione della Provincia Autonoma di Bolzano",
        "funder": "Ripartizione Innovazione, Ricerca e Università — Provincia di Bolzano/Alto Adige",
        "category": "funding",
        "status": "rolling",
        "deadlineText": "scadenze multiple e variabili — verificare portale",
        "tags": ["Alto Adige", "Eurac", "partnership UE", "agroecologia"],
        "summary": "Diversi strumenti provinciali (Research Südtirol, mobilità ricercatori, partenariati UE come AGROECOLOGY e FutureFoodS) potrebbero rilevare per un progetto su migrazione e lavoro agricolo radicato sul territorio.",
        "url": "https://innovazione-ricerca.provincia.bz.it/it/agevolazioni-bandi",
    },
    {
        "id": "dach-lead-agency",
        "title": "Procedura D-A-CH (Germania-Austria-Svizzera) tra DFG, FWF e SNF",
        "funder": "DFG (Germania) / FWF (Austria) / SNF (Svizzera)",
        "category": "funding",
        "status": "rolling",
        "deadlineText": "nessuna scadenza fissa — presentazione continua",
        "tags": ["DACH", "Germania", "Austria", "Svizzera", "meccanismo permanente"],
        "summary": "Meccanismo di co-finanziamento trilaterale per progetti con partner in Germania, Austria e Svizzera — area con forte tradizione di studi su lavoro migrante.",
        "url": "https://www.dfg.de/de/foerderung/foerdermoeglichkeiten/programme/inter-foerdermassnahmen/antragstellung-oesterreich-schweiz",
    },
    {
        "id": "imiscoe-network",
        "title": "Rete IMISCOE — conferenze, forum e gruppi di lavoro su migrazione",
        "funder": "IMISCOE (rete europea di istituti di ricerca sulla migrazione)",
        "category": "network",
        "status": "rolling",
        "deadlineText": "call periodiche — prossima da verificare",
        "tags": ["rete", "conferenze", "migration studies"],
        "summary": "La principale rete europea di ricerca sulla migrazione organizza conferenze e workshop con call for papers ricorrenti — buona vetrina per trovare partner.",
        "url": "https://www.imiscoe.org/",
    },
]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_firestore():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not raw:
        print("ERRORE: variabile d'ambiente FIREBASE_SERVICE_ACCOUNT mancante.")
        sys.exit(1)
    cred_dict = json.loads(raw)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def get_doc(db, collection, doc_id, default=None):
    snap = db.collection(collection).document(doc_id).get()
    return snap.to_dict() if snap.exists else (default or {})


def should_run(config, meta):
    frequency = config.get("frequency", DEFAULT_FREQUENCY)
    threshold_days = FREQ_DAYS.get(frequency, FREQ_DAYS[DEFAULT_FREQUENCY])
    last_run = meta.get("lastRun")
    if not last_run:
        return True, "prima esecuzione"
    try:
        last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
    except ValueError:
        return True, "lastRun illeggibile"
    elapsed_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
    if elapsed_days >= threshold_days - 1:
        return True, "soglia raggiunta ({:.1f}/{} giorni)".format(elapsed_days, threshold_days)
    return False, "non ancora ({:.1f}/{} giorni)".format(elapsed_days, threshold_days)


def seed_if_empty(db):
    existing = list(db.collection("calls").limit(1).stream())
    if existing:
        return False
    batch = db.batch()
    for item in SEED_CALLS:
        doc_id = item["id"]
        data = {k: v for k, v in item.items() if k != "id"}
        data["foundAt"] = now_iso()
        data["source"] = "seed-manuale"
        batch.set(db.collection("calls").document(doc_id), data)
    batch.commit()
    print("Seed iniziale inserito ({} bandi).".format(len(SEED_CALLS)))
    return True


def slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:80] or "voce"


def search_funding_tenders_portal(keywords):
    """Interroga l'API pubblica (non ufficialmente documentata) del portale
    EU Funding & Tenders per Horizon Europe. Se l'endpoint cambia o non
    risponde come atteso, la funzione fallisce in modo silenzioso: non deve
    mai bloccare il resto dello scan."""
    results = []
    url = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    text_query = " ".join(keywords[:6]) if keywords else "migration agriculture"
    params = {"apiKey": "SEDIA", "text": '"{}"'.format(text_query), "pageSize": 15, "pageNumber": 1}
    body = {
        "query": {
            "bool": {
                "must": [
                    {"terms": {"type": ["1"]}},  # 1 = call for proposals
                    {"terms": {"status": ["31094502", "31094501"]}},  # forthcoming, open
                ]
            }
        }
    }
    try:
        resp = requests.post(url, params=params, json=body, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        hits = (data.get("results") or data.get("hits") or [])
        for hit in hits:
            fields = hit.get("metadata", hit)
            title = _first(fields, ["title", "callTitle"]) or "Bando Horizon Europe"
            identifier = _first(fields, ["identifier", "callIdentifier", "reference"])
            deadline = _first(fields, ["deadlineDate", "deadline"])
            item_id = "horizon-" + slugify(identifier or title)
            results.append({
                "id": item_id,
                "title": title if not identifier else "{} ({})".format(title, identifier),
                "funder": "Commissione Europea — Horizon Europe / Funding & Tenders Portal",
                "category": "funding",
                "status": "open",
                "deadlineDate": _parse_date(deadline),
                "tags": ["UE", "Horizon Europe"],
                "summary": "Trovato tramite ricerca automatica per parola chiave sul portale Funding & Tenders. Verificare rilevanza e requisiti sulla pagina ufficiale.",
                "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals?callIdentifier=" + (identifier or ""),
                "source": "funding-tenders-api",
            })
    except Exception as exc:  # noqa: BLE001 — vogliamo continuare comunque
        print("Avviso: ricerca su Funding & Tenders Portal non riuscita ({}). Salto questa fonte.".format(exc))
    return results


def _first(d, keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def _parse_date(value):
    if not value:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else None


def check_watch_pages(keywords, previous_hashes):
    """Per ogni pagina istituzionale nota: scarica il testo, controlla se
    contiene una delle parole chiave e se il contenuto è cambiato rispetto
    all'ultima esecuzione. Non "capisce" il contenuto: segnala solo dove
    guardare a mano."""
    results = []
    new_hashes = dict(previous_hashes)
    for page in WATCH_PAGES:
        try:
            resp = requests.get(page["url"], headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            if BeautifulSoup is not None:
                text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
            text_lower = text.lower()
            content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
            changed = previous_hashes.get(page["id"]) not in (None, content_hash)
            new_hashes[page["id"]] = content_hash

            matched_keywords = [kw for kw in keywords if kw and kw.lower() in text_lower]

            if not matched_keywords and not changed:
                continue  # niente di nuovo da segnalare per questa pagina

            note_parts = []
            if matched_keywords:
                note_parts.append("parole chiave trovate: " + ", ".join(matched_keywords[:5]))
            if changed:
                note_parts.append("contenuto della pagina cambiato dall'ultimo controllo")
            summary = "Da verificare manualmente — " + "; ".join(note_parts) + "."

            results.append({
                "id": "watch-" + page["id"],
                "title": page["title"],
                "funder": page["funder"],
                "category": page["category"],
                "status": "watch",
                "deadlineText": "vedi pagina ufficiale",
                "tags": ["da verificare"] + (["aggiornata"] if changed else []),
                "summary": summary,
                "url": page["url"],
                "source": "page-watcher",
            })
        except Exception as exc:  # noqa: BLE001
            print("Avviso: impossibile controllare {} ({}). Salto.".format(page["url"], exc))
    return results, new_hashes


def upsert_calls(db, items):
    if not items:
        return 0
    batch = db.batch()
    count = 0
    for item in items:
        doc_id = item.pop("id")
        item["foundAt"] = now_iso()
        batch.set(db.collection("calls").document(doc_id), item, merge=True)
        count += 1
        if count % 400 == 0:  # limite batch Firestore
            batch.commit()
            batch = db.batch()
    batch.commit()
    return count


def close_expired_calls(db):
    today = datetime.now(timezone.utc).date().isoformat()
    docs = db.collection("calls").where("status", "in", ["open", "closing"]).stream()
    n = 0
    for doc in docs:
        data = doc.to_dict()
        deadline = data.get("deadlineDate")
        if deadline and deadline < today:
            doc.reference.update({"status": "closed"})
            n += 1
    return n


def main():
    db = init_firestore()
    config = get_doc(db, "config", "main", default={"keywords": [], "frequency": DEFAULT_FREQUENCY})
    meta = get_doc(db, "meta", "status", default={})

    seeded = seed_if_empty(db)

    run_due, reason = should_run(config, meta)
    print("Verifica frequenza: {}".format(reason))
    if not run_due and not seeded:
        print("Non è ancora il momento di eseguire la scansione. Fine.")
        return

    keywords = config.get("keywords") or ["migrazione", "lavoro agricolo", "migrant labour agriculture"]
    print("Scansione in corso con parole chiave: {}".format(keywords))

    previous_hashes = meta.get("pageHashes", {})

    sources_checked = []
    all_new_items = []

    horizon_items = search_funding_tenders_portal(keywords)
    sources_checked.append("Horizon Europe / Funding & Tenders Portal ({} risultati)".format(len(horizon_items)))
    all_new_items.extend(horizon_items)

    watch_items, new_hashes = check_watch_pages(keywords, previous_hashes)
    sources_checked.append("Pagine istituzionali monitorate: {} segnalazioni su {}".format(len(watch_items), len(WATCH_PAGES)))
    all_new_items.extend(watch_items)

    closed_count = close_expired_calls(db)
    written = upsert_calls(db, all_new_items)

    db.collection("meta").document("status").set({
        "lastRun": now_iso(),
        "sourcesChecked": sources_checked,
        "pageHashes": new_hashes,
        "notes": "{} voci scritte/aggiornate, {} bandi contrassegnati come scaduti.".format(written, closed_count),
    }, merge=True)

    print("Fatto: {} voci aggiornate, {} bandi chiusi automaticamente.".format(written, closed_count))


if __name__ == "__main__":
    main()
