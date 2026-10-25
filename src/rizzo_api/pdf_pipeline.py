# -*- coding: utf-8 -*-
"""
Elaborazione di un PDF, interamente in memoria: bytes in ingresso -> bytes in
uscita. Nessun file temporaneo, nessuna cache.

Riusa il motore originale senza modificarlo:
  - `pdf_text.collect_readable_text`  testo da analizzare (pagine + annotazioni +
                                      campi modulo + segnalibri), come l'UI;
  - `app.analyze` (via engine)        riconoscimento PII e placeholder;
  - helper di `pdf_export`            indice char-preciso della pagina, pattern
                                      con confini di parola e sillabazione,
                                      rettangoli dei match, pulizia di
                                      annotazioni/campi/segnalibri/allegati/metadati.

Rispetto a `pdf_export.redact_pdf` (che resta invariato per l'UI) qui si
aggiunge:
  1. il CropBox viene ALLARGATO al MediaBox durante analisi e redazione e poi
     ripristinato: il testo fuori dall'area visibile e' invisibile a get_text()
     e sopravviverebbe alla redazione, pur restando nel file;
  2. la decisione "protetto / da anonimizzare" per ogni occorrenza, PRIMA di
     creare le annotazioni di redazione (letterhead.Protector);
  3. `Document.scrub()`: link (anche mailto:), JavaScript, miniature, allegati,
     XMP, metadati, risposte alle annotazioni;
  4. salvataggio completo (`tobytes(garbage=4, clean=True)`): nessuna revisione
     incrementale, oggetti non piu' referenziati eliminati;
  5. verifica dei residui sull'output, consapevole delle regioni protette.
"""

import re
import sys

import fitz  # PyMuPDF

from . import letterhead as lh
from .engine import APP_DIR, check_deadline
from .errors import ApiError, bad_request, unprocessable
from .text_pipeline import label_of

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
import pdf_export  # noqa: E402 - src/app/pdf_export.py, invariato
import pdf_text  # noqa: E402 - src/app/pdf_text.py, invariato

PDF_MAGIC = b"%PDF-"
MAGIC_WINDOW = 1024          # lo standard ammette byte spuri prima dell'header


# --------------------------------------------------------------------------- #
# Apertura e validazione
# --------------------------------------------------------------------------- #
def open_pdf(data, settings):
    if not data:
        raise bad_request("empty_file", "Il file e' vuoto.")
    if PDF_MAGIC not in data[:MAGIC_WINDOW]:
        raise ApiError(415, "unsupported_media_type", "Il file non e' un PDF.")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise bad_request("invalid_pdf", "PDF non valido o danneggiato.")
    try:
        if not doc.is_pdf:
            raise bad_request("invalid_pdf", "PDF non valido o danneggiato.")
        if doc.needs_pass:
            raise unprocessable("encrypted_pdf", "PDF protetto da password: rimuovi la protezione e riprova.")
        if doc.page_count < 1:
            raise bad_request("invalid_pdf", "Il PDF non contiene pagine.")
        if doc.page_count > settings.max_pages:
            raise ApiError(413, "too_many_pages",
                           f"Il PDF supera il limite di {settings.max_pages} pagine.")
        if doc.xref_length() > settings.max_pdf_objects:
            raise ApiError(413, "pdf_too_complex", "Il PDF contiene troppi oggetti.")
    except BaseException:
        doc.close()
        raise
    return doc


def _expand_cropboxes(doc):
    """CropBox -> MediaBox su tutte le pagine; ritorna i CropBox originali."""
    orig = []
    for page in doc:
        crop = fitz.Rect(page.cropbox)
        orig.append(crop)
        if crop != fitz.Rect(page.mediabox):
            page.set_cropbox(page.mediabox)
    return orig


def _restore_cropboxes(doc, orig):
    for page, crop in zip(doc, orig):
        if crop != fitz.Rect(page.cropbox):
            page.set_cropbox(crop)


def _harden(doc):
    """Tutto cio' che apply_redactions() non tocca e che puo' trattenere dati."""
    doc.scrub(
        attached_files=True, clean_pages=True, embedded_files=True,
        hidden_text=False,        # gia' estratto, analizzato e redatto come il resto
        javascript=True, metadata=True, redactions=False, redact_images=0,
        remove_links=True,        # anche mailto:/URI con dati personali
        reset_fields=False,       # i valori dei campi sono gia' ripuliti uno per uno
        reset_responses=True, thumbnails=True, xml_metadata=True,
    )
    pdf_export._scrub_metadata(doc)
    # PieceInfo: dati privati dell'applicazione che ha creato il file (Illustrator
    # e simili ci salvano anche il documento sorgente) — invisibili, ma nel file.
    try:
        cat = doc.pdf_catalog()
        if doc.xref_get_key(cat, "PieceInfo")[0] != "null":
            doc.xref_set_key(cat, "PieceInfo", "null")
        for page in doc:
            for key in ("PieceInfo", "Metadata"):
                if doc.xref_get_key(page.xref, key)[0] != "null":
                    doc.xref_set_key(page.xref, key, "null")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Regioni protette
