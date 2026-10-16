# Source this to set AP_ROOT:  source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap_root.sh"
# Order: env AP_ROOT > [paths] ap_root in config/backends.toml (repo file; the machine-local
# toml lives inside AP_ROOT, so it can't set this) > /mnt/storage/asset-pipeline.
# Mirrors asset_pipeline/config.ap_root(). Needs python3 >= 3.11 (tomllib) for the config
# lookup; without it the default is used.
if [ -z "${AP_ROOT:-}" ]; then
  _ap_repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
  AP_ROOT="$(python3 -c 'import sys, tomllib
print(tomllib.load(open(sys.argv[1], "rb")).get("paths", {}).get("ap_root", ""))' \
    "$_ap_repo/config/backends.toml" 2>/dev/null || true)"
  AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"
  unset _ap_repo
fi
export AP_ROOT
