# -*- coding: utf-8 -*-
"""End-to-end della Private API contro un container VERO (modello reale).

Si salta se non sono impostate:
  RIZZO_API_E2E_URL   es. http://rizzo-pii-api:5005   (hostname sulla rete Docker condivisa)
  RIZZO_API_E2E_KEY   una delle chiavi configurate nel servizio

Uso tipico, da un container client collegato alla stessa rete privata:
  docker run --rm --network rizzo-pii-private -v "$PWD":/w -w /w \\
    -e RIZZO_API_E2E_URL=http://rizzo-pii-api:5005 -e RIZZO_API_E2E_KEY=... \\
    python:3.11-slim sh -c "pip install -q pymupdf && python -m unittest -v tests/test_api_e2e.py"

Le asserzioni sul modello sono volutamente poche e robuste (un CF con checksum
valido lo trova sempre la rete regex; nomi propri evidenti in frasi piane): il
test verifica il servizio, non l'accuratezza del modello.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
import uuid

from _api_support import CF_PERSON, CF_PERSON_2, ORG, PIVA_ENTE, STUDENT, letter_pdf

URL = os.environ.get("RIZZO_API_E2E_URL", "").rstrip("/")
KEY = os.environ.get("RIZZO_API_E2E_KEY", "")

try:
    import fitz  # noqa: F401
    HAVE_FITZ = True
except ImportError:
    HAVE_FITZ = False


def call(method, path, body=None, ctype=None, key=KEY, timeout=600):
    req = urllib.request.Request(URL + path, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def multipart(pdf, options=None):
    b = uuid.uuid4().hex
    parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"d.pdf\"\r\n"
             f"Content-Type: application/pdf\r\n\r\n".encode() + pdf + b"\r\n"]
    if options is not None:
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"options\"\r\n\r\n"
                     f"{json.dumps(options)}\r\n".encode())
    return b"".join(parts) + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def pdf_text(data):
    with fitz.open(stream=data, filetype="pdf") as d:
        return "\n".join(p.get_text() for p in d)


@unittest.skipUnless(URL and KEY, "RIZZO_API_E2E_URL / RIZZO_API_E2E_KEY non impostate")
class E2E(unittest.TestCase):

    def test_health(self):
        st, _h, body = call("GET", "/health", key=None)
        self.assertEqual((st, json.loads(body)), (200, {"status": "ok"}))

    def test_senza_chiave(self):
        st, _h, body = call("POST", "/api/v1/anonymize-text", json.dumps({"text": "x"}).encode(),
                            "application/json", key=None)
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")

    def test_anonymize_text_modello_reale(self):
        text = (f"Il sottoscritto {STUDENT}, nato a Ravenna il 3 marzo 1990, codice fiscale "
                f"{CF_PERSON}, dichiara di aver ricevuto la comunicazione.")
        st, h, body = call("POST", "/api/v1/anonymize-text",
                           json.dumps({"text": text}).encode(), "application/json")
        self.assertEqual(st, 200, body[:300])
        out = json.loads(body)
        self.assertNotIn(CF_PERSON, out["anonymized_text"])
        self.assertNotIn(STUDENT, out["anonymized_text"])
        self.assertIn("[CF_1]", out["anonymized_text"])
        self.assertEqual(h.get("Cache-Control"), "no-store")
        self.assertTrue(all("value" not in e for e in out["entities"]))

    @unittest.skipUnless(HAVE_FITZ, "serve pymupdf per costruire i PDF")
    def test_anonymize_pdf_con_carta_intestata(self):
        body, ctype = multipart(letter_pdf(pages=2), {"preserve_letterhead": True,
                                                      "letterhead_mode": "automatic",
                                                      "letterhead_pages": "all"})
        st, h, data = call("POST", "/api/v1/anonymize", body, ctype)
        self.assertEqual(st, 200, data[:300])
        self.assertEqual(h.get("Content-Type"), "application/pdf")
        txt = pdf_text(data)
        self.assertNotIn(CF_PERSON, txt)
        self.assertNotIn(STUDENT, txt)
        self.assertEqual(txt.count(PIVA_ENTE), 2)     # P.IVA dell'ente nelle 2 intestazioni
        head = txt.split("Spett.le", 1)[0]
        self.assertIn(ORG, head)

    @unittest.skipUnless(HAVE_FITZ, "serve pymupdf per costruire i PDF")
    def test_anonymize_pdf_senza_esclusioni(self):
        body, ctype = multipart(letter_pdf())
        st, h, data = call("POST", "/api/v1/anonymize", body, ctype)
        self.assertEqual(st, 200, data[:300])
        txt = pdf_text(data)
        for v in (CF_PERSON, PIVA_ENTE, STUDENT):
            self.assertNotIn(v, txt)
        self.assertNotIn(STUDENT.encode(), data)

    def test_concorrenza_senza_contaminazione(self):
        cfs = [CF_PERSON, CF_PERSON_2]
        res, errs = {}, []

        def go(i):
            try:
                text = f"Pratica numero {i}: codice fiscale {cfs[i % 2]}."
                st, _h, body = call("POST", "/api/v1/anonymize-text",
                                    json.dumps({"text": text}).encode(), "application/json")
                res[i] = (st, json.loads(body))
            except Exception as e:          # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        for i, (st, out) in res.items():
            self.assertIn(st, (200, 503))           # 503 = slot occupati, previsto
            if st == 200:
                self.assertTrue(out["anonymized_text"].startswith(f"Pratica numero {i}"))
                for cf in cfs:
                    self.assertNotIn(cf, out["anonymized_text"])
                self.assertEqual(sum(e["label"] == "CF" for e in out["entities"]), 1)


if __name__ == "__main__":
    unittest.main()
