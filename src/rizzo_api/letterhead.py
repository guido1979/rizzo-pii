# -*- coding: utf-8 -*-
"""
Carta intestata: geometria delle pagine, riconoscimento automatico, regole di
protezione. Nessuna redazione qui: questo modulo produce SOLO l'elenco delle
regioni protette e decide, occorrenza per occorrenza, se un valore va lasciato
in chiaro. La decisione avviene PRIMA che la redazione venga applicata.

Sistemi di coordinate
---------------------
  * "visualizzato" (quello dei client): punti PDF (1/72"), origine nell'angolo in
    alto a sinistra del CropBox, y verso il basso, rotazione /Rotate gia'
    applicata, cioe' la pagina come la mostra un viewer;
  * "di lavoro" (quello interno): coordinate NON ruotate di PyMuPDF con il
    CropBox allargato al MediaBox (vedi pdf_pipeline: il testo fuori dal CropBox
    va visto e redatto anch'esso). Estrazione del testo e redazioni usano questo.

  lavoro = (visualizzato * derotation_matrix) + delta_cropbox
  visualizzato = (lavoro - delta_cropbox) * rotation_matrix

Riconoscimento automatico (letterhead_mode="automatic")
-------------------------------------------------------
Per ogni pagina in ambito, separatamente (pagine diverse possono avere
intestazioni diverse, o non averla affatto):

  1. Bande candidate: il 25% superiore (intestazione) e il 15% inferiore (pie' di
     pagina) della pagina VISUALIZZATA. Non tutta la banda e' carta intestata:
     e' solo l'area in cui la si cerca.
  2. Cluster: partendo dal bordo, si raccolgono le righe di testo consecutive.
     Ci si ferma al primo "stacco" verticale ampio, a una riga che apre il
     corpo del documento (destinatario "Spett.le", "Oggetto:", "Prot.", luogo e
     data, formule di saluto...) o a una riga che ha l'aspetto di una frase
     (molte parole senza indicatori istituzionali, oppure verbi tipici
     dell'atto: "nato a", "residente", "dichiara"...).
  3. Segnali: tipi di indicatori istituzionali presenti nel cluster (forma
     giuridica o tipo di ente, indirizzo/CAP, P.IVA/C.F., telefono/fax, email,
     PEC, sito web), presenza di un'immagine (logo) nella banda, ripetizione
     della stessa riga nella stessa banda su altre pagine (cifre normalizzate).
  4. Accettazione: >= 2 tipi di indicatori, oppure 1 tipo + logo o
     ripetizione, oppure logo + ripetizione. Sulle pagine successive alla
     prima si richiede in piu' logo o ripetizione: li' la parte alta della
     pagina e' spesso gia' corpo del documento.
  5. Regioni protette: i rettangoli delle SOLE righe del cluster accettato (non
     l'intera banda). Se il cluster non e' accettato, la pagina non ha nessuna
     regione automatica: nel dubbio si anonimizza.

Anche dentro una regione automatica restano in chiaro solo le categorie
"istituzionali" (AUTO_PRESERVABLE_LABELS). Un nome di persona (FULLNAME), un
codice fiscale personale (16 caratteri), un IBAN, una data... vengono anonimizzati
comunque. Un'email resta in chiaro solo se la parte locale non contiene il nome
o il cognome di una persona rilevata nel documento.
"""

import re
import statistics

import fitz  # PyMuPDF

HEADER_FRAC = 0.25
FOOTER_FRAC = 0.15
MAX_LINE_CHARS = 160
SENTENCE_WORDS = 10
REGION_PAD = 1.0
MIN_REPEAT_LEN = 6

AUTO_PRESERVABLE_LABELS = frozenset({
    "ORG", "STREET", "BUILDINGNUM", "CITY", "ZIPCODE", "PROVINCE",
    "PIVA", "CF", "TELEPHONENUM", "EMAIL", "URL",
})

