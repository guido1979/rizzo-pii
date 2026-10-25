# -*- coding: utf-8 -*-
"""
Server WSGI della Rizzo PII Private API.

  GET  /health                  stato operativo (senza autenticazione, solo da reti ammesse)
  POST /api/v1/anonymize        PDF (multipart) -> PDF anonimizzato
  POST /api/v1/analyze          testo (JSON) o PDF (multipart) -> entita' e posizioni
  POST /api/v1/anonymize-text   testo (JSON) -> testo anonimizzato

Nient'altro e' esposto: l'UI originale (/, /preview, /doc/..., /settings,
/config, /port-check...) appartiene a un'altra applicazione Flask che questo
server non monta.

Statelessness:
  - gli upload multipart sono letti in BytesIO (`_InMemoryRequest`): Werkzeug di
    default riversa su file temporaneo ogni file oltre i 500 KB;
  - nessuna sessione, nessun cookie, nessuna cache: tutto cio' che riguarda il
    documento vive in variabili locali della richiesta;
  - i log contengono solo metadati (id richiesta, client, endpoint, status,
    durata, conteggi): mai body, nomi file, testo, entita' o messaggi di
    eccezione.
"""

import gc
import io
import logging
import threading
import time
import uuid

from flask import Flask, Request, g, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from . import API_VERSION
from . import engine as engine_mod
from .auth import RateLimiter, authenticate
from .errors import ApiError, Deadline, bad_request
from .options import parse_options
from .pdf_pipeline import process_pdf
from .settings import load_settings
from .text_pipeline import anonymize_text, mapping_of, public_entities

log = logging.getLogger("rizzo_api")

MAX_FORM_FIELD_BYTES = 64 * 1024
DOCUMENT_ENDPOINTS = ("/api/v1/",)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class _InMemoryRequest(Request):
    """Upload sempre in RAM: il limite lo mette MAX_CONTENT_LENGTH."""
    max_form_memory_size = MAX_FORM_FIELD_BYTES
    max_form_parts = 16

    def _get_file_stream(self, total_content_length, content_type, filename=None,
                         content_length=None):
        return io.BytesIO()


