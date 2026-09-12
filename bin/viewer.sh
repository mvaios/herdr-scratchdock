#!/usr/bin/env bash
# The dock pane's command. Runs in the scratchpad directory (herdr sets --cwd at
# open time), so every viewer choice below just needs to honour its own cwd.
#
# VIEWER=builtin      the bundled watcher: a live tree, newest first (default)
# VIEWER=file-viewer  the herdr-file-viewer plugin, when installed: a richer
#                     browser with file preview, but it only refreshes on demand
# VIEWER=<argv>       any command of your own, run in the scratchpad directory
set -uo pipefail

root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

viewer="${SCRATCHDOCK_VIEWER:-}"
if [ -z "$viewer" ] && [ -n "${HERDR_PLUGIN_CONFIG_DIR:-}" ] && [ -f "$HERDR_PLUGIN_CONFIG_DIR/config.env" ]; then
  viewer=$(sed -n 's/^[[:space:]]*\(SCRATCHDOCK_\)\{0,1\}VIEWER=//p' "$HERDR_PLUGIN_CONFIG_DIR/config.env" | tail -n1 | tr -d '"'"'")
fi
viewer="${viewer:-builtin}"

builtin_viewer() {
  for py in "${SCRATCHDOCK_PYTHON:-}" python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
    [ -n "$py" ] || continue
    command -v "$py" >/dev/null 2>&1 && exec "$py" "$root/bin/viewer.py"
  done
  # Last resort: no python at all. A plain refreshing listing still beats a blank
  # pane, and it is the one thing every POSIX box can do.
  while :; do
    printf '\033[H\033[2J%s\n\n' "$PWD"
    ls -lAt 2>/dev/null || true
    sleep 2
  done
}

# The file viewer is a separate plugin (smarzban/herdr-file-viewer); ask herdr
# where it lives rather than guessing a path, and fall through when it is absent.
file_viewer_bin() {
  local H="${HERDR_BIN_PATH:-herdr}" list
  list=$("$H" plugin list --json 2>/dev/null) || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$list" <<'PY'
import json, os, sys
try:
    plugins = json.loads(sys.argv[1])["result"]["plugins"]
except Exception:
    sys.exit(1)
for plugin in plugins:
    if plugin.get("plugin_id") == "herdr-file-viewer" and plugin.get("enabled"):
        binary = os.path.join(plugin.get("plugin_root", ""), "target", "release", "herdr-file-viewer")
        if os.access(binary, os.X_OK):
            print(binary)
            sys.exit(0)
sys.exit(1)
PY
}

# herdr injects the *invoking* pane's context, and a viewer that trusts
# `focused_pane_cwd` over its own cwd (herdr-file-viewer does) would root itself
# at the agent's project instead of the scratchpad. Retarget the context at this
# pane's directory, or drop it when we cannot rewrite it.
retarget_context() {
  [ -n "${HERDR_PLUGIN_CONTEXT_JSON:-}" ] || return 0
  if command -v python3 >/dev/null 2>&1; then
    local rewritten
    rewritten=$(printf '%s' "$HERDR_PLUGIN_CONTEXT_JSON" | python3 -c '
import json, sys
try:
    context = json.load(sys.stdin)
except Exception:
    context = {}
for key in ("focused_pane_cwd", "workspace_cwd", "cwd"):
    context[key] = sys.argv[1]
print(json.dumps(context))
' "$PWD" 2>/dev/null)
    if [ -n "$rewritten" ]; then
      export HERDR_PLUGIN_CONTEXT_JSON="$rewritten"
      return 0
    fi
  fi
  unset HERDR_PLUGIN_CONTEXT_JSON
}

retarget_context

case "$viewer" in
builtin)
  builtin_viewer
  ;;
auto | file-viewer)
  # Not the default: herdr-file-viewer reads the tree once and refreshes on a
  # keypress, and a dock you have to poke is a dock that lies about what the
  # agent just wrote. Opt in when preview matters more than liveness.
  if bin=$(file_viewer_bin) && [ -n "$bin" ]; then
    exec "$bin"
  fi
  builtin_viewer
  ;;
*)
  # shellcheck disable=SC2086 # a configured viewer is an argv string on purpose
  exec ${viewer}
  ;;
esac
