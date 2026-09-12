#!/usr/bin/env python3
"""The scratchpad dock: a live, keyboard-driven view of one agent's scratchpad.

Newest first at every level, because the interesting file in a scratchpad is
almost always the one the agent just wrote. The tree rescans once a second and
only repaints when something actually changed, so an idle dock costs one stat
sweep and no terminal traffic.

Images preview inline through the Kitty graphics protocol, which herdr renders
in a compatible outer terminal; text previews as text. Everything else reports
what it is and gets out of the way.
"""

from __future__ import annotations

import base64
import os
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

ROOT = Path(os.environ.get("SCRATCHDOCK_DIR") or os.getcwd())
AGENT_PANE = os.environ.get("SCRATCHDOCK_AGENT_PANE") or ""
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"

POLL = 0.25          # input responsiveness
RESCAN = 1.0         # directory rescan
FORCE_REDRAW = 15.0  # so the age column stays honest while nothing changes
MAX_DEPTH = 6
MAX_ENTRIES = 2000
FRESH = 30.0
DOUBLE_CLICK = 0.4

# herdr sends a modified key as ESC + the key, so alt+enter is ESC LF. The CSI u
# and modifyOtherKeys spellings are accepted too, in case the dock ever runs
# under a terminal that negotiated one of those directly.
ALT_ENTER = ("alt-\r", "alt-\n", "\033[13;3u", "\033[27;3;13~")
PREVIEW_BYTES = 256 * 1024
# `pane.graphics.set` refuses an oversized frame with `image_too_large`. Probing
# it puts the boundary at 512 KiB: 489817 bytes was accepted and 537316 refused.
# This sits just under, leaving room for whatever the server counts alongside the
# pixels.
MAX_IMAGE_BYTES = 504 * 1024

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic"}

# SGR mouse reporting (1006) on top of normal button tracking (1000): clicks and
# wheel notches, no drag spam, and coordinates that survive past column 223.
MOUSE_ON = "\033[?1000h\033[?1006h"
MOUSE_OFF = "\033[?1006l\033[?1000l"

DIM = "\033[2m"
BOLD = "\033[1m"
REVERSE = "\033[7m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


# ----------------------------------------------------------------- formatting


def human_size(size: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.0f}G"


def human_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def terminal_size() -> tuple[int, int]:
    """This pane's size, never COLUMNS/LINES.

    herdr launches the dock with the inviting pane's environment, so an inherited
    COLUMNS describes the agent's pane and would have the dock padding its columns
    to somebody else's width. Ask the tty, and only guess if there is none.
    """
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
    except OSError:
        pass
    return 80, 24


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


# ----------------------------------------------------------------------- scan


class Entry:
    __slots__ = ("path", "depth", "is_dir", "size", "mtime")

    def __init__(self, path: Path, depth: int, is_dir: bool, size: int, mtime: float):
        self.path, self.depth, self.is_dir = path, depth, is_dir
        self.size, self.mtime = size, mtime


def scan(root: Path, collapsed: set[Path]) -> list[Entry]:
    rows: list[Entry] = []
    budget = [MAX_ENTRIES]

    def walk(directory: Path, depth: int) -> None:
        if budget[0] <= 0 or depth >= MAX_DEPTH:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return

        def mtime_of(entry: os.DirEntry) -> float:
            try:
                return entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                return 0.0

        for entry in sorted(entries, key=mtime_of, reverse=True):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            rows.append(Entry(path, depth, is_dir, stat.st_size, stat.st_mtime))
            if is_dir and path not in collapsed:
                walk(path, depth + 1)

    walk(root, 0)
    return rows


def signature(rows: list[Entry]) -> tuple:
    return tuple((str(r.path), r.is_dir, r.size, r.mtime) for r in rows)


# -------------------------------------------------------------------- preview


