# -*- coding: utf-8 -*-
"""Private API: niente persistenza, niente contaminazione tra richieste, log puliti.

Persistenza: durante le richieste (riuscite, fallite, interrotte) si intercettano
TUTTE le aperture di file in scrittura e le funzioni di `tempfile`, e si
confronta il contenuto delle directory scrivibili prima/dopo. In produzione la
garanzia e' piu' forte (root filesystem in sola lettura, /tmp su tmpfs): vedi
la verifica sul container in docs/PRIVATE_API.md.
"""

import builtins
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _api_support import CF_PERSON, DEPS, KEY_A, ROOT, STUDENT, letter_pdf

if DEPS:
    from _api_support import ApiCase
else:                                               # pragma: no cover
    ApiCase = unittest.TestCase

SENTINELS = (STUDENT, "Bramante", CF_PERSON, "giulietta.bramante")


class _WriteSpy:
    """Registra ogni apertura in scrittura e ogni uso di tempfile."""

    def __init__(self):
        self.events = []
        self._patches = []

    def __enter__(self):
        real_open, real_os_open = builtins.open, os.open

        def spy_open(file, mode="r", *a, **kw):
            if any(c in mode for c in "wax+"):
                self.events.append(("open", str(file), mode))
            return real_open(file, mode, *a, **kw)

        def spy_os_open(path, flags, *a, **kw):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                self.events.append(("os.open", str(path), flags))
            return real_os_open(path, flags, *a, **kw)

        def tmp(name):
            real = getattr(tempfile, name)

            def f(*a, **kw):
                self.events.append(("tempfile", name, ""))
                return real(*a, **kw)
            return f

        self._patches = [mock.patch("builtins.open", spy_open), mock.patch("io.open", spy_open),
                         mock.patch("os.open", spy_os_open)]
        self._patches += [mock.patch(f"tempfile.{n}", tmp(n)) for n in
                          ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile",
                           "SpooledTemporaryFile", "TemporaryDirectory")]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


def _snapshot(dirs):
    out = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if "__pycache__" in p.parts or ".git" in p.parts:
                continue
            try:
                out[str(p)] = p.stat().st_mtime_ns
            except OSError:
                pass
    return out


