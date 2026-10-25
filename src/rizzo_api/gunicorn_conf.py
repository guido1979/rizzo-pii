# -*- coding: utf-8 -*-
"""
Configurazione gunicorn della Private API.

- 1 worker di default: il modello (~1,2 GB) sta in memoria una volta per
  processo; ogni worker in piu' e' una copia intera. Il parallelismo lo danno i
  thread (gthread), limitati dal semaforo RIZZO_API_MAX_CONCURRENT dell'app.
- access log DISATTIVATO: il log delle richieste lo scrive l'app, con i soli
  metadati (vedi server.py). Gunicorn non logga mai i body.
- worker_tmp_dir in /dev/shm (RAM): il file di heartbeat del worker non
  finisce su disco, e con il root filesystem in sola lettura non ci sarebbe
  comunque un altro posto scrivibile.
- X-Forwarded-For non viene mai usato: l'allowlist di rete dell'app guarda
  REMOTE_ADDR, cioe' l'indirizzo reale del peer TCP.
"""

import os

_port = int(os.environ.get("RIZZO_API_PORT", "5005"))
_concurrent = int(os.environ.get("RIZZO_API_MAX_CONCURRENT", "2"))
_timeout = int(os.environ.get("RIZZO_API_REQUEST_TIMEOUT", "300"))

bind = f"0.0.0.0:{_port}"
workers = int(os.environ.get("RIZZO_API_WORKERS", "1"))
worker_class = "gthread"
threads = _concurrent + 2            # +2: /health e i 503 rispondono anche a pieno carico
timeout = _timeout + 60              # backstop: lo stop cooperativo scatta prima
graceful_timeout = 30
keepalive = 5
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
preload_app = False

accesslog = None
errorlog = "-"
loglevel = os.environ.get("RIZZO_API_GUNICORN_LOG_LEVEL", "warning")
capture_output = False

limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190
