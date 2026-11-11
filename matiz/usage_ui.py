"""Widget de uso das ferramentas de IA: botão na barra + painel com detalhes."""
from __future__ import annotations

import threading
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from gi.repository import Adw  # noqa: E402

from . import claude_limits as CL  # noqa: E402
from . import usage as U  # noqa: E402

REFRESH_S = 60

CSS = """
.uso-painel { padding: 12px; }
.uso-card { background-color: alpha(#ffffff, 0.04); border-radius: 10px; padding: 10px 12px; }
.uso-titulo { font-weight: bold; font-size: 1.05em; }
.uso-col { font-size: 0.85em; opacity: 0.7; }
.uso-barra { background-color: alpha(#88c0d0, 0.85); border-radius: 2px 2px 0 0; }
.uso-barra.vazia { background-color: alpha(#ffffff, 0.08); }
.uso-ponto-ativo { color: #a3be8c; }
.uso-ponto-ocupado { color: #ebcb8b; }
"""


def _label(text, *classes, xalign=0.0, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign, wrap=wrap)
    for c in classes:
        lb.add_css_class(c)
    return lb


def _when(ts: float | None) -> str:
    if not ts:
        return "nunca"
    d = time.time() - ts
    if d < 120:
        return "agora há pouco"
    if d < 3600:
        return f"há {int(d // 60)} min"
    if d < 86400:
        return f"há {int(d // 3600)} h"
    return datetime.fromtimestamp(ts).strftime("em %d/%m/%Y")


