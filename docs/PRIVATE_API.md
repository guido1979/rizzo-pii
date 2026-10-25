# Rizzo PII Private API

Microservizio REST **privato** di anonimizzazione documentale, costruito sul motore di
Rizzo PII (modello mmBERT + rete regex/checksum). È pensato per essere chiamato **solo
dai backend** di altre applicazioni sulla stessa rete Docker: non ha interfaccia
grafica, non pubblica porte sull'host, non ha un dominio.

> **Hostname degli esempi.** In tutto il documento il servizio è chiamato
> `http://rizzo-pii-api:5005`. È l'alias di rete definito in
> [`docker-compose.coolify.yml`](../docker-compose.coolify.yml): deve corrispondere
> all'**hostname o alias Docker effettivamente disponibile** sulla rete condivisa fra il
> servizio e il tuo container. Il nome si risolve **solo dentro quella rete**: un'applicazione
> ospitata su un altro server, o su questo server ma non collegata alla rete, non lo vede
> (vedi [§10](#10-collegare-le-applicazioni-sulla-rete-docker-di-coolify)).

Indice:
[1. Architettura](#1-architettura) ·
[2. Autenticazione](#2-autenticazione) ·
[3. Endpoint](#3-endpoint) ·
[4. Opzioni](#4-opzioni) ·
[5. Carta intestata](#5-preserve-letterhead-esclusione-della-carta-intestata) ·
[6. Errori](#6-gestione-degli-errori) ·
[7. Limiti e timeout](#7-limiti-dimensioni-e-timeout) ·
[8. Esempi](#8-esempi-di-richieste) ·
[9. Deployment](#9-build-e-deployment-privato-su-coolify) ·
[10. Rete Docker](#10-collegare-le-applicazioni-sulla-rete-docker-di-coolify) ·
[11. Trattamento dei dati](#11-trattamento-dei-dati-e-misure-tecniche) ·
[12. Test](#12-test) ·
[13. Licenze](#13-licenze) ·
[14. Limiti noti](#14-limiti-noti)

---

## 1. Architettura

```
 container client (PHP/Python/Node)            container rizzo-pii-api (read-only, utente 10001)
 ┌───────────────────────────┐   rete Docker   ┌──────────────────────────────────────────────┐
 │ POST /api/v1/anonymize    │   privata       │ gunicorn (gthread) ─ rizzo_api.server         │
 │ Authorization: Bearer ... │ ──────────────▶ │   auth · allowlist rete · rate limit · slot   │
 │ multipart: file + options │                 │   validazione PDF / opzioni                   │
 │                           │ ◀────────────── │   rizzo_api.pdf_pipeline  (tutto in RAM)      │
 │ application/pdf           │                 │     └─ motore ORIGINALE src/app (invariato):  │
 └───────────────────────────┘                 │        app.analyze · pdf_export · pdf_text   │
                                               │   /tmp = tmpfs · nessun volume · no porte    │
                                               └──────────────────────────────────────────────┘
```

- **Motore e API separati.** `src/rizzo_api/` non riconosce PII: importa il motore
  originale di `src/app/` (`app.analyze()`, `pdf_export`, `pdf_text`) senza modificarlo.
  L'applicazione Flask dell'interfaccia web resta in memoria come effetto dell'import ma
  **non viene servita**: `/`, `/preview`, `/doc/...`, `/settings`, `/config`, `/port-check`
  e la cache di anteprima `_DOCS` non sono raggiungibili dal servizio API.
- **Modello caricato una volta per processo** (1 worker gunicorn), condiviso dai thread.
  Le chiamate al modello sono serializzate da un lock: il tokenizer HF non è pensato per
  l'uso concorrente.
- **Stateless:** ogni richiesta lavora su variabili locali; nessuna sessione, nessuna
  cache, nessun file.

## 2. Autenticazione

Ogni endpoint sotto `/api/v1/` richiede:

```
Authorization: Bearer <API_KEY>
```

- Le chiavi sono definite **solo** tramite variabili d'ambiente / secret del servizio:
  `RIZZO_API_KEYS="gestionale:<chiave>,portale:<chiave>"` oppure `RIZZO_API_KEYS_FILE`
  (file con una coppia `client:chiave` per riga, es. un Docker secret).
- Una chiave per **applicazione chiamante**: il nome del client compare nei log (la chiave
  mai), e il rate limit è per client. Revocare un client = togliere la sua riga e riavviare.
- Lunghezza minima 32 caratteri. Generazione consigliata: `openssl rand -hex 32`.
- Senza chiavi configurate il servizio **non parte** (fail-closed).
- Confronto a tempo costante (`hmac.compare_digest` su SHA-256); in memoria restano solo
  i digest.
- Risposta a chiave assente o errata: `401` con `WWW-Authenticate: Bearer`.

Oltre alla chiave, ogni richiesta (anche `/health`) deve arrivare da un indirizzo
compreso in `RIZZO_API_ALLOWED_NETWORKS` (default: reti private RFC1918, loopback, ULA
IPv6), altrimenti `403`. L'indirizzo considerato è quello del peer TCP: `X-Forwarded-For`
è ignorato.

## 3. Endpoint

### `GET /health`

Solo stato operativo. Nessuna autenticazione, nessuna informazione su configurazione,
versioni, modello o documenti.

```http
GET /health HTTP/1.1
Host: rizzo-pii-api:5005

HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok"}
```

### `POST /api/v1/anonymize`

PDF → PDF anonimizzato, restituito direttamente nel corpo della risposta.

| Campo multipart | Obbligatorio | Contenuto |
| --- | --- | --- |
| `file` | sì | il PDF (il controllo è sul contenuto, non sull'estensione o sul Content-Type della parte) |
| `options` | no | JSON con le [opzioni](#4-opzioni) |

Nessun altro campo è ammesso (`400 unknown_field`).

```http
POST /api/v1/anonymize HTTP/1.1
Host: rizzo-pii-api:5005
Authorization: Bearer 3f9c...e1
Content-Type: multipart/form-data; boundary=XyZ

--XyZ
Content-Disposition: form-data; name="file"; filename="documento.pdf"
Content-Type: application/pdf

%PDF-1.7 ...
--XyZ
Content-Disposition: form-data; name="options"

{"preserve_letterhead": true, "letterhead_mode": "automatic", "letterhead_pages": "all"}
--XyZ--
```

Risposta `200`:

| Header | Significato |
| --- | --- |
| `Content-Type: application/pdf` | il documento anonimizzato |
| `Content-Disposition: attachment; filename="documento_anonimizzato.pdf"` | nome fisso: il nome originale può contenere dati personali e non viene riflesso |
| `Cache-Control: no-store`, `Pragma: no-cache`, `X-Content-Type-Options: nosniff` | su **tutte** le risposte |
| `X-PII-Entities` | valori distinti riconosciuti |
| `X-PII-Redactions` | occorrenze redatte |
| `X-PII-Preserved` | occorrenze lasciate in chiaro dalle regole di esclusione |
| `X-PII-Skipped` | valori troppo corti/ambigui per essere cercati nel PDF (restano in chiaro: vedi `strict`) |
| `X-PII-Pages`, `X-PII-Pages-Without-Text` | pagine totali / pagine fatte di sole immagini |
| `X-PII-Letterhead-Regions` | regioni protette applicate |
| `X-Request-ID` | identificativo da citare nelle segnalazioni (compare anche nei log) |

Gli header contengono **solo conteggi**: nessun valore, nessun nome di file.

Il PDF viene restituito solo se la verifica finale non trova valori riconosciuti ancora
leggibili fuori dalle regioni protette; altrimenti `422 anonymization_incomplete`.

### `POST /api/v1/anonymize-text`

```http
POST /api/v1/anonymize-text HTTP/1.1
Host: rizzo-pii-api:5005
Authorization: Bearer 3f9c...e1
Content-Type: application/json

{"text": "La pratica di Giulietta Bramante, C.F. BRMGLT90A41A944X, e' chiusa.",
 "options": {"exclude_tags": ["DATE"]}}
```

```json
{
  "anonymized_text": "La pratica di [FULLNAME_1], C.F. [CF_1], e' chiusa.",
  "entities": [
    {"label": "FULLNAME", "placeholder": "[FULLNAME_1]", "start": 14, "end": 32,
     "source": "modello", "validated": false, "preserved": false},
    {"label": "CF", "placeholder": "[CF_1]", "start": 39, "end": 55,
     "source": "regex", "validated": true, "preserved": false}
  ],
  "stats": {"entities": 2, "unique": 2, "preserved": 0, "by_label": {"FULLNAME": 1, "CF": 1}}
}
```

- `start`/`end` sono offset (caratteri Unicode, fine esclusa) sul testo **inviato**.
- Stesso valore → stesso placeholder (`[FULLNAME_1]` due volte = stesso soggetto).
- I valori originali e il dizionario `mapping` compaiono solo con `return_mapping: true`
  **e** `RIZZO_API_ALLOW_RETURN_MAPPING=true` sul servizio (altrimenti `403`). Il servizio
  non conserva il dizionario: se serve la reversibilità, la custodisce il client.
- Nessun file temporaneo: il testo resta in memoria per la sola durata della richiesta.

### `POST /api/v1/analyze`

Stesse regole di autenticazione e sicurezza; non conserva nulla. Due forme:

**Testo** (`application/json`, stesso body di `anonymize-text`) → `{"type": "text", "entities": [...], "stats": {...}}`
con le entità come sopra (senza testo anonimizzato).

**PDF** (`multipart/form-data`, stessi campi di `anonymize`) → posizioni nella pagina:

```json
{
  "type": "pdf",
  "pages": 2,
  "pages_without_text": [],
  "skipped": 0,
  "entities": [
    {"placeholder": "[FULLNAME_1]", "label": "FULLNAME", "searchable": true,
     "occurrences": [
       {"page": 1, "preserved": false, "preserved_by": null,
        "rects": [{"x": 133.4, "y": 243.1, "width": 98.2, "height": 15.7}]}
     ]}
  ],
  "preserved_regions": [
    {"page": 1, "x": 109.0, "y": 38.25, "width": 153.17, "height": 15.74, "source": "automatic"}
  ],
  "letterhead_detection": [
    {"page": 1, "bands": [{"band": "header", "accepted": true, "lines": 5, "logo": true,
                           "repeated": false, "markers": ["email", "ente", "fiscale", "indirizzo"]}]}
  ]
}
```

Le coordinate sono nel [sistema della §5.2](#52-modalità-b--coordinate). `analyze` serve
anche a **verificare in anticipo** quali regioni verrebbero protette (`preserved_regions`) e
perché (`letterhead_detection`), prima di chiamare `anonymize` con le stesse opzioni.

## 4. Opzioni

| Opzione | Tipo | Default | Note |
| --- | --- | --- | --- |
| `preserve_letterhead` | bool | `false` | attiva le esclusioni della §5. Senza, si anonimizza **tutto** |
| `letterhead_mode` | `"automatic"` \| `"coordinates"` \| `"values"` | `"automatic"` | per il testo solo `"values"` |
| `letterhead_pages` | `"first"` \| `"all"` | `"first"` | pagine a cui si applicano le regole |
| `preserve_regions` | lista di regioni | `[]` | solo con `coordinates` (almeno una) |
| `preserve_values` | lista di stringhe | `[]` | max 50, ciascuna ≥ 3 caratteri alfanumerici e ≤ 200 caratteri |
| `preserve_values_scope` | `"letterhead"` \| `"document"` | PDF `"letterhead"`, testo `"document"` | vedi §5.3 |
| `anonymization_level` | `"standard"` \| `"strict"` | `"standard"` | vedi sotto |
| `return_mapping` | bool | `false` | solo JSON (`analyze`, `anonymize-text`); per `anonymize` → `400` |
| `exclude_tags` | lista di tag | `[]` | tag **rilevati ma lasciati in chiaro** (es. `["DATE"]`); elenco in [TASSONOMIA_TAG.md](TASSONOMIA_TAG.md), più `URL` e `IPADDR` |

Validazione **stretta**: chiavi sconosciute, tipi errati, combinazioni incoerenti (regioni
senza `preserve_letterhead`, regioni con una modalità diversa da `coordinates`…) danno
`400 invalid_options` / `invalid_region`. Un refuso non viene mai ignorato in silenzio.

**`anonymization_level`**
- `standard`: il PDF viene restituito; `X-PII-Skipped` e `X-PII-Pages-Without-Text`
  segnalano cosa potrebbe essere rimasto in chiaro.
- `strict`: se ci sono valori non localizzabili (`skipped > 0`) o pagine senza testo
  estraibile, nessun PDF: `422 anonymization_incomplete`.
- In entrambi i livelli, un valore riconosciuto ancora leggibile nell'output fuori dalle
  regioni protette blocca la risposta (`422`).

## 5. Preserve Letterhead (esclusione della carta intestata)

La protezione viene decisa **prima** della redazione: per ogni occorrenza di un valore
riconosciuto, il servizio stabilisce se cade in un'area protetta e, solo se non è così,
crea l'annotazione di redazione e rimuove il testo dal content stream. Non c'è nessun
"ripristino grafico" successivo: il testo protetto non viene mai toccato, quello non
protetto non viene mai ridisegnato.

Regola comune a tutte le modalità: **un'occorrenza è protetta solo se tutti i suoi
caratteri** (il centro di ogni glifo) **cadono dentro l'area protetta.** Un valore a cavallo
del bordo viene anonimizzato.

### 5.1 Modalità A — `automatic`

Algoritmo, per ogni pagina in ambito, **separatamente** (pagine diverse possono avere
intestazioni diverse, o nessuna):

1. **Bande candidate:** 25% superiore (intestazione) e 15% inferiore (piè di pagina)
   della pagina visualizzata. Sono l'area di ricerca, **non** la regione protetta.
2. **Cluster:** dal bordo verso l'interno si raccolgono righe consecutive. Ci si ferma a:
   uno stacco verticale ampio (> 3 interlinee, min 24 pt); una riga che apre il corpo
   (`Spett.le`, `Egregio`, `Alla c.a.`, `Oggetto:`, `Prot.`, luogo e data, formule di
   saluto, `Il sottoscritto`, `Dott.`/`Avv.`…); una riga con l'aspetto di una frase
   (≥ 10 parole senza indicatori istituzionali, oppure "nato a", "residente",
   "dichiara"…).
3. **Segnali:** tipi di indicatori nel cluster: forma giuridica/tipo di ente, indirizzo
   o CAP, P.IVA/C.F., telefono/fax, email, etichetta PEC, sito web; immagine (logo) nella
   banda; stessa riga ripetuta nella stessa banda su altre pagine (cifre normalizzate).
4. **Accettazione:** ≥ 2 tipi di indicatori, oppure 1 tipo + logo o ripetizione, oppure
   logo + ripetizione. **Dalla seconda pagina in poi** serve in più logo o ripetizione:
   lì la parte alta della pagina è spesso già corpo del documento.
5. **Regioni protette:** i rettangoli delle **sole righe del cluster** (non l'intera banda).
   Cluster non accettato ⇒ nessuna regione: nel dubbio si anonimizza.

Anche dentro una regione automatica restano in chiaro **solo le categorie istituzionali**:
`ORG`, `STREET`, `BUILDINGNUM`, `CITY`, `ZIPCODE`, `PROVINCE`, `PIVA`, `TELEPHONENUM`,
`EMAIL`, `URL`, e `CF` **solo se numerico a 11 cifre** (codice fiscale di un ente).
Restano anonimizzati comunque: nomi di persona (`FULLNAME`, anche "Dirigente: Mario
Rossi" dentro l'intestazione), codici fiscali personali (16 caratteri), IBAN, date,
documenti d'identità, ecc. Un'email resta in chiaro solo se la parte prima della `@` non
contiene nome o cognome di una persona rilevata nel documento.

Il logo e le altre immagini non vengono mai toccati dalla redazione, a meno che un'area
da redigere non vi si sovrapponga.

**Limiti del riconoscimento automatico** (per questo la modalità è prudente):
- euristica su layout e parole chiave italiane: intestazioni in altre lingue o con
  terminologia insolita possono non essere riconosciute (⇒ anonimizzate);
- intestazioni **solo grafiche** (testo dentro il logo, intestazione vettoriale
  convertita in tracciati) non hanno testo da proteggere né da anonimizzare;
- loghi disegnati come grafica vettoriale non contano come "logo";
- un indirizzo/telefono **personale** posto in alto in una pagina con logo e indicatori
  potrebbe essere scambiato per carta intestata (`STREET`/`TELEPHONENUM` sono categorie
  preservabili). Se il rischio non è accettabile, usa `coordinates` o `values`, oppure
  verifica prima con `analyze`;
- impaginazioni a colonne o intestazioni laterali (fuori dalle bande) non sono cercate.

### 5.2 Modalità B — `coordinates`

```json
{
  "preserve_letterhead": true,
  "letterhead_mode": "coordinates",
  "letterhead_pages": "first",
  "preserve_regions": [
    {"page": 1, "x": 0, "y": 0, "width": 595, "height": 120}
  ]
}
```

| Aspetto | Definizione |
| --- | --- |
| **Unità** | punti PDF: 1 pt = 1/72 di pollice (A4 = 595 × 842 pt). `UserUnit` non è considerato |
| **Origine** | angolo **in alto a sinistra** dell'area visibile della pagina (**CropBox**), `x` verso destra, `y` **verso il basso** |
| **Rotazione** | coordinate della pagina **come viene visualizzata**, con `/Rotate` già applicato. Una pagina A4 ruotata di 90° si misura 842 × 595 e la sua "parte alta" è quella che il lettore vede in alto |
| **Pagine** | `page` è **1-based**. Se `page` è assente la regione vale per tutte le pagine di `letterhead_pages` (`first` = solo la 1, `all` = tutte) |
| **Conversione** | interna: `rect_lavoro = rect_client × derotation_matrix + offset_CropBox` (PyMuPDF), verificata da test su pagine ruotate di 0/90/180/270° e con CropBox spostato |
| **Fuori pagina** | regione **parzialmente** fuori: viene ritagliata sui bordi della pagina (il ritaglio effettivo è visibile in `preserved_regions` di `analyze`). Regione **interamente** fuori dalla pagina o `page` oltre l'ultima pagina: `400 invalid_region` |
| **Contenuto** | dentro una regione esplicita resta in chiaro **tutto** il testo, nomi di persona compresi: è una scelta esplicita del client |

Per ricavare le coordinate: `analyze` restituisce i rettangoli delle entità nello stesso
sistema; in alternativa, qualunque viewer che mostri le coordinate in punti con origine in
alto a sinistra.

### 5.3 Modalità C — esclusioni testuali (`preserve_values`)

```json
{
  "preserve_letterhead": true,
  "letterhead_mode": "values",
  "preserve_values": ["Istituto Scolastico Example", "segreteria@example.it"]
}
```

`preserve_values` si può anche **combinare** con `automatic` o `coordinates`.

Politica:
- **Corrispondenza esatta:** stesso testo, maiuscole comprese, parola intera (niente match
  dentro altre parole), spazi multipli/a capo equivalenti a uno spazio.
- **Contenimento:** un'entità resta in chiaro solo se è **interamente contenuta**
  nell'occorrenza del valore protetto (es. `ORG` "Example" dentro "Istituto Scolastico
  Example"). Un'entità più ampia che contiene il valore viene anonimizzata.
- **Valori ripetuti e omonimi — ambito `letterhead` (default per i PDF):** un'occorrenza è
  protetta solo se si trova nella **zona d'intestazione** delle pagine di
  `letterhead_pages`: bande superiore (25%) e inferiore (15%), più le regioni
  `coordinates`/`automatic` eventualmente attive. Lo stesso valore nel corpo del documento
  (un omonimo, o l'ente citato nel testo) viene **anonimizzato**.
- **Ambito `document`:** opt-in esplicito, protegge ogni occorrenza ovunque. È l'unico
  ambito possibile per il testo puro (che non ha layout), e va dichiarato esplicitamente
  (`"preserve_values_scope": "document"`), altrimenti `400`.
- Valori troppo corti (< 3 caratteri alfanumerici) sono rifiutati: proteggerebbero troppo.

### 5.4 `letterhead_pages`

- `first`: regole applicate solo alla pagina 1. Le altre pagine sono anonimizzate per intero.
- `all`: regole applicate a ogni pagina **valutandola separatamente**: in `automatic`
  ogni pagina ha il suo riconoscimento (una pagina senza intestazione, o con
  un'intestazione diversa, viene trattata per quello che è); in `coordinates` una regione
  senza `page` si applica a ogni pagina, ma il testo protetto è solo quello che sta
  davvero nella regione su quella pagina.

## 6. Gestione degli errori

Tutti gli errori hanno lo stesso formato e **nessun dato del documento** nel messaggio:

```json
{"error": {"code": "invalid_pdf", "message": "PDF non valido o danneggiato.",
           "request_id": "0b6f2c1e9e3c4a5f8d0c7e1a2b3c4d5e"}}
```

| Status | `code` | Causa | Cosa fare |
| --- | --- | --- | --- |
| 400 | `invalid_options`, `invalid_region` | opzioni non valide | correggere la richiesta |
| 400 | `missing_file`, `empty_file`, `unknown_field` | multipart incompleto o con campi in più | |
| 400 | `invalid_pdf` | PDF danneggiato o senza pagine | |
| 400 | `invalid_json`, `empty_text` | body JSON non valido / testo vuoto | |
| 401 | `unauthorized` | chiave assente o errata | |
| 403 | `forbidden` | sorgente fuori da `RIZZO_API_ALLOWED_NETWORKS` | |
| 403 | `mapping_not_allowed` | `return_mapping` disabilitato sul servizio | |
| 404/405 | `not_found`, `method_not_allowed` | | |
| 413 | `payload_too_large`, `too_many_pages`, `pdf_too_complex`, `text_too_long` | limiti superati | dividere il documento |
| 415 | `unsupported_media_type` | Content-Type errato o file che non è un PDF | |
| 422 | `encrypted_pdf` | PDF con password di apertura | rimuovere la protezione |
| 422 | `no_text_layer` | scansione/solo immagini: niente testo estraibile | OCR a monte (non fornito dal servizio) |
| 422 | `anonymization_incomplete` | verifica finale fallita, o `strict` non soddisfatto | non usare il documento; segnalare il `request_id` |
| 429 | `rate_limited` | troppe richieste per questo client | rispettare `Retry-After` |
| 500 | `internal_error` | errore inatteso | riprovare; segnalare il `request_id` |
| 503 | `busy` | tutti gli slot occupati oltre `QUEUE_TIMEOUT` | rispettare `Retry-After` |
| 504 | `processing_timeout` | superato `REQUEST_TIMEOUT` | ridurre il documento o alzare il limite |

Un client corretto **non usa mai** il corpo di una risposta non-200 come documento.

## 7. Limiti, dimensioni e timeout

Variabili d'ambiente del servizio:

| Variabile | Default | Effetto |
| --- | --- | --- |
| `RIZZO_API_KEYS` / `RIZZO_API_KEYS_FILE` | — (obbligatoria) | chiavi per client |
| `RIZZO_API_MAX_UPLOAD_MB` | `20` | dimensione massima della richiesta (`413`) |
| `RIZZO_API_MAX_PAGES` | `300` | pagine massime |
| `RIZZO_API_MAX_TEXT_CHARS` | `1000000` | caratteri massimi (testo inviato o estratto dal PDF) |
| `RIZZO_API_MAX_PDF_OBJECTS` | `200000` | oggetti massimi nella xref (difesa da PDF "bomba") |
| `RIZZO_API_REQUEST_TIMEOUT` | `300` | secondi massimi di elaborazione (`504`) |
| `RIZZO_API_MAX_CONCURRENT` | `2` | documenti elaborati in parallelo |
| `RIZZO_API_QUEUE_TIMEOUT` | `15` | attesa massima di uno slot libero (`503`) |
| `RIZZO_API_RATE_LIMIT_PER_MINUTE` | `60` | richieste/minuto per client (`0` = off) |
| `RIZZO_API_ALLOWED_NETWORKS` | reti private | CIDR ammessi; `*` disattiva il filtro (sconsigliato) |
| `RIZZO_API_ALLOW_RETURN_MAPPING` | `false` | permette `return_mapping` |
| `RIZZO_API_LOG_LEVEL` | `INFO` | nessun livello registra contenuti |
| `RIZZO_API_WORKERS` | `1` | worker gunicorn: ognuno è una copia del modello (~1,2 GB) |
| `OMP_NUM_THREADS` | `4` | thread CPU di torch |
| `RIZZO_API_MEMORY_LIMIT`, `RIZZO_API_CPUS` | `6g`, `4` | solo compose: limiti del container |

**Timeout lato client.** Impostare il timeout di lettura **almeno** pari a
`RIZZO_API_REQUEST_TIMEOUT` (+ margine), e un timeout di connessione breve (5 s). Ordini di
grandezza su CPU: pochi secondi per una pagina, decine di secondi/minuti per documenti di
centinaia di pagine.

**Come funziona il timeout.** È cooperativo: la richiesta si interrompe tra un blocco di
inferenza (8 chunk) e l'altro e tra una pagina e l'altra. Un thread Python non si può
uccidere dall'esterno; il `timeout` di gunicorn (`REQUEST_TIMEOUT + 60`) resta come rete di
sicurezza.

**Memoria.** Il picco per richiesta è dell'ordine di qualche volta la dimensione del PDF
(input + documento aperto + output + verifica) più il testo estratto. Con i default
(2 richieste × 20 MB) il limite di 6 GB lascia ampio margine al modello (~1,2 GB).
`RIZZO_API_MAX_PAGES`, `MAX_TEXT_CHARS` e `MAX_PDF_OBJECTS` limitano i documenti piccoli
ma "pesanti". Il limite del container (`mem_limit`) è l'ultima difesa.

## 8. Esempi di richieste

Esempi completi e funzionanti in [`docs/examples/private_api/`](examples/private_api/)
(`client.php`, `client.py`, `client.mjs`). In tutti: URL e chiave da variabili d'ambiente,
mai nel codice.

### curl (da un container sulla rete privata)

```bash
curl -sS --fail-with-body -o anonimizzato.pdf -D - \
  -H "Authorization: Bearer $RIZZO_PII_API_KEY" \
  -F "file=@documento.pdf;type=application/pdf" \
  -F 'options={"preserve_letterhead":true,"letterhead_mode":"automatic","letterhead_pages":"all"}' \
  http://rizzo-pii-api:5005/api/v1/anonymize

curl -sS -H "Authorization: Bearer $RIZZO_PII_API_KEY" -H "Content-Type: application/json" \
  -d '{"text":"La pratica di Giulietta Bramante e chiusa."}' \
  http://rizzo-pii-api:5005/api/v1/anonymize-text
```

### PHP (estensione curl, PHP ≥ 8.1)

```php
$ch = curl_init(getenv('RIZZO_PII_URL') . '/api/v1/anonymize');
curl_setopt_array($ch, [
    // niente CURLOPT_POST: POSTFIELDS implica POST; impostato dopo POSTFIELDS romperebbe il multipart
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_CONNECTTIMEOUT => 5,
    CURLOPT_TIMEOUT => 330,
    CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . getenv('RIZZO_PII_API_KEY')],
    CURLOPT_POSTFIELDS => [
        // CURLStringFile: il PDF passa dalla memoria, nessun file temporaneo nel client
        'file' => new CURLStringFile($pdfBytes, 'documento.pdf', 'application/pdf'),
        'options' => json_encode(['preserve_letterhead' => true, 'letterhead_mode' => 'automatic']),
    ],
]);
$body = curl_exec($ch);
$status = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
if ($status !== 200) {
    $err = json_decode($body, true)['error'];   // code, message, request_id
    throw new RuntimeException("{$status} {$err['code']}: {$err['message']}");
}
// $body è il PDF anonimizzato
```

### Python (`requests`)

```python
import json, os, requests

r = requests.post(
    f"{os.environ['RIZZO_PII_URL']}/api/v1/anonymize",
    headers={"Authorization": f"Bearer {os.environ['RIZZO_PII_API_KEY']}"},
    files={"file": ("documento.pdf", pdf_bytes, "application/pdf")},
    data={"options": json.dumps({"preserve_letterhead": True, "letterhead_mode": "automatic"})},
    timeout=(5, 330),
)
if r.status_code != 200:
    raise RuntimeError(r.json()["error"])
anonimizzato = r.content
```

### Node.js (≥ 18, `fetch` nativo)

```js
const form = new FormData();
form.append("file", new Blob([pdfBuffer], { type: "application/pdf" }), "documento.pdf");
form.append("options", JSON.stringify({ preserve_letterhead: true, letterhead_mode: "automatic" }));

const res = await fetch(`${process.env.RIZZO_PII_URL}/api/v1/anonymize`, {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.RIZZO_PII_API_KEY}` },
  body: form,
  signal: AbortSignal.timeout(330_000),
});
if (!res.ok) throw new Error(JSON.stringify((await res.json()).error));
const anonimizzato = Buffer.from(await res.arrayBuffer());
```

## 9. Build e deployment privato su Coolify

### Build locale e prova

```bash
docker build -f Dockerfile.api -t rizzo-pii-api .     # scarica torch CPU e il modello (~1,2 GB)

docker network create --driver bridge --internal rizzo-pii-private
docker run -d --name rizzo-pii-api --network rizzo-pii-private --network-alias rizzo-pii-api \
  --read-only --tmpfs /tmp:size=512m,mode=1777,noexec,nosuid,nodev \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256 \
  --memory 6g --memory-swap 6g \
  -e RIZZO_API_KEYS="prova:$(openssl rand -hex 32)" rizzo-pii-api
```

Il download del modello avviene **solo in build** (stage `model` del Dockerfile). A
runtime `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` e la rete `--internal` non ha
comunque uscita verso Internet: il servizio parte ed elabora documenti anche con
`--network none` (verificato, vedi §12).

### Coolify

1. **Rete privata** (una volta, sul server): `docker network create --driver bridge --internal rizzo-pii-private`.
   Il nome è configurabile con `RIZZO_PRIVATE_NETWORK`.
2. **Nuova risorsa** → *Docker Compose* dal repository, file `docker-compose.coolify.yml`.
3. **Nessun dominio:** lascia vuoto il campo *Domains* del servizio e non aggiungere
   variabili `SERVICE_FQDN_*`. Senza dominio Coolify non crea regole sul proxy pubblico.
   Il compose non ha `ports`: nessun bind sull'host.
4. **Variabili d'ambiente:** `RIZZO_API_KEYS` come variabile **segreta** (obbligatoria: senza,
   il compose fallisce). Le altre sono facoltative (§7).
5. **Deploy.** Lo stato *healthy* arriva dopo il caricamento del modello (`start_period` 120 s).

Cosa imposta il compose: utente non-root `10001`, **root filesystem in sola lettura**,
`/tmp` su **tmpfs** (unica area scrivibile, in RAM), `cap_drop: ALL`,
`no-new-privileges`, `pids_limit`, limiti di memoria e CPU, **swap disabilitato** per il
container (`memswap_limit = mem_limit`), healthcheck, nessun volume, log ruotati.

> Verifica nella tua versione di Coolify, dopo il primo deploy, che il container non abbia
> porte pubblicate e non sia collegato a reti che non ti aspetti:
> `docker inspect <container> --format '{{json .NetworkSettings.Ports}} {{json .NetworkSettings.Networks}}'`.
> Coolify può collegare i servizi anche a una propria rete di progetto: non espone porte,
> ma rende il servizio raggiungibile dagli altri container di quella rete (resta comunque
> obbligatoria la API key).

## 10. Collegare le applicazioni sulla rete Docker di Coolify

Il servizio è raggiungibile come `http://rizzo-pii-api:5005` **solo** dai container
collegati alla rete `rizzo-pii-private`.

**App client gestita da Coolify con un docker-compose** — aggiungi la rete esterna al
servizio che deve chiamare l'API:

```yaml
services:
  backend:
    # ...
    environment:
      RIZZO_PII_URL: http://rizzo-pii-api:5005
      RIZZO_PII_API_KEY: ${RIZZO_PII_API_KEY}     # segreta, in Coolify
    networks:
      - default
      - rizzo-pii-private
networks:
  rizzo-pii-private:
    external: true
```

**Container già esistente** (es. app Coolify basata su Dockerfile/Nixpacks):
`docker network connect rizzo-pii-private <container_client>`. Attenzione: un collegamento
manuale può non sopravvivere al redeploy del client; la forma dichiarativa (compose o
l'opzione di rete personalizzata della tua versione di Coolify) è preferibile.

**Verifica dal container client:**

```bash
docker exec <container_client> sh -c 'wget -qO- http://rizzo-pii-api:5005/health || curl -s http://rizzo-pii-api:5005/health'
# {"status":"ok"}
```

**Applicazioni su altri server.** Una rete bridge Docker esiste solo sull'host che la ospita:
`rizzo-pii-api` **non è risolvibile né raggiungibile** da un'applicazione su un altro server,
né da una macchina in rete locale, né da Internet. Per un client remoto servirebbe un
collegamento privato tra i server (VPN/WireGuard, rete overlay Swarm…) **e** un riesame
di `RIZZO_API_ALLOWED_NETWORKS`, oppure un'istanza del servizio su quel server. Non
pubblicare la porta per comodità: il servizio è progettato per non essere esposto.

## 11. Trattamento dei dati e misure tecniche

Il servizio **non conserva** documenti originali, documenti anonimizzati o dizionari.
Misure implementate:

| Rischio | Misura |
| --- | --- |
| Upload su disco | multipart letto in `BytesIO` (`_InMemoryRequest`): Werkzeug di default spoolerebbe su file ogni upload > 500 KB. Limite `MAX_CONTENT_LENGTH`; campi form ≤ 64 KB |
| File temporanei | nessuno creato dal codice; PyMuPDF apre e salva da/verso memoria (`stream=`, `tobytes()`); root filesystem **read-only** e `/tmp` su **tmpfs**: una scrittura imprevista finirebbe in RAM o fallirebbe |
| Cache / sessioni | nessuna sessione, nessun cookie, nessuna cache documentale; la cache di anteprima dell'UI (`_DOCS`) non è raggiungibile |
| Dizionari | costruiti in memoria per localizzare i valori e scartati a fine richiesta; restituiti al client solo su richiesta esplicita e se abilitato |
| Log | solo `request_id`, client, metodo, path, status, durata, conteggi. Mai body, nomi file, testo, entità, chiavi o messaggi di eccezione (solo classe e riga di codice). Access log di gunicorn disattivato |
| Risorse | documenti PyMuPDF chiusi in `finally`, buffer di upload chiusi, `gc.collect()` a fine richiesta, anche su errore/timeout |
| Swap | `memswap_limit = mem_limit` nel compose |
| Chiamate esterne / telemetria | modello scaricato solo in build; a runtime `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`; rete `--internal` senza uscita. **PyMuPDF fissato a 1.28.2**: la 2.0 importa `onnxruntime` (per `pymupdf.layout`), che all'avvio scrive un device id e un database di eventi di telemetria; il build fallisce se `onnxruntime` viene caricato |
| Isolamento | nessuno stato condiviso tra richieste oltre al modello (sola lettura, accesso serializzato) e alle regex compilate |

**Cosa viene rimosso dal PDF in uscita:** testo PII dal content stream (redazione vera,
`apply_redactions`) anche **fuori dal CropBox** (area non visibile ma presente nel file);
pixel delle immagini sotto le aree redatte; valori nelle annotazioni, nei campi modulo e nei
segnalibri; allegati e file incorporati; JavaScript; **tutti i link** (anche `mailto:`);
miniature; metadati e XMP; `PieceInfo`; **revisioni incrementali** e oggetti non più
referenziati (il file viene riscritto da zero con garbage collection). Una verifica finale
rilegge l'output e blocca la risposta se un valore riconosciuto è ancora leggibile fuori
dalle regioni protette.

**Limiti che è corretto conoscere:**
- **Cancellazione dalla RAM non garantita.** Python non azzera la memoria liberata, le
  stringhe sono immutabili e possono esistere copie interne (librerie C, buffer del kernel).
  I dati restano in memoria finché le pagine non vengono riutilizzate o il processo termina.
  Il servizio riduce il tempo di permanenza, non può promettere una cancellazione fisica.
- **Swap dell'host:** `memswap_limit` evita lo swap del container se il kernel supporta
  l'accounting dello swap nei cgroup; per una garanzia completa disabilitare o cifrare lo
  swap dell'host.
- **Core dump** del processo, snapshot della VM o del host, ibernazione: fuori dal
  controllo del servizio.
- **Riconoscimento:** il modello può non riconoscere un'entità; ciò che non viene
  riconosciuto non viene anonimizzato. La verifica finale controlla i valori *riconosciuti*,
  non quelli mancati. Rivedere i documenti critici.
- **Testo dentro immagini** (scansioni, firme, timbri): non anonimizzato. PDF senza testo →
  `422`; pagine-immagine in un PDF misto → conteggiate in `X-PII-Pages-Without-Text`
  (`strict` le rifiuta). Nessun OCR: il progetto originale non lo prevede.

**GDPR.** Questo documento descrive misure tecniche; **non** dichiara il servizio conforme
al GDPR. La conformità dipende dal trattamento complessivo (basi giuridiche, valutazione
d'impatto, contratti, misure organizzative, gestione dei sistemi che chiamano il servizio).

## 12. Test

```bash
python -m unittest discover -v tests                   # tutta la suite
python -m unittest discover -v -s tests -p "test_api_*.py"
```

I test della Private API usano documenti **sintetici** generati al volo (nomi inventati,
codici fiscali e partite IVA con checksum calcolato, domini `.example.it`) e un motore
finto con la stessa interfaccia di `app.analyze()` più la vera rete regex/checksum. Servono
`flask` e `pymupdf` (senza, si saltano); `tests/test_api_settings.py` gira con la sola
libreria standard.

| File | Copertura |
| --- | --- |
| `test_api_private.py` | anonimizzazione PDF/testo, testo estraibile, metadati/link/allegati/annotazioni/TOC, revisioni incrementali, testo fuori dal CropBox, multipagina, layout, rotazione, PDF non validi/cifrati/scansionati/troppo grandi, auth, allowlist, endpoint UI non esposti, CORS, rate limit, validazione, errori interni senza dati, timeout |
| `test_api_letterhead.py` | modalità automatica (preservazione, nomi e CF personali nell'intestazione, pagine senza intestazione, `first`/`all`, intestazioni diverse), coordinate (dentro/fuori, bordi, regioni senza pagina, fuori pagina, rotazione, CropBox), valori (zona, ambito, parola intera, combinazioni) |
| `test_api_stateless.py` | nessuna scrittura su file (richieste riuscite, fallite, interrotte; con **controllo negativo** sullo spool di Werkzeug), risorse rilasciate, log senza contenuti, concorrenza senza contaminazione, `503` |
| `test_api_settings.py` | configurazione fail-closed, chiavi, reti, rate limiter |
| `test_api_e2e.py` | contro il container vero (modello reale), da un container client sulla rete privata |

**Verifica del container** (dopo ogni build): `scripts/verify_private_api.sh [immagine]`.
Avvia l'immagine con root filesystem read-only, tmpfs, `cap_drop ALL` e **senza rete**
(nessun download a runtime), poi su una rete `--internal` esegue `test_api_e2e.py` da un
container client per hostname; infine controlla che `/tmp` non contenga file, che
`docker diff` sia vuoto, che i log non contengano i dati sintetici né la chiave e che non ci
siano porte pubblicate.

## 13. Licenze

- Il codice di questo repository è **MIT** ([LICENSE](../LICENSE)); i riferimenti agli autori
  originali sono conservati.
- L'immagine `Dockerfile.api` contiene **PyMuPDF**, licenza **AGPL-3.0** (o commerciale
  Artifex). Vedi [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md). In sintesi, non come
  consulenza legale:
  - l'AGPL riguarda il software, non i documenti elaborati;
  - **§13 (uso via rete):** chi interagisce in remoto con una versione **modificata** del
    programma deve poter ottenere il sorgente corrispondente. Questo fork modifica il
    progetto (aggiunge `src/rizzo_api/`): se il servizio è usato da soggetti diversi da chi
    lo gestisce (altre organizzazioni, clienti), va reso disponibile il sorgente del fork,
    modifiche comprese. Se lo usano solo sistemi interni della stessa organizzazione la
    questione è più limitata, ma va valutata con il proprio consulente;
  - per un uso chiuso: sostituire PyMuPDF o acquistare la licenza commerciale Artifex.
- Altre dipendenze aggiunte dall'immagine API: Flask, Werkzeug (BSD-3-Clause), gunicorn (MIT);
  torch, transformers (Apache-2.0/BSD) come nell'immagine originale.
- I pesi del modello (`rizzoaiacademy/rizzo-pii-0.3B`) hanno la licenza indicata nella loro
  model card su Hugging Face: verificarla prima della distribuzione dell'immagine.

## 14. Limiti noti

- Riconoscimento automatico della carta intestata euristico (§5.1): prudente per
  costruzione, ma non infallibile. Per documenti standardizzati preferire `coordinates`.
- Nessun OCR: scansioni rifiutate (`422`), pagine-immagine segnalate.
- Valori "rumorosi" (< 2 caratteri alfanumerici, o 2 sole cifre) non vengono cercati nel PDF
  (`X-PII-Skipped`): restano in chiaro in `standard`, bloccano in `strict`.
- I campi modulo con un valore PII vengono ripuliti, ma i link vengono **tutti** rimossi e
  JavaScript eliminato: un modulo interattivo può perdere comportamento (non contenuto
  visibile).
- Testo **fuori dal MediaBox** (oltre il foglio, non solo oltre l'area visibile) non è
  estratto da PyMuPDF e quindi né analizzato né verificato. Caso raro, ma possibile in PDF
  costruiti ad arte.
- Rate limit e semaforo di concorrenza sono **per processo**: con `RIZZO_API_WORKERS > 1` i
  limiti si moltiplicano per il numero di worker (e così la memoria).
- Il timeout è cooperativo (§7).
