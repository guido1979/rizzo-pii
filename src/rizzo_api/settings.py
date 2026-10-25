# -*- coding: utf-8 -*-
"""
Configurazione del servizio, SOLO da variabili d'ambiente (o da file di secret
montati, per le API key). Nessun valore sensibile ha un default nel codice.

Le chiavi non vengono mai tenute in chiaro dopo il caricamento: si conserva lo
SHA-256 di ciascuna, e il confronto avviene digest contro digest con
`hmac.compare_digest` (vedi auth.py). `Settings.__repr__` non le mostra.

Variabili (tutte con prefisso RIZZO_API_):
  KEYS                 "client_a:chiave_a,client_b:chiave_b"
  KEYS_FILE            file con una coppia "client:chiave" per riga (Docker/Coolify secret)
  MAX_UPLOAD_MB        dimensione massima della richiesta (default 20)
  MAX_PAGES            pagine massime di un PDF (default 300)
  MAX_TEXT_CHARS       caratteri massimi di testo da analizzare (default 1.000.000)
  MAX_PDF_OBJECTS      oggetti massimi nella xref del PDF (default 200.000)
  REQUEST_TIMEOUT      secondi massimi di elaborazione per richiesta (default 300)
  MAX_CONCURRENT       elaborazioni documentali in parallelo (default 2)
  QUEUE_TIMEOUT        secondi di attesa di uno slot libero prima del 503 (default 15)
  RATE_LIMIT_PER_MINUTE  richieste/minuto per client, 0 = disattivato (default 60)
  ALLOWED_NETWORKS     CIDR ammessi come sorgente, separati da virgola
                       (default: reti private RFC1918 + loopback + ULA IPv6)
  ALLOW_RETURN_MAPPING "true" per permettere ai client di chiedere il dizionario
                       placeholder->valore nelle risposte JSON (default false)
  LOG_LEVEL            INFO (default) | WARNING | DEBUG — nessun livello logga contenuti
"""

import hashlib
import ipaddress
import os
from dataclasses import dataclass, field

ENV_PREFIX = "RIZZO_API_"

# Sorgenti ammesse di default: solo indirizzi privati. Una rete Docker bridge usa
# 172.16/12 o 10/8; il loopback serve all'healthcheck interno al container.
DEFAULT_ALLOWED_NETWORKS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "::1/128", "fc00::/7",
)

# Una chiave corta e' indovinabile: sotto questa soglia il servizio non parte.
MIN_KEY_LENGTH = 32


class ConfigError(RuntimeError):
    """Configurazione assente o non valida: il servizio NON deve partire."""


def _env(name, default=None, environ=None):
    environ = os.environ if environ is None else environ
    v = environ.get(ENV_PREFIX + name)
    return default if v is None or str(v).strip() == "" else str(v).strip()


def _int(name, default, minimum, maximum, environ):
    raw = _env(name, None, environ)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{ENV_PREFIX}{name} deve essere un intero")
    if not minimum <= v <= maximum:
        raise ConfigError(f"{ENV_PREFIX}{name} fuori intervallo [{minimum}, {maximum}]")
    return v


def _bool(name, default, environ):
    raw = _env(name, None, environ)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _key_digest(key):
    return hashlib.sha256(key.encode("utf-8")).digest()


def parse_key_entries(text, source):
    """Righe/elementi "client:chiave" -> {digest: client}. Le chiavi senza nome
    di client ricevono un nome progressivo (client-1, client-2...)."""
    out = {}
    entries = [e.strip() for chunk in text.splitlines() for e in chunk.split(",")]
    for i, entry in enumerate(e for e in entries if e and not e.startswith("#")):
        client, sep, key = entry.partition(":")
        if not sep:
            client, key = f"client-{i + 1}", entry
        client, key = client.strip(), key.strip()
        if not client or not all(c.isalnum() or c in "-_." for c in client):
            raise ConfigError(f"Nome client non valido in {source} (ammessi: lettere, cifre, - _ .)")
        if len(key) < MIN_KEY_LENGTH:
            raise ConfigError(f"Chiave del client '{client}' in {source} troppo corta "
                              f"(minimo {MIN_KEY_LENGTH} caratteri)")
        out[_key_digest(key)] = client
    return out


