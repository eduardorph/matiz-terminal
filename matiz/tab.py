"""Uma aba: faixa colorida + árvore de painéis (divisões com Gtk.Paned)."""
from __future__ import annotations

import os
import uuid

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .pane import Pane, mix  # noqa: E402,F401  (mix reexportado para app.py)


def dot_icon(hexstr: str) -> Gio.Icon:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
        f'<circle cx="8" cy="8" r="6" fill="{hexstr}"/></svg>'
    )
    return Gio.BytesIcon.new(GLib.Bytes.new(svg.encode()))


_stripe_css = None
_stripe_colors: set[str] = set()


def _color_class(hexstr: str) -> str:
    """Classe CSS para a cor (registra a regra na primeira vez)."""
    global _stripe_css
    hexstr = hexstr.lower()
    if _stripe_css is None:
        _stripe_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), _stripe_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    if hexstr not in _stripe_colors:
        _stripe_colors.add(hexstr)
        _stripe_css.load_from_string("".join(
            f".matiz-stripe.cor-{c[1:]} {{ background-color: {c}; }}" for c in sorted(_stripe_colors)
        ))
    return f"cor-{hexstr[1:]}"


class Tab(Gtk.Box):
    def __init__(self, window, *, tab_id=None, color=None, custom_title=None, config=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.config = config or {}
        self.tab_id = tab_id or uuid.uuid4().hex
        self.color: str | None = color
        self.custom_title: str | None = custom_title
        self.page = None  # Adw.TabPage, definido pela janela
        self.active: Pane | None = None

        self.stripe = Gtk.Box(hexpand=True, height_request=4)
        self.stripe.add_css_class("matiz-stripe")
        self.append(self.stripe)
        self.content = Gtk.Box(hexpand=True, vexpand=True)
        self.append(self.content)

    # ---------- árvore de painéis ----------
    def _root(self):
        return self.content.get_first_child()

    def panes(self) -> list[Pane]:
        out = []

        def walk(w):
            if isinstance(w, Pane):
                out.append(w)
            elif isinstance(w, Gtk.Paned):
                walk(w.get_start_child())
                walk(w.get_end_child())

        walk(self._root())
        return out

    def _new_pane(self, state: dict | None = None) -> Pane:
        state = state or {}
        p = Pane(self, pane_id=state.get("id"), config=self.config)
        p.apply_color(self.color)
        p.cwd = state.get("cwd") or p.cwd
        p.resume = state.get("resume")
        return p

    def _replace(self, old, new):
        parent = old.get_parent()
        if isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is old:
                parent.set_start_child(None)
                parent.set_start_child(new)
            else:
                parent.set_end_child(None)
                parent.set_end_child(new)
        else:
            self.content.remove(old)
            self.content.append(new)

    @staticmethod
    def _make_paned(orientation, a, b, ratio=0.5):
        paned = Gtk.Paned(orientation=orientation, wide_handle=True, hexpand=True, vexpand=True)
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_start_child(a)
        paned.set_end_child(b)
        paned._matiz_ratio = ratio

        # aplica a proporção quando o tamanho real for conhecido
        def apply(*_):
            total = paned.get_width() if orientation == Gtk.Orientation.HORIZONTAL else paned.get_height()
            if total <= 1:
                return True
            paned.set_position(int(total * paned._matiz_ratio))
            return False

        GLib.timeout_add(30, apply)
        return paned

    def build(self, layout: dict | None, scrollbacks: dict, *, start=True):
        """Monta a árvore a partir do layout salvo e inicia as sessões."""
        def make(node):
            if not node or "pane" in node or "split" not in node:
                st = (node or {}).get("pane", node or {})
                p = self._new_pane(st)
                if start:
                    p.start(cwd=p.cwd, resume=st.get("resume"), scrollback=scrollbacks.get(p.pane_id))
                return p
            orient = Gtk.Orientation.HORIZONTAL if node["split"] == "h" else Gtk.Orientation.VERTICAL
            return self._make_paned(orient, make(node.get("a")), make(node.get("b")),
                                    float(node.get("ratio", 0.5)))

        self.content.append(make(layout))
        panes = self.panes()
        self.active = panes[0] if panes else None

    def split(self, pane: Pane, horizontal: bool) -> Pane:
        """horizontal=True → lado a lado; False → em cima/embaixo."""
        new = self._new_pane({"cwd": pane.current_dir()})
        orient = Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL
        holder = Gtk.Box()  # segura o lugar enquanto o painel é movido
        self._replace(pane, holder)
        paned = self._make_paned(orient, pane, new)
        self._replace(holder, paned)
        new.start(cwd=new.cwd)
        self.active = new
        GLib.idle_add(lambda: (new.term.grab_focus(), False)[1])
        self._update_pane_marks()
        return new

    def remove_pane(self, pane: Pane) -> bool:
        """Tira o painel da árvore. Retorna True se a aba ficou vazia."""
        parent = pane.get_parent()
        if not isinstance(parent, Gtk.Paned):
            return True
        sibling = parent.get_end_child() if parent.get_start_child() is pane else parent.get_start_child()
        parent.set_start_child(None)
        parent.set_end_child(None)
        self._replace(parent, sibling)
        panes = self.panes()
        if self.active is pane or self.active not in panes:
            self.active = panes[0] if panes else None
        if self.active:
            GLib.idle_add(lambda: (self.active and self.active.term.grab_focus(), False)[1])
        self._update_pane_marks()
        return False

    def focus_dir(self, dx: int, dy: int):
        """Move o foco para o painel vizinho na direção (dx, dy)."""
        cur = self.active
        if not cur:
            return
        ok, pt = cur.compute_point(self, Graphene_point(cur.get_width() / 2, cur.get_height() / 2))
        cx, cy = (pt.x, pt.y) if ok else (0, 0)
        best, best_d = None, None
        for p in self.panes():
            if p is cur:
                continue
            ok, q = p.compute_point(self, Graphene_point(p.get_width() / 2, p.get_height() / 2))
            if not ok:
                continue
            vx, vy = q.x - cx, q.y - cy
            if (dx and vx * dx <= 0) or (dy and vy * dy <= 0):
                continue
            # distância no eixo pedido + penalidade pelo desvio no outro eixo
            d = (abs(vx) + 2 * abs(vy)) if dx else (abs(vy) + 2 * abs(vx))
            if best_d is None or d < best_d:
                best, best_d = p, d
        if best:
            best.term.grab_focus()

    def on_pane_focused(self, pane: Pane):
        self.active = pane
        self._update_pane_marks()
        self.window.on_active_pane_changed(self)

    def _update_pane_marks(self):
        many = len(self.panes()) > 1
        for p in self.panes():
            p.close_btn.set_visible(many)
            if many and p is self.active:
                p.add_css_class("matiz-pane-ativo")
            else:
                p.remove_css_class("matiz-pane-ativo")

    def layout_state(self) -> dict | None:
        def node(w):
            if isinstance(w, Pane):
                return {"pane": w.to_state()}
            if isinstance(w, Gtk.Paned):
                horiz = w.get_orientation() == Gtk.Orientation.HORIZONTAL
                total = w.get_width() if horiz else w.get_height()
                ratio = (w.get_position() / total) if total > 1 else getattr(w, "_matiz_ratio", 0.5)
                return {"split": "h" if horiz else "v", "ratio": round(min(max(ratio, 0.05), 0.95), 3),
                        "a": node(w.get_start_child()), "b": node(w.get_end_child())}
            return None

        return node(self._root())

    # ---------- aparência ----------
    @property
    def cwd(self) -> str:
        return self.active.cwd if self.active else os.path.expanduser("~")

    @property
    def fg_name(self):
        return self.active.fg_name if self.active else None

    def apply_color(self):
        for p in self.panes():
            p.apply_color(self.color)
        for cls in [c for c in self.stripe.get_css_classes() if c.startswith("cor-")]:
            self.stripe.remove_css_class(cls)
        if self.color:
            self.stripe.add_css_class(_color_class(self.color))
        self.stripe.set_visible(bool(self.color))
        if self.page is not None:
            self.page.set_indicator_icon(dot_icon(self.color) if self.color else None)

    def set_color(self, color: str | None):
        self.color = color
        self.apply_color()

    def display_title(self) -> str:
        if self.custom_title:
            return self.custom_title
        base = self.active.display_title() if self.active else "Matiz"
        n = len(self.panes())
        return f"{base}  (+{n - 1})" if n > 1 else base

    def refresh_title(self):
        if self.page is not None:
            title = self.display_title()
            if self.page.get_title() != title:
                self.page.set_title(title)
            self.page.set_tooltip(GLib.markup_escape_text(self.cwd))

    def to_state(self) -> dict:
        return {
            "id": self.tab_id,
            "title": self.custom_title,
            "color": self.color,
            "layout": self.layout_state(),
            "active_pane": self.active.pane_id if self.active else None,
        }


def Graphene_point(x, y):
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene
    p = Graphene.Point()
    p.x, p.y = x, y
    return p
