# -*- coding: utf-8 -*-
"""Preserve Letterhead: modalita' automatica, coordinate, valori; pagine first/all.

Il criterio di fondo di ogni test: cio' che sta nella carta intestata resta in
chiaro SOLO se e' protetto esplicitamente o riconosciuto con sufficiente
affidabilita'; tutto il resto (corpo del documento, nomi di persona, codici
fiscali personali, omonimi fuori dalla regione) viene anonimizzato comunque.
"""

import unittest

from _api_support import (BODY_MARKER, CF_ENTE, CF_PERSON, DEPS, HEAD_PERSON, HEADER_LINES, ORG,
                          PIVA_ENTE, STUDENT, letter_pdf)

if DEPS:
    import fitz
    from _api_support import ApiCase
else:                                               # pragma: no cover
    ApiCase = unittest.TestCase

AUTO = {"preserve_letterhead": True, "letterhead_mode": "automatic"}


def page_texts(pdf):
    with fitz.open(stream=pdf, filetype="pdf") as d:
        return [p.get_text() for p in d]


def header_and_body(page_text):
    """(intestazione, corpo): il corpo comincia da "Spett.le"."""
    i = page_text.index("Spett.le")
    return page_text[:i], page_text[i:]


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Automatica(ApiCase):

    def test_carta_intestata_preservata_corpo_anonimizzato(self):
        r = self.post_pdf(letter_pdf(), AUTO)
        self.assertEqual(r.status_code, 200, r.data[:300])
        head, body = header_and_body(page_texts(r.data)[0])
        for line in HEADER_LINES:
            self.assertIn(line, head)
        self.assertIn(PIVA_ENTE, head)
        self.assertNotIn(STUDENT, body)
        self.assertNotIn(CF_PERSON, body)
        self.assertNotIn(ORG, body)          # omonimo nel corpo: anonimizzato
        self.assertGreater(int(r.headers["X-PII-Preserved"]), 0)
        self.assertGreater(int(r.headers["X-PII-Letterhead-Regions"]), 0)

    def test_nome_di_persona_nella_intestazione_anonimizzato(self):
        r = self.post_pdf(letter_pdf(head_person=True), AUTO)
        head, _ = header_and_body(page_texts(r.data)[0])
        self.assertNotIn(HEAD_PERSON, head)
        self.assertIn("Dirigente scolastico", head)
        self.assertIn(ORG, head)

    def test_cf_personale_nella_intestazione_anonimizzato(self):
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        doc[0].insert_text((110, 122), f"Referente C.F. {CF_PERSON}", fontsize=10)
        r = self.post_pdf(doc.tobytes(), AUTO)
        txt = page_texts(r.data)[0]
        self.assertNotIn(CF_PERSON, txt)
        self.assertIn(CF_ENTE, txt)          # il C.F. numerico dell'ente resta

    def test_senza_indicatori_niente_preservazione(self):
        """Parte alta della pagina senza segnali di carta intestata: si anonimizza."""
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((60, 50), ORG, fontsize=10)
        p.insert_text((60, 300), f"Comunicazione per {STUDENT}.", fontsize=11)
        r = self.post_pdf(doc.tobytes(), AUTO)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(ORG, page_texts(r.data)[0])
        self.assertEqual(r.headers["X-PII-Letterhead-Regions"], "0")

    def test_corpo_in_alto_su_pagina_successiva_non_preservato(self):
        """Pagina 2 senza intestazione: il corpo parte in alto e contiene indirizzo e
        telefono (due indicatori). Senza logo ne' righe ripetute non e' carta intestata."""
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        p = doc.new_page()
        p.insert_text((60, 50), "Recapito: Via Larga 3, Imola - Tel. 333 7654321", fontsize=10)
        p.insert_text((60, 64), "Studio della pratica in corso", fontsize=10)
        r = self.post_pdf(doc.tobytes(), {**AUTO, "letterhead_pages": "all"})
        p2 = page_texts(r.data)[1]
        self.assertNotIn("Via Larga 3", p2)
        self.assertNotIn("Imola", p2)
        self.assertNotIn("333 7654321", p2)

    def test_letterhead_pages_first(self):
        r = self.post_pdf(letter_pdf(pages=3), {**AUTO, "letterhead_pages": "first"})
        pages = page_texts(r.data)
        self.assertIn(ORG, header_and_body(pages[0])[0])
        for p in pages[1:]:
            self.assertNotIn(ORG, p)

    def test_letterhead_pages_all_intestazione_ripetuta(self):
        r = self.post_pdf(letter_pdf(pages=3), {**AUTO, "letterhead_pages": "all"})
        for p in page_texts(r.data):
            head, body = header_and_body(p)
            self.assertIn(ORG, head)
            self.assertNotIn(STUDENT, body)

    def test_all_con_intestazioni_diverse_o_assenti(self):
        """'all' non presume la stessa intestazione ovunque: pagina con intestazione
        diversa riconosciuta per conto suo, pagina senza intestazione anonimizzata."""
        doc = fitz.open(stream=letter_pdf(pages=1), filetype="pdf")
        p2 = doc.new_page()
        p2.insert_image((40, 30, 90, 80), pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), 0))
        p2.insert_text((110, 50), "Studio Tecnico Associato Ponente", fontsize=10)
        p2.insert_text((110, 64), "Viale dei Mille 7 - 16100 Genova - P.IVA " + PIVA_ENTE, fontsize=10)
        p2.insert_text((60, 250), f"Spett.le {STUDENT}", fontsize=11)
        p3 = doc.new_page()
        p3.insert_text((60, 60), f"Allegato riservato a {STUDENT}", fontsize=11)
        r = self.post_pdf(doc.tobytes(), {**AUTO, "letterhead_pages": "all"})
        pages = page_texts(r.data)
        self.assertIn("Studio Tecnico Associato Ponente", pages[1])
        self.assertNotIn(STUDENT, pages[1])
        self.assertNotIn(STUDENT, pages[2])

    def test_analyze_espone_regioni_e_segnali(self):
        r = self.post_pdf(letter_pdf(), AUTO, path="/api/v1/analyze")
        self.assertEqual(r.status_code, 200)
        regions = r.json["preserved_regions"]
        self.assertTrue(regions)
        self.assertTrue(all(reg["source"] == "automatic" and reg["page"] == 1 for reg in regions))
        bands = r.json["letterhead_detection"][0]["bands"]
        self.assertTrue(any(b["band"] == "header" and b["accepted"] for b in bands))
        stud = [e for e in r.json["entities"] if e["label"] == "FULLNAME"]
        self.assertTrue(all(not o["preserved"] for e in stud for o in e["occurrences"]))


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Coordinate(ApiCase):

    @staticmethod
    def opts(*regions):
        return {"preserve_letterhead": True, "letterhead_mode": "coordinates",
                "preserve_regions": list(regions)}

    def test_regione_protetta_e_anonimizzazione_fuori(self):
        r = self.post_pdf(letter_pdf(head_person=True),
                          self.opts({"page": 1, "x": 0, "y": 0, "width": 595, "height": 120}))
        self.assertEqual(r.status_code, 200, r.data[:300])
        head, body = header_and_body(page_texts(r.data)[0])
        self.assertIn(ORG, head)
        self.assertIn(HEAD_PERSON, head)      # coordinate: tutto cio' che e' dentro resta
        self.assertNotIn(ORG, body)
        self.assertNotIn(STUDENT, body)

    def test_regione_parziale_solo_le_righe_coperte(self):
        # copre solo le prime due righe dell'intestazione (y 38..66)
        r = self.post_pdf(letter_pdf(), self.opts({"page": 1, "x": 100, "y": 30, "width": 300, "height": 38}))
        head, _ = header_and_body(page_texts(r.data)[0])
        self.assertIn(ORG, head)
        self.assertNotIn(PIVA_ENTE, head)

    def test_valore_a_cavallo_del_bordo_viene_anonimizzato(self):
        # il bordo destro taglia a meta' il nome dell'ente: nel dubbio si redige
        r = self.post_pdf(letter_pdf(), self.opts({"page": 1, "x": 100, "y": 30, "width": 60, "height": 20}))
        self.assertNotIn(ORG, page_texts(r.data)[0])

    def test_regione_senza_pagina_vale_per_letterhead_pages(self):
        reg = {"x": 0, "y": 0, "width": 595, "height": 120}
        r = self.post_pdf(letter_pdf(pages=2), {**self.opts(reg), "letterhead_pages": "all"})
        self.assertTrue(all(ORG in header_and_body(p)[0] for p in page_texts(r.data)))
        r = self.post_pdf(letter_pdf(pages=2), {**self.opts(reg), "letterhead_pages": "first"})
        pages = page_texts(r.data)
        self.assertIn(ORG, pages[0])
        self.assertNotIn(ORG, pages[1])

    def test_pagina_inesistente(self):
        r = self.post_pdf(letter_pdf(), self.opts({"page": 5, "x": 0, "y": 0, "width": 10, "height": 10}))
        self.assertError(r, 400, "invalid_region")

    def test_regione_interamente_fuori_pagina(self):
        r = self.post_pdf(letter_pdf(), self.opts({"page": 1, "x": 700, "y": 0, "width": 100, "height": 100}))
        self.assertError(r, 400, "invalid_region")

    def test_regione_parzialmente_fuori_viene_ritagliata(self):
        r = self.post_pdf(letter_pdf(), self.opts({"page": 1, "x": -50, "y": -50, "width": 1000, "height": 170}))
        self.assertEqual(r.status_code, 200)
        self.assertIn(ORG, page_texts(r.data)[0])
        a = self.post_pdf(letter_pdf(), self.opts({"page": 1, "x": -50, "y": -50, "width": 1000, "height": 170}),
                          path="/api/v1/analyze").json["preserved_regions"][0]
        self.assertEqual((a["x"], a["y"], a["width"]), (0, 0, 595))

    def test_coordinate_su_pagina_ruotata(self):
        """Le coordinate si riferiscono alla pagina COME VISUALIZZATA. Ruotata di 90
        gradi, l'intestazione (in alto nella pagina non ruotata) appare sul lato destro."""
        src = letter_pdf(rotate=90)
        with fitz.open(stream=src, filetype="pdf") as d:
            p = d[0]
            hit = p.search_for(ORG)[0] * p.rotation_matrix   # dove la vede il lettore
            disp_w = p.rect.width
        self.assertGreater(hit.x0, disp_w / 2)
        reg = {"page": 1, "x": hit.x0 - 2, "y": 0, "width": disp_w - hit.x0 + 2, "height": 595}
        r = self.post_pdf(src, self.opts(reg))
        head, body = header_and_body(page_texts(r.data)[0])
        self.assertIn(ORG, head)
        self.assertNotIn(STUDENT, body)
        # la stessa regione "letta" come se la pagina non fosse ruotata non protegge nulla
        wrong = {"page": 1, "x": 0, "y": 0, "width": 595, "height": 120}
        r = self.post_pdf(src, self.opts(wrong))
        self.assertNotIn(ORG, page_texts(r.data)[0])

    def test_coordinate_su_tutte_le_rotazioni(self):
        """Per 0/90/180/270 gradi: la regione disegnata attorno a dove il LETTORE vede
        l'intestazione la protegge, e il corpo resta anonimizzato."""
        for rot in (0, 90, 180, 270):
            with self.subTest(rot=rot):
                src = letter_pdf(rotate=rot)
                with fitz.open(stream=src, filetype="pdf") as d:
                    p = d[0]
                    hit = fitz.Rect(p.search_for(ORG)[0] * p.rotation_matrix)
                    hit |= fitz.Rect(p.search_for(PIVA_ENTE)[0] * p.rotation_matrix)
                reg = {"page": 1, "x": hit.x0 - 3, "y": hit.y0 - 3,
                       "width": hit.width + 6, "height": hit.height + 6}
                r = self.post_pdf(src, self.opts(reg))
                txt = page_texts(r.data)[0]
                self.assertIn(ORG, txt)
                self.assertIn(PIVA_ENTE, txt)
                self.assertNotIn(STUDENT, txt)
                self.assertNotIn(CF_PERSON, txt)

    def test_cropbox_spostato(self):
        """Origine = angolo in alto a sinistra del CropBox, non del MediaBox."""
        doc = fitz.open(stream=letter_pdf(), filetype="pdf")
        doc[0].set_cropbox(fitz.Rect(20, 20, 575, 822))
        src = doc.tobytes()
        r = self.post_pdf(src, self.opts({"page": 1, "x": 0, "y": 0, "width": 555, "height": 100}))
        self.assertIn(ORG, page_texts(r.data)[0])
        with fitz.open(stream=r.data, filetype="pdf") as out:
            self.assertEqual(fitz.Rect(out[0].cropbox), fitz.Rect(20, 20, 575, 822))


