# -*- coding: utf-8 -*-
"""Configurazione e autenticazione della Private API: solo libreria standard,
gira anche nella CI base (senza flask/pymupdf)."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rizzo_api.auth import RateLimiter, authenticate  # noqa: E402
from rizzo_api.errors import ApiError  # noqa: E402
from rizzo_api.settings import ConfigError, load_settings, parse_key_entries  # noqa: E402

K1 = "k" * 40
K2 = "z" * 40


class Settings(unittest.TestCase):

    def test_senza_chiavi_il_servizio_non_parte(self):
        with self.assertRaises(ConfigError):
            load_settings({})

    def test_chiave_troppo_corta(self):
        with self.assertRaises(ConfigError):
            load_settings({"RIZZO_API_KEYS": "app:corta"})

    def test_piu_client_e_file_di_secret(self):
        with tempfile.NamedTemporaryFile("w", suffix=".keys", delete=False) as f:
            f.write(f"# commento\nbackend-php:{K2}\n")
        try:
            s = load_settings({"RIZZO_API_KEYS": f"gestionale:{K1}", "RIZZO_API_KEYS_FILE": f.name})
        finally:
            Path(f.name).unlink()
        self.assertEqual(s.clients, ["backend-php", "gestionale"])

    def test_le_chiavi_non_restano_in_chiaro(self):
        s = load_settings({"RIZZO_API_KEYS": f"app:{K1}"})
        self.assertNotIn(K1, repr(s))
        self.assertNotIn(K1, str(s.key_digests))
        self.assertIn(hashlib.sha256(K1.encode()).digest(), s.key_digests)

    def test_valori_fuori_intervallo(self):
        for k, v in (("MAX_UPLOAD_MB", "0"), ("MAX_CONCURRENT", "abc"), ("ALLOWED_NETWORKS", "300.1.1.1/8"),
                     ("LOG_LEVEL", "TRACE")):
            with self.subTest(k=k), self.assertRaises(ConfigError):
                load_settings({"RIZZO_API_KEYS": f"app:{K1}", f"RIZZO_API_{k}": v})

    def test_reti_ammesse(self):
        s = load_settings({"RIZZO_API_KEYS": f"app:{K1}"})
        for ok in ("172.18.0.5", "10.1.2.3", "192.168.1.1", "127.0.0.1", "::1", "::ffff:172.18.0.5"):
            self.assertTrue(s.source_allowed(ok), ok)
        for ko in ("8.8.8.8", "203.0.113.1", "2001:db8::1", "", "nonsense"):
            self.assertFalse(s.source_allowed(ko), ko)
        s = load_settings({"RIZZO_API_KEYS": f"app:{K1}", "RIZZO_API_ALLOWED_NETWORKS": "*"})
        self.assertTrue(s.source_allowed("8.8.8.8"))


class Auth(unittest.TestCase):
    digests = parse_key_entries(f"alfa:{K1},beta:{K2}", "test")

    def test_chiave_valida(self):
        self.assertEqual(authenticate(f"Bearer {K1}", self.digests), "alfa")
        self.assertEqual(authenticate(f"bearer {K2}", self.digests), "beta")

    def test_chiave_mancante_o_errata(self):
        for h in (None, "", "Bearer", "Bearer ", f"Basic {K1}", f"Bearer {K1}x", f"Bearer {K1[:-1]}"):
            with self.subTest(h=h), self.assertRaises(ApiError) as cm:
                authenticate(h, self.digests)
            self.assertEqual(cm.exception.status, 401)

    def test_rate_limiter(self):
        rl = RateLimiter(2)
        rl.check("a")
        rl.check("a")
        with self.assertRaises(ApiError) as cm:
            rl.check("a")
        self.assertEqual(cm.exception.status, 429)
        rl.check("b")
        RateLimiter(0).check("a")        # 0 = disattivato


if __name__ == "__main__":
    unittest.main()
