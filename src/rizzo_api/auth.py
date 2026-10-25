# -*- coding: utf-8 -*-
"""
Autenticazione `Authorization: Bearer <API_KEY>` + rate limit per client.

Il token ricevuto viene ridotto a SHA-256 e confrontato con `hmac.compare_digest`
contro TUTTI i digest configurati, senza uscire al primo match: il tempo di
risposta non dipende ne' dalla lunghezza della chiave ne' da quale client la
possiede. Il token non viene mai loggato; nei log compare solo il nome del client.
"""

import hashlib
import hmac
import threading
import time

from .errors import ApiError

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Bearer realm="rizzo-pii-api"'}


def authenticate(header_value, key_digests):
    """Nome del client autenticato. Solleva ApiError(401) altrimenti."""
    scheme, _, token = (header_value or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "unauthorized", "Autenticazione richiesta.", _UNAUTHORIZED_HEADERS)
    digest = hashlib.sha256(token.encode("utf-8", "replace")).digest()
    client = None
    for known, name in key_digests.items():
        if hmac.compare_digest(digest, known):
            client = name
    if client is None:
        raise ApiError(401, "unauthorized", "Credenziali non valide.", _UNAUTHORIZED_HEADERS)
    return client


class RateLimiter:
    """Token bucket per client, in memoria del processo (niente servizi esterni).
    Con piu' worker gunicorn il limite vale per worker: e' documentato."""

    def __init__(self, per_minute):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets = {}
        self._lock = threading.Lock()

    def check(self, client):
        if self.capacity <= 0:
            return
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(client, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[client] = (tokens, now)
                retry = max(1, int((1.0 - tokens) / self.rate) + 1)
                raise ApiError(429, "rate_limited", "Troppe richieste: riprova piu' tardi.",
                               {"Retry-After": str(retry)})
            self._buckets[client] = (tokens - 1.0, now)