_I = re.IGNORECASE
MARKERS = {
    "ente": re.compile(
        r"\b(s\.?\s?r\.?\s?l|s\.?\s?p\.?\s?a|s\.?\s?a\.?\s?s|s\.?\s?n\.?\s?c|s\.?\s?c\.?\s?a\.?\s?r\.?\s?l|"
        r"istituto|comune|provincia|regione|ministero|universit[aà]|scuola|liceo|"
        r"azienda|ente|studio|associazione|fondazione|consorzio|camera|ordine|agenzia|"
        r"ufficio|cooperativa|asl|asp|ats|ospedale|banca|tribunale|prefettura|"
        r"questura|direzione|dipartimento|segreteria|onlus|aps|odv)\b", _I),
    "indirizzo": re.compile(
        r"\b(via|viale|piazza|p\.?\s?zza|corso|c\.so|largo|vicolo|strada|località|loc\.|"
        r"contrada|lungomare|piazzale|borgo)\b|\b\d{5}\s+[A-Z]", _I),
    "fiscale": re.compile(
        r"\b(p\.?\s?\.?iva|partita\s+iva|c\.?\s?f\.|cod(?:ice)?\.?\s*fisc(?:ale)?|vat)\b", _I),
    "telefono": re.compile(r"\b(tel|telefono|fax|cell)\b\.?|\+39", _I),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    # "PEC" come etichetta, non il dominio di un indirizzo (gia' contato da "email")
    "pec": re.compile(r"(?<![@.\w])pec\b(?![.@])", _I),
    # dominio NON preceduto da "@": un indirizzo email non vale anche come sito web
    "web": re.compile(r"\bwww\.|https?://|(?<![@.\w-])[\w-]+\.(?:it|eu|com|org|net|gov\.it|edu\.it)\b", _I),
}

# Righe che aprono il corpo del documento: il cluster si ferma QUI (riga esclusa).
STOP_LINE = re.compile(
    r"^\s*(spett|egr|gent|ill\.?mo|ill\.?ma|preg|chiar|alla\s+c|al\s+sig|alla\s+sig|ai\s+sig|"
    r"c\.\s?a\.(?!\s?p)|att\.|oggetto|ogg\.|prot\.|prot\s*n|protocollo|rif\.|riferimento|"
    r"n\.\s*prot|data\b|lì|addì|caro\b|cara\b|dear\b|to:|raccomandata|a\s+mezzo|"
    r"via\s+pec|trasmess|al\s+dirigente|ai\s+genitori|agli\b|alle\b|distinti|cordiali|"
    r"in\s+fede|firma|il\s+dirigente|il\s+responsabile|il\s+presidente|il\s+sottoscritt|"
    r"la\s+sottoscritt|premesso|dott[.\s]|avv[.\s]|sig\.|signor)", _I)
# "Roma, 12 marzo 2025" / "Milano, lì 3/4/2025": luogo e data, quindi corpo.
PLACE_DATE = re.compile(
    r"^\s*[A-ZÀ-Ý][\w'À-ÿ .-]{1,40},\s*(l[iì]\.?\s*)?"
    r"(\d{1,2}\s*[/.-]\s*\d{1,2}|\d{1,2}\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\w*)", _I)
SENTENCE = re.compile(
    r"\b(nat[oa]|resident[ei]|domiciliat[oa]|sottoscritt[oa]|dichiar\w*|in\s+qualit[aà]|"
    r"il\s+quale|la\s+quale|chiede|richiede|comunica|si\s+invita|con\s+la\s+presente)\b", _I)


# --------------------------------------------------------------------------- #
# Geometria
# --------------------------------------------------------------------------- #
class PageGeom:
    """Conversioni visualizzato <-> lavoro per UNA pagina. Va costruita PRIMA di
    allargare il CropBox (servono rotazione e CropBox originali)."""

    def __init__(self, page):
        self.rect = fitz.Rect(page.rect)                   # pagina visualizzata
        self.rot = fitz.Matrix(page.rotation_matrix)
        self.derot = fitz.Matrix(page.derotation_matrix)
        crop, media = fitz.Rect(page.cropbox), fitz.Rect(page.mediabox)
        self.crop = crop
        self.dx, self.dy = crop.x0 - media.x0, crop.y0 - media.y0

    def to_work(self, r):
        r = fitz.Rect(r) * self.derot
        return fitz.Rect(r.x0 + self.dx, r.y0 + self.dy, r.x1 + self.dx, r.y1 + self.dy)

    def to_display(self, r):
        r = fitz.Rect(r)
        r = fitz.Rect(r.x0 - self.dx, r.y0 - self.dy, r.x1 - self.dx, r.y1 - self.dy)
        return r * self.rot

    def header_band(self):
        return fitz.Rect(0, 0, self.rect.width, self.rect.height * HEADER_FRAC)

    def footer_band(self):
        return fitz.Rect(0, self.rect.height * (1 - FOOTER_FRAC), self.rect.width, self.rect.height)