def is_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError as error:
        # A multi-byte character can straddle the end of the read, and a file cut
        # mid-character is still text. Anything failing earlier than the last
        # three bytes is a genuinely non-UTF-8 byte, not a clipped one.
        if error.start >= len(chunk) - 3:
            return True

    # Not UTF-8, but a note saved in a legacy encoding is still something worth
    # reading, and the preview decodes with replacement anyway. Take it when
    # nearly every byte is one a text file would plausibly hold; a blob that
    # happens to contain no NUL fails this on its control bytes.
    textish = sum(1 for byte in chunk if 0x20 <= byte < 0x7F or byte in (9, 10, 13) or byte >= 0xA0)
    return bool(chunk) and textish >= len(chunk) * 0.95


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


_dimension_cache: dict[tuple[str, float], tuple[int, int] | None] = {}


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """Pixel size of an image, memoised on (path, mtime).

    This is called from `preview_lines`, which runs on every repaint — every
    `j` and `k` included. For a PNG that is a header read, but for anything else
    it shells out to `sips`, and one subprocess per keystroke would block the
    input loop. The answer only changes when the file does.
    """
    dimensions = png_dimensions(path)
    if dimensions:
        return dimensions
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key in _dimension_cache:
        return _dimension_cache[key]
    measured = _measure_image(path)
    if len(_dimension_cache) > 64:
        _dimension_cache.clear()
    _dimension_cache[key] = measured
    return measured


def _measure_image(path: Path) -> tuple[int, int] | None:
    # sips is macOS-only and every other reader is a third-party dependency, so
    # elsewhere the preview simply goes without a pixel size.
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    width = re.search(r"pixelWidth:\s*(\d+)", out)
    height = re.search(r"pixelHeight:\s*(\d+)", out)
    if width and height:
        return int(width.group(1)), int(height.group(1))
    return None


def socket_request(method: str, params: dict) -> dict | None:
    """One newline-delimited JSON request to the herdr socket.

    The pane graphics API has no CLI wrapper, so this is the only way to reach it.
    A failure here is always cosmetic — the preview degrades to its text header —
    so every error path returns None rather than disturbing the viewer.
    """
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return None
    import json
    import socket as socket_module

    try:
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(path)
            sock.sendall((json.dumps({"id": "scratchdock", "method": method, "params": params}) + "\n").encode())
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                buffer += chunk
        return json.loads(buffer.split(b"\n")[0])
    except (OSError, ValueError):
        return None


_cell_size: tuple[int, int] | None = None


def cell_size() -> tuple[int, int]:
    """The attached client's cell size in pixels, asked once."""
    global _cell_size
    if _cell_size is None:
        info = (socket_request("pane.graphics.info", {"pane_id": own_pane()}) or {}).get("result") or {}
        _cell_size = (
            int(info.get("cell_width_px") or 8),
            int(info.get("cell_height_px") or 17),
        )
    return _cell_size


def image_payload(path: Path, box_px: tuple[int, int]) -> tuple[bytes, int, int] | None:
    """PNG bytes small enough to send, with the dimensions of those bytes.

    Resizing here is only ever about the payload cap — herdr scales the image to
    the placement rectangle itself, so a bigger PNG buys nothing. A PNG that is
    already under the cap is therefore sent untouched, which is what makes image
    previews work on a machine without `sips` (that is, on Linux).
    """
    if path.suffix.lower() == ".png":
        try:
            data = path.read_bytes()
        except OSError:
            return None
        dimensions = png_header_dimensions(data)
        if dimensions and len(data) <= MAX_IMAGE_BYTES:
            return (data, *dimensions)
        if not dimensions:
            return None
        # Over the cap: the only way down is a resample, which needs sips.

    source = image_dimensions(path)
    target = None
    if source and source[0] > 0 and source[1] > 0:
        scale = min(box_px[0] / source[0], box_px[1] / source[1], 1.0)
        target = max(1, round(max(source) * scale))

    converted = Path(os.environ.get("TMPDIR", "/tmp")) / f"scratchdock-{os.getpid()}.png"
    argv = ["sips", "-s", "format", "png"]
    if target is not None:
        argv += ["-Z", str(target)]
    argv += [str(path), "--out", str(converted)]
    try:
        if subprocess.run(argv, capture_output=True, timeout=20).returncode != 0:
            return None
        data = converted.read_bytes()
        converted.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        return None
    dimensions = png_header_dimensions(data)
    if not dimensions or len(data) > MAX_IMAGE_BYTES:
        return None
    return (data, *dimensions)


