#!/usr/bin/env python3
"""scratchdock — dock a working agent's scratchpad directory beside it.

Subcommands: event | open | close | toggle | path

`event` is the manifest hook and must stay quiet and idempotent: herdr fires it
on every agent state transition, several times a minute per pane. The other
subcommands are user-facing actions and print what they did.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "mvaios.scratchdock"

# A session directory created within this many seconds *before* the agent process
# started still counts as that process's own: the agent writes the directory a
# moment before/after exec, and both orders have been observed.
START_SLACK = 10.0

DEFAULTS = {
    # Statuses that open the dock. Empty string disables auto-open entirely.
    "OPEN_ON": "working",
    # Statuses that close it again. Empty by default: the scratchpad is most
    # interesting *after* the agent stops, so the dock stays until dismissed.
    "CLOSE_ON": "",
    # Agents to dock for. "*" for any agent herdr detects.
    "AGENTS": "claude",
    "DIRECTION": "right",
    # Fraction of the split the dock takes. herdr splits 50/50; a file list does
    # not need half the window, and the agent pane is the one you read.
    "RATIO": "0.32",
    "FOCUS": "0",
    # Override the scratchpad root; default is /tmp/claude-<uid>.
    "SCRATCHPAD_ROOT": "",
}

# argv0 basenames that count as the agent process inside a pane, per herdr agent id.
AGENT_BINS = {"claude": ("claude",)}


# ---------------------------------------------------------------- config/state


def config() -> dict[str, str]:
    """DEFAULTS overlaid with config.env (KEY=VALUE lines) then the environment."""
    cfg = dict(DEFAULTS)
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        path = Path(config_dir) / "config.env"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().upper().removeprefix("SCRATCHDOCK_")
            if key in cfg:
                cfg[key] = value.strip().strip("'\"")
    for key in cfg:
        override = os.environ.get("SCRATCHDOCK_" + key)
        if override is not None:
            cfg[key] = override
    return cfg


def state_dir() -> Path:
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    path = Path(base) if base else Path.home() / ".local" / "state" / "scratchdock"
    path = path / "docks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_file(agent_pane: str) -> Path:
    # Pane ids are `w<id>:p<n>`; the colon is legal in a filename but slashes are
    # not, so normalize anything that is not id-ish rather than trusting the shape.
    return state_dir() / (re.sub(r"[^A-Za-z0-9_.:-]", "_", agent_pane) + ".json")


def load_state(agent_pane: str) -> dict:
    try:
        return json.loads(state_file(agent_pane).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(agent_pane: str, data: dict) -> None:
    try:
        state_file(agent_pane).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def clear_state(agent_pane: str) -> None:
    state_file(agent_pane).unlink(missing_ok=True)


# ------------------------------------------------------------------ herdr calls


def herdr(*args: str) -> dict | None:
    """Run a herdr CLI command and return its parsed envelope, or None on any failure."""
    try:
        proc = subprocess.run(
            [HERDR, *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def snapshot_panes() -> dict[str, dict]:
    doc = herdr("api", "snapshot")
    if not doc:
        return {}
    snap = doc.get("result", {}).get("snapshot", {})
    panes = {}
    # `panes` is every pane; `agents` is the agent-bearing subset and carries the
    # same shape. Merge agent rows second so agent fields win on overlap.
    for row in list(snap.get("panes") or []) + list(snap.get("agents") or []):
        pane_id = row.get("pane_id")
        if pane_id:
            panes.setdefault(pane_id, {}).update(row)
    return panes


def agent_pid(pane_id: str, agent: str) -> int | None:
    """PID of the agent process running in `pane_id`, by argv0 basename."""
    doc = herdr("pane", "process-info", "--pane", pane_id)
    if not doc:
        return None
    names = AGENT_BINS.get(agent, (agent,))
    procs = doc.get("result", {}).get("process_info", {}).get("foreground_processes") or []
    for proc in procs:
        argv = proc.get("argv") or []
        argv0 = proc.get("argv0") or (argv[0] if argv else "")
        if os.path.basename(argv0) in names:
            pid = proc.get("pid")
            return int(pid) if isinstance(pid, int) else None
    return None


def process_start(pid: int) -> float | None:
    """Epoch seconds the process started, via `ps -o lstart=` (macOS and Linux)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = out.stdout.strip()
    if not stamp:
        return None
    try:
        # Local time, no zone in the output, so mktime interprets it correctly.
        return time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