# --------------------------------------------------------------------------- #
def _build_protector(doc, geoms, options, mapping):
    n = doc.page_count
    scope = lh.pages_in_scope(n, options.letterhead_pages)
    coord, auto, auto_report = {}, {}, {}
    if not options.preserve_letterhead:
        return lh.Protector({}, {}, (), None, set()), coord, auto, auto_report

    if options.letterhead_mode == "coordinates":
        for idx, reg in enumerate(options.preserve_regions):
            if reg.page is not None and reg.page > n:
                raise bad_request("invalid_region", f"Regione {idx + 1}: la pagina {reg.page} "
                                  f"non esiste (il documento ha {n} pagine).")
            targets = [reg.page - 1] if reg.page is not None else scope
            for i in targets:
                r = lh.clip_region(geoms[i], reg)
                if r is None:
                    raise bad_request("invalid_region", f"Regione {idx + 1}: interamente fuori "
                                      f"dalla pagina {i + 1}.")
                coord.setdefault(i, []).append(r)
    elif options.letterhead_mode == "automatic":
        auto, auto_report = lh.detect_automatic(doc, geoms, scope)

    zone = None
    if options.preserve_values and options.preserve_values_scope == "letterhead":
        zone = {i: [geoms[i].to_work(geoms[i].header_band()),
                    geoms[i].to_work(geoms[i].footer_band())]
                + coord.get(i, []) + auto.get(i, []) for i in scope}
    prot = lh.Protector(coord, auto, options.preserve_values, zone, lh.name_tokens(mapping))
    return prot, coord, auto, auto_report


# --------------------------------------------------------------------------- #
# Piano: occorrenze di ogni valore, protette o da redigere
# --------------------------------------------------------------------------- #
def _usable_items(mapping):
    items = sorted(((ph, v) for ph, v in mapping.items() if isinstance(v, str) and v.strip()),
                   key=lambda kv: -len(kv[1]))
    usable, skipped = [], []
    for ph, val in items:
        pat = None if pdf_export._too_noisy(val) else pdf_export._value_pattern(val)
        if pat is None:
            skipped.append(ph)
        else:
            usable.append((ph, val, pat))
    return usable, skipped


def _page_occurrences(page_no, page, usable, protector):
    text, boxes = pdf_export._page_char_index(page)
    occ = []
    if not text.strip():
        return occ
    for ph, val, pat in usable:
        label = label_of(ph)
        for m in pat.finditer(text):
            rects = pdf_export._match_rects(boxes, m)
            if not rects:
                continue
            why = protector.reason(page_no, label, val, text, boxes, m.start(), m.end())
            occ.append({"ph": ph, "label": label, "rects": rects, "preserved_by": why})
    return occ


def _verify_residuals(out_bytes, usable, protector):
    """Placeholder il cui valore e' ancora leggibile nell'output FUORI dalle
    occorrenze legittimamente protette. Controlla anche annotazioni, campi
    modulo e segnalibri (li' nessuna protezione: devono essere puliti)."""
    residual = set()
    with fitz.open(stream=out_bytes, filetype="pdf") as doc:
        _expand_cropboxes(doc)
        for i, page in enumerate(doc):
            check_deadline()
            for o in _page_occurrences(i, page, usable, protector):
                if o["preserved_by"] is None:
                    residual.add(o["ph"])
        non_page = _non_page_text(doc)
    # annotazioni, campi modulo, segnalibri: niente regioni protette, niente sconti
    for ph, _val, pat in usable:
        if pat.search(non_page):
            residual.add(ph)
    return sorted(residual)


def _non_page_text(doc):
    """Testo che non sta nel content stream: annotazioni, campi modulo, TOC."""
    parts = []
    for page in doc:
        for a in page.annots() or ():
            if a.type[0] == fitz.PDF_ANNOT_REDACT:
                continue
            info = a.info or {}
            parts.extend(str(info.get(k) or "") for k in ("content", "subject", "title"))
        for w in page.widgets() or ():
            if isinstance(w.field_value, str):
                parts.append(w.field_value)
    parts.extend(str(e[1] or "") for e in (doc.get_toc(simple=True) or ()))
    return "\n".join(parts)


def _region_json(geom, page_no, r, source):
    d = geom.to_display(r)
    return {"page": page_no + 1, "x": round(d.x0, 2), "y": round(d.y0, 2),
            "width": round(d.width, 2), "height": round(d.height, 2), "source": source}


