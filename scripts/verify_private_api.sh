#!/usr/bin/env bash
# Verifica end-to-end della Private API su un container VERO (modello reale).
#
#   docker build -f Dockerfile.api -t rizzo-pii-api .
#   scripts/verify_private_api.sh [immagine]
#
# Cosa controlla (vedi docs/PRIVATE_API.md §11-12):
#   1. il container parte con root filesystem READ-ONLY, /tmp su tmpfs, utente non-root,
#      cap_drop ALL, e SENZA RETE (--network none): nessuna chiamata a Hugging Face a runtime;
#   2. su una rete privata --internal, un container client chiama l'API per hostname
#      (tests/test_api_e2e.py: PDF, testo, carta intestata, 401, concorrenza);
#   3. dopo le richieste: /tmp vuoto, nessuna modifica al filesystem del container
#      (docker diff), nessun dato sintetico dei test nei log;
#   4. nessuna porta pubblicata sull'host.
# Crea e poi rimuove: rete rizzo-pii-verify, container rizzo-pii-verify / -nonet.
set -euo pipefail

IMAGE="${1:-rizzo-pii-api}"
NET=rizzo-pii-verify
NAME=rizzo-pii-verify
KEY="verify:$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARDEN=(--read-only --tmpfs /tmp:size=512m,mode=1777,noexec,nosuid,nodev
        --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256
        --memory 6g --memory-swap 6g)

cleanup() { docker rm -f "$NAME" "$NAME-nonet" >/dev/null 2>&1 || true
            docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

wait_healthy() {
  for _ in $(seq 1 90); do
    s=$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || echo missing)
    [ "$s" = healthy ] && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$1")" = true ] || { docker logs "$1"; return 1; }
    sleep 2
  done
  docker logs "$1"; return 1
}

echo "== 1. avvio senza rete (nessun download a runtime)"
docker run -d --name "$NAME-nonet" --network none "${HARDEN[@]}" -e RIZZO_API_KEYS="$KEY" "$IMAGE" >/dev/null
wait_healthy "$NAME-nonet"
docker exec "$NAME-nonet" python -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:5005/api/v1/anonymize-text', method='POST',
    data=json.dumps({'text': 'Pratica di Giulietta Bramante.'}).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ${KEY#*:}'})
print('   anonymize-text offline:', json.load(urllib.request.urlopen(req, timeout=120))['anonymized_text'])"
docker rm -f "$NAME-nonet" >/dev/null

echo "== 2. rete privata --internal + client per hostname"
docker network create --driver bridge --internal "$NET" >/dev/null
docker run -d --name "$NAME" --network "$NET" --network-alias rizzo-pii-api "${HARDEN[@]}" \
  -e RIZZO_API_KEYS="$KEY" "$IMAGE" >/dev/null
wait_healthy "$NAME"
# `docker port` elenca solo i bind sull'host (EXPOSE da solo non ne crea)
[ -z "$(docker port "$NAME")" ] || { echo "porte pubblicate!"; docker port "$NAME"; exit 1; }
echo "   nessuna porta pubblicata, utente: $(docker exec "$NAME" id -u)"

# il client usa la stessa immagine (ha gia' pymupdf), con il codice dei test montato
docker run --rm --network "$NET" --read-only --tmpfs /tmp -v "$ROOT/tests:/t:ro" -w /t \
  -e RIZZO_API_E2E_URL=http://rizzo-pii-api:5005 -e RIZZO_API_E2E_KEY="${KEY#*:}" \
  -e PYTHONPATH=/t:/app/src -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python "$IMAGE" -W ignore -m unittest -v test_api_e2e

echo "== 3. nessuna traccia dopo le richieste"
left=$(docker exec "$NAME" find /tmp -mindepth 1 -type f)
[ -z "$left" ] || { echo "file in /tmp: $left"; exit 1; }
diff=$(docker diff "$NAME")
[ -z "$diff" ] || { echo "filesystem del container modificato:"; echo "$diff"; exit 1; }
if docker logs "$NAME" 2>&1 | grep -E -i "bramante|giulietta|BRMGLT|QRCTTV|${KEY#*:}"; then
  echo "dati nei log!"; exit 1
fi
echo "   /tmp vuoto, docker diff vuoto, log senza dati:"
docker logs "$NAME" 2>&1 | grep "request_id=" | tail -3 | sed 's/^/   /'
echo "OK"
