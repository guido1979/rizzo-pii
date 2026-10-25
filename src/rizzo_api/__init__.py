# -*- coding: utf-8 -*-
"""
Rizzo PII Private API — microservizio REST privato sopra il motore Rizzo PII.

Livello API SEPARATO dal motore: qui non c'e' nessuna logica di riconoscimento
delle PII. Il motore resta quello di `src/app/` (modello mmBERT + rete
regex/checksum di `app.analyze()`, redazione di `pdf_export`), importato senza
modifiche da `engine.py`. Questo pacchetto aggiunge soltanto:

  - autenticazione con API key (Bearer), allowlist di rete, rate limit;
  - validazione di richieste, opzioni e PDF;
  - elaborazione STATELESS in memoria (niente upload su disco, niente cache);
  - protezione della carta intestata applicata PRIMA della redazione;
  - pulizia del PDF in uscita (metadati, link, JS, miniature, allegati,
    revisioni incrementali, testo fuori dal CropBox).

Contratto per i client: docs/PRIVATE_API.md.
"""

API_VERSION = "1.0.0"
