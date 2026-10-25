# -*- coding: utf-8 -*-
"""
Errori dell'API: un codice stabile (per i client), un messaggio GENERICO e lo
status HTTP. Nessun messaggio contiene dati del documento o testo delle
eccezioni interne: il testo di un'eccezione puo' riportare frammenti del
contenuto (es. un ValueError con il valore che non si e' riusciti a leggere).

Formato della risposta:
  {"error": {"code": "invalid_pdf", "message": "...", "request_id": "..."}}
"""


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.headers = headers or {}


def bad_request(code, message):
    return ApiError(400, code, message)


def unprocessable(code, message):
    return ApiError(422, code, message)


class Deadline(Exception):
    """Tempo massimo di elaborazione superato (controllo cooperativo)."""
