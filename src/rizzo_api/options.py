# -*- coding: utf-8 -*-
"""
Validazione delle opzioni di anonimizzazione (campo `options` del multipart,
oppure oggetto `options` del JSON).

Validazione STRETTA: una chiave sconosciuta e' un 400, non viene ignorata. Un
refuso come "preserve_letterhed" ignorato in silenzio produrrebbe un documento
diverso da quello che il client crede di aver chiesto; nel verso opposto
("exclude_tag" ignorato) lascerebbe in chiaro dati che il client voleva
anonimizzati, o viceversa.

Default prudenti: senza opzioni si anonimizza TUTTO, nessuna regione protetta,
nessun dizionario nella risposta.
"""

import json
import math
from dataclasses import dataclass, field

from .errors import bad_request

LETTERHEAD_MODES = ("automatic", "coordinates", "values")
LETTERHEAD_PAGES = ("first", "all")
LEVELS = ("standard", "strict")
VALUE_SCOPES = ("letterhead", "document")

MAX_REGIONS = 50
MAX_VALUES = 50
MAX_VALUE_LEN = 200
MIN_VALUE_ALNUM = 3          # "Rossi" si', "SA" no: un valore corto protegge troppo
MAX_COORD = 14_400           # 200 pollici: oltre il limite di una pagina PDF (UserUnit a parte)
MAX_OPTIONS_BYTES = 64 * 1024

_KNOWN = {
    "preserve_letterhead", "letterhead_mode", "letterhead_pages", "preserve_regions",
    "preserve_values", "preserve_values_scope", "anonymization_level",
    "return_mapping", "exclude_tags",
}


@dataclass(frozen=True)
class Region:
    """Regione protetta in coordinate PDF della pagina VISUALIZZATA (vedi
    docs/PRIVATE_API.md): punti tipografici (1/72"), origine in alto a sinistra
    del CropBox, y verso il basso, rotazione /Rotate gia' applicata.
    page = numero 1-based, oppure None = tutte le pagine di `letterhead_pages`."""
    page: object
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Options:
    preserve_letterhead: bool = False
    letterhead_mode: str = "automatic"
    letterhead_pages: str = "first"
    preserve_regions: tuple = ()
    preserve_values: tuple = ()
    preserve_values_scope: str = "letterhead"
    anonymization_level: str = "standard"
    return_mapping: bool = False
    exclude_tags: tuple = field(default=())

    @property
    def strict(self):
        return self.anonymization_level == "strict"

    @property
    def any_preservation(self):
        return self.preserve_letterhead


def _bool(d, key, default):
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise bad_request("invalid_options", f"'{key}' deve essere true o false.")
    return v


def _enum(d, key, allowed, default):
    v = d.get(key, default)
    if v not in allowed:
        raise bad_request("invalid_options",
                          f"'{key}' deve essere uno di: {', '.join(allowed)}.")
    return v


def _number(v, key):
    # bool e' un int in Python: true/false come coordinata e' un errore del client
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise bad_request("invalid_region", f"'{key}' deve essere un numero finito.")
    if abs(v) > MAX_COORD:
        raise bad_request("invalid_region", f"'{key}' fuori dall'intervallo ammesso.")
    return float(v)


def _regions(raw):
    if not isinstance(raw, list):
        raise bad_request("invalid_region", "'preserve_regions' deve essere una lista.")
    if len(raw) > MAX_REGIONS:
        raise bad_request("invalid_region", f"Al massimo {MAX_REGIONS} regioni protette.")
    out = []
    for r in raw:
        if not isinstance(r, dict) or set(r) - {"page", "x", "y", "width", "height"}:
            raise bad_request("invalid_region", "Regione non valida: campi ammessi "
                              "page, x, y, width, height.")
        page = r.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
            raise bad_request("invalid_region", "'page' deve essere un intero >= 1 (oppure assente).")
        x, y = _number(r.get("x"), "x"), _number(r.get("y"), "y")
        w, h = _number(r.get("width"), "width"), _number(r.get("height"), "height")
        if w <= 0 or h <= 0:
            raise bad_request("invalid_region", "'width' e 'height' devono essere > 0.")
        out.append(Region(page, x, y, w, h))
    return tuple(out)