def pages_in_scope(n_pages, letterhead_pages):
    return [0] if letterhead_pages == "first" else list(range(n_pages))


def clip_region(geom, region):
    """Regione del client (visualizzata) -> rettangolo di lavoro, ritagliato
    sulla pagina. None se cade interamente fuori."""
    r = fitz.Rect(region.x, region.y, region.x + region.width, region.y + region.height)
    r &= geom.rect
    if r.is_empty or r.width <= 0 or r.height <= 0:
        return None
    return geom.to_work(r)


# --------------------------------------------------------------------------- #
# Riconoscimento automatico
# --------------------------------------------------------------------------- #
def _lines(page, geom):
    """Righe di testo VISIBILI: (testo, rect_lavoro, rect_visualizzato) + immagini."""
    lines, images = [], []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") == 1:
            images.append(geom.to_display(b["bbox"]))
            continue
        for ln in b.get("lines", []):
            txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            if not txt:
                continue
            work = fitz.Rect(ln["bbox"])
            disp = geom.to_display(work)
            if not disp.intersects(geom.rect):           # fuori dalla pagina visibile
                continue
            lines.append((txt, work, disp))
    return lines, images


def _norm_line(t):
    return re.sub(r"\s+", " ", re.sub(r"\d", "#", t)).strip().casefold()


def _kinds(text):
    return {k for k, rx in MARKERS.items() if rx.search(text)}


def _body_like(text):
    if STOP_LINE.match(text) or PLACE_DATE.match(text) or SENTENCE.search(text):
        return True
    if len(text) > MAX_LINE_CHARS:
        return True
    return len(text.split()) >= SENTENCE_WORDS and len(_kinds(text)) < 2


def _cluster(lines, band, from_top, median_h):
    """Righe consecutive dal bordo della pagina verso l'interno, dentro la banda."""
    cand = [ln for ln in lines if band.contains(fitz.Point((ln[2].x0 + ln[2].x1) / 2,
                                                           (ln[2].y0 + ln[2].y1) / 2))]
    cand.sort(key=lambda ln: (ln[2].y0, ln[2].x0), reverse=not from_top)
    gap_max = max(3.0 * median_h, 24.0)
    out, edge = [], None
    for ln in cand:
        disp = ln[2]
        if edge is not None:
            gap = (disp.y0 - edge) if from_top else (edge - disp.y1)
            if gap > gap_max:
                break
        if _body_like(ln[0]):
            break
        out.append(ln)
        if from_top:
            edge = disp.y1 if edge is None else max(edge, disp.y1)
        else:
            edge = disp.y0 if edge is None else min(edge, disp.y0)
    return out


def detect_automatic(doc, geoms, scope_pages):
    """{indice_pagina: [rect_lavoro, ...]} delle righe riconosciute come carta
    intestata, piu' un rapporto diagnostico per pagina (solo conteggi/segnali)."""
    per_page = {}
    repeat = {}
    for i, page in enumerate(doc):
        lines, images = _lines(page, geoms[i])
        per_page[i] = (lines, images)
        g = geoms[i]
        for side, band in (("h", g.header_band()), ("f", g.footer_band())):
            for txt, _w, disp in lines:
                if band.intersects(disp):
                    key = (side, _norm_line(txt))
                    if len(key[1]) >= MIN_REPEAT_LEN:
                        repeat.setdefault(key, set()).add(i)

    regions, report = {}, {}
    for i in scope_pages:
        lines, images = per_page[i]
        g = geoms[i]
        heights = [ln[2].height for ln in lines if ln[2].height > 0]
        median_h = statistics.median(heights) if heights else 12.0
        found = []
        signals = []
        for side, band, from_top in (("h", g.header_band(), True), ("f", g.footer_band(), False)):
            cl = _cluster(lines, band, from_top, median_h)
            if not cl:
                continue
            kinds = set().union(*(_kinds(ln[0]) for ln in cl))
            logo = any(band.intersects(im) for im in images)
            repeated = any(len(repeat.get((side, _norm_line(ln[0])), ())) >= 2 for ln in cl)
            ok = len(kinds) >= 2 or (kinds and (logo or repeated)) or (logo and repeated)
            if i > 0 and not (logo or repeated):
                ok = False
            signals.append({"band": "header" if from_top else "footer",
                            "markers": sorted(kinds), "logo": logo, "repeated": repeated,
                            "lines": len(cl), "accepted": bool(ok)})
            if ok:
                found.extend(fitz.Rect(ln[1]) + (-REGION_PAD, -REGION_PAD, REGION_PAD, REGION_PAD)
                             for ln in cl)
        if found:
            regions[i] = found
        report[i] = signals
    return regions, report


