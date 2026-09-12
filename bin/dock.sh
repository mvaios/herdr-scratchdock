#!/usr/bin/env bash
# Thin launcher: herdr runs manifest commands without a shell, so argv[0] has to
# be something that is always on PATH. bash is; a specific python is not. Find an
# interpreter here and hand the real work to dock.py.
set -euo pipefail

root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

for py in "${SCRATCHDOCK_PYTHON:-}" python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
  [ -n "$py" ] || continue
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" "$root/bin/dock.py" "$@"
  fi
done

# No interpreter: stay silent on event hooks (they fire constantly and a failing
# hook would just spam the plugin log), but tell a human who ran an action.
if [ "${1:-}" != "event" ]; then
  echo "scratchdock: no python3 found (set SCRATCHDOCK_PYTHON)" >&2
fi
exit 1
