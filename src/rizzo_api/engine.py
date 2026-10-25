# -*- coding: utf-8 -*-
"""
Adattatore verso il motore ORIGINALE di Rizzo PII (`src/app/app.py`).

Il motore non viene modificato ne' copiato: si importa il modulo `app` e se ne
usa `analyze()` — modello mmBERT + rete regex/checksum + fusione + placeholder —
esattamente come fa l'interfaccia web. Dell'import resta in memoria anche
l'oggetto Flask dell'UI (`app.app`), che pero' NON viene mai servito: il server
WSGI dell'API e' un'altra applicazione (server.py), quindi /preview, /doc/...,
/settings, /config e la cache `_DOCS` dell'UI non sono raggiungibili.

Stato globale del motore, verificato:
  - `app.nlp` (pipeline HF): condiviso, sola lettura dei pesi. Il tokenizer
    "fast" di HF non e' pensato per l'uso concorrente da piu' thread
    ("Already borrowed"), quindi le chiamate al modello passano da un lock;
  - `app.analyze()`: solo variabili locali (entita', contatori, dizionario);
  - `detectors`: regex compilate a livello di modulo, immutabili;
  - `EXCLUDED_TAGS`/`MAPPING_ENABLED`/`_DOCS` dell'UI: non usati dall'API (i tag
    esclusi arrivano sempre espliciti dalla richiesta).

Timeout cooperativo: il pipeline viene avvolto in `_GuardedPipeline`, che
elabora i chunk a lotti e controlla la scadenza della richiesta tra un lotto e
l'altro. Un thread Python non si puo' interrompere dall'esterno: questo e' il
punto piu' fine in cui la richiesta puo' fermarsi senza abbattere il worker.
"""

import os
import sys
import threading
import time
from pathlib import Path

from .errors import Deadline

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BATCH_CHUNKS = 8

_local = threading.local()


def set_deadline(ts):
    """Scadenza (time.monotonic) della richiesta in corso su QUESTO thread."""
    _local.deadline = ts


def check_deadline():
    d = getattr(_local, "deadline", None)
    if d is not None and time.monotonic() > d:
        raise Deadline()


def remaining():
    d = getattr(_local, "deadline", None)
    return None if d is None else max(0.0, d - time.monotonic())


class _GuardedPipeline:
    """Pipeline HF serializzato e interrompibile tra un lotto e l'altro.
    L'uscita e' sempre una lista di liste allineata all'input."""

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.Lock()

    def __call__(self, inputs, **kw):
        single = isinstance(inputs, str)
        items = [inputs] if single else list(inputs)
        out = []
        for i in range(0, len(items), BATCH_CHUNKS):
            check_deadline()
            batch = items[i:i + BATCH_CHUNKS]
            wait = remaining()
            if not self._lock.acquire(timeout=-1 if wait is None else wait):
                raise Deadline()
            try:
                res = self._inner(batch, **kw)
            finally:
                self._lock.release()
            # con un solo input HF puo' rispondere con la lista "piatta" delle entita'
            if len(batch) == 1 and (not res or isinstance(res[0], dict)):
                res = [res]
            out.extend(res)
        return out[0] if single else out


class CoreEngine:
    """Motore reale: `app.analyze` del progetto originale."""

    def __init__(self, core):
        self._core = core
        if not isinstance(core.nlp, _GuardedPipeline):
            core.nlp = _GuardedPipeline(core.nlp)
        self.tag_names = tuple(core.TAG_NAMES)
        self.model_name = os.path.basename(str(core.MODEL_DIR).rstrip("/\\"))

    def analyze(self, text, excluded):
        # mapping_enabled=True e' interno: serve a localizzare i valori nel PDF e
        # a calcolare gli offset; cosa esce nella risposta lo decide il chiamante.
        return self._core.analyze(text, list(excluded), mapping_enabled=True)


def load_core_engine():
    """Importa il motore originale (carica il modello UNA volta per processo)."""
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import app as core  # noqa: E402 - src/app/app.py, invariato
    return CoreEngine(core)