WATCHED = [Path(tempfile.gettempdir()), Path.cwd(), ROOT / "src", ROOT / "tests",
           Path.home() / ".local" / "share" / "rizzo-pii"]


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class NessunaPersistenza(ApiCase):

    def _run_case(self, fn):
        before = _snapshot(WATCHED)
        with _WriteSpy() as spy:
            fn()
        after = _snapshot(WATCHED)
        self.assertEqual(spy.events, [], "scritture su file durante la richiesta")
        new_or_changed = {k for k in after if before.get(k) != after[k]}
        self.assertEqual(new_or_changed, set())

    def _raw_multipart(self, pdf, boundary="frontiera"):
        """Body multipart gia' codificato. Il test client di Werkzeug, se gli si passa
        un dict, CODIFICA lui il multipart e oltre 500 KB lo fa su file temporaneo:
        sarebbe una scrittura del client di test, non del server."""
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"x.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
        body += pdf + f"\r\n--{boundary}--\r\n".encode()
        return body, f"multipart/form-data; boundary={boundary}"

    def test_richiesta_riuscita(self):
        # oltre la soglia (500 KB) oltre la quale Werkzeug, di suo, spoolerebbe su disco
        body, ctype = self._raw_multipart(letter_pdf(pages=3) + b"%" + b"0" * (600 * 1024))
        self.assertGreater(len(body), 600 * 1024)

        def go():
            r = self.client.post("/api/v1/anonymize", headers=self.auth(), content_type=ctype,
                                 input_stream=io.BytesIO(body), content_length=len(body))
            self.assertEqual(r.status_code, 200, r.data[:200])
            r = self.post_text(f"Ciao {STUDENT}")
            self.assertEqual(r.status_code, 200)
        self._run_case(go)

    def test_controllo_lo_spy_vede_lo_spool_di_werkzeug(self):
        """Controllo negativo: con la Request standard di Flask lo stesso upload FINISCE
        su file temporaneo e lo spy lo vede. Senza questo, il test sopra potrebbe
        passare solo perche' lo spy e' cieco."""
        import flask
        body, ctype = self._raw_multipart(letter_pdf() + b"%" + b"0" * (600 * 1024))
        self.app.request_class = flask.Request
        try:
            with _WriteSpy() as spy:
                self.client.post("/api/v1/anonymize", headers=self.auth(), content_type=ctype,
                                 input_stream=io.BytesIO(body), content_length=len(body))
        finally:
            from rizzo_api.server import _InMemoryRequest
            self.app.request_class = _InMemoryRequest
        self.assertTrue(spy.events, "lo spy non ha visto lo spool su disco di Werkzeug")

    def test_richiesta_fallita(self):
        def go():
            self.assertEqual(self.post_pdf(b"%PDF-1.4 rotto").status_code, 400)
            self.engine.fail = True
            self.assertEqual(self.post_pdf(letter_pdf()).status_code, 500)
        self._run_case(go)

    def test_richiesta_interrotta(self):
        """Il client chiude la connessione a meta' upload."""
        boundary = "frontiera"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"x.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
        body += letter_pdf()[:2000]

        def go():
            r = self.client.post("/api/v1/anonymize", headers=self.auth(),
                                 input_stream=io.BytesIO(body), content_length=len(body) + 50_000,
                                 content_type=f"multipart/form-data; boundary={boundary}")
            self.assertIn(r.status_code, (400, 413))
        self._run_case(go)

    def test_upload_grande_resta_in_memoria(self):
        from rizzo_api.server import _InMemoryRequest
        s = _InMemoryRequest.from_values()._get_file_stream(10 * 1024 * 1024, "application/pdf", "a.pdf")
        self.assertIsInstance(s, io.BytesIO)

    def test_risorse_rilasciate_dopo_la_richiesta(self):
        """Nessun documento PyMuPDF aperto e nessun buffer di upload vivo dopo la
        risposta, ne' dopo un errore."""
        import gc
        import fitz
        marker = b"%MARCATORE-UPLOAD-UNIVOCO"
        for fail in (False, True):
            self.engine.fail = fail
            r = self.post_pdf(letter_pdf() + marker)
            self.assertEqual(r.status_code, 500 if fail else 200)
            del r
            gc.collect()
            docs = [o for o in gc.get_objects() if isinstance(o, fitz.Document) and not o.is_closed]
            self.assertEqual(docs, [])
            bufs = [o for o in gc.get_objects()
                    if isinstance(o, io.BytesIO) and not o.closed and marker in o.getbuffer().tobytes()]
            self.assertEqual(bufs, [])


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class LogSenzaContenuti(ApiCase):
    settings_kwargs = {"allow_return_mapping": True}

    def test_log_non_contengono_dati(self):
        records = []

        class H(logging.Handler):
            def emit(self, rec):
                records.append(self.format(rec))

        h = H(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        logging.getLogger("rizzo_api").setLevel(logging.DEBUG)
        try:
            pdf = letter_pdf()
            self.post_pdf(pdf)
            self.post_pdf(pdf, path="/api/v1/analyze")
            self.post_pdf(pdf, {"preserve_letterhead": True})
            self.post_text(f"Ciao {STUDENT}, C.F. {CF_PERSON}", {"return_mapping": True})
            self.post_pdf(pdf, {"opzione_sbagliata": STUDENT})
            self.client.get(f"/api/v1/{STUDENT}", headers=self.auth())     # 404 col nome nel path
            self.engine.fail = True
            self.post_pdf(pdf)
            self.post_text(f"Ciao {STUDENT}")
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        joined = "\n".join(records)
        self.assertIn("path=/api/v1/anonymize", joined)        # i log ci sono...
        for s in SENTINELS + (KEY_A, "documento.pdf"):
            self.assertNotIn(s, joined)                        # ...ma senza contenuti

    def test_chiave_mai_nei_log_ne_nelle_risposte(self):
        r = self.post_text("x", key=KEY_A + "zz")
        self.assertNotIn(KEY_A, r.get_data(as_text=True))
        self.assertNotIn(KEY_A, repr(self.settings))


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Concorrenza(ApiCase):
    N = 8
    NAMES = tuple(f"Persona{c} Sintetica{c}" for c in "ABCDEFGH")
    engine_kwargs = {"entities": tuple((n, "FULLNAME") for n in NAMES), "delay": 0.2}
    settings_kwargs = {"max_concurrent": 8, "queue_timeout": 30, "allow_return_mapping": True}

    def _pdf_for(self, name, marker):
        import fitz
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((60, 100), f"Documento {marker}", fontsize=11)
        p.insert_text((60, 130), f"Intestatario {name}", fontsize=11)
        return doc.tobytes()

    def test_richieste_concorrenti_isolate(self):
        results, errors = {}, []

        def worker(i):
            try:
                c = self.app.test_client()
                name, marker = self.NAMES[i], f"M{i:03d}"
                if i % 2:
                    r = c.post("/api/v1/anonymize-text", headers=self.auth(),
                               json={"text": f"{marker}: {name}", "options": {"return_mapping": True}})
                    results[i] = ("text", r.status_code, r.get_json())
                else:
                    r = self.post_pdf(self._pdf_for(name, marker), client=c)
                    results[i] = ("pdf", r.status_code, r.data)
            except Exception as e:                      # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(self.N)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLess(time.monotonic() - t0, 0.2 * self.N)   # davvero in parallelo
        from _api_support import text_of
        for i, (kind, status, payload) in results.items():
            self.assertEqual(status, 200)
            others = [n for j, n in enumerate(self.NAMES) if j != i]
            if kind == "text":
                self.assertEqual(payload["anonymized_text"], f"M{i:03d}: [FULLNAME_1]")
                self.assertEqual(payload["mapping"], {"[FULLNAME_1]": self.NAMES[i]})
                blob = json.dumps(payload)
            else:
                blob = text_of(payload)
                self.assertIn(f"M{i:03d}", blob)
                self.assertNotIn(self.NAMES[i], blob)
            for n in others:
                self.assertNotIn(n, blob)

    def test_opzioni_di_una_richiesta_non_valgono_per_le_altre(self):
        out = {}

        def worker(i):
            c = self.app.test_client()
            opts = {"exclude_tags": ["FULLNAME"]} if i == 0 else {}
            r = c.post("/api/v1/anonymize-text", headers=self.auth(),
                       json={"text": f"Nome {self.NAMES[i]}", "options": opts})
            out[i] = r.get_json()["anonymized_text"]

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(out[0], f"Nome {self.NAMES[0]}")
        for i in (1, 2, 3):
            self.assertEqual(out[i], "Nome [FULLNAME_1]")


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Occupato(ApiCase):
    engine_kwargs = {"delay": 1.0}
    settings_kwargs = {"max_concurrent": 1, "queue_timeout": 0}

    def test_503_quando_tutti_gli_slot_sono_occupati(self):
        first = {}
        t = threading.Thread(target=lambda: first.setdefault(
            "r", self.app.test_client().post("/api/v1/anonymize-text", headers=self.auth(),
                                             json={"text": "uno"}).status_code))
        t.start()
        time.sleep(0.3)
        r = self.post_text("due")
        t.join()
        self.assertError(r, 503, "busy")
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(first["r"], 200)
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
