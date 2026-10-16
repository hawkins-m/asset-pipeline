#!/usr/bin/env bash
# comfy-api-proxy: serves the Comfy API v2 (what the Comfy SDK talks to) in front of a local
# ComfyUI. Its own venv; it talks to ComfyUI over HTTP only and never touches its install.
# Usage: scripts/comfy_api_proxy.sh install | start | stop | status
#   COMFYUI_URL (default http://127.0.0.1:8188), PROXY_PORT (default 8190: 8188 is ComfyUI,
#   8189 is a second ComfyUI here and the proxy's own default).
set -euo pipefail
AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"
ENV="$AP_ROOT/envs/comfy-api-proxy"
VERSION="${PROXY_VERSION:-0.1.6}"
COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
PROXY_PORT="${PROXY_PORT:-8190}"
PYTHON="${PYTHON:-python3.12}"

case "${1:-}" in
  install)
    [ -x "$ENV/bin/python" ] || "$PYTHON" -m venv "$ENV"
    "$ENV/bin/python" -m pip install -U pip
    "$ENV/bin/python" -m pip install "comfy-api-proxy==$VERSION"
    "$ENV/bin/comfy-api-proxy" --help >/dev/null && echo "installed comfy-api-proxy $VERSION in $ENV" ;;
  start)
    [ "$PROXY_PORT" != 8188 ] && [ "$PROXY_PORT" != 8189 ] || { echo "port $PROXY_PORT is a ComfyUI port"; exit 1; }
    "$ENV/bin/comfy-api-proxy" start --comfyui "$COMFYUI_URL" --host 127.0.0.1 --port "$PROXY_PORT"
    echo "AP_COMFY_SDK_URL=http://127.0.0.1:$PROXY_PORT" ;;
  stop|status)
    "$ENV/bin/comfy-api-proxy" "$1" ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