# --------------------------------------------------------------------------- #
# Ingresso principale
# --------------------------------------------------------------------------- #
def process_pdf(data, options, engine, settings, redact=True):
    """redact=True  -> {"pdf": bytes, "stats": {...}}
    redact=False -> analisi: {"entities": [...], "preserved_regions": [...], ...}"""
    doc = open_pdf(data, settings)
    try:
        geoms = [lh.PageGeom(p) for p in doc]
        orig_crops = _expand_cropboxes(doc)

        no_text_pages = [i + 1 for i, p in enumerate(doc)
                         if not p.get_text().strip() and p.get_images(full=False)]
        text = pdf_text.collect_readable_text(doc).strip()
        if not text:
            raise unprocessable("no_text_layer", "Il PDF non ha testo estraibile (scansione o "
                                "sole immagini): serve un OCR, non supportato da questo servizio.")
        if len(text) > settings.max_text_chars:
            raise ApiError(413, "text_too_long", "Il testo del PDF supera il limite consentito.")
        check_deadline()

        res = engine.analyze(text, options.exclude_tags)
        mapping = res["mapping"]
        check_deadline()

        protector, coord, auto, auto_report = _build_protector(doc, geoms, options, mapping)
        usable, skipped = _usable_items(mapping)

        plan = []
        for i, page in enumerate(doc):
            check_deadline()
            plan.append(_page_occurrences(i, page, usable, protector))

        regions = [_region_json(geoms[i], i, r, "coordinates") for i in sorted(coord) for r in coord[i]]
        regions += [_region_json(geoms[i], i, r, "automatic") for i in sorted(auto) for r in auto[i]]

        if not redact:
            return _analysis(doc, geoms, plan, mapping, skipped, regions, auto_report,
                             no_text_pages, options)

        stats = _apply(doc, plan, usable)
        _restore_cropboxes(doc, orig_crops)
        _harden(doc)
        check_deadline()
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    residual = _verify_residuals(out, usable, protector)
    stats.update({
        "skipped": len(skipped),
        "residual": len(residual),
        "pages": len(geoms),
        "pages_without_text": len(no_text_pages),
        "letterhead_regions": len(regions),
        "entities": len(mapping),
    })
    return {"pdf": out, "stats": stats}


def _apply(doc, plan, usable):
    """Annotazioni di redazione SOLO per le occorrenze non protette, poi
    rimozione vera dal content stream; poi annotazioni/campi/segnalibri/allegati."""
    patterns = [(pat, ph) for ph, _v, pat in usable]
    redacted = preserved = n_annots = n_widgets = 0
    for page, occ in zip(doc, plan):
        check_deadline()
        taken = []
        for o in occ:
            if o["preserved_by"]:
                preserved += 1
                continue
            placed, labeled = False, False
            for r in o["rects"]:
                if pdf_export._covered(r, taken):
                    continue
                fs = 0 if labeled else pdf_export._fit_fontsize(o["ph"], r)
                pdf_export._add_redact_annot(page, r, o["ph"] if fs else None, fs or 6,
                                             pdf_export.REDACT_FILL, pdf_export.REDACT_TEXT)
                taken.append(fitz.Rect(r))
                placed, labeled = True, labeled or bool(fs)
            redacted += placed
        if taken:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        n_annots += pdf_export._scrub_annots(page, patterns)
        n_widgets += pdf_export._scrub_widgets(page, patterns)
    pdf_export._scrub_toc(doc, patterns)
    pdf_export._strip_embedded(doc)
    return {"redactions": redacted, "preserved": preserved,
            "annotations": n_annots, "fields": n_widgets}


def _analysis(doc, geoms, plan, mapping, skipped, regions, auto_report, no_text_pages, options):
    by_ph = {}
    for i, occ in enumerate(plan):
        for o in occ:
            by_ph.setdefault(o["ph"], []).append({
                "page": i + 1,
                "rects": [{k: v for k, v in _region_json(geoms[i], i, r, "").items()
                           if k in ("x", "y", "width", "height")} for r in o["rects"]],
                "preserved": bool(o["preserved_by"]),
                "preserved_by": o["preserved_by"],
            })
    entities = []
    for ph, val in mapping.items():
        e = {"placeholder": ph, "label": label_of(ph), "occurrences": by_ph.get(ph, []),
             "searchable": ph not in skipped}
        if options.return_mapping:
            e["value"] = val
        entities.append(e)
    entities.sort(key=lambda e: (e["label"], int(re.sub(r"\D", "", e["placeholder"]) or 0)))
    out = {
        "pages": len(geoms),
        "pages_without_text": no_text_pages,
        "entities": entities,
        "preserved_regions": regions,
        "skipped": len(skipped),
    }
    if auto_report:
        out["letterhead_detection"] = [{"page": i + 1, "bands": auto_report[i]}
                                       for i in sorted(auto_report)]
    return out