def _where(exc):
    """Classe dell'eccezione + punto del codice. Il MESSAGGIO no: puo' contenere
    dati del documento."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    loc = f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}" if tb else "?"
    return f"{type(exc).__name__} at {loc}"


def create_app(engine=None, settings=None):
    settings = settings or load_settings()
    logging.getLogger("rizzo_api").setLevel(settings.log_level)
    if engine is None:
        engine = engine_mod.load_core_engine()

    app = Flask("rizzo_api", static_folder=None)
    app.request_class = _InMemoryRequest
    app.config.update(MAX_CONTENT_LENGTH=settings.max_upload_bytes, JSON_SORT_KEYS=False)
    app.json.ensure_ascii = False

    slots = threading.BoundedSemaphore(settings.max_concurrent)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    known_tags = frozenset(engine.tag_names)
    state = {"ready": True}

    # ------------------------------------------------------------------ #
    # Ciclo di vita della richiesta
    # ------------------------------------------------------------------ #
    @app.before_request
    def _begin():
        g.request_id = uuid.uuid4().hex
        g.t0 = time.monotonic()
        g.client = "-"
        g.metrics = {}
        if not settings.source_allowed(request.remote_addr):
            raise ApiError(403, "forbidden", "Accesso non consentito.")
        if request.path.startswith(DOCUMENT_ENDPOINTS):
            g.client = authenticate(request.headers.get("Authorization"), settings.key_digests)
            limiter.check(g.client)

    @app.after_request
    def _finish(resp):
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        resp.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        resp.headers.pop("Set-Cookie", None)
        ms = int((time.monotonic() - getattr(g, "t0", time.monotonic())) * 1000)
        extra = " ".join(f"{k}={v}" for k, v in getattr(g, "metrics", {}).items())
        # la rotta, non il path: un path inesistente e' testo scelto dal client
        route = request.url_rule.rule if request.url_rule else "<unmatched>"
        log.info("request_id=%s client=%s method=%s path=%s status=%s ms=%d %s",
                 getattr(g, "request_id", "-"), getattr(g, "client", "-"), request.method,
                 route, resp.status_code, ms, extra)
        return resp

    def _error(status, code, message, headers=None):
        resp = jsonify({"error": {"code": code, "message": message,
                                  "request_id": getattr(g, "request_id", None)}})
        resp.status_code = status
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp

    @app.errorhandler(ApiError)
    def _api_error(e):
        return _error(e.status, e.code, e.message, e.headers)

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_e):
        return _error(413, "payload_too_large",
                      f"Richiesta oltre il limite di {settings.max_upload_bytes // (1024 * 1024)} MB.")

    @app.errorhandler(HTTPException)
    def _http(e):
        codes = {404: "not_found", 405: "method_not_allowed", 400: "bad_request",
                 415: "unsupported_media_type"}
        return _error(e.code, codes.get(e.code, "http_error"), "Richiesta non valida."
                      if e.code == 400 else (e.name or "Errore"))

    @app.errorhandler(Exception)
    def _unexpected(e):
        log.error("request_id=%s unhandled %s", getattr(g, "request_id", "-"), _where(e))
        return _error(500, "internal_error", "Errore interno durante l'elaborazione.")

    # ------------------------------------------------------------------ #
    # Utility per gli endpoint documentali
    # ------------------------------------------------------------------ #
    def _run(fn):
        """Slot di concorrenza + scadenza + rilascio della memoria, anche su errore."""
        if not slots.acquire(timeout=settings.queue_timeout):
            raise ApiError(503, "busy", "Servizio occupato: riprova tra poco.", {"Retry-After": "5"})
        engine_mod.set_deadline(time.monotonic() + settings.request_timeout)
        try:
            return fn()
        except Deadline:
            raise ApiError(504, "processing_timeout", "Tempo massimo di elaborazione superato.")
        finally:
            engine_mod.set_deadline(None)
            slots.release()
            gc.collect()

    def _require(kind):
        mt = request.mimetype
        if kind == "multipart" and mt != "multipart/form-data":
            raise ApiError(415, "unsupported_media_type", "Usa multipart/form-data.")
        if kind == "json" and mt != "application/json":
            raise ApiError(415, "unsupported_media_type", "Usa application/json.")

    def _multipart():
        """(bytes del PDF, opzioni grezze) dal multipart. Campi ammessi: file, options."""
        unknown = (set(request.form) - {"options"}) | (set(request.files) - {"file"})
        if unknown:
            raise bad_request("unknown_field", "Campi ammessi: 'file' e 'options'.")
        fs = request.files.get("file")
        if fs is None:
            raise bad_request("missing_file", "Campo 'file' mancante.")
        try:
            data = fs.stream.getvalue() if isinstance(fs.stream, io.BytesIO) else fs.read()
        finally:
            fs.close()
        g.metrics["bytes"] = len(data)
        return data, request.form.get("options")

    def _json_body():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise bad_request("invalid_json", "Il body deve essere un oggetto JSON.")
        unknown = set(body) - {"text", "options"}
        if unknown:
            raise bad_request("unknown_field", "Campi ammessi: 'text' e 'options'.")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise bad_request("empty_text", "Il campo 'text' e' obbligatorio e non puo' essere vuoto.")
        if len(text) > settings.max_text_chars:
            raise ApiError(413, "text_too_long", "Il testo supera il limite consentito.")
        g.metrics["chars"] = len(text)
        return text, body.get("options")

    def _check_mapping(opts):
        if opts.return_mapping and not settings.allow_return_mapping:
            raise ApiError(403, "mapping_not_allowed",
                           "La restituzione del dizionario e' disabilitata su questo servizio.")

    def _text_stats(res, ents, n_preserved):
        by_label = {}
        for e in ents:
            if not e.get("preserved"):
                by_label[e["label"]] = by_label.get(e["label"], 0) + 1
        return {"entities": len(ents), "unique": res["n_unique"], "preserved": n_preserved,
                "by_label": dict(sorted(by_label.items(), key=lambda kv: -kv[1]))}

    # ------------------------------------------------------------------ #
    # Endpoint
    # ------------------------------------------------------------------ #
    @app.route("/health", methods=["GET", "HEAD"])
    def health():
        ok = state["ready"]
        return jsonify({"status": "ok" if ok else "starting"}), (200 if ok else 503)

    @app.route("/api/v1/anonymize", methods=["POST"])
    def anonymize():
        _require("multipart")
        data, raw_opts = _multipart()
        opts = parse_options(raw_opts, known_tags, "pdf")
        if opts.return_mapping:
            raise bad_request("invalid_options", "'return_mapping' non e' disponibile quando la "
                              "risposta e' un PDF: usa /api/v1/analyze o /api/v1/anonymize-text.")

        def work():
            return process_pdf(data, opts, engine, settings, redact=True)

        try:
            result = _run(work)
        finally:
            del data
        st = result["stats"]
        g.metrics.update(pages=st["pages"], redactions=st["redactions"],
                         preserved=st["preserved"], residual=st["residual"])
        if st["residual"]:
            raise ApiError(422, "anonymization_incomplete",
                           "Alcuni valori riconosciuti risultano ancora leggibili nel PDF: "
                           "il documento non viene restituito.")
        if opts.strict and (st["skipped"] or st["pages_without_text"]):
            raise ApiError(422, "anonymization_incomplete",
                           "Livello 'strict': il documento contiene valori non localizzabili "
                           "o pagine senza testo estraibile.")
        resp = app.response_class(result["pdf"], mimetype="application/pdf")
        resp.headers["Content-Disposition"] = 'attachment; filename="documento_anonimizzato.pdf"'
        resp.headers.update({
            "X-PII-Entities": str(st["entities"]),
            "X-PII-Redactions": str(st["redactions"]),
            "X-PII-Preserved": str(st["preserved"]),
            "X-PII-Skipped": str(st["skipped"]),
            "X-PII-Pages": str(st["pages"]),
            "X-PII-Pages-Without-Text": str(st["pages_without_text"]),
            "X-PII-Letterhead-Regions": str(st["letterhead_regions"]),
        })
        return resp

    @app.route("/api/v1/analyze", methods=["POST"])
    def analyze():
        if request.mimetype == "multipart/form-data":
            data, raw_opts = _multipart()
            opts = parse_options(raw_opts, known_tags, "pdf")
            _check_mapping(opts)
            try:
                out = _run(lambda: process_pdf(data, opts, engine, settings, redact=False))
            finally:
                del data
            g.metrics.update(pages=out["pages"], entities=len(out["entities"]))
            return jsonify({"type": "pdf", **out})

        _require("json")
        text, raw_opts = _json_body()
        opts = parse_options(raw_opts, known_tags, "text")
        _check_mapping(opts)

        def work():
            res = engine.analyze(text, opts.exclude_tags)
            _anon, ents, n_pres = anonymize_text(text, res, opts)
            return res, ents, n_pres

        res, ents, n_pres = _run(work)
        g.metrics.update(entities=len(ents))
        return jsonify({"type": "text",
                        "entities": public_entities(ents, opts.return_mapping),
                        "stats": _text_stats(res, ents, n_pres)})

    @app.route("/api/v1/anonymize-text", methods=["POST"])
    def anonymize_text_route():
        _require("json")
        text, raw_opts = _json_body()
        opts = parse_options(raw_opts, known_tags, "text")
        _check_mapping(opts)

        def work():
            res = engine.analyze(text, opts.exclude_tags)
            return (res, *anonymize_text(text, res, opts))

        res, anon, ents, n_pres = _run(work)
        g.metrics.update(entities=len(ents), preserved=n_pres)
        body = {"anonymized_text": anon,
                "entities": public_entities(ents, opts.return_mapping),
                "stats": _text_stats(res, ents, n_pres)}
        if opts.return_mapping:
            body["mapping"] = mapping_of(ents)
        return jsonify(body)

    app.config["RIZZO_API_VERSION"] = API_VERSION
    return app
