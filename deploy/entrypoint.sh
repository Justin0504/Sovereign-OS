#!/bin/sh
# Fix the mounted volume's ownership, then drop privileges before exec'ing the app.
#
# Railway — and most PaaS hosts — attach volumes root-owned at container start, which
# overrides whatever the image did at build time. A container that runs as an
# unprivileged user then cannot create anything under the mount, and the app dies with
# EACCES on its own data directory.
#
# Running the app as root would fix it and is the wrong trade: this process holds other
# people's API keys in memory. So the entrypoint starts as root purely to chown the
# mount, then hands off with `exec` so the app is PID 1 and receives signals directly —
# without that, a stop request would be swallowed here and the platform would eventually
# kill the container instead of letting it shut down.
set -e

APP_UID=10001
APP_GID=10001
DATA_DIR="${SOVEREIGN_SAAS_ROOT:-/data/tenants}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    # Only the mount root, not a recursive walk of tenant data: on a volume holding many
    # tenants that walk grows with the data and delays every single start.
    chown "$APP_UID:$APP_GID" /data "$DATA_DIR" 2>/dev/null || true

    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups "$@"
    fi
    # No setpriv: refuse rather than silently continuing as root. A deployment that
    # quietly runs a credential-holding process with full privileges is worse than one
    # that fails and says why.
    echo "entrypoint: setpriv unavailable; refusing to run as root" >&2
    exit 1
fi

exec "$@"
