# -*- coding: utf-8 -*-
"""Supporto ai test della Private API: motore finto e PDF sintetici.

Il motore finto ha la STESSA forma di risposta di `app.analyze()` (segments +
mapping + contatori) e usa la vera rete regex/checksum di `detectors`: cosi' i
test girano senza torch/transformers e senza il modello da 1,2 GB, e non
dipendono da cosa il modello predice oggi. L'integrazione col motore reale e'
coperta da tests/test_api_e2e.py, contro il container.

TUTTI i dati sono sintetici: nomi inventati, codici fiscali e partite IVA
generati qui con checksum valido, domini .example.it.
"""

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "src" / "app"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    import fitz  # noqa: F401
    import flask  # noqa: F401
    DEPS = True
except ImportError:            # la CI base non installa flask/pymupdf
    DEPS = False

KEY_A = "a" * 20 + "-test-key-app-alpha-0001"
KEY_B = "b" * 20 + "-test-key-app-bravo-0002"

TAG_NAMES = ("FULLNAME", "AGE", "GENDER", "DATE", "TIME", "STREET", "BUILDINGNUM", "ZIPCODE",
             "CITY", "PROVINCE", "EMAIL", "TELEPHONENUM", "CF", "PIVA", "ID_DOC", "IBAN",
             "CREDITCARDNUMBER", "AMOUNT", "TARGA", "ORG", "DOCID", "CATASTO", "URL", "IPADDR")


# --------------------------------------------------------------------------- #
# Codici sintetici con checksum valido
# --------------------------------------------------------------------------- #
_ODD = {**{str(i): v for i, v in enumerate([1, 0, 5, 7, 9, 13, 15, 17, 19, 21])},
        **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                   [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14,
                    16, 10, 22, 25, 24, 23]))}


def make_cf(prefix15):
    tot = 0
    for i, c in enumerate(prefix15):
        if i % 2 == 0:
            tot += _ODD[c]
        else:
            tot += int(c) if c.isdigit() else ord(c) - 65
    return prefix15 + chr(65 + tot % 26)


def make_piva(prefix10):
    t = 0
    for i, c in enumerate(map(int, prefix10)):
        if i % 2 == 0:
            t += c
        else:
            x = c * 2
            t += x - 9 if x > 9 else x
    return prefix10 + str((10 - t % 10) % 10)


CF_PERSON = make_cf("BRMGLT90A41A944")          # sintetico
CF_PERSON_2 = make_cf("QRCTTV75C12H501")
PIVA_ENTE = make_piva("8001234567")
CF_ENTE = make_piva("9204561238")               # C.F. numerico di un ente (11 cifre)

ORG = "Istituto Comprensivo Valle Serena"
STUDENT = "Giulietta Bramante"
HEAD_PERSON = "Ottavio Quercioli"
OTHER_PERSON = "Rosalba Ferrandini"

DEFAULT_ENTITIES = (
    (ORG, "ORG"),
    (STUDENT, "FULLNAME"),
    (HEAD_PERSON, "FULLNAME"),
    (OTHER_PERSON, "FULLNAME"),
    ("Via delle Ginestre 14", "STREET"),
    ("Via Larga 3", "STREET"),
    ("Bologna", "CITY"),
    ("Imola", "CITY"),
)


# --------------------------------------------------------------------------- #
# Motore finto
# --------------------------------------------------------------------------- #
class FakeEngine:
    """Stessa interfaccia di rizzo_api.engine.CoreEngine."""
    tag_names = TAG_NAMES

    def __init__(self, entities=DEFAULT_ENTITIES, delay=0.0, fail=False):
        self.entities = tuple(entities)
        self.delay = delay
        self.fail = fail
        self.calls = 0

    def analyze(self, text, excluded):
        from detectors import detect_regex
        from rizzo_api.engine import check_deadline
        self.calls += 1
        end = time.monotonic() + self.delay
        while time.monotonic() < end:            # come i lotti del pipeline vero
            check_deadline()
            time.sleep(0.01)
        check_deadline()
        if self.fail:
            # messaggio con dentro il testo: NON deve arrivare a log o risposta
            raise ValueError("errore simulato su: " + text[:80])
        cands = []
        for value, label in self.entities:
            rx = r"(?<!\w)" + r"\s+".join(re.escape(t) for t in value.split()) + r"(?!\w)"
            for m in re.finditer(rx, text):
                cands.append({"label": label, "start": m.start(), "end": m.end(),
                              "validated": False, "source": "modello"})
        cands += detect_regex(text)
        cands = [c for c in cands if c["label"] not in set(excluded)]
        cands.sort(key=lambda c: (-(c["validated"]), -(c["end"] - c["start"]), c["start"]))
        kept = []
        for c in cands:
            if all(c["end"] <= k["start"] or c["start"] >= k["end"] for k in kept):
                kept.append(c)
        kept.sort(key=lambda c: c["start"])
        counters, seen, mapping, segments, pos = {}, {}, {}, [], 0
        for e in kept:
            val = text[e["start"]:e["end"]]
            key = (e["label"], " ".join(val.split()).casefold())
            if key not in seen:
                counters[e["label"]] = counters.get(e["label"], 0) + 1
                seen[key] = f"[{e['label']}_{counters[e['label']]}]"
                mapping[seen[key]] = val
            if e["start"] > pos:
                segments.append({"t": text[pos:e["start"]]})
            segments.append({"label": e["label"], "ph": seen[key], "src": e["source"],
                             "validated": e["validated"], "t": val})
            pos = e["end"]
        if pos < len(text):
            segments.append({"t": text[pos:]})
        return {"segments": segments, "mapping": mapping, "n_unique": len(seen),
                "n_entities": len(kept)}