# --------------------------------------------------------------------------- #
# Regole di protezione per occorrenza
# --------------------------------------------------------------------------- #
def _inside(boxes, rects):
    """Tutti i caratteri (centro del glifo) dentro almeno uno dei rettangoli."""
    if not rects:
        return False
    any_box = False
    for b in boxes:
        if b is None or b.is_empty:
            continue
        any_box = True
        c = fitz.Point((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)
        if not any(r.contains(c) for r in rects):
            return False
    return any_box


def name_tokens(mapping):
    toks = set()
    for ph, val in mapping.items():
        if ph.startswith("[FULLNAME_"):
            toks.update(t for t in re.findall(r"[^\W\d_]{3,}", val.casefold()))
    return toks


def auto_label_ok(label, value, names):
    if label not in AUTO_PRESERVABLE_LABELS:
        return False
    if label == "CF":                     # solo il C.F. numerico (11 cifre) di un ente
        return re.sub(r"\s", "", value).isdigit()
    if label == "EMAIL":
        local = value.split("@", 1)[0].casefold()
        return not any(t in local for t in names)
    return True


class Protector:
    """Decide se un'occorrenza (pagina, caratteri del match) resta in chiaro.

    regions_coord: {pagina: [rect]}  — modalita' coordinate: tutto il testo dentro
    regions_auto:  {pagina: [rect]}  — modalita' automatica: solo categorie istituzionali
    value_zone:    {pagina: [rect]} oppure None (= tutto il documento) per preserve_values
    values:        valori esatti da preservare
    """

    def __init__(self, regions_coord, regions_auto, values, value_zone, names):
        self.coord = regions_coord
        self.auto = regions_auto
        self.values = [re.compile(r"(?<!\w)" + r"\s+".join(re.escape(t) for t in v.split()) + r"(?!\w)")
                       for v in values]
        self.value_zone = value_zone
        self.names = names
        self._spans_cache = {}

    @property
    def active(self):
        return bool(self.coord or self.auto or self.values)

    def _value_spans(self, page_no, text, boxes):
        # chiave sul CONTENUTO: in verifica la stessa pagina ha un testo diverso
        # (redatto), e un id() riciclato restituirebbe span di un'altra stringa
        key = (page_no, text)
        if key not in self._spans_cache:
            spans = []
            zone = None if self.value_zone is None else self.value_zone.get(page_no, [])
            for rx in self.values:
                for m in rx.finditer(text):
                    if zone is None or _inside(boxes[m.start():m.end()], zone):
                        spans.append((m.start(), m.end()))
            self._spans_cache = {key: spans}          # una pagina alla volta
        return self._spans_cache[key]

    def reason(self, page_no, label, value, text, boxes, start, end):
        """'coordinates' | 'automatic' | 'values' | None (= va anonimizzato)."""
        if not self.active:
            return None
        mboxes = boxes[start:end]
        if _inside(mboxes, self.coord.get(page_no, [])):
            return "coordinates"
        if self.values:
            for a, b in self._value_spans(page_no, text, boxes):
                if a <= start and end <= b:
                    return "values"
        if (self.auto.get(page_no) and auto_label_ok(label, value, self.names)
                and _inside(mboxes, self.auto[page_no])):
            return "automatic"
        return None
