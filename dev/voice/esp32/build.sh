#!/usr/bin/env bash
# Build/flash wrapper: applies sdkconfig.defaults + sdkconfig.secrets.defaults.
# Usage: ./build.sh build | ./build.sh flash monitor | ...
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f sdkconfig.secrets.defaults ]; then
    echo "sdkconfig.secrets.defaults missing — run ./gen_secrets.py first" >&2
    exit 1
fi

# IDF only applies *.defaults when generating sdkconfig from scratch — a
# stale sdkconfig would silently keep old WiFi credentials / token.
if [ -f sdkconfig ] && { [ sdkconfig.secrets.defaults -nt sdkconfig ] ||
                         [ sdkconfig.defaults -nt sdkconfig ]; }; then
    echo "defaults changed — regenerating sdkconfig"
    rm -f sdkconfig
fi

# shellcheck disable=SC1090
source "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" >/dev/null

exec idf.py -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.secrets.defaults" "$@"
