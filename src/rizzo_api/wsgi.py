# -*- coding: utf-8 -*-
"""
Entry point WSGI per gunicorn:  gunicorn -c src/rizzo_api/gunicorn_conf.py rizzo_api.wsgi:application

Il modello viene caricato qui, una volta per processo worker. Se la
configurazione manca o non e' valida (es. nessuna API key) l'import fallisce,
il worker non parte e il container si ferma: meglio un servizio giu' che un
servizio aperto.
"""

import logging
import sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Le librerie terze non loggano contenuti, ma il loro livello DEBUG non serve qui.
for noisy in ("transformers", "urllib3", "werkzeug", "PIL"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from rizzo_api.server import create_app  # noqa: E402

application = create_app()
