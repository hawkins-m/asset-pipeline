#!/usr/bin/env bash
# One-time, no-sudo setup of the HIP build toolchain under $AP_ROOT/tools.
# See scripts/rocm_build_env.sh for why.
set -euo pipefail
AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"

# 1) Shim ROCM_HOME: everything from /opt/rocm, plus bin/hipcc -> Ubuntu hipcc driver.
S="$AP_ROOT/tools/rocm-shim"
mkdir -p "$S/bin"
for e in /opt/rocm/*; do [ "$(basename "$e")" = bin ] || ln -sfn "$e" "$S/"; done
for e in /opt/rocm/bin/*; do ln -sfn "$e" "$S/bin/"; done
ln -sfn /usr/bin/hipcc "$S/bin/hipcc"

# 2) libxml2.so.2 (+ ICU 74) from Ubuntu 24.04 for AMD's lld.
C="$AP_ROOT/tools/compat-lib"
if [ ! -e "$C/libxml2.so.2" ]; then
  mkdir -p "$C"
  tmp=$(mktemp -d)
  base=http://archive.ubuntu.com/ubuntu/pool/main
  curl -sfo "$tmp/xml.deb" "$base/libx/libxml2/libxml2_2.9.14+dfsg-1.3ubuntu3_amd64.deb"
  curl -sfo "$tmp/icu.deb" "$base/i/icu/libicu74_74.2-1ubuntu3_amd64.deb"
  for d in "$tmp"/*.deb; do dpkg-deb -x "$d" "$tmp/x"; done
  cp -a "$tmp"/x/usr/lib/x86_64-linux-gnu/{libxml2.so.2*,libicu*.so.74*} "$C/"
  rm -rf "$tmp"
fi

LD_LIBRARY_PATH="$C" /opt/rocm/llvm/bin/ld.lld --version
