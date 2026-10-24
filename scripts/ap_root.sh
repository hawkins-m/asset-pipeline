# Source this to set AP_ROOT:  source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap_root.sh"
# Order: env AP_ROOT > [paths] ap_root in the user config (~/.config/terraformer/config.toml)
# > ~/.local/share/terraformer. Mirrors asset_pipeline/config.ap_root() through
# scripts/ap_config.py (needs python3 >= 3.11).
if [ -z "${AP_ROOT:-}" ]; then
  AP_ROOT="$(python3 "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap_config.py" paths.ap_root 2>/dev/null || true)"
  AP_ROOT="${AP_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/terraformer}"
fi
export AP_ROOT