class UsageButton(Gtk.MenuButton):
    def __init__(self, run_in_new_tab=None):
        super().__init__(tooltip_text="Uso de IA (Claude Code, OpenCode, Codex…)")
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name="utilities-system-monitor-symbolic"))
        self.short = Gtk.Label(label="IA")
        self.short.add_css_class("numeric")
        box.append(self.short)
        self.set_child(box)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content.add_css_class("uso-painel")
        self.content.append(_label("Carregando…", "dim-label"))
        scroller = Gtk.ScrolledWindow(child=self.content, propagate_natural_height=True,
                                      max_content_height=640, min_content_width=440)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.popover = Gtk.Popover(child=scroller)
        self.set_popover(self.popover)
        self.popover.connect("notify::visible", lambda p, *_: p.get_visible() and self.refresh())

        self.run_in_new_tab = run_in_new_tab
        self._busy = False
        self._data: list[U.ToolUsage] = []
        self._updated = 0.0
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_S, lambda: (self.refresh(), True)[1])

    # ---------- coleta em segundo plano ----------
    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                data = U.collect()
            except Exception as e:  # nunca derruba o terminal
                data = []
                print(f"matiz: falha ao coletar uso: {e}")
            GLib.idle_add(self._apply, data)

        threading.Thread(target=work, daemon=True, name="matiz-uso").start()

    def _apply(self, data):
        self._busy = False
        self._data = data
        self._updated = time.time()
        active = sum(t.active for t in data)
        busy = sum(t.busy for t in data)
        label = f"{active}" + (f" · {busy}⚡" if busy else "") if active else "IA"
        # como no Orca: percentual dos limites do plano direto na barra
        claude = next((t for t in data if t.id == "claude"), None)
        if claude and claude.limits:
            short = {"5 h": "5h", "Semanal": "7d"}
            parts = [f"{short.get(l['nome'], l['nome'])} {l['pct']:.0f}%" for l in claude.limits
                     if l["nome"] in short]
            if parts:
                label = "  ·  ".join(parts)
                worst = max(l["pct"] for l in claude.limits)
                for c in ("warning", "error"):
                    self.short.remove_css_class(c)
                if worst >= 90:
                    self.short.add_css_class("error")
                elif worst >= 70:
                    self.short.add_css_class("warning")
        self.short.set_label(label)
        tip = [f"{t.name}: {t.active} sessão(ões) aberta(s)" + (f", {t.busy} trabalhando" if t.busy else "")
               for t in data if t.installed]
        self.set_tooltip_text("Uso de IA — clique para detalhes\n" + "\n".join(tip))
        self._render()
        return False

    # ---------- desenho ----------
    def _render(self):
        while (c := self.content.get_first_child()) is not None:
            self.content.remove(c)
        if not self._data:
            self.content.append(_label("Nenhuma ferramenta de IA encontrada.", "dim-label"))
        for t in self._data:
            self.content.append(self._card(t))
        foot = Gtk.Box(spacing=6)
        foot.append(_label(f"Atualizado às {datetime.fromtimestamp(self._updated):%H:%M:%S} · "
                           "dados lidos do disco, nada é enviado", "dim-label", "caption", wrap=True))
        btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Atualizar", hexpand=True,
                         halign=Gtk.Align.END)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_: self.refresh())
        foot.append(btn)
        self.content.append(foot)

    def _card(self, t: U.ToolUsage) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("uso-card")

        head = Gtk.Box(spacing=8)
        head.append(_label(t.name, "uso-titulo"))
        if t.active:
            dot = _label("●", "uso-ponto-ocupado" if t.busy else "uso-ponto-ativo")
            head.append(dot)
            status = f"{t.active} aberta{'s' if t.active != 1 else ''}"
            if t.busy:
                status += f" · {t.busy} trabalhando"
        elif not t.installed:
            status = "não instalado"
        else:
            status = f"último uso {_when(t.last_used)}"
        head.append(_label(status, "dim-label", "caption", xalign=1.0))
        head.get_last_child().set_hexpand(True)
        card.append(head)
        card.append(self._login_row(t))

        if not t.has_data:
            card.append(_label("Sem uso nos últimos 7 dias.", "dim-label"))
        else:
            card.append(self._table(t))
            card.append(self._spark(t.hourly))
            if t.models:
                total = sum(n for _, n in t.models) or 1
                top = "  ·  ".join(f"{m.removeprefix('claude-')} {n * 100 // total}%" for m, n in t.models[:3])
                card.append(_label(f"Modelos (7 dias): {top}", "dim-label", "caption", wrap=True))

        if t.id == "claude":
            card.append(self._claude_limits_row(t))
        for lim in t.limits:
            row = Gtk.Box(spacing=8)
            row.append(_label(f"Limite {lim['nome']}", "caption", "numeric"))
            bar = Gtk.LevelBar(min_value=0, max_value=100, value=min(lim["pct"], 100), hexpand=True,
                               valign=Gtk.Align.CENTER)
            row.append(bar)
            reset = lim.get("reinicia")
            txt = f"{lim['pct']:.0f}%" + (f" · zera {datetime.fromtimestamp(reset):%d/%m %H:%M}" if reset else "")
            row.append(_label(txt, "caption", "numeric"))
            if lim["pct"] >= 90:
                bar.add_css_class("error")
            elif lim["pct"] >= 70:
                bar.add_css_class("warning")
            card.append(row)
        if t.limits and t.limits_updated:
            card.append(_label(f"Limites atualizados {_when(t.limits_updated)} "
                               "(a cada resposta do agente)", "dim-label", "caption"))

        if t.note:
            card.append(_label(t.note, "dim-label", "caption", wrap=True))
        return card

    # ---------- login ----------
    def _login_row(self, t: U.ToolUsage) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        cmd = {"claude": ["claude", "auth", "login"], "codex": ["codex", "login"],
               "opencode": ["opencode", "auth", "login"]}.get(t.id)
        if not t.installed:
            hint = {"codex": "npm i -g @openai/codex"}.get(t.id)
            row.append(_label(f"Para instalar: {hint}" if hint else "Não instalado", "dim-label", "caption"))
            return row
        a = t.auth
        if a is None:
            row.append(_label("Login: —", "dim-label", "caption"))
        elif a.get("conectado"):
            plano = f" · plano {a['plano'].capitalize()}" if a.get("plano") else ""
            row.append(_label(f"✓ Conectado{plano}", "caption", "success"))
        else:
            row.append(_label("Não conectado", "caption", "warning"))
        if cmd and self.run_in_new_tab:
            btn = Gtk.Button(label="Trocar conta" if a and a.get("conectado") else "Entrar",
                             halign=Gtk.Align.END, hexpand=True)
            btn.add_css_class("flat" if a and a.get("conectado") else "suggested-action")
            btn.add_css_class("caption")
            btn.connect("clicked", lambda *_: self._login(cmd))
            row.append(btn)
        return row

    def _login(self, cmd):
        self.popover.popdown()
        CL.forget_auth_cache()
        self.run_in_new_tab(cmd)

    # ---------- limites do plano do Claude ----------
    def _claude_limits_row(self, t: U.ToolUsage) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        if t.limits_enabled:
            if not t.limits:
                row.append(_label("Limites do plano: aparecem após a próxima resposta do Claude.",
                                  "dim-label", "caption", wrap=True))
            off = Gtk.Button(label="Desativar", halign=Gtk.Align.END, hexpand=True)
            off.add_css_class("flat")
            off.add_css_class("caption")
            off.connect("clicked", lambda *_: (CL.disable(), self.refresh()))
            row.append(off)
            return row
        msg = ("Limites do plano (5 h / semanal) desativados — o Orca pode ter "
               "reescrito a configuração." if CL.read_limits() else "Ver limites do plano (5 h / semanal)")
        row.append(_label(msg, "caption", wrap=True))
        on = Gtk.Button(label="Ativar", halign=Gtk.Align.END, hexpand=True, valign=Gtk.Align.CENTER)
        on.add_css_class("suggested-action")
        on.add_css_class("caption")
        on.connect("clicked", lambda *_: self._confirm_enable())
        row.append(on)
        return row

    def _confirm_enable(self):
        self.popover.popdown()
        dlg = Adw.AlertDialog(
            heading="Mostrar limites do plano do Claude?",
            body=("O Claude Code informa os limites (5 h e semanal) ao comando da barra de status. "
                  "O Matiz vai instalar um intermediário nessa barra que anota os números e repassa "
                  "tudo para a barra de status atual (a do Orca continua funcionando).\n\n"
                  "Altera ~/.claude/settings.json (com backup em settings.json.matiz-bak). "
                  "Nenhuma senha ou token é lido. Dá para desativar a qualquer momento."))
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", "Ativar")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r == "ok":
                try:
                    CL.enable()
                except OSError as e:
                    print(f"matiz: falha ao ativar limites: {e}")
                self.refresh()

        dlg.connect("response", resp)
        dlg.present(self.get_root())

    def _table(self, t: U.ToolUsage) -> Gtk.Widget:
        g = Gtk.Grid(column_spacing=18, row_spacing=4)
        cols = [("5h", "Últimas 5 h"), ("hoje", "Hoje"), ("7d", "7 dias")]
        for j, (_, title) in enumerate(cols, 1):
            g.attach(_label(title, "uso-col", xalign=1.0), j, 0, 1, 1)
        rows = [
            ("Mensagens", lambda a: f"{a.messages}"),
            ("Tokens de saída", lambda a: U.fmt_tokens(a.out)),
            ("Entrada", lambda a: U.fmt_tokens(a.inp + a.cache_write)),
            ("Lidos do cache", lambda a: U.fmt_tokens(a.cache_read)),
        ]
        if any(a.cost is not None for a in t.windows.values()):
            rows.append(("Custo", lambda a: f"US$ {a.cost:.2f}" if a.cost is not None else "—"))
        for i, (name, fn) in enumerate(rows, 1):
            g.attach(_label(name, "dim-label"), 0, i, 1, 1)
            for j, (key, _) in enumerate(cols, 1):
                g.attach(_label(fn(t.windows[key]), "numeric", xalign=1.0), j, i, 1, 1)
        g.get_child_at(0, 1).set_hexpand(True)
        return g

    def _spark(self, hourly: list[int]) -> Gtk.Widget:
        """Barras das últimas 24 h (sem cairo: caixas com altura proporcional)."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bars = Gtk.Box(spacing=2, height_request=34, homogeneous=True)
        top = max(hourly) or 1
        now_h = datetime.now().hour
        for i, v in enumerate(hourly):
            h = max(2, round(32 * v / top))
            b = Gtk.Box(height_request=h, valign=Gtk.Align.END, hexpand=True)
            b.add_css_class("uso-barra")
            if not v:
                b.add_css_class("vazia")
            hour = (now_h - (23 - i)) % 24
            b.set_tooltip_text(f"{hour:02d}h: {U.fmt_tokens(v)} tokens")
            bars.append(b)
        outer.append(bars)
        axis = Gtk.Box()
        axis.append(_label("24 h atrás", "dim-label", "caption"))
        axis.append(_label("agora", "dim-label", "caption", xalign=1.0))
        axis.get_last_child().set_hexpand(True)
        outer.append(axis)
        return outer
