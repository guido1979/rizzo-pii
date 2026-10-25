#!/usr/bin/env python3
"""Client di esempio della Rizzo PII Private API (Python 3.9+, `pip install requests`).

  RIZZO_PII_URL=http://rizzo-pii-api:5005 RIZZO_PII_API_KEY=... \
    python client.py ingresso.pdf uscita.pdf
"""
import json
import os
import sys

import requests

BASE = os.environ.get("RIZZO_PII_URL", "http://rizzo-pii-api:5005").rstrip("/")
KEY = os.environ["RIZZO_PII_API_KEY"]          # mai nel codice: variabile d'ambiente / secret
HEADERS = {"Authorization": f"Bearer {KEY}"}
TIMEOUT = (5, 330)                             # (connessione, lettura) >= RIZZO_API_REQUEST_TIMEOUT


def anonymize_pdf(pdf_bytes, options):
    r = requests.post(f"{BASE}/api/v1/anonymize", headers=HEADERS, timeout=TIMEOUT,
                      files={"file": ("documento.pdf", pdf_bytes, "application/pdf")},
                      data={"options": json.dumps(options)})
    if r.status_code != 200:
        err = r.json()["error"]                # {"code", "message", "request_id"}
        raise RuntimeError(f"{r.status_code} {err['code']}: {err['message']} ({err['request_id']})")
    stats = {k: v for k, v in r.headers.items() if k.startswith("X-PII-")}
    return r.content, stats


def anonymize_text(text, options=None):
    r = requests.post(f"{BASE}/api/v1/anonymize-text", headers=HEADERS, timeout=TIMEOUT,
                      json={"text": text, "options": options or {}})
    r.raise_for_status()
    return r.json()["anonymized_text"]


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "rb") as f:
        pdf = f.read()
    out, stats = anonymize_pdf(pdf, {"preserve_letterhead": True, "letterhead_mode": "automatic",
                                     "letterhead_pages": "all"})
    with open(dst, "wb") as f:
        f.write(out)
    print(stats)
    print(anonymize_text("Pratica di Giulietta Bramante, nata a Ravenna."))