# --------------------------------------------------------------------------- #
# PDF sintetici
# --------------------------------------------------------------------------- #
HEADER_LINES = (
    ORG,
    "Via delle Ginestre 14 - 40121 Bologna",
    f"C.F. {CF_ENTE} - P.IVA {PIVA_ENTE}",
    "Tel. 051 2345678 - segreteria@vallesereno.example.it",
    "PEC: vallesereno@pec.example.it - www.vallesereno.example.it",
)
FOOTER_LINE = "Sede: Via delle Ginestre 14 - 40121 Bologna - Tel. 051 2345678"
BODY_MARKER = "Oggetto: comunicazione alle famiglie"


def _logo(page, rect):
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 24, 24), 0)
    pix.clear_with(120)
    page.insert_image(rect, pixmap=pix)


def letter_page(doc, body_lines, letterhead=True, logo=True, footer=True,
                head_person=False, width=595, height=842):
    page = doc.new_page(width=width, height=height)
    y = 40
    if letterhead:
        if logo:
            _logo(page, (40, 30, 90, 80))
        for ln in HEADER_LINES:
            page.insert_text((110, y + 10), ln, fontsize=10)
            y += 14
        if head_person:
            page.insert_text((110, y + 10), f"Dirigente scolastico {HEAD_PERSON}", fontsize=10)
            y += 14
    y = max(y, 200)
    page.insert_text((60, y), "Spett.le famiglia", fontsize=11)
    y += 30
    page.insert_text((60, y), BODY_MARKER, fontsize=11)
    y += 24
    for ln in body_lines:
        page.insert_text((60, y), ln, fontsize=11)
        y += 18
    if footer:
        page.insert_text((60, height - 40), FOOTER_LINE, fontsize=8)
    return page


DEFAULT_BODY = (
    f"La studentessa {STUDENT}, codice fiscale {CF_PERSON},",
    "residente in Via Larga 3, Imola, e' iscritta presso",
    f"l'{ORG} per l'anno in corso.",
    "Per informazioni scrivere a giulietta.bramante@mail.example.it",
)


def letter_pdf(pages=1, body=DEFAULT_BODY, letterhead_pages="all", rotate=0, **kw):
    import fitz
    doc = fitz.open()
    for i in range(pages):
        lh = letterhead_pages == "all" or i == 0
        letter_page(doc, body, letterhead=lh, logo=lh and kw.get("logo", True),
                    footer=lh and kw.get("footer", True), head_person=kw.get("head_person", False))
    if rotate:
        for p in doc:
            p.set_rotation(rotate)
    doc.set_metadata({"author": STUDENT, "title": f"Pratica {STUDENT}"})
    out = doc.tobytes()
    doc.close()
    return out


def text_of(pdf_bytes, full_mediabox=True):
    """Tutto il testo estraibile, anche fuori dal CropBox."""
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        parts = []
        for p in d:
            if full_mediabox:
                p.set_cropbox(p.mediabox)
            parts.append(p.get_text())
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Base dei test HTTP
# --------------------------------------------------------------------------- #
if DEPS:
    import io
    import json
    import unittest

    class ApiCase(unittest.TestCase):
        engine_kwargs = {}
        settings_kwargs = {}

        def setUp(self):
            from rizzo_api.server import create_app
            from rizzo_api.settings import Settings, parse_key_entries
            self.engine = FakeEngine(**self.engine_kwargs)
            self.settings = Settings(
                key_digests=parse_key_entries(f"app-a:{KEY_A},app-b:{KEY_B}", "test"),
                **self.settings_kwargs)
            self.app = create_app(engine=self.engine, settings=self.settings)
            self.client = self.app.test_client()

        @staticmethod
        def auth(key=KEY_A):
            return {"Authorization": f"Bearer {key}"}

        def post_pdf(self, pdf, options=None, path="/api/v1/anonymize", key=KEY_A, client=None):
            data = {"file": (io.BytesIO(pdf), "documento.pdf")}
            if options is not None:
                data["options"] = options if isinstance(options, str) else json.dumps(options)
            return (client or self.client).post(path, headers=self.auth(key), data=data,
                                                content_type="multipart/form-data")

        def post_text(self, text, options=None, path="/api/v1/anonymize-text", key=KEY_A):
            body = {"text": text}
            if options is not None:
                body["options"] = options
            return self.client.post(path, headers=self.auth(key), json=body)

        def assertError(self, resp, status, code):
            self.assertEqual(resp.status_code, status, resp.get_data(as_text=True)[:300])
            self.assertEqual(resp.json["error"]["code"], code)
            self.assertIn("request_id", resp.json["error"])