# -------------------------------------------------------------------- resolving


def scratchpad_root(cfg: dict[str, str]) -> Path:
    if cfg["SCRATCHPAD_ROOT"]:
        return Path(cfg["SCRATCHPAD_ROOT"])
    return Path("/tmp") / f"claude-{os.getuid()}"


def project_slug(cwd: str) -> str:
    """The agent's cwd flattened the way Claude Code names its temp directories."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def birth_time(path: Path) -> float:
    stat = path.stat()
    born = getattr(stat, "st_birthtime", 0) or 0
    # Linux usually has no birth time; ctime is the closest stand-in for a
    # directory that is created once and never renamed.
    return float(born or stat.st_ctime)


def resolve_scratchpad(pane_id: str, pane: dict, cfg: dict[str, str]) -> Path | None:
    """The scratchpad directory of the agent session running in `pane_id`.

    Sessions are per-process and per-cwd, so the cwd narrows the candidates to one
    project and the agent's process start time picks the session out of that
    project's history — including a session started mid-process by /clear, which
    is simply the newest one born after the process did.
    """
    cwd = pane.get("cwd") or pane.get("foreground_cwd")
    if not cwd:
        return None
    base = scratchpad_root(cfg) / project_slug(cwd)
    try:
        sessions = [child for child in base.iterdir() if child.is_dir()]
    except OSError:
        return None
    if not sessions:
        return None

    pid = agent_pid(pane_id, pane.get("agent") or "claude")
    started = process_start(pid) if pid else None
    if started is not None:
        own = [s for s in sessions if birth_time(s) >= started - START_SLACK]
        if own:
            sessions = own
    session = max(sessions, key=birth_time)

    scratchpad = session / "scratchpad"
    if not scratchpad.is_dir():
        # The agent creates this lazily, on first use. Creating it here means the
        # dock can open the moment work starts instead of after the first write.
        try:
            scratchpad.mkdir(parents=True, exist_ok=True)
        except OSError:
            return session if session.is_dir() else None
    return scratchpad


# ------------------------------------------------------------------ dock opening


def live_dock(agent_pane: str, panes: dict[str, dict]) -> str | None:
    """The dock pane recorded for `agent_pane`, if it is still open."""
    dock = load_state(agent_pane).get("dock_pane")
    if dock and dock in panes:
        return dock
    if dock:
        clear_state(agent_pane)
    return None


def resize_dock(dock_pane: str, cfg: dict[str, str]) -> None:
    """Shrink the fresh 50/50 split down to RATIO. Cosmetic: failure is silent.

    `pane resize` moves the divider by a ratio delta, so this reads the split the
    dock lives in, works out how far the divider has to travel, and moves it once.
    """
    try:
        ratio = float(cfg["RATIO"])
    except ValueError:
        return
    if not 0.05 <= ratio <= 0.95:
        return

    doc = herdr("pane", "layout", "--pane", dock_pane)
    layout = (doc or {}).get("result", {}).get("layout") or {}
    rect = next(
        (p.get("rect") for p in layout.get("panes") or [] if p.get("pane_id") == dock_pane),
        None,
    )
    if not rect:
        return

    vertical = cfg["DIRECTION"] == "down"
    size_key = "height" if vertical else "width"
    # The dock's own split is the smallest one that contains it and runs the same
    # way the dock was split off; anything larger is an ancestor split whose ratio
    # would move the wrong divider.
    candidates = [
        split for split in layout.get("splits") or []
        if split.get("direction") == cfg["DIRECTION"]
        and contains(split.get("rect") or {}, rect)
    ]
    if not candidates:
        return
    split = min(candidates, key=lambda s: (s.get("rect") or {}).get(size_key, 0))
    span = (split.get("rect") or {}).get(size_key, 0)
    if not span:
        return

    current = split.get("ratio")
    if not isinstance(current, (int, float)):
        return
    # `ratio` is the first pane's share, and the dock is the second.
    delta = (1.0 - ratio) - float(current)
    if abs(delta) < 0.01:
        return
    grow = "down" if vertical else "right"
    shrink = "up" if vertical else "left"
    herdr(
        "pane", "resize", "--pane", dock_pane,
        "--direction", grow if delta > 0 else shrink,
        "--amount", f"{abs(delta):.4f}",
    )


def contains(outer: dict, inner: dict) -> bool:
    try:
        return (
            outer["x"] <= inner["x"]
            and outer["y"] <= inner["y"]
            and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
            and outer["y"] + outer["height"] >= inner["y"] + inner["height"]
        )
    except (KeyError, TypeError):
        return False


def open_dock(agent_pane: str, cfg: dict[str, str], panes: dict[str, dict]) -> tuple[bool, str]:
    existing = live_dock(agent_pane, panes)
    if existing:
        return True, f"already docked in {existing}"

    pane = panes.get(agent_pane)
    if pane is None:
        return False, f"no such pane: {agent_pane}"
    scratchpad = resolve_scratchpad(agent_pane, pane, cfg)
    if scratchpad is None:
        return False, f"no scratchpad directory for {agent_pane}"

    doc = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "dock",
        "--placement", "split",
        "--target-pane", agent_pane,
        "--direction", cfg["DIRECTION"],
        "--cwd", str(scratchpad),
        "--env", f"SCRATCHDOCK_DIR={scratchpad}",
        "--env", f"SCRATCHDOCK_AGENT_PANE={agent_pane}",
        "--focus" if cfg["FOCUS"] == "1" else "--no-focus",
    )
    dock_pane = (
        (doc or {}).get("result", {}).get("plugin_pane", {}).get("pane", {}).get("pane_id")
    )
    if not dock_pane:
        return False, "herdr plugin pane open failed"

    save_state(agent_pane, {"dock_pane": dock_pane, "dir": str(scratchpad)})
    resize_dock(dock_pane, cfg)
    return True, f"docked {scratchpad} in {dock_pane}"


def close_dock(agent_pane: str, panes: dict[str, dict]) -> tuple[bool, str]:
    dock = live_dock(agent_pane, panes)
    if not dock:
        clear_state(agent_pane)
        return True, f"no dock open for {agent_pane}"
    herdr("plugin", "pane", "close", dock)
    clear_state(agent_pane)
    return True, f"closed {dock}"


# --------------------------------------------------------------------- dispatch


def opener() -> str | None:
    for candidate in ("open", "xdg-open"):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            path = os.path.join(directory, candidate)
            if os.access(path, os.X_OK):
                return path
    return None


def reveal(scratchpad: Path) -> tuple[bool, str]:
    launcher = opener()
    if not launcher:
        return False, "no opener (open/xdg-open) on PATH"
    try:
        subprocess.Popen(
            [launcher, str(scratchpad)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"could not open the folder: {error}"
    return True, f"revealed {scratchpad}"


def copy_path(scratchpad: Path) -> tuple[bool, str]:
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            proc = subprocess.run(argv, input=str(scratchpad), text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True, f"copied {scratchpad}"
    return False, "no clipboard tool found (pbcopy, wl-copy, xclip)"


def open_shell(agent_pane: str, scratchpad: Path, cfg: dict[str, str]) -> tuple[bool, str]:
    doc = herdr(
        "pane", "split", "--pane", agent_pane,
        "--direction", cfg["DIRECTION"], "--cwd", str(scratchpad), "--focus",
    )
    pane = (doc or {}).get("result", {}).get("pane", {}).get("pane_id")
    if not pane:
        return False, "herdr pane split failed"
    return True, f"shell in {pane} at {scratchpad}"


def focused_agent_pane(panes: dict[str, dict]) -> str | None:
    """The pane an action should act on: the focused pane, or its agent sibling.

    Actions are usually invoked from the agent pane itself, but they are just as
    likely to be invoked from the dock — so a focused dock resolves back to the
    agent it belongs to, which makes `toggle` work from either side.
    """
    pane_id = os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        try:
            context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
        except ValueError:
            context = {}
        pane_id = context.get("focused_pane_id")
    if not pane_id:
        return None
    for path in state_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("dock_pane") == pane_id:
            return path.stem
    return pane_id if pane_id in panes else None


def handle_event(cfg: dict[str, str]) -> int:
    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except ValueError:
        return 0
    data = event.get("data") or {}
    kind = data.get("type")
    pane_id = data.get("pane_id")
    if not pane_id:
        return 0

    if kind == "pane_closed":
        # Either half can go: an agent pane takes its dock with it, and a dock the
        # user closed by hand must not leave state claiming it is still open.
        if load_state(pane_id):
            close_dock(pane_id, snapshot_panes())
        else:
            for path in state_dir().glob("*.json"):
                try:
                    data_ = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data_.get("dock_pane") == pane_id:
                    path.unlink(missing_ok=True)
        return 0

    if kind != "pane_agent_status_changed":
        return 0

    agent = data.get("agent") or ""
    agents = [a.strip() for a in cfg["AGENTS"].split(",") if a.strip()]
    if "*" not in agents and agent not in agents:
        return 0

    status = data.get("agent_status") or ""
    open_on = [s.strip() for s in cfg["OPEN_ON"].split(",") if s.strip()]
    close_on = [s.strip() for s in cfg["CLOSE_ON"].split(",") if s.strip()]

    if status in close_on:
        close_dock(pane_id, snapshot_panes())
    elif status in open_on:
        panes = snapshot_panes()
        # Cheap guard first: an already-docked pane must not cost a snapshot walk
        # plus a `ps` on every single status flip of a busy agent.
        if not live_dock(pane_id, panes):
            open_dock(pane_id, cfg, panes)
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "toggle"
    cfg = config()

    if mode == "event":
        try:
            return handle_event(cfg)
        except Exception:  # noqa: BLE001 - a hook must never fail loudly
            return 0

    panes = snapshot_panes()
    agent_pane = focused_agent_pane(panes)
    if not agent_pane:
        print("scratchdock: no focused pane (run this from inside herdr)", file=sys.stderr)
        return 1

    if mode == "open":
        ok, message = open_dock(agent_pane, cfg, panes)
    elif mode == "close":
        ok, message = close_dock(agent_pane, panes)
    elif mode == "toggle":
        if live_dock(agent_pane, panes):
            ok, message = close_dock(agent_pane, panes)
        else:
            ok, message = open_dock(agent_pane, cfg, panes)
    elif mode in ("path", "reveal", "copy-path", "shell"):
        pane = panes.get(agent_pane) or {}
        scratchpad = resolve_scratchpad(agent_pane, pane, cfg)
        if scratchpad is None:
            ok, message = False, f"no scratchpad directory for {agent_pane}"
        elif mode == "path":
            ok, message = True, str(scratchpad)
        elif mode == "reveal":
            ok, message = reveal(scratchpad)
        elif mode == "copy-path":
            ok, message = copy_path(scratchpad)
        else:
            ok, message = open_shell(agent_pane, scratchpad, cfg)
    else:
        print(f"scratchdock: unknown command {mode!r}", file=sys.stderr)
        return 2

    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
