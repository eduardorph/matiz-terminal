"""Um painel: VteTerminal + sessão (daemon ou local) + rastreamento do que roda."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import uuid

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

from . import client, procinfo, protocol, session  # noqa: E402

# Paleta Nord (fundo, texto e 16 cores ANSI)
BASE_BG = "#2e3440"
BASE_FG = "#d8dee9"
PALETTE = [
    "#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#88c0d0", "#e5e9f0",
    "#4c566a", "#d08770", "#b5d19c", "#f0d399", "#94b4d4", "#c4a3c0", "#8fbcbb", "#eceff4",
]

ENV_DROP = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "VTE_VERSION",
            "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "GIO_LAUNCHED_DESKTOP_FILE",
            "GIO_LAUNCHED_DESKTOP_FILE_PID"}

# --- padrões clicáveis (Ctrl+clique) ---
PCRE2_CASELESS, PCRE2_MULTILINE, PCRE2_UTF = 0x8, 0x400, 0x80000
URL_RE = r"(?:https?|ftp|file)://[^\s<>\"'`]*[^\s<>\"'`.,;:!?)\]}]"
# arquivo com extensão, opcionalmente :linha[:coluna]  (ex.: src/app.py:42:7, ./a.ts, ~/x.md)
# ou qualquer caminho que comece com /, ./, ../ ou ~/ (ex.: /etc/hostname, ./Makefile)
PATH_RE = (r"(?<![\w/.~-])(?:"
           r"(?:~/|\.{1,2}/|/)?(?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9]{0,7}"
           r"|(?:~|\.{1,2})?/[\w@+-][\w.@+-]*(?:/[\w.@+-]+)*"
           r")(?::\d+(?::\d+)?)?")
# traceback do Python: File "/x/y.py", line 42
PYTB_RE = r'File "[^"]+", line \d+'
_PATH_SPLIT = re.compile(r"^(.*?)(?::(\d+))?(?::(\d+))?$")


def rgba(hexstr: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(hexstr)
    return c


def mix(a: str, b: str, t: float) -> str:
    """Mistura duas cores hex: t=0 → a, t=1 → b."""
    ca, cb = rgba(a), rgba(b)
    r = ca.red + (cb.red - ca.red) * t
    g = ca.green + (cb.green - ca.green) * t
    bl = ca.blue + (cb.blue - ca.blue) * t
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(bl * 255))


def default_font(config: dict) -> Pango.FontDescription:
    name = config.get("fonte")
    if not name:
        try:
            name = Gio.Settings.new("org.gnome.desktop.interface").get_string("monospace-font-name")
        except Exception:
            name = None
    return Pango.FontDescription.from_string(name or "Monospace 11")


def parse_link(text: str, tag_kind: str) -> tuple[str, int | None, int | None]:
    """Converte o texto clicado em (caminho, linha, coluna)."""
    if tag_kind == "pytb":
        m = re.match(r'File "([^"]+)", line (\d+)', text)
        return (m.group(1), int(m.group(2)), None) if m else (text, None, None)
    m = _PATH_SPLIT.match(text)
    path, line, col = m.group(1), m.group(2), m.group(3)
    return path, int(line) if line else None, int(col) if col else None


class Pane(Gtk.Box):
    def __init__(self, tab, *, pane_id=None, config=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("matiz-pane")
        self.tab = tab
        self.window = tab.window
        self.config = config or {}
        self.pane_id = pane_id or uuid.uuid4().hex
        self.shell_pid: int | None = None
        self.cwd: str = os.path.expanduser("~")
        self.resume: dict | None = None
        self.fg_name: str | None = None
        self._misses = 0
        self.dirty = False
        self.exited = False
        self._pending_resume: dict | None = None
        self._pending_scrollback: str | None = None
        self._resume_typed = False
        self.sock = None          # conexão com o matiz-daemon (modo daemon)
        self.dec = None
        self.attached = False     # True se reconectou a uma sessão já viva
        self._commit_id = None
        self._watch = None
        self._size = (0, 0)
        self._mute_until = 0.0
        # notificações
        self.busy_since: float | None = None   # comando em primeiro plano desde…
        self.busy_cmd: str | None = None
        self.claude_status: str | None = None
        self.claude_busy_since: float | None = None

        self.term = Vte.Terminal(hexpand=True, vexpand=True)
        self.term.set_font(default_font(self.config))
        self.term.set_scrollback_lines(int(self.config.get("linhas_rolagem", 20000)))
        self.term.set_audible_bell(False)
        self.term.set_mouse_autohide(True)
        self.term.set_allow_hyperlink(True)
        self.term.set_scroll_on_keystroke(True)
        self.term.set_scroll_on_output(False)
        self.term.connect("child-exited", self._on_child_exited)
        self.term.connect("contents-changed", self._on_contents_changed)
        self.term.connect("bell", lambda *_: self.window.on_pane_bell(self))
        self._setup_links()

        # botão direito: foca este painel e mostra o menu (Copiar, Colar, Cor, Painéis…)
        self.term.set_context_menu_model(self.window.term_menu)
        rclick = Gtk.GestureClick(button=3)
        rclick.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        rclick.connect("pressed", lambda *_: (self.term.grab_focus(), self.window._fill_open_with()))
        self.term.add_controller(rclick)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.tab.on_pane_focused(self))
        self.term.add_controller(focus)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, child=self.term)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # ✕ no canto: só aparece quando a aba está dividida (ver Tab._update_pane_marks)
        self.close_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Fechar painel (Ctrl+Shift+W)",
                                    halign=Gtk.Align.END, valign=Gtk.Align.START,
                                    margin_top=6, margin_end=16, focus_on_click=False, visible=False)
        self.close_btn.add_css_class("circular")
        self.close_btn.add_css_class("osd")
        self.close_btn.add_css_class("matiz-fechar-painel")
        self.close_btn.connect("clicked", lambda *_: self.window._close_pane(self))
        overlay = Gtk.Overlay(child=scroller, hexpand=True, vexpand=True)
        overlay.add_overlay(self.close_btn)
        self.append(overlay)

    # ---------- aparência ----------
    def apply_color(self, color: str | None):
        bg = mix(BASE_BG, color, 0.13) if color else BASE_BG
        self.term.set_colors(rgba(BASE_FG), rgba(bg), [rgba(c) for c in PALETTE])

    def display_title(self) -> str:
        home = os.path.expanduser("~")
        cwd = self.cwd
        base = os.path.basename(cwd.rstrip("/")) or "/"
        if self.resume and self.resume.get("kind") == "claude":
            return f"Claude · {base}"
        if self.fg_name:
            return f"{self.fg_name} · {base}"
        return "Pasta pessoal" if cwd == home else base

    # ---------- links (Ctrl+clique) ----------
    def _setup_links(self):
        self._link_tags: dict[int, str] = {}
        for kind, pat in (("url", URL_RE), ("pytb", PYTB_RE), ("path", PATH_RE)):
            try:
                rx = Vte.Regex.new_for_match(pat, -1, PCRE2_MULTILINE | PCRE2_UTF)
            except GLib.Error as e:
                print(f"matiz: regex {kind} inválida: {e.message}", file=sys.stderr)
                continue
            tag = self.term.match_add_regex(rx, 0)
            self.term.match_set_cursor_name(tag, "pointer")
            self._link_tags[tag] = kind
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_click)
        self.term.add_controller(click)

    def _on_click(self, gesture, _n, x, y):
        state = gesture.get_current_event_state()
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return
        target = self.link_at(x, y)
        if target and self.window.open_link(self, *target):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def link_at(self, x, y):
        """('url', uri) | ('file', path, linha, coluna) | None"""
        try:
            uri = self.term.check_hyperlink_at(x, y)  # OSC 8
        except Exception:
            uri = None
        if uri:
            return ("url", uri)
        res = self.term.check_match_at(x, y)
        text, tag = (res if isinstance(res, tuple) else (res, -1))
        if not text:
            return None
        kind = self._link_tags.get(tag, "path")
        if kind == "url":
            return ("url", text)
        path, line, col = parse_link(text, kind)
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.current_dir(), path)
        path = os.path.normpath(path)
        if not os.path.exists(path):
            return None
        return ("file", path, line, col)

    def current_dir(self) -> str:
        """Pasta do programa em primeiro plano (ou da shell)."""
        if self.shell_pid:
            fg = procinfo.foreground_pid_of(self.shell_pid)
            d = procinfo.read_cwd(fg) if fg else None
            return d or procinfo.read_cwd(self.shell_pid) or self.cwd
        return self.cwd

    # ---------- processo ----------
    def _env(self) -> list[str]:
        env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
        env["MATIZ_TAB_ID"] = self.pane_id
        env["COLORTERM"] = "truecolor"
        return [f"{k}={v}" for k, v in env.items()]

    def start(self, cwd: str | None = None, resume: dict | None = None, scrollback: str | None = None):
        if cwd and os.path.isdir(cwd):
            self.cwd = cwd
        self._pending_resume = resume
        self._resume_typed = False
        self._pending_scrollback = scrollback
        if self.window.use_daemon:
            try:
                self._start_daemon()
                return
            except OSError as e:
                print(f"matiz: daemon indisponível ({e}); usando modo local", file=sys.stderr)
        self._start_local()

    # --- modo local: o VTE é dono do PTY (processos morrem com a janela) ---
    def _start_local(self):
        if self._pending_scrollback:
            self._feed_scrollback(self._pending_scrollback)
            self._pending_scrollback = None
        shell = os.environ.get("SHELL") or "/bin/bash"
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, self.cwd, [shell], self._env(),
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, self._on_spawned, None,
        )

    # --- modo daemon: o matiz-daemon é dono do PTY (processos sobrevivem) ---
    def _start_daemon(self):
        sock = client.connect()
        self.sock = sock
        self.dec = protocol.Decoder()
        self._size = (self.term.get_row_count(), self.term.get_column_count())
        shell = os.environ.get("SHELL") or "/bin/bash"
        env = self._env() + ["TERM=xterm-256color", "VTE_VERSION=8400"]
        sock.sendall(protocol.jframe(b"H", {
            "op": "open", "proto": protocol.PROTO, "id": self.pane_id, "cwd": self.cwd,
            "argv": [shell], "env": env, "rows": self._size[0], "cols": self._size[1],
        }))
        if self._commit_id is None:
            self._commit_id = self.term.connect("commit", self._on_commit)
        self._watch = GLib.io_add_watch(
            sock.fileno(), GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, self._on_sock,
        )

    def _send(self, kind: bytes, payload: bytes = b""):
        if not self.sock:
            return
        try:
            self.sock.sendall(protocol.frame(kind, payload))
        except OSError:
            pass  # a desconexão é tratada em _on_sock

    def _on_commit(self, _term, text, _size):
        # Tudo que o VTE mandaria para o processo (teclas, colagem, respostas
        # a consultas do terminal) vai para o daemon. Durante a reexibição do
        # buffer, respostas a consultas antigas são descartadas.
        if self.sock and time.monotonic() >= self._mute_until:
            self._send(b"I", text.encode("utf-8", "surrogateescape"))

    def _on_sock(self, _fd, cond):
        data = b""
        if cond & GLib.IOCondition.IN:
            try:
                data = self.sock.recv(262144)
            except OSError:
                data = b""
        if not data:
            self._on_disconnect()
            return False
        try:
            msgs = self.dec.feed(data)
        except ValueError:
            self._on_disconnect()
            return False
        for kind, payload in msgs:
            if kind == b"O":
                self.term.feed(payload)
            elif kind == b"S":
                info = json.loads(payload)
                self.shell_pid = info["pid"]
                self.attached = bool(info.get("attached"))
                if self.attached:
                    # Reconectou: a sessão continua viva, nada a retomar.
                    self._pending_resume = None
                    self._pending_scrollback = None
                    self._mute_until = time.monotonic() + 0.6
                else:
                    if self._pending_scrollback:
                        self._feed_scrollback(self._pending_scrollback)
                        self._pending_scrollback = None
                    if self._pending_resume:
                        GLib.timeout_add(1500, self._type_resume)
                self.check_size(force=True)
            elif kind == b"X":
                self._close_sock()
                self._on_child_exited(None, 0)
                return False
            elif kind == b"E":
                msg = json.loads(payload).get("error", "?")
                self.term.feed(f"\r\n\x1b[31mmatiz-daemon: {msg}\x1b[0m\r\n".encode())
        return True

    def _close_sock(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _on_disconnect(self):
        """O daemon caiu sem a shell ter saído: recria a sessão e retoma o
        que estava rodando (mesma lógica de quando o PC reinicia)."""
        self._close_sock()
        if self.window.quitting or self.exited:
            return
        self.shell_pid = None
        self.term.feed(b"\r\n\x1b[33m-- conexao com o matiz-daemon perdida; recriando a sessao --\x1b[0m\r\n")
        resume = self.resume
        GLib.timeout_add(800, lambda: (self.start(cwd=self.cwd, resume=resume), False)[1])

    def check_size(self, force=False):
        if not self.sock:
            return
        size = (self.term.get_row_count(), self.term.get_column_count())
        if force or size != self._size:
            self._size = size
            self._send(b"R", json.dumps({"rows": size[0], "cols": size[1]}).encode())

    # ---------- busca / limpar / exportar ----------
    def set_search(self, text: str):
        """Define a busca. Retorna True (ok), None (vazia) ou False (inválida)."""
        if not text:
            self.term.search_set_regex(None, 0)
            return None
        flags = PCRE2_MULTILINE | PCRE2_UTF | (0 if any(c.isupper() for c in text) else PCRE2_CASELESS)
        try:
            regex = Vte.Regex.new_for_search(re.escape(text), -1, flags)
        except GLib.Error:
            return False
        self.term.search_set_regex(regex, 0)
        self.term.search_set_wrap_around(True)
        return True

    def clear(self):
        self.term.reset(True, True)
        if self.sock:
            self._send(b"C")  # o daemon também esquece a saída guardada
        if not self.fg_name:
            self.term.feed_child(b"\x0c")  # Ctrl+L: a shell redesenha o prompt
        session.scrollback_path(self.pane_id).unlink(missing_ok=True)

    def full_text(self) -> str:
        try:
            vadj = self.term.get_vadjustment()
            res = self.term.get_text_range_format(
                Vte.Format.TEXT, int(vadj.get_lower()), 0,
                int(vadj.get_upper()) - 1, self.term.get_column_count())
        except Exception:
            res = self.term.get_text_format(Vte.Format.TEXT)
        text = res[0] if isinstance(res, tuple) else res
        return (text or "").rstrip() + "\n"

    def kill_session(self):
        """Painel/aba fechado pelo usuário: encerra a sessão no daemon."""
        if self.sock:
            self._send(b"K")
            self._close_sock()

    def _feed_scrollback(self, text: str):
        lines = text.rstrip("\n").split("\n")
        body = "\r\n".join(lines)
        stamp = time.strftime("%d/%m %H:%M")
        banner = f"\x1b[38;2;136;192;208m── sessão restaurada ({stamp}) ──\x1b[0m"
        self.term.feed(f"\x1b[2m{body}\x1b[0m\r\n{banner}\r\n".encode())

    def _on_spawned(self, _term, pid, error, *_):
        if error is not None or pid <= 0:
            msg = getattr(error, "message", str(error))
            self.term.feed(f"\r\n\x1b[31mFalha ao iniciar a shell: {msg}\x1b[0m\r\n".encode())
            return
        self.shell_pid = pid
        if self._pending_resume:
            GLib.timeout_add(1500, self._type_resume)

    def _on_contents_changed(self, *_):
        self.dirty = True
        if self._pending_resume and self.shell_pid and not self._resume_typed:
            GLib.timeout_add(200, self._type_resume)

    def _type_resume(self):
        r = self._pending_resume
        if not r or self._resume_typed:
            return False
        self._resume_typed = True
        self._pending_resume = None
        text = procinfo.command_line(r["argv"])
        rcwd = r.get("cwd")
        if r.get("kind") == "claude" and rcwd and os.path.realpath(rcwd) != os.path.realpath(self.cwd):
            text = f"cd {shlex.quote(rcwd)} && {text}"
        if r.get("mode") == "run":
            text += "\n"
            self.resume = r  # já conhecido; evita "piscar" antes da próxima sondagem
        self.term.feed_child(text.encode())
        return False

    def _on_child_exited(self, _term, _status):
        self.exited = True
        self.shell_pid = None
        self.window.on_pane_exited(self)

    def probe(self, rerun, authoritative=False):
        """Atualiza cwd/processo em primeiro plano/comando para retomar e
        detecta eventos para notificação (comando longo terminou, Claude
        terminou de responder)."""
        if not self.shell_pid or self.exited:
            return
        fg = procinfo.foreground_pid_of(self.shell_pid)
        cand = procinfo.resume_for(fg, rerun) if fg else None
        now = time.monotonic()

        # --- comando longo terminou? ---
        if fg and self.busy_since is None:
            self.busy_since = now
            # o Claude fica em primeiro plano o tempo todo: tem aviso próprio
            is_claude = bool(cand and cand.get("kind") == "claude")
            self.busy_cmd = procinfo.command_line(cand["argv"]) if cand and not is_claude else None
        elif not fg and self.busy_since is not None:
            took = now - self.busy_since
            cmd = self.busy_cmd
            self.busy_since = self.busy_cmd = None
            if cmd:
                self.window.on_command_finished(self, cmd, took)

        # --- Claude terminou de responder? ---
        status = None
        if cand and cand.get("kind") == "claude":
            sess = procinfo.claude_session(fg)
            status = (sess or {}).get("status")
        if status == "busy" and self.claude_status != "busy":
            self.claude_busy_since = now
        elif self.claude_status == "busy" and status and status != "busy":
            took = now - (self.claude_busy_since or now)
            self.window.on_claude_finished(self, status, took)
        self.claude_status = status

        if cand:
            self.resume = cand
            self._misses = 0
        else:
            self._misses += 1
            # Durante desligamento os processos podem morrer antes de nós:
            # só esquecemos o comando após várias sondagens sem ele.
            if authoritative or self._misses >= 3:
                self.resume = None
        self.fg_name = cand["label"] if cand else None
        shell_cwd = procinfo.read_cwd(self.shell_pid)
        if self.resume and self.resume.get("kind") == "claude" and self.resume.get("cwd"):
            self.cwd = self.resume["cwd"]
        elif shell_cwd:
            self.cwd = shell_cwd

    def scrollback_text(self, max_lines: int) -> str | None:
        """Texto da tela + histórico (sem cores). Não salva para apps que
        redesenham a tela inteira ao retomar (Claude, vim, htop, ssh...)."""
        if self.resume and self.resume.get("mode") == "run":
            return None
        try:
            vadj = self.term.get_vadjustment()
            upper = int(vadj.get_upper())
            start = max(int(vadj.get_lower()), upper - max_lines)
            res = self.term.get_text_range_format(
                Vte.Format.TEXT, start, 0, upper - 1, self.term.get_column_count()
            )
            text = res[0] if isinstance(res, tuple) else res
        except Exception:
            try:
                res = self.term.get_text_format(Vte.Format.TEXT)
                text = res[0] if isinstance(res, tuple) else res
            except Exception:
                return None
        if not text:
            return None
        lines = text.rstrip().split("\n")
        if len(lines) > 1:
            lines = lines[:-1]  # remove o prompt vazio final
        return "\n".join(lines) if any(l.strip() for l in lines) else None

    def to_state(self) -> dict:
        return {"id": self.pane_id, "cwd": self.cwd, "resume": self.resume}
