# scratchdock

A herdr plugin that docks a coding agent's **scratchpad directory** in a split
pane beside it, automatically, while it works.

Claude Code is handed a per-session temp directory for intermediate files —
`/tmp/claude-<uid>/<cwd-slug>/<session-uuid>/scratchpad` — and it uses it
constantly: fetched docs, extracted JSON, generated scripts, half-finished
output. Nothing in herdr or on the marketplace shows you that directory, so the
files an agent is actually working with stay invisible until it mentions one.

scratchdock opens it beside the agent the moment it starts working, and keeps it
open after it stops, which is when you usually want to read what it left behind.

```
┌ claude ──────────────────────────┬ Scratchpad ──────────────┐
│ > render that button for me      │ scratchpad · e8878fda    │
│                                  │ 4 file(s) · 1.2M         │
│ ● Writing button.png…            │                          │
│                                  │ ● button.png   986K   2s │
│                                  │   ButtonShot.swift 2.5K  │
│                                  │ ──────────── button.png  │
│                                  │ 1280×720 · 986.1K        │
│                                  │  ╭──────────────────╮    │
│                                  │  │    [ Continue ]  │    │
│                                  │  ╰──────────────────╯    │
│                                  │ j/k move · o open · ?    │
└──────────────────────────────────┴──────────────────────────┘
```

## Install

```sh
herdr plugin link /path/to/herdr-scratchdock
```

macOS and Linux. Needs `python3` on `PATH` (set `SCRATCHDOCK_PYTHON` if yours
lives somewhere unusual). No build step, no dependencies.

## How it finds the scratchpad

An agent pane gives you a cwd, not a session id, and one project accumulates a
session directory per run. scratchdock narrows it in two steps:

1. The pane's cwd, flattened the way Claude Code names its temp directories
   (`/Users/me/src/app` → `-Users-me-src-app`), picks the project.
2. The agent process's start time picks the session out of that project's
   history: the newest session directory born after the process did. That also
   handles `/clear`, which starts a new session inside the same process.

It is a heuristic, and it has one blind spot: two agents running in the *same*
directory at the same time can resolve to the same session. Everything else —
worktrees, several projects, several panes — maps cleanly.

`herdr plugin action invoke mvaios.scratchdock.path` prints what it would dock, which is
the quickest way to check it on your own setup.

## Inside the dock

The dock is keyboard-driven and read-only: it will open a file for you, but it
never writes to the scratchpad the agent is using.

| Key | |
| --- | --- |
| `j` `k` `↑` `↓` | move |
| click | select a row |
| double click | unfold a directory · open a file's preview |
| wheel | scroll |
| `g` `G` | first / last |
| `enter` | reveal the file's location in the file manager |
| `alt+enter` | open the file in the default app |
| `space` | unfold a directory · toggle a file's preview |
| `p` | toggle the preview pane |
| `o` `f` | aliases for `alt+enter` and `enter` |
| `e` | open in `$EDITOR`, in a new herdr pane below |
| `s` | type the path into the agent's prompt |
| `y` | copy the path |
| `r` | rescan now |
| `?` | key help |
| `q` | close the dock |

`space` toggles and a double click only opens. That asymmetry is deliberate:
a key you press twice should do something visible both times, while clicking the
row you are already on should not blank half the pane.

`enter` leaves the terminal and `space` does not, which is the split that matters
in a dock: browsing the tree and previewing files never launches anything, and
the two keys that do hand a file to the rest of the machine are the two that say
so. herdr encodes a modified key as ESC followed by the key, so `alt+enter`
arrives as ESC LF; the CSI u and modifyOtherKeys spellings are accepted too.

**Previews.** Text files preview as text. Images preview as images, through
herdr's pane graphics API (`pane.graphics.set`), so they render as a real image
layer over the pane rather than as escape codes in the scrollback. That needs a
Kitty graphics-capable outer terminal — Ghostty, Kitty, WezTerm — and
`[terminal].kitty_graphics` left at its default. Where it is unavailable the
preview falls back to the image's dimensions and size. Non-PNG formats are
converted with `sips`, so inline previews of JPEG/HEIC are macOS-only; PNG works
everywhere.

