# Matiz

A terminal for Linux/GNOME written in Python with GTK 4, libadwaita and VTE (the same
emulation engine used by GNOME Terminal and Ptyxis). It is designed for people who work with
AI agents in the terminal: colored tabs to tell at a glance what runs where, sessions that
survive closing the window or rebooting the computer, and a panel with Claude Code, OpenCode
and Codex usage.

## Features

- **Colored tabs.** Right-click the tab title (or press `Ctrl+Alt+1…8`) to pick a color. It
  shows up on the tab, as a stripe at the top, in the terminal background and in the window
  header.
- **Persistent sessions.** A background process (`matiz-daemon`) owns the shells:
  - when you **close the window**, everything keeps running; when you reopen it, each tab
    reconnects and the screen is redrawn;
  - when you **reboot**, Matiz recreates the tabs from the saved state: Claude Code comes
    back in the same conversation (`claude --resume <id>`), safe programs (ssh, vim, htop,
    `npm run dev`…) are run again, and any other command is typed at the prompt, waiting for
    your Enter. Nothing dangerous (`sudo`, `rm`…) is ever run automatically.
- **Split panes.** Split a tab side by side or top/bottom. Each pane is an independent session
  and the layout is restored.
- **Notifications.** When a long-running command finishes, or Claude Code finishes replying,
  in a tab you are not looking at, the tab is highlighted and, if the window is in the
  background, a desktop notification is shown.
- **Ctrl+click** on URLs, paths and `file:line:column` (including Python tracebacks): opens in
  the browser, in your editor at the right line, or in the file manager.
- **File tree** (`Ctrl+Shift+B`) in a sidebar, following the active pane's directory:
  double-click opens a file in your editor; the context menu offers `cd`, insert path into the
  terminal, new tab in that folder, copy path and more.
- **AI usage widget** in the header bar: open sessions, login status, messages and tokens
  (5h / today / 7 days), cost when the tool records it, a chart of the last 24 h and the
  **percentage of your plan limits** (5h / weekly). Everything is read from local files.
- Scrollback search, save scrollback to a file, open the current folder in another app,
  pinned tabs, a menu with every shortcut (`Ctrl+?`).

## Requirements

- Linux with GTK ≥ 4.14 and libadwaita ≥ 1.8 (tested on Ubuntu 26.04 / GNOME 50)
- Python ≥ 3.10 with PyGObject
- VTE for GTK 4 (`Vte 3.91`)

On Debian/Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91
```

There are no PyPI dependencies.

## Installation

```bash
git clone <repository-url> matiz
cd matiz
./bin/matiz
```

To show it in the application menu, create `~/.local/share/applications/dev.matiz.Terminal.desktop`
(replace the path with where you cloned it):

```ini
[Desktop Entry]
Name=Matiz
Comment=Terminal with colored tabs and persistent sessions
Exec=/path/to/matiz/bin/matiz
Icon=utilities-terminal
Terminal=false
Type=Application
Categories=System;TerminalEmulator;
StartupWMClass=dev.matiz.Terminal
```

To open it with `Ctrl+Alt+T` on GNOME, free the default shortcut and create a custom one:

```bash
K=org.gnome.settings-daemon.plugins.media-keys
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/matiz/
gsettings set $K terminal "[]"
gsettings set $K.custom-keybinding:$P name 'Matiz'
gsettings set $K.custom-keybinding:$P command "$PWD/bin/matiz"
gsettings set $K.custom-keybinding:$P binding '<Primary><Alt>t'
gsettings set $K custom-keybindings "['$P']"
```

## Shortcuts

They are also available in the ☰ menu, by right-clicking the tab title and by right-clicking
inside the terminal. `Ctrl+?` or `F1` show the full list.

| Action | Shortcut |
|---|---|
| New tab / new tab in this folder | `Ctrl+Shift+T` / `Ctrl+Shift+D` |
| Close pane (or the tab, if it is the only one) | `Ctrl+Shift+W` or the ✕ in the pane corner |
| Split side by side / top and bottom | `Alt+Shift++` / `Alt+Shift+-` |
| Go to neighboring pane | `Alt+←` `↑` `→` `↓` |
| Tab color / no color | `Ctrl+Alt+1…8` / `Ctrl+Alt+0` |
| Rename tab | `Ctrl+Shift+R` |
| Copy / paste | `Ctrl+Shift+C` / `Ctrl+Shift+V` |
| File tree for the current folder | `Ctrl+Shift+B` |
| Open folder in the file manager | `Ctrl+Shift+E` |
| Copy folder path | `Ctrl+Shift+Y` |
| Search the terminal | `Ctrl+Shift+F` (Enter ↑, Shift+Enter ↓, Esc closes) |
| Save scrollback to a file | `Ctrl+Shift+S` |
| Clear screen and scrollback | `Ctrl+Shift+K` |
| Next / previous tab | `Ctrl+PgDn` / `Ctrl+PgUp` |
| Move tab | `Ctrl+Shift+PgDn` / `Ctrl+Shift+PgUp` |
| Go to tab N | `Alt+1…9` |
| Zoom | `Ctrl++` / `Ctrl+-` / `Ctrl+0` |
| Open link or file:line | `Ctrl+click` |

## AI usage widget

| Tool | Data source |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` (tokens per message) and `~/.claude/sessions/` (open sessions) |
| OpenCode | `~/.local/share/opencode/opencode.db` (tokens and cost), opened read-only |
| Codex | `~/.codex/sessions/**/*.jsonl` (tokens and plan limit percentage) |