def png_header_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def place_image(path: Path, box: tuple[int, int, int, int]) -> bool:
    """Draw `path` inside the cell rectangle `box` = (col, row, cols, rows).

    The placement rectangle is what herdr scales the image to, so handing it the
    whole preview box stretches a 16:9 screenshot into whatever shape the pane
    happens to be. Instead the rectangle is cut down to the cells the image's own
    aspect ratio occupies and centred in the space, leaving pane background
    around it — letterboxing, not stretching. The arithmetic runs off the
    dimensions of the bytes being sent, so it holds whether or not those bytes
    were resampled on the way here.
    """
    col, row, cols, rows = box
    cell_w, cell_h = cell_size()
    payload = image_payload(path, (cols * cell_w, rows * cell_h))
    if not payload:
        return False
    data, width, height = payload
    if width <= 0 or height <= 0:
        return False

    # Never upscale: a small image stays small rather than being blown up blurry.
    scale = min(cols * cell_w / width, rows * cell_h / height, 1.0)
    # Round to the nearest cell rather than up: half a cell of slack in each
    # direction is invisible, while a whole spare cell is a visible band.
    used_cols = max(1, min(cols, round(width * scale / cell_w)))
    used_rows = max(1, min(rows, round(height * scale / cell_h)))
    reply = socket_request(
        "pane.graphics.set",
        {
            "pane_id": own_pane(),
            "format": "png",
            "image_width": width,
            "image_height": height,
            "data_base64": base64.standard_b64encode(data).decode("ascii"),
            "placement": {
                "viewport_col": col + (cols - used_cols) // 2,
                "viewport_row": row + (rows - used_rows) // 2,
                "grid_cols": used_cols,
                "grid_rows": used_rows,
            },
        },
    )
    return bool(reply and "result" in reply)


def clear_image() -> None:
    socket_request("pane.graphics.clear", {"pane_id": own_pane()})


# ---------------------------------------------------------------------- shell


def run_detached(argv: list[str]) -> None:
    try:
        subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        pass


