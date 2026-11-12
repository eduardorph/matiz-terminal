"""AI tool usage widget: header bar button + popover with details."""
from __future__ import annotations

import threading
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from gi.repository import Adw  # noqa: E402

import re  # noqa: E402

from . import claude_limits as CL  # noqa: E402
from . import usage as U  # noqa: E402
from .i18n import _, fmt_decimal, fmt_time, ngettext  # noqa: E402

REFRESH_S = 60

CSS = """
.usage-panel { padding: 12px; }
.usage-card { background-color: alpha(#ffffff, 0.04); border-radius: 10px; padding: 10px 12px; }
.usage-title { font-weight: bold; font-size: 1.05em; }
.usage-col { font-size: 0.85em; opacity: 0.7; }
.usage-bar { background-color: alpha(#88c0d0, 0.85); border-radius: 2px 2px 0 0; }
.usage-bar.empty { background-color: alpha(#ffffff, 0.08); }
.usage-dot-active { color: #a3be8c; }
.usage-dot-busy { color: #ebcb8b; }
"""


def _label(text, *classes, xalign=0.0, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign, wrap=wrap)
    for c in classes:
        lb.add_css_class(c)
    return lb


def _when(ts: float | None) -> str:
    if not ts:
        return _("never")
    d = time.time() - ts
    if d < 120:
        return _("just now")
    if d < 3600:
        n = int(d // 60)
        return ngettext("{n} minute ago", "{n} minutes ago", n).format(n=n)
    if d < 86400:
        n = int(d // 3600)
        return ngettext("{n} hour ago", "{n} hours ago", n).format(n=n)
    # Translators: strftime format for a past date, e.g. "on 2026-09-28"
    return fmt_time(ts, _("on %Y-%m-%d"))


def _limit_name(name: str) -> str:
    """Limit names are stored in English; translate for display."""
    m = re.fullmatch(r"(\d+)h", name)
    if m:
        # Translators: limit window of N hours, e.g. "5h"
        return _("{n}h").format(n=m.group(1))
    m = re.fullmatch(r"(\d+)d", name)
    if m:
        # Translators: limit window of N days, e.g. "3d"
        return _("{n}d").format(n=m.group(1))
    return _(name)


class UsageButton(Gtk.MenuButton):
    def __init__(self, run_in_new_tab=None):
        super().__init__(tooltip_text=_("AI usage (Claude Code, OpenCode, Codex…)"))
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name="utilities-system-monitor-symbolic"))
        # Translators: short label of the AI usage button in the header bar
        self.short = Gtk.Label(label=_("AI"))
        self.short.add_css_class("numeric")
        box.append(self.short)
        self.set_child(box)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content.add_css_class("usage-panel")
        self.content.append(_label(_("Loading…"), "dim-label"))
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

    # ---------- background collection ----------
    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                data = U.collect()
            except Exception as e:  # never take the terminal down
                data = []
                print(f"matiz: failed to collect usage: {e}")
            GLib.idle_add(self._apply, data)

        threading.Thread(target=work, daemon=True, name="matiz-usage").start()

    def _apply(self, data):
        self._busy = False
        self._data = data
        self._updated = time.time()
        active = sum(t.active for t in data)
        busy = sum(t.busy for t in data)
        label = f"{active}" + (f" · {busy}⚡" if busy else "") if active else _("AI")
        # like Orca: plan limit percentages right in the header bar
        claude = next((t for t in data if t.id == "claude"), None)
        if claude and claude.limits:
            # mapped on the untranslated (English) names
            # Translators: very short names for the 5-hour and weekly limits in the header bar
            short = {"5h": _("5h"), "Weekly": _("7d")}
            parts = [_("{name} {pct}%").format(name=short[l["name"]], pct=f"{l['pct']:.0f}")
                     for l in claude.limits if l["name"] in short]
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
        tip = []
        for t in data:
            if not t.installed:
                continue
            line = ngettext("{tool}: {n} open session", "{tool}: {n} open sessions",
                            t.active).format(tool=t.name, n=t.active)
            if t.busy:
                line = _("{sessions}, {n} working").format(sessions=line, n=t.busy)
            tip.append(line)
        self.set_tooltip_text(_("AI usage — click for details") + "\n" + "\n".join(tip))
        self._render()
        return False

    # ---------- rendering ----------
    def _render(self):
        while (c := self.content.get_first_child()) is not None:
            self.content.remove(c)
        if not self._data:
            self.content.append(_label(_("No AI tools found."), "dim-label"))
        for t in self._data:
            self.content.append(self._card(t))
        foot = Gtk.Box(spacing=6)
        # Translators: strftime format for the time of the last refresh
        when = fmt_time(self._updated, _("%H:%M:%S"))
        foot.append(_label(_("Updated at {time} · data read from disk, nothing is sent").format(time=when),
                           "dim-label", "caption", wrap=True))
        btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"), hexpand=True,
                         halign=Gtk.Align.END)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_: self.refresh())
        foot.append(btn)
        self.content.append(foot)

    def _card(self, t: U.ToolUsage) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("usage-card")

        head = Gtk.Box(spacing=8)
        head.append(_label(t.name, "usage-title"))
        if t.active:
            dot = _label("●", "usage-dot-busy" if t.busy else "usage-dot-active")
            head.append(dot)
            status = ngettext("{n} open", "{n} open", t.active).format(n=t.active)
            if t.busy:
                status = _("{open} · {n} working").format(open=status, n=t.busy)
        elif not t.installed:
            status = _("not installed")
        else:
            status = _("last used {when}").format(when=_when(t.last_used))
        head.append(_label(status, "dim-label", "caption", xalign=1.0))
        head.get_last_child().set_hexpand(True)
        card.append(head)
        card.append(self._login_row(t))

        if not t.has_data:
            card.append(_label(_("No usage in the last 7 days."), "dim-label"))
        else:
            card.append(self._table(t))
            card.append(self._spark(t.hourly))
            if t.models:
                total = sum(n for _, n in t.models) or 1
                top = "  ·  ".join(f"{m.removeprefix('claude-')} {n * 100 // total}%" for m, n in t.models[:3])
                card.append(_label(_("Models (7 days): {models}").format(models=top),
                                   "dim-label", "caption", wrap=True))

        if t.id == "claude":
            card.append(self._claude_limits_row(t))
        for lim in t.limits:
            row = Gtk.Box(spacing=8)
            # Translators: {name} is a limit window name such as "5h" or "Weekly"
            row.append(_label(_("{name} limit").format(name=_limit_name(lim["name"])), "caption", "numeric"))
            bar = Gtk.LevelBar(min_value=0, max_value=100, value=min(lim["pct"], 100), hexpand=True,
                               valign=Gtk.Align.CENTER)
            row.append(bar)
            reset = lim.get("resets_at")
            txt = f"{lim['pct']:.0f}%"
            if reset:
                # Translators: strftime format for when a limit resets
                txt = _("{pct} · resets {when}").format(pct=txt, when=fmt_time(reset, _("%b %d %H:%M")))
            row.append(_label(txt, "caption", "numeric"))
            if lim["pct"] >= 90:
                bar.add_css_class("error")
            elif lim["pct"] >= 70:
                bar.add_css_class("warning")
            card.append(row)
        if t.limits and t.limits_updated:
            card.append(_label(_("Limits updated {when} (on every agent reply)").format(
                when=_when(t.limits_updated)), "dim-label", "caption"))

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
            row.append(_label(_("To install: {command}").format(command=hint) if hint else _("Not installed"),
                              "dim-label", "caption"))
            return row
        a = t.auth
        if a is None:
            row.append(_label(_("Login: —"), "dim-label", "caption"))
        elif a.get("logged_in"):
            if a.get("plan"):
                text = _("✓ Logged in · {plan} plan").format(plan=a["plan"].capitalize())
            else:
                text = _("✓ Logged in")
            row.append(_label(text, "caption", "success"))
        else:
            row.append(_label(_("Not logged in"), "caption", "warning"))
        if cmd and self.run_in_new_tab:
            btn = Gtk.Button(label=_("Switch account") if a and a.get("logged_in") else _("Log in"),
                             halign=Gtk.Align.END, hexpand=True)
            btn.add_css_class("flat" if a and a.get("logged_in") else "suggested-action")
            btn.add_css_class("caption")
            btn.connect("clicked", lambda *_: self._login(cmd))
            row.append(btn)
        return row

    def _login(self, cmd):
        self.popover.popdown()
        CL.forget_auth_cache()
        self.run_in_new_tab(cmd)

    # ---------- Claude plan limits ----------
    def _claude_limits_row(self, t: U.ToolUsage) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        if t.limits_enabled:
            if not t.limits:
                row.append(_label(_("Plan limits: shown after Claude's next reply."),
                                  "dim-label", "caption", wrap=True))
            off = Gtk.Button(label=_("Disable"), halign=Gtk.Align.END, hexpand=True)
            off.add_css_class("flat")
            off.add_css_class("caption")
            off.connect("clicked", lambda *_: (CL.disable(), self.refresh()))
            row.append(off)
            return row
        msg = (_("Plan limits (5h / weekly) disabled — Orca may have rewritten the setting.")
               if CL.read_limits() else _("Show plan limits (5h / weekly)"))
        row.append(_label(msg, "caption", wrap=True))
        on = Gtk.Button(label=_("Enable"), halign=Gtk.Align.END, hexpand=True, valign=Gtk.Align.CENTER)
        on.add_css_class("suggested-action")
        on.add_css_class("caption")
        on.connect("clicked", lambda *_: self._confirm_enable())
        row.append(on)
        return row

    def _confirm_enable(self):
        self.popover.popdown()
        dlg = Adw.AlertDialog(
            heading=_("Show Claude plan limits?"),
            body=_("Claude Code reports its plan limits (5h and weekly) to the status line command. "
                   "Matiz will install a wrapper there that records the numbers and passes everything "
                   "on to your current status line (Orca's keeps working).\n\n"
                   "This changes ~/.claude/settings.json (backed up to settings.json.matiz-bak). "
                   "No password or token is read. You can disable it at any time."))
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("ok", _("Enable"))
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r == "ok":
                try:
                    CL.enable()
                except OSError as e:
                    print(f"matiz: failed to enable limits: {e}")
                self.refresh()

        dlg.connect("response", resp)
        dlg.present(self.get_root())

    def _table(self, t: U.ToolUsage) -> Gtk.Widget:
        g = Gtk.Grid(column_spacing=18, row_spacing=4)
        cols = [("5h", _("Last 5h")), ("today", _("Today")), ("7d", _("7 days"))]
        for j, (_key, title) in enumerate(cols, 1):
            g.attach(_label(title, "usage-col", xalign=1.0), j, 0, 1, 1)
        rows = [
            (_("Messages"), lambda a: f"{a.messages}"),
            (_("Output tokens"), lambda a: U.fmt_tokens(a.out)),
            (_("Input"), lambda a: U.fmt_tokens(a.inp + a.cache_write)),
            (_("Cache reads"), lambda a: U.fmt_tokens(a.cache_read)),
        ]
        if any(a.cost is not None for a in t.windows.values()):
            # Translators: cost in US dollars; {amount} is already formatted (e.g. "1.25")
            rows.append((_("Cost"), lambda a: _("${amount}").format(amount=fmt_decimal(a.cost, 2))
                         if a.cost is not None else "—"))
        for i, (name, fn) in enumerate(rows, 1):
            g.attach(_label(name, "dim-label"), 0, i, 1, 1)
            for j, (key, _title) in enumerate(cols, 1):
                g.attach(_label(fn(t.windows[key]), "numeric", xalign=1.0), j, i, 1, 1)
        g.get_child_at(0, 1).set_hexpand(True)
        return g

    def _spark(self, hourly: list[int]) -> Gtk.Widget:
        """Bars for the last 24 h (no cairo: boxes with proportional height)."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bars = Gtk.Box(spacing=2, height_request=34, homogeneous=True)
        top = max(hourly) or 1
        now_h = datetime.now().hour
        for i, v in enumerate(hourly):
            h = max(2, round(32 * v / top))
            b = Gtk.Box(height_request=h, valign=Gtk.Align.END, hexpand=True)
            b.add_css_class("usage-bar")
            if not v:
                b.add_css_class("empty")
            hour = (now_h - (23 - i)) % 24
            # Translators: tooltip of an hourly usage bar, e.g. "14h: 1.2M tokens"
            b.set_tooltip_text(_("{hour}h: {tokens} tokens").format(hour=f"{hour:02d}", tokens=U.fmt_tokens(v)))
            bars.append(b)
        outer.append(bars)
        axis = Gtk.Box()
        axis.append(_label(_("24 h ago"), "dim-label", "caption"))
        axis.append(_label(_("now"), "dim-label", "caption", xalign=1.0))
        axis.get_last_child().set_hexpand(True)
        outer.append(axis)
        return outer
