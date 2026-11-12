"""Sidebar with the file tree of the active pane's directory."""
from __future__ import annotations

import os
import shlex

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .i18n import _  # noqa: E402

ATTRS = ",".join([
    "standard::name", "standard::display-name", "standard::type", "standard::is-hidden",
    "standard::symbolic-icon", "standard::size", "standard::is-symlink",
])

CSS = """
.matiz-tree { background-color: #272c36; }
.matiz-tree listview { background: transparent; }
.matiz-tree .tree-top { padding: 4px 4px 4px 8px; border-bottom: 1px solid alpha(#ffffff, 0.06); }
.matiz-tree .tree-path { font-size: 0.9em; opacity: 0.8; }
.matiz-tree row { padding: 1px 4px; }
"""


def _is_dir(info: Gio.FileInfo) -> bool:
    return info.get_file_type() == Gio.FileType.DIRECTORY


def _file_of(info: Gio.FileInfo) -> Gio.File | None:
    return info.get_attribute_object("standard::file")


class FileTree(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, width_request=180)
        self.add_css_class("matiz-tree")
        self.window = window
        self.root: str | None = None
        self.following = True
        self.show_hidden = False
        self._menu_path: str | None = None

        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # --- top: path + buttons ---
        top = Gtk.Box(spacing=2)
        top.add_css_class("tree-top")
        self.path_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.START)
        self.path_label.add_css_class("tree-path")
        top.append(self.path_label)

        def tool(icon, tip, cb, toggle=False):
            b = (Gtk.ToggleButton if toggle else Gtk.Button)(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("toggled" if toggle else "clicked", cb)
            top.append(b)
            return b

        tool("go-up-symbolic", _("Up one level"), lambda *_: self._go_up())
        self.hidden_btn = tool("view-reveal-symbolic", _("Show hidden files"),
                               lambda b: self._set_hidden(b.get_active()), toggle=True)
        self.follow_btn = tool("find-location-symbolic", _("Follow the terminal directory"),
                               lambda b: self._set_follow(b.get_active()), toggle=True)
        self.follow_btn.set_active(True)
        self.append(top)

        # --- tree list ---
        self.filter = Gtk.CustomFilter.new(self._visible)
        self.sorter = Gtk.CustomSorter.new(self._compare)
        self.selection = Gtk.SingleSelection(autoselect=False, can_unselect=True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        self.list = Gtk.ListView(model=self.selection, factory=factory)
        self.list.connect("activate", self._on_activate)
        scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scroller)

        self._build_menu()

    # ---------- model ----------
    def _visible(self, info) -> bool:
        if self.show_hidden:
            return True
        name = info.get_name() or ""
        return not (name.startswith(".") or info.get_is_hidden())

    @staticmethod
    def _compare(a, b, *_):
        da, db = _is_dir(a), _is_dir(b)
        if da != db:
            return Gtk.Ordering.SMALLER if da else Gtk.Ordering.LARGER
        na, nb = (a.get_display_name() or "").casefold(), (b.get_display_name() or "").casefold()
        return Gtk.Ordering.SMALLER if na < nb else (Gtk.Ordering.LARGER if na > nb else Gtk.Ordering.EQUAL)

    def _dir_model(self, gfile: Gio.File) -> Gio.ListModel:
        dl = Gtk.DirectoryList.new(ATTRS, gfile)
        dl.set_monitored(True)
        return Gtk.SortListModel.new(Gtk.FilterListModel.new(dl, self.filter), self.sorter)

    def _children(self, info):
        if not _is_dir(info):
            return None
        f = _file_of(info)
        return self._dir_model(f) if f else None

    def set_root(self, path: str):
        if not path or not os.path.isdir(path) or path == self.root:
            return
        self.root = path
        home = os.path.expanduser("~")
        shown = "~" + path[len(home):] if path.startswith(home) else path
        self.path_label.set_label(shown or "/")
        self.path_label.set_tooltip_text(path)
        tree = Gtk.TreeListModel.new(self._dir_model(Gio.File.new_for_path(path)), False, False, self._children)
        self.selection.set_model(tree)

    def follow(self, pane):
        """Called by the window: follows the active pane's directory."""
        if self.following and pane is not None and self.get_visible():
            self.set_root(pane.current_dir())

    # ---------- rows ----------
    def _setup_row(self, _f, item):
        exp = Gtk.TreeExpander(indent_for_icon=True)
        box = Gtk.Box(spacing=6)
        icon = Gtk.Image()
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        box.append(icon)
        box.append(label)
        exp.set_child(box)
        item.set_child(exp)
        rc = Gtk.GestureClick(button=3)
        rc.connect("pressed", lambda g, n, x, y: self._on_right_click(item, exp, x, y))
        exp.add_controller(rc)

    def _bind_row(self, _f, item):
        row = item.get_item()
        info = row.get_item()
        exp = item.get_child()
        exp.set_list_row(row)
        box = exp.get_child()
        icon, label = box.get_first_child(), box.get_last_child()
        gicon = info.get_symbolic_icon()
        if gicon:
            icon.set_from_gicon(gicon)
        label.set_label(info.get_display_name() or info.get_name())
        f = _file_of(info)
        exp.set_tooltip_text(f.get_path() if f else None)

    def _path_at(self, position: int) -> tuple[str | None, bool, object]:
        row = self.selection.get_model().get_item(position) if self.selection.get_model() else None
        if row is None:
            return None, False, None
        info = row.get_item()
        f = _file_of(info)
        return (f.get_path() if f else None), _is_dir(info), row

    def _on_activate(self, _list, position):
        path, is_dir, row = self._path_at(position)
        if not path:
            return
        if is_dir:
            row.set_expanded(not row.get_expanded())
        else:
            self.window.open_link(self.window.pane(), "file", path, None, None)

    # ---------- context menu ----------
    def _build_menu(self):
        group = Gio.SimpleActionGroup()
        acts = {
            "open": self._act_open, "insert": self._act_insert, "cd": self._act_cd,
            "new-tab": self._act_new_tab, "set-root": self._act_root, "copy": self._act_copy,
            "reveal": self._act_reveal,
        }
        for name, cb in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: self._menu_path and cb(self._menu_path))
            group.add_action(a)
        self.insert_action_group("tree", group)

        self.menu_file = Gio.Menu()
        s1 = Gio.Menu()
        s1.append(_("Open"), "tree.open")
        s1.append(_("Insert path in terminal"), "tree.insert")
        self.menu_file.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append(_("Copy path"), "tree.copy")
        s2.append(_("Show in folder"), "tree.reveal")
        self.menu_file.append_section(None, s2)

        self.menu_dir = Gio.Menu()
        d1 = Gio.Menu()
        d1.append(_("Go to this folder in the terminal (cd)"), "tree.cd")
        d1.append(_("Insert path in terminal"), "tree.insert")
        d1.append(_("New tab in this folder"), "tree.new-tab")
        d1.append(_("Use as tree root"), "tree.set-root")
        self.menu_dir.append_section(None, d1)
        d2 = Gio.Menu()
        d2.append(_("Open in file manager"), "tree.open")
        d2.append(_("Copy path"), "tree.copy")
        self.menu_dir.append_section(None, d2)

    def _on_right_click(self, item, widget, x, y):
        pos = item.get_position()
        path, is_dir, _row = self._path_at(pos)
        if not path:
            return
        self.selection.set_selected(pos)
        self._menu_path = path
        pop = Gtk.PopoverMenu.new_from_model(self.menu_dir if is_dir else self.menu_file)
        pop.set_has_arrow(False)
        pop.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
        pop.popup()

    def _act_open(self, path):
        if os.path.isdir(path):
            self.window._launch_folder(path)
        else:
            self.window.open_link(self.window.pane(), "file", path, None, None)

    def _act_insert(self, path):
        pane = self.window.pane()
        if pane:
            pane.term.feed_child((shlex.quote(path) + " ").encode())
            pane.term.grab_focus()

    def _act_cd(self, path):
        pane = self.window.pane()
        if not pane:
            return
        if pane.fg_name:
            # Translators: {program} is the running program; quoted text is a menu item
            self.window.toast(_("A program is running ({program}); use “New tab in this folder”")
                              .format(program=pane.fg_name))
            return
        pane.term.feed_child(f"cd {shlex.quote(path)}\n".encode())
        pane.term.grab_focus()

    def _act_new_tab(self, path):
        self.window.new_tab(cwd=path)

    def _act_root(self, path):
        self.follow_btn.set_active(False)
        self.set_root(path)

    def _act_copy(self, path):
        provider = Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(path.encode()))
        self.get_clipboard().set_content(provider)
        self.window.toast(_("Copied: {path}").format(path=path))

    def _act_reveal(self, path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(self.window, None, None)

    # ---------- top buttons ----------
    def _go_up(self):
        if self.root and self.root != "/":
            self.follow_btn.set_active(False)
            self.set_root(os.path.dirname(self.root.rstrip("/")) or "/")

    def _set_hidden(self, on):
        self.show_hidden = on
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _set_follow(self, on):
        self.following = on
        if on:
            self.root = None  # force reload
            self.follow(self.window.pane())