@dataclass(frozen=True)
class Settings:
    key_digests: dict = field(repr=False)          # {sha256(chiave): nome_client}
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pages: int = 300
    max_text_chars: int = 1_000_000
    max_pdf_objects: int = 200_000
    request_timeout: int = 300
    max_concurrent: int = 2
    queue_timeout: int = 15
    rate_limit_per_minute: int = 60
    # default = reti private: un Settings costruito senza questo campo NON deve
    # diventare un servizio aperto a qualunque sorgente
    allowed_networks: tuple = tuple(ipaddress.ip_network(n) for n in DEFAULT_ALLOWED_NETWORKS)
    allow_return_mapping: bool = False
    log_level: str = "INFO"

    @property
    def clients(self):
        return sorted(set(self.key_digests.values()))

    def source_allowed(self, addr):
        """True se l'indirizzo sorgente sta in una delle reti ammesse.
        Tupla vuota = nessun filtro: solo con RIZZO_API_ALLOWED_NETWORKS="*"."""
        if not self.allowed_networks:
            return True
        try:
            ip = ipaddress.ip_address((addr or "").split("%")[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.allowed_networks)


def load_settings(environ=None):
    """Settings dall'ambiente. Solleva ConfigError (fail-closed) se mancano le
    chiavi o un valore e' fuori intervallo."""
    environ = os.environ if environ is None else environ
    digests = {}
    raw_keys = _env("KEYS", None, environ)
    if raw_keys:
        digests.update(parse_key_entries(raw_keys, ENV_PREFIX + "KEYS"))
    keys_file = _env("KEYS_FILE", None, environ)
    if keys_file:
        try:
            with open(keys_file, encoding="utf-8") as f:
                digests.update(parse_key_entries(f.read(), ENV_PREFIX + "KEYS_FILE"))
        except OSError:
            raise ConfigError(f"Impossibile leggere {ENV_PREFIX}KEYS_FILE")
    if not digests:
        raise ConfigError(f"Nessuna API key configurata: imposta {ENV_PREFIX}KEYS "
                          f"oppure {ENV_PREFIX}KEYS_FILE")

    nets_raw = _env("ALLOWED_NETWORKS", ",".join(DEFAULT_ALLOWED_NETWORKS), environ)
    if nets_raw == "*":
        nets = ()
    else:
        try:
            nets = tuple(ipaddress.ip_network(n.strip(), strict=False)
                         for n in nets_raw.split(",") if n.strip())
        except ValueError:
            raise ConfigError(f"{ENV_PREFIX}ALLOWED_NETWORKS contiene un CIDR non valido")

    level = _env("LOG_LEVEL", "INFO", environ).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"{ENV_PREFIX}LOG_LEVEL non valido")

    return Settings(
        key_digests=digests,
        max_upload_bytes=_int("MAX_UPLOAD_MB", 20, 1, 200, environ) * 1024 * 1024,
        max_pages=_int("MAX_PAGES", 300, 1, 5000, environ),
        max_text_chars=_int("MAX_TEXT_CHARS", 1_000_000, 1_000, 20_000_000, environ),
        max_pdf_objects=_int("MAX_PDF_OBJECTS", 200_000, 1_000, 5_000_000, environ),
        request_timeout=_int("REQUEST_TIMEOUT", 300, 5, 3600, environ),
        max_concurrent=_int("MAX_CONCURRENT", 2, 1, 32, environ),
        queue_timeout=_int("QUEUE_TIMEOUT", 15, 0, 600, environ),
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 60, 0, 100_000, environ),
        allowed_networks=nets,
        allow_return_mapping=_bool("ALLOW_RETURN_MAPPING", False, environ),
        log_level=level,
    )