Images **fit** the preview box rather than filling it. The placement rectangle
is what herdr scales an image to, so handing it the whole box stretches a 16:9
screenshot into whatever shape the pane happens to be — in a narrow dock that is
a 2× distortion. Instead the image is scaled to fit with its aspect intact, and
the rectangle is cut down to the cells it actually occupies and centred, leaving
pane background around it. The residual error is sub-cell: under 1% on a typical
screenshot.

Selecting a file follows it across rescans, so the cursor stays put while the
agent churns files underneath it.

## Actions

| Action | What it does |
| --- | --- |
| `mvaios.scratchdock.toggle` | Dock the focused agent's scratchpad, or close it. Works from either pane. |
| `mvaios.scratchdock.open` | Open the dock. |
| `mvaios.scratchdock.close` | Close it. |
| `mvaios.scratchdock.reveal` | Open the scratchpad folder in the file manager. |
| `mvaios.scratchdock.shell` | Open a shell pane with the scratchpad as its working directory. |
| `mvaios.scratchdock.copy-path` | Copy the scratchpad path to the clipboard. |
| `mvaios.scratchdock.path` | Print the directory it resolves, without opening anything. |

Bind the toggle in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "cmd+shift+s"
type = "plugin_action"
command = "mvaios.scratchdock.toggle"
description = "toggle scratchpad dock"

[[keys.command]]
key = "ctrl+alt+s"
type = "plugin_action"
command = "mvaios.scratchdock.toggle"
description = "toggle scratchpad dock"
```

Then `herdr server reload-config`.

## Automatic docking

The dock opens on `pane.agent_status_changed` when the status becomes `working`,
once per agent pane — a second event while the dock is up is a no-op, so a busy
agent flipping status does not stack panes. Closing either pane cleans up the
other side.

Configure it in `config.env` (copy `config.example.env`):

```sh
cp config.example.env "$(herdr plugin config-dir mvaios.scratchdock)/config.env"
```

| Key | Default | |
| --- | --- | --- |
| `OPEN_ON` | `working` | Statuses that open the dock. Empty disables auto-open. |
| `CLOSE_ON` | *(empty)* | Statuses that close it. Try `idle,done` for a dock that comes and goes. |
| `AGENTS` | `claude` | Agents to dock for, or `*` for all. |
| `DIRECTION` | `right` | `right` or `down`. |
| `RATIO` | `0.32` | Share of the split the dock takes. |
| `FOCUS` | `0` | `1` to focus the dock when it opens. |
| `SCRATCHPAD_ROOT` | *(empty)* | Override `/tmp/claude-<uid>`. |
| `VIEWER` | `builtin` | See below. |

Every key also works as an environment variable with a `SCRATCHDOCK_` prefix.

## Viewers

`VIEWER=builtin` (the default) is the bundled watcher: a live tree, newest first
at every level, sizes and ages, and a `●` on anything touched in the last 30
seconds, plus the keys and previews above. It polls once a second and only
repaints when the tree actually changes.

`VIEWER=file-viewer` hands the pane to
[herdr-file-viewer](https://github.com/smarzban/herdr-file-viewer) when that
plugin is installed — a richer browser with a preview pane, at the cost of
liveness: it reads the tree once and refreshes on a keypress.

`VIEWER=<command>` runs anything else, with the scratchpad as its working
directory.

> A viewer that reads `HERDR_PLUGIN_CONTEXT_JSON` gets it rewritten to point at
> the scratchpad first. herdr injects the *inviting* pane's context, and a viewer
> that trusts `focused_pane_cwd` over its own cwd would otherwise root itself at
> the agent's project instead.

## Limits

- Only Claude Code keeps a per-session scratchpad directory, so `AGENTS=claude`
  is the useful default. Point `SCRATCHPAD_ROOT` elsewhere for another layout.
- The dock never writes to the scratchpad. `o` and `e` hand a file to another
  program, which is then free to do as it likes with it.
- Inline image previews need a Kitty graphics-capable outer terminal, and
  non-PNG formats need `sips` (macOS).
- The dock reports mouse input, so herdr hands clicks to it instead of using
  them for its own selection inside that pane.
- Agents in the same directory at the same time share a resolution (see above).

## License

MIT — see [LICENSE](LICENSE).