@unittest.skipUnless(DEPS, "servono flask e pymupdf")
class Valori(ApiCase):

    @staticmethod
    def opts(values, **kw):
        return {"preserve_letterhead": True, "letterhead_mode": "values",
                "preserve_values": values, **kw}

    def test_valore_preservato_solo_nella_zona_intestazione(self):
        r = self.post_pdf(letter_pdf(), self.opts([ORG, "segreteria@vallesereno.example.it"]))
        self.assertEqual(r.status_code, 200, r.data[:300])
        head, body = header_and_body(page_texts(r.data)[0])
        self.assertIn(ORG, head)
        self.assertIn("segreteria@vallesereno.example.it", head)
        self.assertNotIn(ORG, body)           # omonimo nel corpo: anonimizzato
        self.assertNotIn(PIVA_ENTE, head)     # non elencato: anonimizzato

    def test_ambito_documento_esplicito(self):
        r = self.post_pdf(letter_pdf(), self.opts([ORG], preserve_values_scope="document"))
        _, body = header_and_body(page_texts(r.data)[0])
        self.assertIn(ORG, body)

    def test_valore_esatto_e_parola_intera(self):
        # "Istituto Comprensivo" non e' il valore completo: nessuna protezione parziale
        # dell'ente in una regione diversa, e niente match dentro altre parole
        r = self.post_pdf(letter_pdf(), self.opts(["Valle Seren"]))
        self.assertNotIn(ORG, page_texts(r.data)[0])

    def test_valori_combinati_con_modalita_automatica(self):
        # in automatico un nome di persona resta anonimizzato, a meno di un'esclusione esplicita
        r = self.post_pdf(letter_pdf(head_person=True), {**AUTO, "preserve_values": [HEAD_PERSON]})
        head, body = header_and_body(page_texts(r.data)[0])
        self.assertIn(HEAD_PERSON, head)
        self.assertIn(BODY_MARKER, body)


if __name__ == "__main__":
    unittest.main()
