# -*- coding: utf-8 -*-
"""
Analisi e anonimizzazione di TESTO (nessun file, nessun disco).

`app.analyze()` restituisce `segments`: la sequenza ordinata di pezzi di testo
in chiaro e di entita' (con il valore in `t`, dato che il motore viene sempre
chiamato con mapping_enabled=True). Sommando le lunghezze si ricostruiscono gli
offset di ogni entita' sul testo originale senza toccare il motore.

Esclusioni testuali (`preserve_values`): un'entita' resta in chiaro solo se la
sua span e' INTERAMENTE contenuta in un'occorrenza esatta (parola intera,
maiuscole comprese, spazi normalizzati) di un valore da preservare. Per il
testo l'ambito e' sempre l'intero documento, dichiarato esplicitamente dal
client con preserve_values_scope="document" (vedi options.py).
"""

import re

PLACEHOLDER_RX = re.compile(r"^\[(.+)_(\d+)\]$")


def label_of(placeholder):
    m = PLACEHOLDER_RX.match(placeholder)
    return m.group(1) if m else "UNKNOWN"


def value_regex(value):
    """Valore esatto, parola intera, spazi flessibili, case-sensitive."""
    body = r"\s+".join(re.escape(tok) for tok in value.split())
    return re.compile(r"(?<!\w)" + body + r"(?!\w)")


def entities_from_result(text, res):
    """[{label, placeholder, start, end, source, validated, value}] in ordine."""
    ents, pos = [], 0
    for seg in res["segments"]:
        if "ph" not in seg:
            pos += len(seg["t"])
            continue
        val = seg["t"]
        ents.append({
            "label": seg["label"],
            "placeholder": seg["ph"],
            "start": pos,
            "end": pos + len(val),
            "source": seg.get("src"),
            "validated": bool(seg.get("validated")),
            "value": val,
        })
        pos += len(val)
    # coerenza con il testo: se il motore cambiasse forma dei segmenti, meglio
    # fermarsi che restituire offset sbagliati (cioe' anonimizzare la cosa sbagliata)
    if pos != len(text) or any(text[e["start"]:e["end"]] != e["value"] for e in ents):
        raise RuntimeError("segments incoerenti con il testo")
    return ents


def preserved_spans(text, values):
    spans = []
    for v in values:
        spans.extend((m.start(), m.end()) for m in value_regex(v).finditer(text))
    return spans


def anonymize_text(text, res, options):
    """-> (testo_anonimizzato, entita', n_preservate)."""
    ents = entities_from_result(text, res)
    spans = preserved_spans(text, options.preserve_values) if options.preserve_letterhead else []
    out, pos, n_preserved = [], 0, 0
    for e in ents:
        e["preserved"] = any(a <= e["start"] and e["end"] <= b for a, b in spans)
        out.append(text[pos:e["start"]])
        if e["preserved"]:
            out.append(e["value"])
            n_preserved += 1
        else:
            out.append(e["placeholder"])
        pos = e["end"]
    out.append(text[pos:])
    return "".join(out), ents, n_preserved


def public_entities(ents, include_values):
    """Entita' per la risposta JSON: il valore solo se richiesto e consentito."""
    keys = ("label", "placeholder", "start", "end", "source", "validated", "preserved")
    out = []
    for e in ents:
        d = {k: e[k] for k in keys if k in e}
        if include_values:
            d["value"] = e["value"]
        out.append(d)
    return out


def mapping_of(ents):
    """Dizionario placeholder -> valore delle sole entita' sostituite."""
    m = {}
    for e in ents:
        if not e.get("preserved"):
            m.setdefault(e["placeholder"], e["value"])
    return m