def copy_to_clipboard(text: str) -> bool:
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            proc = subprocess.run(argv, input=text, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    return False


def opener() -> str | None:
    """The platform's launcher, chosen by platform rather than by PATH order.

    Not a search across both names: on several Linux distributions `/usr/bin/open`
    is util-linux's `openvt`, so preferring whichever appears first on PATH picks
    a virtual-terminal tool and quietly fails.
    """
    return shutil_which("open" if sys.platform == "darwin" else "xdg-open")


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


# --------------------------------------------------------------------- viewer


HELP = [
    ("j / k  ↑ ↓", "move"),
    ("g / G", "first / last"),
    ("enter", "reveal the file's location"),
    ("alt+enter", "open in the default app"),
    ("space", "unfold a directory · preview a file"),
    ("p", "toggle preview pane"),
    ("o / f", "aliases for alt+enter / enter"),
    ("e", "open in $EDITOR, in a new herdr pane"),
    ("s", "send the path to the agent"),
    ("y", "copy the path"),
    ("r", "rescan now"),
    ("click", "select · double click unfolds/previews"),
    ("wheel", "scroll"),
    ("?", "this help"),
    ("q", "close the dock"),
]


class Viewer:
    def __init__(self) -> None:
        self.rows: list[Entry] = []
        self.collapsed: set[Path] = set()
        self.selected = 0
        self.top = 0
        self.preview_on = True
        self.help_on = False
        self.status = ""
        self.status_until = 0.0
        self.signature: tuple | None = None
        self.last_draw = 0.0
        self.image_shown = False
        self.image_key: tuple | None = None
        self.image_box: tuple[int, int, int, int] | None = None
        self.dirty = True
        self.inbuf = b""
        self.last_click = 0.0
        # Where the tree is on screen, so a click can be turned back into a row.
        self.tree_first_row = 4  # 1-indexed terminal row
        self.tree_height = 0

    # -- state ---------------------------------------------------------------

    def current(self) -> Entry | None:
        if 0 <= self.selected < len(self.rows):
            return self.rows[self.selected]
        return None

    def rescan(self) -> None:
        keep = self.current()
        self.rows = scan(ROOT, self.collapsed)
        current = signature(self.rows)
        if current != self.signature:
            self.signature = current
            self.dirty = True
            # Keep the cursor on the same file across a rescan; a scratchpad the
            # agent is writing to reorders constantly, and a selection that slid
            # onto a different file every second would be unusable.
            if keep is not None:
                for index, row in enumerate(self.rows):
                    if row.path == keep.path:
                        self.selected = index
                        break
                else:
                    self.selected = min(self.selected, max(0, len(self.rows) - 1))

    def say(self, message: str) -> None:
        self.status = message
        self.status_until = time.time() + 4
        self.dirty = True

    # -- actions -------------------------------------------------------------

    def act_open(self) -> None:
        entry = self.current()
        if entry is None:
            return
        launcher = opener()
        if not launcher:
            self.say("no opener (open/xdg-open) on PATH")
            return
        run_detached([launcher, str(entry.path)])
        self.say(f"opened {entry.path.name}")

    def act_reveal(self) -> None:
        entry = self.current()
        target = entry.path if entry else ROOT
        launcher = opener()
        if not launcher:
            self.say("no opener (open/xdg-open) on PATH")
            return
        # Only macOS `open` understands -R, and only it can select a file inside
        # its folder. Elsewhere the closest thing is opening the folder itself.
        if sys.platform == "darwin" and entry is not None:
            run_detached([launcher, "-R", str(target)])
        else:
            run_detached([launcher, str(target if target.is_dir() else target.parent)])
        self.say(f"revealed {target.name}")

    def act_editor(self) -> None:
        entry = self.current()
        if entry is None or entry.is_dir:
            return
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        # Open beside the dock rather than over it, so the tree stays visible.
        doc = run_json([HERDR, "pane", "split", "--pane", own_pane(), "--direction", "down", "--cwd", str(ROOT), "--focus"])
        pane = (doc or {}).get("result", {}).get("pane", {}).get("pane_id")
        if not pane:
            self.say("could not split a pane for the editor")
            return
        run_json([HERDR, "pane", "run", pane, editor, str(entry.path)])
        self.say(f"{editor} {entry.path.name}")

    def act_send(self) -> None:
        entry = self.current()
        if entry is None:
            return
        if not AGENT_PANE:
            self.say("no agent pane recorded for this dock")
            return
        run_json([HERDR, "pane", "send-text", AGENT_PANE, str(entry.path) + " "])
        self.say(f"sent path to {AGENT_PANE}")

    def act_copy(self) -> None:
        entry = self.current()
        target = str(entry.path if entry else ROOT)
        self.say("copied path" if copy_to_clipboard(target) else "no clipboard tool found")

    def act_enter(self, toggle: bool = True) -> None:
        """Unfold a directory, or show a file's preview.

        `toggle` is what separates the keyboard from the mouse here: space is a
        toggle, so pressing it on the file you are already previewing closes the
        preview again and the key always does something visible. A double click
        only ever opens — clicking the row you are already on should not blank
        half the pane.
        """
        entry = self.current()
        if entry is None:
            return
        if entry.is_dir:
            if entry.path in self.collapsed:
                self.collapsed.discard(entry.path)
            else:
                self.collapsed.add(entry.path)
            self.signature = None
            self.rescan()
        else:
            self.preview_on = (not self.preview_on) if toggle else True
        self.dirty = True

    def move(self, delta: int) -> None:
        if not self.rows:
            return
        self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))
        self.dirty = True

    # -- drawing -------------------------------------------------------------

    def tree_lines(self, width: int, height: int) -> list[str]:
        if not self.rows:
            return [f"{DIM}empty — waiting for the agent to write something{RESET}"]

        if self.selected < self.top:
            self.top = self.selected
        if self.selected >= self.top + height:
            self.top = self.selected - height + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - height)))

        now = time.time()
        lines = []
        for index in range(self.top, min(len(self.rows), self.top + height)):
            row = self.rows[index]
            age = now - row.mtime
            fresh = age < FRESH
            indent = "  " * row.depth
            name = row.path.name + "/" if row.is_dir else row.path.name
            if row.is_dir and row.path in self.collapsed:
                name += " …"
            right = "" if row.is_dir else f"{human_size(row.size):>7} {human_age(age):>4}"

            # Lay the row out in plain text, then paint it: escape codes have no
            # width, and mixing them into the arithmetic is how columns drift.
            prefix = f"{'●' if fresh else ' '} {indent}"
            room = width - len(prefix) - len(right) - 1
            if room < 4:
                lines.append(fit(prefix + name, width))
                continue
            name = fit(name, room)
            gap = " " * (width - len(prefix) - len(name) - len(right))

            if row.is_dir:
                painted = f"{CYAN}{name}{RESET}"
            elif fresh:
                painted = f"{BOLD}{name}{RESET}"
            else:
                painted = name
            marker = f"{GREEN}●{RESET}" if fresh else " "
            body = f"{marker} {indent}{painted}{gap}{DIM}{right}{RESET}"
            if index == self.selected:
                plain = f"{'●' if fresh else ' '} {indent}{name}{gap}{right}"
                body = f"{REVERSE}{plain}{RESET}"
            lines.append(body)
        return lines

    def preview_lines(self, width: int, height: int) -> tuple[list[str], Path | None]:
        """Text rows for the preview box, plus the image to draw over it, if any."""
        entry = self.current()
        if entry is None:
            return [], None
        if entry.is_dir:
            try:
                count = len(list(os.scandir(entry.path)))
            except OSError:
                count = 0
            return [f"{DIM}directory · {count} item(s){RESET}"], None

        suffix = entry.path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            dimensions = image_dimensions(entry.path)
            shape = f"{dimensions[0]}×{dimensions[1]}" if dimensions else "image"
            header = f"{DIM}{shape} · {human_size(entry.size)}{RESET}"
            return [header], entry.path

        if is_text(entry.path):
            try:
                with entry.path.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(PREVIEW_BYTES)
            except OSError as error:
                return [f"{DIM}unreadable: {error}{RESET}"], None
            lines = [fit(line.rstrip("\n").expandtabs(4), width) for line in text.splitlines()]
            return lines[:height] or [f"{DIM}empty file{RESET}"], None

        return [f"{DIM}binary · {human_size(entry.size)}{RESET}"], None

    def help_lines(self, width: int) -> list[str]:
        lines = [f"{BOLD}keys{RESET}", ""]
        pad = max(len(key) for key, _ in HELP)
        for key, what in HELP:
            lines.append(f"  {YELLOW}{key:<{pad}}{RESET}  {DIM}{fit(what, max(0, width - pad - 6))}{RESET}")
        return lines

    def draw(self) -> None:
        width, height = terminal_size()
        now = time.time()
        out = ["\033[H\033[2J"]

        label = ROOT.name
        session = ROOT.parent.name
        if session:
            label = f"{label} {DIM}·{RESET}{BOLD} {session[:8]}"
        files = sum(1 for row in self.rows if not row.is_dir)
        total = sum(row.size for row in self.rows if not row.is_dir)
        out.append(f"{BOLD}{label}{RESET}\n")
        out.append(f"{DIM}{files} file(s) · {human_size(total)}{RESET}\n\n")

        body_height = height - 4  # header, count, blank, footer
        image: Path | None = None
        self.image_box = None

        self.tree_first_row = 4
        self.tree_height = 0
        # A split pane can be short enough that a tree and a preview do not both
        # fit. Below that, the tree gets the whole body rather than the preview
        # borrowing rows the tree does not have.
        split = self.preview_on and self.rows and body_height >= 5
        if self.help_on:
            for line in self.help_lines(width)[:body_height]:
                out.append(line + "\n")
        elif split:
            tree_height = max(1, min(len(self.rows), (body_height - 2) // 2))
            self.tree_height = tree_height
            preview_height = body_height - tree_height - 1
            tree = self.tree_lines(width, tree_height)
            for line in tree:
                out.append(line + "\n")
            entry = self.current()
            title = fit(entry.path.name, max(0, width - 4)) if entry else ""
            out.append(f"{DIM}{'─' * max(0, width - len(title) - 3)} {title} {RESET}\n")
            lines, image = self.preview_lines(width, preview_height)
            for line in lines[:preview_height]:
                out.append(line + "\n")
            # Measure from the rows actually emitted, not from the budget: a tree
            # with fewer entries than its allowance ends higher up, and an image
            # placed against the allowance would float below its own caption.
            first_row = 3 + len(tree) + 1 + 1  # tree, separator, the size line
            self.image_box = (0, first_row, max(1, width), max(1, height - 1 - first_row))
        else:
            self.tree_height = body_height
            for line in self.tree_lines(width, body_height):
                out.append(line + "\n")

        footer = self.status if now < self.status_until else "space preview · enter reveal · alt+enter open · e edit · ? keys"
        out.append(f"\033[{height};1H{DIM}{fit(footer, width)}{RESET}")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        # The graphics layer is composited by herdr and outlives the text frame,
        # so it is only touched when what it should show actually changes.
        if image is None or self.image_box is None:
            if self.image_shown:
                clear_image()
                self.image_shown = False
                self.image_key = None
            return
        self.draw_image(image, self.image_box)

    def draw_image(self, path: Path, box: tuple[int, int, int, int]) -> None:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return
        key = (str(path), stamp, box)
        if key == self.image_key:
            return
        if place_image(path, box):
            self.image_shown = True
            self.image_key = key
        else:
            if self.image_shown:
                clear_image()
            self.image_shown = False
            self.image_key = None

    # -- loop ----------------------------------------------------------------

    def click(self, column: int, row: int) -> None:
        """A left click at 1-indexed terminal cell (column, row)."""

        if self.help_on:
            self.help_on = False
            self.dirty = True
            return
        first, height = self.tree_first_row, self.tree_height
        if not (first <= row < first + height):
            return
        index = self.top + (row - first)
        if index >= len(self.rows):
            return
        now = time.time()
        double = index == self.selected and now - self.last_click < DOUBLE_CLICK
        self.selected = index
        self.last_click = now
        if double:
            # The desktop gesture: a double click opens what is under it — a
            # directory unfolds, a file gets its preview.
            self.act_enter(toggle=False)
        self.dirty = True

    def mouse(self, button: int, column: int, row: int, pressed: bool) -> None:
        if button == 64:
            self.move(-3)
        elif button == 65:
            self.move(3)
        elif pressed and (button & 0b11) == 0:
            self.click(column, row)

    def handle(self, key: str) -> bool:
        """Returns False to quit."""
        if self.help_on and key != "?":
            self.help_on = False
            self.dirty = True
            return key != "q"
        if key in ("q", "\x03", "\x04"):
            return False
        if key in ("j", "\x1b[B"):
            self.move(1)
        elif key in ("k", "\x1b[A"):
            self.move(-1)
        elif key == "g":
            self.selected, self.dirty = 0, True
        elif key == "G":
            self.selected, self.dirty = max(0, len(self.rows) - 1), True
        elif key in ("\r", "\n"):
            self.act_reveal()
        elif key in ALT_ENTER:
            self.act_open()
        elif key == " ":
            self.act_enter()
        elif key == "\x1b":
            self.help_on = False
            self.dirty = True
        elif key == "p":
            self.preview_on = not self.preview_on
            self.dirty = True
        elif key == "o":
            self.act_open()
        elif key == "e":
            self.act_editor()
        elif key == "f":
            self.act_reveal()
        elif key == "s":
            self.act_send()
        elif key == "y":
            self.act_copy()
        elif key == "r":
            self.signature = None
            self.rescan()
        elif key == "?":
            self.help_on = not self.help_on
            self.dirty = True
        return True

    def events(self) -> list[tuple] | None:
        """Parse whatever is readable into ("key", str) and ("mouse", ...) events.

        Mouse reports and arrow keys arrive as multi-byte escape sequences that a
        single read can split in half, so unparsable trailing bytes stay in the
        buffer for the next read instead of being handled as stray keystrokes.
        """
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return None
        if not data:
            return None
        self.inbuf += data

        out: list[tuple] = []
        while self.inbuf:
            buffer = self.inbuf
            if buffer.startswith(b"\033[<"):
                match = re.match(rb"\033\[<(\d+);(\d+);(\d+)([Mm])", buffer)
                if not match:
                    break
                out.append(("mouse", int(match[1]), int(match[2]), int(match[3]), match[4] == b"M"))
                self.inbuf = buffer[match.end():]
            elif buffer.startswith(b"\033["):
                match = re.match(rb"\033\[[0-9;?]*[@-~]", buffer)
                if not match:
                    break
                out.append(("key", match.group().decode("ascii", "replace")))
                self.inbuf = buffer[match.end():]
            elif buffer.startswith(b"\033O"):
                if len(buffer) < 3:
                    break
                out.append(("key", buffer[:3].decode("ascii", "replace")))
                self.inbuf = buffer[3:]
            elif buffer == b"\033":
                break  # an escape sequence that has not finished arriving
            elif buffer.startswith(b"\033"):
                # herdr encodes a modified key the classic way: ESC then the key
                # (`alt+enter` arrives as ESC LF). Anything that was going to be
                # a CSI or SS3 sequence was handled above.
                out.append(("key", "alt-" + buffer[1:2].decode("utf-8", "replace")))
                self.inbuf = buffer[2:]
            else:
                chunk = buffer.split(b"\033", 1)[0] or buffer[:1]
                for char in chunk.decode("utf-8", "replace"):
                    out.append(("key", char))
                self.inbuf = buffer[len(chunk):]
        return out

    # -- loop ----------------------------------------------------------------

    def run(self) -> int:
        last_scan = 0.0
        while True:
            now = time.time()
            if now - last_scan >= RESCAN:
                self.rescan()
                last_scan = now
            if self.dirty or now - self.last_draw > FORCE_REDRAW:
                self.draw()
                self.dirty = False
                self.last_draw = now

            ready, _, _ = select.select([sys.stdin], [], [], POLL)
            if not ready:
                # A lone ESC is held back in case it is the start of a sequence;
                # once a poll goes by with nothing after it, it was the Escape key.
                if self.inbuf == b"\033":
                    self.inbuf = b""
                    if not self.handle("\x1b"):
                        return 0
                continue
            events = self.events()
            if events is None:
                return 0
            for event in events:
                if event[0] == "mouse":
                    self.mouse(event[1], event[2], event[3], event[4])
                elif not self.handle(event[1]):
                    return 0


def run_json(argv: list[str]) -> dict | None:
    import json

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def own_pane() -> str:
    return os.environ.get("HERDR_PANE_ID") or ""


def main() -> int:
    if not sys.stdin.isatty():
        print(f"scratchdock: {ROOT}")
        return 0

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)

    def leave(*_: object) -> None:
        clear_image()
        sys.stdout.write(MOUSE_OFF + "\033[?25h\033[?1049l")
        sys.stdout.flush()
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except termios.error:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, leave)
    viewer = Viewer()
    signal.signal(signal.SIGWINCH, lambda *_: setattr(viewer, "dirty", True))

    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?1049h\033[?25l" + MOUSE_ON)
        return viewer.run()
    except KeyboardInterrupt:
        return 0
    finally:
        clear_image()
        sys.stdout.write(MOUSE_OFF + "\033[?25h\033[?1049l")
        sys.stdout.flush()
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except termios.error:
            pass


if __name__ == "__main__":
    sys.exit(main())
