# -*- coding: utf-8 -*-
"""Private API: anonimizzazione PDF e testo, autenticazione, validazione, errori.

Motore finto (tests/_api_support.py), PDF sintetici generati al volo. Senza
flask/pymupdf i test si saltano, come quelli che importano app.py.
"""

import io
import json
import unittest

from _api_support import (CF_PERSON, DEPS, HEAD_PERSON, KEY_A, KEY_B, ORG, STUDENT,
                          BODY_MARKER, letter_pdf, text_of)

if DEPS:
    import fitz
    from _api_support import ApiCase
else:                                               # pragma: no cover
    ApiCase = unittest.TestCase


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class AnonymizePdf(ApiCase):

    def test_pdf_anonimizzato_e_restituito_direttamente(self):
        r = self.post_pdf(letter_pdf())
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF-"))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["Pragma"], "no-cache")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertGreater(int(r.headers["X-PII-Redactions"]), 0)

    def test_nessun_dato_oscurato_nel_testo_estraibile(self):
        r = self.post_pdf(letter_pdf())
        txt = text_of(r.data)
        for v in (STUDENT, CF_PERSON, ORG, "giulietta.bramante@mail.example.it", "Via Larga 3"):
            self.assertNotIn(v, txt)
        self.assertIn("[FULLNAME_", txt)
        self.assertIn(BODY_MARKER, txt)          # il testo non-PII resta

    def test_header_senza_dati_personali(self):
        r = self.post_pdf(letter_pdf())
        joined = "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        for v in (STUDENT, CF_PERSON, "documento.pdf", "Bramante"):
            self.assertNotIn(v, joined)
        self.assertIn('filename="documento_anonimizzato.pdf"', r.headers["Content-Disposition"])

    def test_metadati_link_allegati_e_annotazioni_ripuliti(self):
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        page = doc[0]
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(60, 300, 300, 320),
                          "uri": "mailto:giulietta.bramante@mail.example.it"})
        page.add_text_annot((400, 400), f"Nota su {STUDENT}")
        doc.embfile_add("allegato.txt", f"dati di {STUDENT}".encode())
        doc.set_toc([[1, f"Fascicolo {STUDENT}", 1]])
        src = doc.tobytes()
        doc.close()
        r = self.post_pdf(src)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(STUDENT.encode(), r.data)            # nemmeno nei byte grezzi
        with fitz.open(stream=r.data, filetype="pdf") as out:
            self.assertEqual(out.embfile_count(), 0)
            self.assertEqual(out[0].get_links(), [])
            meta = " ".join(v or "" for v in out.metadata.values())
            self.assertNotIn(STUDENT, meta)
            for a in out[0].annots():
                self.assertNotIn(STUDENT, a.info.get("content", ""))
            self.assertTrue(all(STUDENT not in t[1] for t in out.get_toc()))

    def test_nessuna_revisione_incrementale(self):
        """Un PDF con revisioni incrementali conserva la versione precedente in coda
        al file: l'output deve essere riscritto da zero."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:        # solo per COSTRUIRE il fixture
            f = Path(d) / "rev.pdf"
            f.write_bytes(letter_pdf())
            doc = fitz.open(f)
            doc[0].insert_text((60, 700), "Revisione aggiunta", fontsize=9)
            doc.save(f, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            doc.close()
            src = f.read_bytes()
        self.assertGreaterEqual(src.count(b"%%EOF"), 2)
        r = self.post_pdf(src)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data.count(b"%%EOF"), 1)
        self.assertNotIn(STUDENT.encode(), r.data)

    def test_testo_fuori_dal_cropbox_viene_redatto(self):
        """Testo nel MediaBox ma fuori dal CropBox: invisibile a get_text(), ma nel file."""
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        doc[0].insert_text((60, 830), f"Nascosto: {STUDENT}", fontsize=9)
        doc[0].set_cropbox(fitz.Rect(0, 0, 595, 780))
        src = doc.tobytes()
        doc.close()
        r = self.post_pdf(src)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(STUDENT, text_of(r.data, full_mediabox=True))
        with fitz.open(stream=r.data, filetype="pdf") as out:
            self.assertEqual(fitz.Rect(out[0].cropbox), fitz.Rect(0, 0, 595, 780))

    def test_pdf_multipagina(self):
        r = self.post_pdf(letter_pdf(pages=4))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-PII-Pages"], "4")
        with fitz.open(stream=r.data, filetype="pdf") as out:
            self.assertEqual(out.page_count, 4)
            for p in out:
                self.assertNotIn(STUDENT, p.get_text())
                self.assertIn(BODY_MARKER, p.get_text())

    def test_conservazione_del_layout(self):
        src = letter_pdf(pages=2, rotate=0)
        r = self.post_pdf(src)
        with fitz.open(stream=src, filetype="pdf") as a, fitz.open(stream=r.data, filetype="pdf") as b:
            self.assertEqual(a.page_count, b.page_count)
            for pa, pb in zip(a, b):
                self.assertEqual(pa.rect, pb.rect)
                self.assertEqual(pa.rotation, pb.rotation)
                ra, rb = pa.search_for(BODY_MARKER), pb.search_for(BODY_MARKER)
                self.assertEqual(len(ra), 1)
                self.assertEqual(len(rb), 1)
                self.assertLess(abs(ra[0].x0 - rb[0].x0) + abs(ra[0].y0 - rb[0].y0), 0.5)
                self.assertEqual(len(pa.get_images()), len(pb.get_images()))   # logo intatto

    def test_pdf_ruotato(self):
        r = self.post_pdf(letter_pdf(rotate=90))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(STUDENT, text_of(r.data))
        with fitz.open(stream=r.data, filetype="pdf") as out:
            self.assertEqual(out[0].rotation, 90)

    def test_documento_senza_pii_restituito_ripulito(self):
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Circolare interna senza dati personali.")
        doc.set_metadata({"author": "Autore Sintetico"})
        r = self.post_pdf(doc.tobytes())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-PII-Redactions"], "0")
        with fitz.open(stream=r.data, filetype="pdf") as out:
            self.assertNotEqual(out.metadata.get("author"), "Autore Sintetico")

    def test_exclude_tags(self):
        r = self.post_pdf(letter_pdf(), {"exclude_tags": ["CF"]})
        self.assertEqual(r.status_code, 200)
        txt = text_of(r.data)
        self.assertIn(CF_PERSON, txt)
        self.assertNotIn(STUDENT, txt)

    def test_return_mapping_non_disponibile_per_il_pdf(self):
        r = self.post_pdf(letter_pdf(), {"return_mapping": True})
        self.assertError(r, 400, "invalid_options")


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class ContenutiNonSupportati(ApiCase):

    def test_pdf_scansionato_senza_testo(self):
        doc = fitz.open()
        page = doc.new_page()
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), 0)
        pix.clear_with(255)
        page.insert_image(page.rect, pixmap=pix)
        r = self.post_pdf(doc.tobytes())
        self.assertError(r, 422, "no_text_layer")

    def test_pagine_senza_testo_in_modalita_strict(self):
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        page = doc.new_page()
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), 0)
        pix.clear_with(200)
        page.insert_image(page.rect, pixmap=pix)
        src = doc.tobytes()
        r = self.post_pdf(src)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-PII-Pages-Without-Text"], "1")
        r = self.post_pdf(src, {"anonymization_level": "strict"})
        self.assertError(r, 422, "anonymization_incomplete")

    def test_pdf_protetto_da_password(self):
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        src = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="segreta", owner_pw="owner")
        self.assertError(self.post_pdf(src), 422, "encrypted_pdf")

    def test_pdf_non_valido(self):
        self.assertError(self.post_pdf(b"%PDF-1.7\nquesto non e' un pdf"), 400, "invalid_pdf")

    def test_file_che_non_e_un_pdf(self):
        self.assertError(self.post_pdf(b"\x89PNG\r\n\x1a\n" + b"0" * 100), 415, "unsupported_media_type")

    def test_file_vuoto(self):
        self.assertError(self.post_pdf(b""), 400, "empty_file")


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class LimitiDimensione(ApiCase):
    settings_kwargs = {"max_upload_bytes": 64 * 1024, "max_pages": 3, "max_text_chars": 5000}

    def test_pdf_troppo_grande(self):
        big = letter_pdf() + b"%" + b"0" * (70 * 1024)
        self.assertError(self.post_pdf(big), 413, "payload_too_large")

    def test_troppe_pagine(self):
        self.assertError(self.post_pdf(letter_pdf(pages=4)), 413, "too_many_pages")

    def test_testo_troppo_lungo(self):
        self.assertError(self.post_text("parola " * 1000), 413, "text_too_long")

    def test_json_troppo_grande(self):
        r = self.client.post("/api/v1/anonymize-text", headers=self.auth(),
                             data=json.dumps({"text": "x" * (80 * 1024)}),
                             content_type="application/json")
        self.assertError(r, 413, "payload_too_large")


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Autenticazione(ApiCase):

    def test_senza_api_key(self):
        for path in ("/api/v1/anonymize", "/api/v1/analyze", "/api/v1/anonymize-text"):
            r = self.client.post(path, json={"text": "x"})
            self.assertError(r, 401, "unauthorized")
            self.assertIn("Bearer", r.headers["WWW-Authenticate"])

    def test_api_key_errata(self):
        r = self.post_text(f"Ciao {STUDENT}", key=KEY_A[:-1] + "X")
        self.assertError(r, 401, "unauthorized")

    def test_schema_non_bearer(self):
        r = self.client.post("/api/v1/anonymize-text", json={"text": "x"},
                             headers={"Authorization": f"Basic {KEY_A}"})
        self.assertError(r, 401, "unauthorized")

    def test_piu_client_con_chiavi_distinte(self):
        self.assertEqual(self.post_text(f"Ciao {STUDENT}", key=KEY_A).status_code, 200)
        self.assertEqual(self.post_text(f"Ciao {STUDENT}", key=KEY_B).status_code, 200)

    def test_health_senza_autenticazione_e_senza_dettagli(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json, {"status": "ok"})

    def test_rete_non_ammessa(self):
        r = self.client.get("/health", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertError(r, 403, "forbidden")
        r = self.client.post("/api/v1/anonymize-text", json={"text": "x"}, headers=self.auth(),
                             environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertError(r, 403, "forbidden")

    def test_x_forwarded_for_ignorato(self):
        r = self.client.get("/health", headers={"X-Forwarded-For": "10.0.0.5"},
                            environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)

    def test_endpoint_originali_dell_ui_non_esposti(self):
        for path in ("/", "/preview", "/pdf", "/pdf/preview", "/settings", "/tags", "/config",
                     "/port-check", "/doc/abc/file.pdf", "/analyze", "/healthz"):
            r = self.client.get(path, headers=self.auth())
            self.assertEqual(r.status_code, 404, path)

    def test_niente_cors(self):
        r = self.client.options("/api/v1/anonymize-text", headers={
            "Origin": "https://example.com", "Access-Control-Request-Method": "POST"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class RateLimit(ApiCase):
    settings_kwargs = {"rate_limit_per_minute": 3}

    def test_limite_per_client(self):
        codes = [self.post_text("Testo neutro", key=KEY_A).status_code for _ in range(4)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)
        self.assertEqual(self.post_text("Testo neutro", key=KEY_B).status_code, 200)


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class ValidazioneRichieste(ApiCase):

    def test_content_type_errato(self):
        r = self.client.post("/api/v1/anonymize", headers=self.auth(), json={"text": "x"})
        self.assertError(r, 415, "unsupported_media_type")
        r = self.client.post("/api/v1/anonymize-text", headers=self.auth(), data="text=x",
                             content_type="application/x-www-form-urlencoded")
        self.assertError(r, 415, "unsupported_media_type")

    def test_campo_file_mancante(self):
        r = self.client.post("/api/v1/anonymize", headers=self.auth(), data={"options": "{}"},
                             content_type="multipart/form-data")
        self.assertError(r, 400, "missing_file")

    def test_campi_sconosciuti(self):
        r = self.client.post("/api/v1/anonymize", headers=self.auth(), content_type="multipart/form-data",
                             data={"file": (io.BytesIO(letter_pdf()), "a.pdf"), "extra": "1"})
        self.assertError(r, 400, "unknown_field")

    def test_opzioni_non_json(self):
        self.assertError(self.post_pdf(letter_pdf(), "{non json"), 400, "invalid_options")

    def test_opzione_sconosciuta(self):
        self.assertError(self.post_pdf(letter_pdf(), {"preserve_letterhed": True}), 400, "invalid_options")

    def test_tipi_errati(self):
        for bad in ({"preserve_letterhead": "yes"}, {"letterhead_mode": "magic"},
                    {"letterhead_pages": 2}, {"anonymization_level": "max"},
                    {"exclude_tags": ["NON_ESISTE"]},
                    {"preserve_letterhead": True, "letterhead_mode": "coordinates"},
                    {"preserve_regions": [{"page": 1, "x": 0, "y": 0, "width": 1, "height": 1}]},
                    {"preserve_letterhead": True, "letterhead_mode": "coordinates",
                     "preserve_regions": [{"page": 0, "x": 0, "y": 0, "width": 10, "height": 10}]},
                    {"preserve_letterhead": True, "letterhead_mode": "coordinates",
                     "preserve_regions": [{"page": 1, "x": 0, "y": 0, "width": -1, "height": 10}]},
                    {"preserve_letterhead": True, "letterhead_mode": "values",
                     "preserve_values": ["ab"]}):
            with self.subTest(bad=bad):
                r = self.post_pdf(letter_pdf(), bad)
                self.assertEqual(r.status_code, 400, r.json)
                self.assertIn(r.json["error"]["code"], ("invalid_options", "invalid_region"))

    def test_return_mapping_vietato_di_default(self):
        r = self.post_text(f"Ciao {STUDENT}", {"return_mapping": True})
        self.assertError(r, 403, "mapping_not_allowed")

    def test_testo_vuoto_o_mancante(self):
        self.assertError(self.post_text("   "), 400, "empty_text")
        r = self.client.post("/api/v1/anonymize-text", headers=self.auth(), json={"testo": "x"})
        self.assertError(r, 400, "unknown_field")
        r = self.client.post("/api/v1/anonymize-text", headers=self.auth(), json=["x"])
        self.assertError(r, 400, "invalid_json")


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class AnonymizeText(ApiCase):

    def test_testo_anonimizzato(self):
        text = f"La studentessa {STUDENT}, C.F. {CF_PERSON}, frequenta l'{ORG}."
        r = self.post_text(text)
        self.assertEqual(r.status_code, 200)
        out = r.json
        for v in (STUDENT, CF_PERSON, ORG):
            self.assertNotIn(v, out["anonymized_text"])
        self.assertIn("[FULLNAME_1]", out["anonymized_text"])
        self.assertNotIn("mapping", out)
        self.assertTrue(all("value" not in e for e in out["entities"]))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        # gli offset puntano al testo ORIGINALE
        e = next(e for e in out["entities"] if e["label"] == "FULLNAME")
        self.assertEqual(text[e["start"]:e["end"]], STUDENT)

    def test_preserve_values_nel_testo(self):
        text = f"{ORG} comunica che {STUDENT} e' iscritta."
        opts = {"preserve_letterhead": True, "letterhead_mode": "values",
                "preserve_values": [ORG], "preserve_values_scope": "document"}
        r = self.post_text(text, opts)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(r.json["anonymized_text"].startswith(ORG))
        self.assertNotIn(STUDENT, r.json["anonymized_text"])
        self.assertEqual(r.json["stats"]["preserved"], 1)

    def test_preserve_values_nel_testo_richiede_ambito_esplicito(self):
        opts = {"preserve_letterhead": True, "letterhead_mode": "values",
                "preserve_values": [ORG], "preserve_values_scope": "letterhead"}
        self.assertError(self.post_text(f"{ORG}", opts), 400, "invalid_options")

    def test_modalita_geometriche_non_valide_per_il_testo(self):
        opts = {"preserve_letterhead": True, "letterhead_mode": "automatic"}
        self.assertError(self.post_text(f"{ORG}", opts), 400, "invalid_options")


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class AnonymizeTextConMapping(ApiCase):
    settings_kwargs = {"allow_return_mapping": True}

    def test_mapping_solo_se_abilitato_e_richiesto(self):
        r = self.post_text(f"Ciao {STUDENT}", {"return_mapping": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["mapping"], {"[FULLNAME_1]": STUDENT})
        self.assertNotIn("mapping", self.post_text(f"Ciao {STUDENT}").json)


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Analyze(ApiCase):

    def test_analyze_testo(self):
        text = f"Firmato da {HEAD_PERSON} il giorno stabilito."
        r = self.post_text(text, path="/api/v1/analyze")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["type"], "text")
        e = r.json["entities"][0]
        self.assertEqual((e["label"], text[e["start"]:e["end"]]), ("FULLNAME", HEAD_PERSON))
        self.assertNotIn("value", e)

    def test_analyze_pdf_con_posizioni(self):
        r = self.post_pdf(letter_pdf(pages=2), path="/api/v1/analyze")
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["type"], "pdf")
        self.assertEqual(r.json["pages"], 2)
        names = [e for e in r.json["entities"] if e["label"] == "FULLNAME"]
        self.assertTrue(names)
        occ = names[0]["occurrences"]
        self.assertEqual({o["page"] for o in occ}, {1, 2})
        rect = occ[0]["rects"][0]
        self.assertTrue(set(rect) == {"x", "y", "width", "height"})
        self.assertNotIn(STUDENT, json.dumps(r.json))

    def test_analyze_non_conserva_nulla(self):
        """Due analyze consecutive: la seconda non vede niente della prima."""
        self.post_text(f"Primo testo con {STUDENT}", path="/api/v1/analyze")
        r = self.post_text("Secondo testo neutro.", path="/api/v1/analyze")
        self.assertEqual(r.json["entities"], [])


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class ErroriElaborazione(ApiCase):
    engine_kwargs = {"fail": True}

    def test_errore_interno_generico(self):
        with self.assertLogs("rizzo_api", level="ERROR") as logs:
            r = self.post_pdf(letter_pdf())
        self.assertError(r, 500, "internal_error")
        body = r.get_data(as_text=True)
        self.assertNotIn(STUDENT, body)
        self.assertNotIn("errore simulato", body)
        joined = "\n".join(logs.output)
        self.assertIn("ValueError", joined)
        self.assertNotIn(STUDENT, joined)
        self.assertNotIn("errore simulato", joined)


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Timeout(ApiCase):
    engine_kwargs = {"delay": 3.0}
    settings_kwargs = {"request_timeout": 5}

    def test_timeout_elaborazione(self):
        from rizzo_api import engine as eng
        import time
        orig = eng.set_deadline
        eng.set_deadline = lambda ts: orig(None if ts is None else time.monotonic() + 0.3)
        try:
            r = self.post_text(f"Ciao {STUDENT}")
        finally:
            eng.set_deadline = orig
        self.assertError(r, 504, "processing_timeout")


if __name__ == "__main__":
    unittest.main()