def _values(raw):
    if not isinstance(raw, list):
        raise bad_request("invalid_options", "'preserve_values' deve essere una lista di stringhe.")
    if len(raw) > MAX_VALUES:
        raise bad_request("invalid_options", f"Al massimo {MAX_VALUES} valori da preservare.")
    out = []
    for v in raw:
        if not isinstance(v, str):
            raise bad_request("invalid_options", "'preserve_values' deve contenere solo stringhe.")
        v = " ".join(v.split())
        if len(v) > MAX_VALUE_LEN:
            raise bad_request("invalid_options",
                              f"Ogni valore da preservare puo' avere al massimo {MAX_VALUE_LEN} caratteri.")
        if sum(c.isalnum() for c in v) < MIN_VALUE_ALNUM:
            raise bad_request("invalid_options",
                              f"Ogni valore da preservare deve avere almeno {MIN_VALUE_ALNUM} "
                              "caratteri alfanumerici.")
        if v not in out:
            out.append(v)
    return tuple(out)


def parse_options(raw, known_tags, kind):
    """raw: None | str (JSON) | dict. kind: "pdf" | "text".
    Ritorna Options o solleva ApiError(400)."""
    if raw is None or raw == "":
        d = {}
    elif isinstance(raw, dict):
        d = raw
    elif isinstance(raw, str):
        if len(raw.encode("utf-8", "replace")) > MAX_OPTIONS_BYTES:
            raise bad_request("invalid_options", "Campo 'options' troppo grande.")
        try:
            d = json.loads(raw)
        except ValueError:
            raise bad_request("invalid_options", "Il campo 'options' non e' JSON valido.")
        if not isinstance(d, dict):
            raise bad_request("invalid_options", "Il campo 'options' deve essere un oggetto JSON.")
    else:
        raise bad_request("invalid_options", "Il campo 'options' deve essere un oggetto JSON.")

    unknown = sorted(set(d) - _KNOWN)
    if unknown:
        # i nomi delle chiavi li ha scelti il client: ripeterli non espone dati
        raise bad_request("invalid_options", "Opzioni sconosciute: " + ", ".join(unknown[:10]))

    preserve = _bool(d, "preserve_letterhead", False)
    mode = _enum(d, "letterhead_mode", LETTERHEAD_MODES, "automatic")
    pages = _enum(d, "letterhead_pages", LETTERHEAD_PAGES, "first")
    regions = _regions(d.get("preserve_regions", []))
    values = _values(d.get("preserve_values", []))
    scope = _enum(d, "preserve_values_scope", VALUE_SCOPES,
                  "letterhead" if kind == "pdf" else "document")
    level = _enum(d, "anonymization_level", LEVELS, "standard")
    ret_map = _bool(d, "return_mapping", False)

    raw_tags = d.get("exclude_tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        raise bad_request("invalid_options", "'exclude_tags' deve essere una lista di stringhe.")
    tags = tuple(dict.fromkeys(t.strip().upper() for t in raw_tags if t.strip()))
    bad = [t for t in tags if t not in known_tags]
    if bad:
        raise bad_request("invalid_options", "Tag sconosciuti in 'exclude_tags': " + ", ".join(bad[:10]))

    # Coerenza: nessuna esclusione "a meta'". Regioni o valori senza
    # preserve_letterhead=true sono quasi certamente un errore del client, e
    # ignorarli in silenzio non e' un'opzione.
    if not preserve and (regions or values):
        raise bad_request("invalid_options", "'preserve_regions' e 'preserve_values' richiedono "
                          "preserve_letterhead=true.")
    if regions and mode != "coordinates":
        raise bad_request("invalid_options", "'preserve_regions' si usa solo con "
                          "letterhead_mode='coordinates'.")
    if preserve and mode == "coordinates" and not regions:
        raise bad_request("invalid_options", "letterhead_mode='coordinates' richiede almeno una "
                          "regione in 'preserve_regions'.")
    if preserve and mode == "values" and not values:
        raise bad_request("invalid_options", "letterhead_mode='values' richiede almeno un valore "
                          "in 'preserve_values'.")

    if kind == "text":
        # Un testo non ha layout: niente carta intestata geometrica. Restano solo
        # le esclusioni testuali, e solo con l'ambito dichiarato in chiaro.
        if preserve and mode != "values":
            raise bad_request("invalid_options", "Per il testo e' disponibile solo "
                              "letterhead_mode='values'.")
        if values and scope != "document":
            raise bad_request("invalid_options", "Per il testo 'preserve_values_scope' deve "
                              "essere 'document'.")

    return Options(preserve, mode, pages, regions, values, scope, level, ret_map, tags)