Login status is checked with the official commands (`claude auth status`, `codex login status`);
the **Log in** button opens a tab running `claude auth login` / `codex login`. Matiz does not
read passwords or tokens and makes no network requests.

**Claude plan limits.** Claude Code only exposes the plan limit percentages (5h, weekly…) to
the command configured as its status line (`statusLine`). In the widget, the **Enable** button
installs `bin/matiz-statusline` as a wrapper in `~/.claude/settings.json`: it records the
numbers for Matiz and passes its input, unchanged, to the status line that was already
configured. A backup is written to `settings.json.matiz-bak` before any change; **Disable**
restores the previous setting. If the Matiz folder is removed, the command falls back to the
original status line.

## Configuration

Optional, in `~/.config/matiz/config.json`:

```json
{
  "rerun_extra": [["python", "manage.py", "runserver"], ["docker", "compose", "up"]],
  "persist_processes": true,
  "saved_scrollback_lines": 3000,
  "scrollback_lines": 20000,
  "font": "JetBrains Mono 11",
  "notify_after_seconds": 10,
  "notifications": true,
  "editor": ["code", "--goto", "{file}:{line}:{col}"],
  "usage_widget": true,
  "language": "en"
}
```

| Key | Effect |
|---|---|
| `rerun_extra` | Commands (argv prefixes) that may be re-run automatically on restore |
| `persist_processes` | `false` disables the daemon: processes die when the window closes |
| `saved_scrollback_lines` | Scrollback lines written to disk per pane |
| `scrollback_lines` | Scrollback lines kept in memory per pane |
| `font` | Terminal font (default: the system monospace font) |
| `notify_after_seconds` | Minimum command duration that triggers a notification |
| `notifications` | `false` disables desktop notifications |
| `editor` | Command used to open `file:line` on Ctrl+click (default: VS Code, Cursor, Zed or Sublime, whichever is installed) |
| `usage_widget` | `false` hides the usage widget |
| `language` | Forces the interface language (e.g. `"en"`, `"pt_BR"`); by default the system language is used |

## How it works

```
Matiz (GTK window)  ──Unix socket──▶  matiz-daemon  ──▶  one PTY per pane → shell → programs
```

| File | Role |
|---|---|
| `matiz/app.py` | Window, tabs, menus, shortcuts, notifications, save/restore cycle |
| `matiz/tab.py` | Tab: color, title and pane tree (`Gtk.Paned`) |
| `matiz/pane.py` | Pane: `Vte.Terminal`, daemon connection, links, detection of what is running |
| `matiz/daemon.py` | Daemon that keeps PTYs alive and buffers recent output for redrawing |
| `matiz/protocol.py` | Simple binary protocol between window and daemon |
| `matiz/procinfo.py` | Reads `/proc`: foreground process, working directory, how to resume each program |
| `matiz/session.py` | On-disk state (atomic writes) and configuration |
| `matiz/i18n.py`, `po/`, `locale/` | Translations (gettext) |
| `matiz/filetree.py` | File tree sidebar |
| `matiz/usage.py`, `matiz/usage_ui.py` | AI usage collection and display |
| `matiz/claude_limits.py`, `bin/matiz-statusline` | Claude plan limits via `statusLine` |

Files created at runtime (none of them inside the repository):

| Path | Contents |
|---|---|
| `~/.local/state/matiz/session.json` | Tabs, colors, pane layout, command to resume |
| `~/.local/state/matiz/scrollback/` | Text scrollback of each pane |
| `~/.local/state/matiz/claude-limits.json` | Latest Claude plan limit percentages |
| `$XDG_RUNTIME_DIR/matiz/` | Daemon socket and log |

State is written atomically every 2 s, when the window closes and on SIGTERM.

## Limitations

- Rebooting ends the processes: Matiz recreates the sessions, but a reply Claude was
  generating at that moment is interrupted.
- Redrawing the screen on reconnect uses the last 4 MB of output of each pane.
- After updating the code, close all tabs so a new daemon is started.
- One window at a time.

## Translations

The interface follows the system language (`LANGUAGE` / `LANG`) and falls back to English.
Available: English and Brazilian Portuguese (`pt_BR`).

Source strings are English and marked with `_()` / `ngettext()` (see `matiz/i18n.py`).
Translations live in `po/<lang>.po` and are compiled to `locale/<lang>/LC_MESSAGES/matiz.mo`.
Requires the gettext tools (`sudo apt install gettext`).

```bash
tools/update-translations            # after changing strings: update .pot/.po and recompile
tools/update-translations --new fr   # start a new translation in po/fr.po
```

Then translate the `msgstr` entries in `po/<lang>.po` (with any text editor or a PO editor
such as Poedit/Gtranslator) and run `tools/update-translations` again. The tests check that
every translation is complete, keeps the placeholders and is compiled.

## Tests

```bash
python3 -m unittest discover -s tests
```

The daemon and process detection tests spawn real processes and use temporary directories;
none of them touch the user's configuration.

## License

[MIT](LICENSE) © 2026 Eduardo Freitas
