"""Janela, abas, painéis, menus, notificações e o ciclo salvar/restaurar."""
from __future__ import annotations

import os
import shutil
import signal
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import procinfo, session  # noqa: E402
from .filetree import FileTree  # noqa: E402
from .pane import Pane  # noqa: E402
from .tab import Tab, mix  # noqa: E402

APP_ID = "dev.matiz.Terminal"
PROBE_MS = 2000          # sondagem de processos + gravação do estado
SCROLLBACK_EVERY = 8     # grava histórico a cada N sondagens (~16 s)
CLOSE_DELAY_MS = 1200    # espera antes de fechar painel cuja shell saiu

PRESETS = [
    ("🔴  Vermelho", "#e5484d"),
    ("🟠  Laranja", "#f76b15"),
    ("🟡  Amarelo", "#e2b93d"),
    ("🟢  Verde", "#46a758"),
    ("🔵  Azul", "#3e8ed0"),
    ("🟣  Roxo", "#8e4ec6"),
    ("🩷  Rosa", "#d6409f"),
    ("⚪  Cinza", "#8b8d98"),
]

STATIC_CSS = """
.matiz-pane { border: 1px solid transparent; }
.matiz-pane.matiz-pane-ativo { border-color: alpha(#88c0d0, 0.75); }
paned.horizontal > separator, paned.vertical > separator { background-color: #3b4252; }
button.matiz-fechar-painel { opacity: 0.45; min-width: 24px; min-height: 24px; padding: 2px; }
button.matiz-fechar-painel:hover { opacity: 1; background-color: alpha(#bf616a, 0.85); }
"""


class MatizWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Matiz")
        self.add_css_class("matiz-window")
        self.config = session.load_config()
        self.rerun = procinfo.DEFAULT_RERUN + [
            r for r in self.config.get("reexecutar_extra", []) if isinstance(r, list) and r
        ]
        self.max_lines = int(self.config.get("linhas_historico_salvas", 3000))
        self.notify_after = float(self.config.get("notificar_apos_segundos", 10))
        # Processos sobrevivem ao fechar a janela (matiz-daemon). Desative com
        # "persistir_processos": false na config ou MATIZ_NO_DAEMON=1.
        self.use_daemon = bool(self.config.get("persistir_processos", True)) and not os.environ.get("MATIZ_NO_DAEMON")
        self.quitting = False
        self._menu_page = None
        self._last_state_text = None
        self._tick = 0
        self._last_bell: dict[str, float] = {}

        display = Gdk.Display.get_default()
        static = Gtk.CssProvider()
        static.load_from_string(STATIC_CSS)
        Gtk.StyleContext.add_provider_for_display(display, static, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # --- layout ---
        self.view = Adw.TabView()
        self.view.connect("close-page", self._on_close_page)
        self.view.connect("notify::selected-page", self._on_selected)
        self.view.connect("setup-menu", self._on_setup_menu)
        self.view.connect("create-window", lambda *_: None)  # sem arrastar p/ nova janela
        self.open_with_menu = Gio.Menu()  # compartilhado; preenchido ao abrir os menus
        self._fill_open_with()
        self.view.set_menu_model(self._build_tab_menu())
        self.term_menu = self._build_term_menu()

        self.tabbar = Adw.TabBar(view=self.view, autohide=False)
        header = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="Matiz")
        header.set_title_widget(self.wtitle)
        new_btn = Gtk.Button(icon_name="tab-new-symbolic", tooltip_text="Nova aba (Ctrl+Shift+T)")
        new_btn.connect("clicked", lambda *_: self.new_tab())
        header.pack_start(new_btn)
        split_btn = Gtk.Button(icon_name="view-dual-symbolic", tooltip_text="Dividir lado a lado (Alt+Shift++)")
        split_btn.connect("clicked", lambda *_: self._split(self.pane(), True))
        header.pack_start(split_btn)
        self.tree_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                         tooltip_text="Árvore de arquivos (Ctrl+Shift+B)")
        self.tree_btn.connect("toggled", lambda b: self._show_tree(b.get_active()))
        header.pack_start(self.tree_btn)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu (F10)",
                                  menu_model=self._build_main_menu(), primary=True)
        menu_btn.connect("notify::active", lambda b, *_: b.get_active() and self._fill_open_with())
        header.pack_end(menu_btn)
        if self.config.get("widget_uso", True):
            from .usage_ui import UsageButton
            self.usage_btn = UsageButton(run_in_new_tab=self.run_in_new_tab)
            header.pack_end(self.usage_btn)

        self.toasts = Adw.ToastOverlay(child=self.view)
        self._build_search_bar()

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.add_top_bar(self.tabbar)
        self.tree = FileTree(self)
        self.tree.set_visible(False)
        self.split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=260,
                               start_child=self.tree, end_child=self.toasts)
        self.split.set_shrink_start_child(False)
        self.split.set_resize_start_child(False)
        self.split.set_shrink_end_child(False)
        tv.set_content(self.split)
        tv.add_bottom_bar(self.searchbar)
        self.set_content(tv)

        self._install_actions()
        self._install_shortcuts()
        self.connect("close-request", self._on_close_request)
        self.connect("notify::is-active", lambda *_: self._clear_attention())
        GLib.timeout_add(PROBE_MS, self._on_tick)
        GLib.timeout_add(150, self._check_sizes)

    # ================= menus e ações =================
    def _build_tab_menu(self) -> Gio.Menu:
        def item(label, action, accel=None, target=None):
            it = Gio.MenuItem.new(label, None)
            if target is None:
                it.set_detailed_action(action)
            else:
                it.set_action_and_target_value(action, GLib.Variant.new_string(target))
            if accel:
                it.set_attribute_value("accel", GLib.Variant.new_string(accel))
            return it

        menu = Gio.Menu()
        cores = Gio.Menu()
        swatches = Gio.Menu()
        for i, (label, hexstr) in enumerate(PRESETS, 1):
            swatches.append_item(item(label, "aba.cor", f"<Control><Alt>{i}", hexstr))
        cores.append_section(None, swatches)
        extra = Gio.Menu()
        extra.append_item(item("Personalizada…", "aba.cor-personalizada"))
        extra.append_item(item("Sem cor", "aba.sem-cor", "<Control><Alt>0"))
        cores.append_section(None, extra)
        s1 = Gio.Menu()
        s1.append_submenu("Cor", cores)
        s1.append_item(item("Renomear…", "aba.renomear", "<Control><Shift>r"))
        s1.append_item(item("Fixar / desafixar aba", "aba.fixar"))
        menu.append_section(None, s1)

        paineis = Gio.Menu()
        paineis.append_item(item("Dividir lado a lado", "aba.dividir-h", "<Alt><Shift>plus"))
        paineis.append_item(item("Dividir em cima / embaixo", "aba.dividir-v", "<Alt><Shift>underscore"))
        paineis.append_item(item("Fechar painel", "aba.fechar-painel"))
        menu.append_section(None, paineis)

        pasta = Gio.Menu()
        pasta.append_submenu("Abrir pasta com", self.open_with_menu)
        pasta.append_item(item("Copiar caminho da pasta", "aba.copiar-caminho", "<Control><Shift>y"))
        pasta.append_item(item("Nova aba nesta pasta", "aba.duplicar", "<Control><Shift>d"))
        menu.append_section(None, pasta)

        hist = Gio.Menu()
        hist.append_item(item("Buscar…", "aba.buscar", "<Control><Shift>f"))
        hist.append_item(item("Salvar histórico em arquivo…", "aba.salvar-historico", "<Control><Shift>s"))
        hist.append_item(item("Limpar tela e histórico", "aba.limpar", "<Control><Shift>k"))
        menu.append_section(None, hist)

        fim = Gio.Menu()
        fim.append_item(item("Fechar outras abas", "aba.fechar-outras"))
        fim.append_item(item("Fechar aba", "aba.fechar"))
        menu.append_section(None, fim)
        return menu

    def _build_main_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        top = Gio.Menu()
        it = Gio.MenuItem.new("Nova aba", "aba.nova")
        it.set_attribute_value("accel", GLib.Variant.new_string("<Control><Shift>t"))
        top.append_item(it)
        it = Gio.MenuItem.new("Árvore de arquivos", "aba.arvore")
        it.set_attribute_value("accel", GLib.Variant.new_string("<Control><Shift>b"))
        top.append_item(it)
        menu.append_section(None, top)
        tab_menu = self._build_tab_menu()
        for i in range(tab_menu.get_n_items()):
            link = tab_menu.get_item_link(i, Gio.MENU_LINK_SECTION)
            if link is not None:
                menu.append_section(None, link)
        ajuda = Gio.Menu()
        it = Gio.MenuItem.new("Atalhos de teclado", "aba.atalhos")
        it.set_attribute_value("accel", GLib.Variant.new_string("<Control>question"))
        ajuda.append_item(it)
        menu.append_section(None, ajuda)
        return menu

    def _build_term_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        clip = Gio.Menu()
        for label, action, accel in (("Copiar", "aba.copiar", "<Control><Shift>c"),
                                     ("Colar", "aba.colar", "<Control><Shift>v")):
            it = Gio.MenuItem.new(label, action)
            it.set_attribute_value("accel", GLib.Variant.new_string(accel))
            clip.append_item(it)
        menu.append_section(None, clip)
        tab_menu = self._build_tab_menu()
        for i in range(tab_menu.get_n_items()):
            link = tab_menu.get_item_link(i, Gio.MENU_LINK_SECTION)
            if link is not None:
                menu.append_section(None, link)
        return menu

    def _fill_open_with(self):
        self.open_with_menu.remove_all()
        apps = folder_apps()
        for i, app in enumerate(apps):
            it = Gio.MenuItem.new(app.get_name() + ("    Ctrl+Shift+E" if i == 0 else ""), None)
            it.set_action_and_target_value("aba.abrir-com", GLib.Variant.new_string(app.get_id()))
            self.open_with_menu.append_item(it)
        outro = Gio.Menu()
        outro.append("Outro aplicativo…", "aba.abrir-com-outro")
        self.open_with_menu.append_section(None, outro)

    def _on_setup_menu(self, _view, page):
        # page é a aba clicada (None quando o menu fecha).
        if page is not None:
            self._menu_page = page
            self._fill_open_with()

    def _target_tab(self) -> Tab | None:
        page = self._menu_page or self.view.get_selected_page()
        self._menu_page = None
        return page.get_child() if page else None

    def _target_pane(self) -> Pane | None:
        tab = self._target_tab()
        return tab.active if tab else None

    def _install_actions(self):
        group = Gio.SimpleActionGroup()

        def add(name, cb, ptype=None):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            a.connect("activate", cb)
            group.add_action(a)

        add("cor", lambda _a, v: self._set_color(self._target_tab(), v.get_string()), "s")
        add("sem-cor", lambda *_: self._set_color(self._target_tab(), None))
        add("cor-personalizada", lambda *_: self._pick_color(self._target_tab()))
        add("renomear", lambda *_: self._rename(self._target_tab()))
        add("duplicar", lambda *_: self._duplicate(self._target_pane()))
        add("fechar", lambda *_: self._close_tab(self._target_tab()))
        add("fechar-outras", lambda *_: self._close_others(self._target_tab()))
        add("fixar", lambda *_: self._toggle_pin(self._target_tab()))
        add("dividir-h", lambda *_: self._split(self._target_pane(), True))
        add("dividir-v", lambda *_: self._split(self._target_pane(), False))
        add("fechar-painel", lambda *_: self._close_pane(self._target_pane()))
        add("abrir-com", lambda _a, v: self._open_with(self._target_pane(), v.get_string()), "s")
        add("abrir-com-outro", lambda *_: self._open_with_other(self._target_pane()))
        add("copiar-caminho", lambda *_: self._copy_path(self._target_pane()))
        add("buscar", lambda *_: self._open_search(self._target_tab()))
        add("limpar", lambda *_: self._clear(self._target_pane()))
        add("salvar-historico", lambda *_: self._save_history(self._target_pane()))
        add("nova", lambda *_: self.new_tab())
        add("arvore", lambda *_: self.tree_btn.set_active(not self.tree_btn.get_active()))
        add("copiar", lambda *_: (p := self._target_pane()) and p.term.copy_clipboard_format(_vte().Format.TEXT))
        add("colar", lambda *_: (p := self._target_pane()) and p.term.paste_clipboard())
        add("atalhos", lambda *_: self._show_shortcuts())
        self.insert_action_group("aba", group)

    def _install_shortcuts(self):
        ctrl = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def sc(trigger, fn):
            ctrl.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                action=Gtk.CallbackAction.new(lambda *_: (fn(), True)[1]),
            ))

        cur = lambda: self.current_tab()  # noqa: E731
        pane = lambda: self.pane()  # noqa: E731
        sc("<Control><Shift>t", lambda: self.new_tab())
        sc("<Control><Shift>w", lambda: self._close_pane(pane()))
        sc("<Control><Shift>c", lambda: pane() and pane().term.copy_clipboard_format(_vte().Format.TEXT))
        sc("<Control><Shift>v", lambda: pane() and pane().term.paste_clipboard())
        sc("<Control>Page_Down", lambda: self.view.select_next_page())
        sc("<Control>Page_Up", lambda: self.view.select_previous_page())
        sc("<Control><Shift>Page_Down", lambda: self._move(+1))
        sc("<Control><Shift>Page_Up", lambda: self._move(-1))
        sc("<Control>plus", lambda: self._zoom(1.1))
        sc("<Control>equal", lambda: self._zoom(1.1))
        sc("<Control>minus", lambda: self._zoom(1 / 1.1))
        sc("<Control>0", lambda: self._zoom(None))
        sc("<Control><Shift>r", lambda: self._rename(cur()))
        for i in range(1, 10):
            sc(f"<Alt>{i}", lambda i=i: self._select_index(i - 1))
        for i, (_label, hexstr) in enumerate(PRESETS, 1):
            sc(f"<Control><Alt>{i}", lambda c=hexstr: self._set_color(cur(), c))
        sc("<Control><Alt>0", lambda: self._set_color(cur(), None))
        sc("<Control><Shift>e", lambda: self._open_with(pane(), None))
        sc("<Control><Shift>y", lambda: self._copy_path(pane()))
        sc("<Control><Shift>d", lambda: self._duplicate(pane()))
        sc("<Control><Shift>f", lambda: self._open_search(cur()))
        sc("<Control><Shift>k", lambda: self._clear(pane()))
        sc("<Control><Shift>s", lambda: self._save_history(pane()))
        # painéis
        for t in ("<Alt><Shift>plus", "<Alt><Shift>equal", "<Alt><Shift>KP_Add"):
            sc(t, lambda: self._split(pane(), True))
        for t in ("<Alt><Shift>underscore", "<Alt><Shift>minus", "<Alt><Shift>KP_Subtract"):
            sc(t, lambda: self._split(pane(), False))
        sc("<Alt>Left", lambda: cur() and cur().focus_dir(-1, 0))
        sc("<Alt>Right", lambda: cur() and cur().focus_dir(+1, 0))
        sc("<Alt>Up", lambda: cur() and cur().focus_dir(0, -1))
        sc("<Alt>Down", lambda: cur() and cur().focus_dir(0, +1))
        sc("<Control>question", lambda: self._show_shortcuts())
        sc("<Control><Shift>b", lambda: self.tree_btn.set_active(not self.tree_btn.get_active()))
        sc("<Control><Shift>question", lambda: self._show_shortcuts())
        sc("F1", lambda: self._show_shortcuts())
        self.add_controller(ctrl)

    # ================= operações de aba =================
    def current_tab(self) -> Tab | None:
        page = self.view.get_selected_page()
        return page.get_child() if page else None

    def pane(self) -> Pane | None:
        tab = self.current_tab()
        return tab.active if tab else None

    def tabs(self) -> list[Tab]:
        return [self.view.get_nth_page(i).get_child() for i in range(self.view.get_n_pages())]

    def all_panes(self) -> list[Pane]:
        return [p for t in self.tabs() for p in t.panes()]

    def new_tab(self, *, cwd=None, state=None, select=True) -> Tab:
        state = state or {}
        if cwd is None and not state:
            p = self.pane()
            cwd = p.current_dir() if p else os.path.expanduser("~")
        tab = Tab(self, tab_id=state.get("id"), color=state.get("color"),
                  custom_title=state.get("title"), config=self.config)
        page = self.view.append(tab)
        tab.page = page
        if state.get("pinned"):
            self.view.set_page_pinned(page, True)
        layout = state.get("layout")
        if not layout:
            if state:  # formato antigo: a aba era um único terminal (id = sessão)
                layout = {"pane": {"id": state.get("id"), "cwd": state.get("cwd"), "resume": state.get("resume")}}
            else:
                layout = {"pane": {"cwd": cwd or os.path.expanduser("~")}}
        scrollbacks = {}
        for pid in _layout_ids(layout):
            sb = session.load_scrollback(pid) if pid else None
            if sb:
                scrollbacks[pid] = sb
        tab.build(layout, scrollbacks)
        want = state.get("active_pane")
        for p in tab.panes():
            if p.pane_id == want:
                tab.active = p
        tab._update_pane_marks()
        tab.apply_color()
        tab.refresh_title()
        if select:
            self.view.set_selected_page(page)
            if tab.active:
                tab.active.term.grab_focus()
        self.schedule_save()
        return tab

    # ---------- painéis ----------
    def _split(self, pane, horizontal):
        if pane:
            pane.tab.split(pane, horizontal)
            pane.tab.refresh_title()
            self.schedule_save()

    def _close_pane(self, pane):
        """Fecha o painel; se for o último da aba, fecha a aba."""
        if not pane:
            return
        tab = pane.tab
        if len(tab.panes()) <= 1:
            self._close_tab(tab)
            return
        pane.kill_session()
        session.scrollback_path(pane.pane_id).unlink(missing_ok=True)
        tab.remove_pane(pane)
        tab.refresh_title()
        self._update_chrome()
        self.schedule_save()

    def on_active_pane_changed(self, tab):
        tab.refresh_title()
        if tab is self.current_tab():
            self._update_chrome()

    def _set_color(self, tab, color):
        if tab:
            tab.set_color(color)
            self._update_chrome()
            self.schedule_save()

    def _pick_color(self, tab):
        if not tab:
            return
        dialog = Gtk.ColorDialog(title="Cor da aba", with_alpha=False)
        initial = Gdk.RGBA()
        initial.parse(tab.color or "#3e8ed0")

        def done(d, res):
            try:
                c = d.choose_rgba_finish(res)
            except GLib.Error:
                return
            self._set_color(tab, "#%02x%02x%02x" % (round(c.red * 255), round(c.green * 255), round(c.blue * 255)))

        dialog.choose_rgba(self, initial, None, done)

    def _rename(self, tab):
        if not tab:
            return
        entry = Gtk.Entry(text=tab.custom_title or "", placeholder_text=tab.display_title(),
                          activates_default=True)
        dlg = Adw.AlertDialog(heading="Renomear aba",
                              body="Deixe vazio para voltar ao título automático.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", "Renomear")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def on_resp(_d, resp):
            if resp == "ok":
                tab.custom_title = entry.get_text().strip() or None
                tab.refresh_title()
                self._update_chrome()
                self.schedule_save()

        dlg.connect("response", on_resp)
        dlg.present(self)

    def _show_shortcuts(self):
        groups = [
            ("Abas", [
                ("Nova aba", "<Control><Shift>t"), ("Nova aba nesta pasta", "<Control><Shift>d"),
                ("Fechar painel / aba", "<Control><Shift>w"), ("Renomear aba", "<Control><Shift>r"),
                ("Próxima / anterior", "<Control>Page_Down <Control>Page_Up"),
                ("Mover aba", "<Control><Shift>Page_Down <Control><Shift>Page_Up"),
                ("Ir para a aba 1…9", "<Alt>1"),
            ]),
            ("Cores", [(label.split()[-1], f"<Control><Alt>{i}") for i, (label, _) in enumerate(PRESETS, 1)]
             + [("Sem cor", "<Control><Alt>0")]),
            ("Painéis", [
                ("Dividir lado a lado", "<Alt><Shift>plus"), ("Dividir em cima / embaixo", "<Alt><Shift>underscore"),
                ("Ir para o painel vizinho", "<Alt>Left <Alt>Right <Alt>Up <Alt>Down"),
            ]),
            ("Pasta e histórico", [
                ("Mostrar/esconder árvore de arquivos", "<Control><Shift>b"),
                ("Abrir pasta no gerenciador de arquivos", "<Control><Shift>e"),
                ("Copiar caminho da pasta", "<Control><Shift>y"),
                ("Buscar no terminal", "<Control><Shift>f"),
                ("Salvar histórico em arquivo", "<Control><Shift>s"),
                ("Limpar tela e histórico", "<Control><Shift>k"),
            ]),
            ("Terminal", [
                ("Copiar / colar", "<Control><Shift>c <Control><Shift>v"),
                ("Zoom", "<Control>plus <Control>minus <Control>0"),
                ("Atalhos de teclado", "<Control>question"),
            ]),
        ]
        dlg = Adw.ShortcutsDialog()
        for title, items in groups:
            sec = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                sec.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dlg.add(sec)
        extra = Adw.ShortcutsSection(title="Mouse")
        extra.add(Adw.ShortcutsItem(title="Abrir link ou arquivo:linha (Ctrl+clique)", accelerator="<Control>"))
        extra.add(Adw.ShortcutsItem(title="Menu da aba: botão direito no título da aba", accelerator=""))
        dlg.add(extra)
        dlg.present(self)

    def _show_tree(self, on):
        self.tree.set_visible(on)
        if on:
            self.tree.root = None if self.tree.following else self.tree.root
            self.tree.follow(self.pane())
        self.schedule_save()

    def run_in_new_tab(self, argv):
        """Abre uma aba na pasta pessoal e executa o comando (ex.: login)."""
        tab = self.new_tab(cwd=os.path.expanduser("~"))
        p = tab.active
        if p:
            p._pending_resume = {"argv": list(argv), "mode": "run", "kind": "generic",
                                 "label": argv[0], "cwd": p.cwd}
            p._resume_typed = False
            if p.shell_pid:  # a shell já subiu (modo local rápido)
                GLib.timeout_add(600, p._type_resume)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def _close_others(self, tab):
        if not tab:
            return
        for t in self.tabs():
            if t is not tab and t.page is not None:
                if t.page.get_pinned():
                    self.view.set_page_pinned(t.page, False)
                self.view.close_page(t.page)

    def _toggle_pin(self, tab):
        if tab and tab.page:
            self.view.set_page_pinned(tab.page, not tab.page.get_pinned())
            self.schedule_save()

    def _open_with(self, pane, app_id):
        """app_id=None → gerenciador de arquivos padrão."""
        if not pane:
            return
        self._launch_folder(pane.current_dir(), app_id)

    def _launch_folder(self, path, app_id=None):
        apps = folder_apps()
        app = next((a for a in apps if a.get_id() == app_id), None) if app_id else (apps[0] if apps else None)
        if not app:
            self.toast("Nenhum aplicativo para abrir pastas")
            return
        try:
            ctx = self.get_display().get_app_launch_context()
            app.launch([Gio.File.new_for_path(path)], ctx)
            self.toast(f"Abrindo {os.path.basename(path) or path} com {app.get_name()}")
        except GLib.Error as e:
            self.toast(f"Falha ao abrir: {e.message}")

    def _open_with_other(self, pane):
        if not pane:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(pane.current_dir()))
        launcher.set_always_ask(True)

        def done(lch, res):
            try:
                lch.launch_finish(res)
            except GLib.Error:
                pass

        launcher.launch(self, None, done)

    def _copy_path(self, pane):
        if not pane:
            return
        path = pane.current_dir()
        provider = Gdk.ContentProvider.new_for_bytes(
            "text/plain;charset=utf-8", GLib.Bytes.new(path.encode()))
        self.get_clipboard().set_content(provider)
        self.toast(f"Copiado: {path}")

    def _clear(self, pane):
        if pane:
            pane.clear()

    def _save_history(self, pane):
        if not pane:
            return
        tab = pane.tab
        name = (tab.custom_title or os.path.basename(pane.cwd) or "terminal").replace("/", "-")
        dlg = Gtk.FileDialog(title="Salvar histórico",
                             initial_name=f"{name}-{GLib.DateTime.new_now_local().format('%Y%m%d-%H%M')}.txt")
        dlg.set_initial_folder(Gio.File.new_for_path(pane.current_dir()))

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            try:
                with open(f.get_path(), "w", encoding="utf-8") as fh:
                    fh.write(pane.full_text())
                self.toast(f"Histórico salvo em {f.get_basename()}")
            except OSError as e:
                self.toast(f"Falha ao salvar: {e.strerror}")

        dlg.save(self, None, done)

    # ---------- links (Ctrl+clique) ----------
    def open_link(self, pane, kind, target, line=None, col=None) -> bool:
        if kind == "url":
            Gtk.UriLauncher.new(target).launch(self, None, _ignore_finish("launch_finish"))
            return True
        if os.path.isdir(target):
            self._launch_folder(target)
            return True
        ctype, _ = Gio.content_type_guess(target, None)
        is_text = Gio.content_type_is_a(ctype, "text/plain") or line is not None
        argv = self._editor_argv(target, line, col) if is_text else None
        if argv:
            try:
                proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
                proc.wait_async(None, None)
                where = f":{line}" if line else ""
                self.toast(f"Abrindo {os.path.basename(target)}{where}")
                return True
            except GLib.Error as e:
                self.toast(f"Falha ao abrir editor: {e.message}")
        Gtk.FileLauncher.new(Gio.File.new_for_path(target)).launch(self, None, _ignore_finish("launch_finish"))
        return True

    def _editor_argv(self, path, line, col):
        """Comando do editor. Config "editor": ["code", "--goto", "{file}:{line}:{col}"]."""
        tpl = self.config.get("editor")
        if isinstance(tpl, list) and tpl:
            return [a.format(file=path, line=line or 1, col=col or 1) for a in tpl]
        for name, build in (
            ("code", lambda: ["code", "--goto", f"{path}:{line or 1}:{col or 1}"]),
            ("cursor", lambda: ["cursor", "--goto", f"{path}:{line or 1}:{col or 1}"]),
            ("zed", lambda: ["zed", f"{path}:{line or 1}:{col or 1}"]),
            ("subl", lambda: ["subl", f"{path}:{line or 1}:{col or 1}"]),
        ):
            if shutil.which(name):
                return build()
        return None

    # ---------- notificações ----------
    def _pane_visible(self, pane) -> bool:
        return (self.is_active() and pane.tab is self.current_tab() and pane.tab.active is pane)

    def _attention(self, pane, title, body):
        if self._pane_visible(pane):
            return
        if pane.tab.page is not None and pane.tab is not self.current_tab():
            pane.tab.page.set_needs_attention(True)
        if self.config.get("notificacoes", True) and not self.is_active():
            n = Gio.Notification.new(title)
            n.set_body(body)
            n.set_default_action_and_target("app.focar", GLib.Variant.new_string(pane.pane_id))
            self.get_application().send_notification(f"matiz-{pane.pane_id}", n)

    def on_command_finished(self, pane, cmd, took):
        if took >= self.notify_after:
            short = cmd if len(cmd) <= 60 else cmd[:57] + "…"
            self._attention(pane, f"Terminou: {short}",
                            f"{pane.tab.display_title()} · {_fmt_secs(took)}")

    def on_claude_finished(self, pane, status, took):
        what = "precisa de você" if status not in ("idle", "done") else "terminou de responder"
        self._attention(pane, f"Claude {what}", f"{pane.tab.display_title()} · {_fmt_secs(took)}")

    def on_pane_bell(self, pane):
        now = time.monotonic()
        if now - self._last_bell.get(pane.pane_id, 0) < 10:
            return
        self._last_bell[pane.pane_id] = now
        self._attention(pane, "Alerta no terminal", pane.tab.display_title())

    def focus_pane(self, pane_id):
        for t in self.tabs():
            for p in t.panes():
                if p.pane_id == pane_id:
                    self.view.set_selected_page(t.page)
                    p.term.grab_focus()
        self.present()

    def _clear_attention(self):
        page = self.view.get_selected_page()
        if page is not None and self.is_active():
            page.set_needs_attention(False)

    # ---------- busca ----------
    def _build_search_bar(self):
        self.search_entry = Gtk.SearchEntry(placeholder_text="Buscar no terminal…", hexpand=True)
        up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Anterior (Enter)")
        down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Próxima (Shift+Enter)")
        box = Gtk.Box(spacing=6)
        box.append(self.search_entry)
        box.append(up)
        box.append(down)
        clamp = Adw.Clamp(maximum_size=520, child=box)
        self.searchbar = Gtk.SearchBar(child=clamp, show_close_button=True)
        self.searchbar.connect_entry(self.search_entry)
        self.search_entry.connect("search-changed", lambda *_: self._search_update())
        self.search_entry.connect("activate", lambda *_: self._search_step(-1))
        self.search_entry.connect("previous-match", lambda *_: self._search_step(-1))
        self.search_entry.connect("next-match", lambda *_: self._search_step(+1))
        self.search_entry.connect("stop-search", lambda *_: self._close_search())
        up.connect("clicked", lambda *_: self._search_step(-1))
        down.connect("clicked", lambda *_: self._search_step(+1))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._search_keys)
        self.search_entry.add_controller(keys)
        self.searchbar.connect("notify::search-mode-enabled", self._search_toggled)

    def _search_keys(self, _ctrl, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.SHIFT_MASK:
            self._search_step(+1)
            return True
        return False

    def _open_search(self, tab):
        if tab and tab.page is not None:
            self.view.set_selected_page(tab.page)
        self._search_pane = self.pane()
        self.searchbar.set_search_mode(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _close_search(self):
        self.searchbar.set_search_mode(False)

    def _search_toggled(self, *_):
        if not self.searchbar.get_search_mode():
            for p in self.all_panes():
                p.term.search_set_regex(None, 0)
            p = self.pane()
            if p:
                p.term.grab_focus()

    def _search_target(self):
        p = getattr(self, "_search_pane", None)
        return p if p is not None and p in self.all_panes() else self.pane()

    def _search_update(self):
        p = self._search_target()
        if not p:
            return
        ok = p.set_search(self.search_entry.get_text())
        if ok is False:
            self.search_entry.add_css_class("error")
        else:
            self.search_entry.remove_css_class("error")
            if ok:
                self._search_step(-1)

    def _search_step(self, direction):
        p = self._search_target()
        if p and self.search_entry.get_text():
            if direction < 0:
                p.term.search_find_previous()
            else:
                p.term.search_find_next()

    def _duplicate(self, pane):
        if pane:
            self.new_tab(cwd=pane.current_dir())

    def _close_tab(self, tab):
        if tab and tab.page:
            if tab.page.get_pinned():
                self.view.set_page_pinned(tab.page, False)
            self.view.close_page(tab.page)

    def _move(self, delta):
        page = self.view.get_selected_page()
        if page:
            (self.view.reorder_forward if delta > 0 else self.view.reorder_backward)(page)
            self.schedule_save()

    def _select_index(self, i):
        if i < self.view.get_n_pages():
            self.view.set_selected_page(self.view.get_nth_page(i))

    def _zoom(self, factor):
        p = self.pane()
        if not p:
            return
        scale = 1.0 if factor is None else p.term.get_font_scale() * factor
        for q in self.all_panes():
            q.term.set_font_scale(scale)

    def _on_close_page(self, view, page):
        tab = page.get_child()
        if page.get_pinned() and not self.quitting:
            self.view.set_page_pinned(page, False)
        panes = tab.panes()
        if not self.quitting:
            for p in panes:
                p.kill_session()
        view.close_page_finish(page, True)
        if not self.quitting:
            for p in panes:
                session.scrollback_path(p.pane_id).unlink(missing_ok=True)
            if view.get_n_pages() == 0:
                self.save(authoritative=True)
                self.close()
            else:
                self.schedule_save()
        return True

    def on_pane_exited(self, pane):
        """A shell do painel terminou (ex.: `exit`). Fecha o painel após um
        pequeno atraso: se for desligamento do sistema, o SIGTERM chega antes
        e nada é removido do estado salvo."""
        if self.quitting:
            return

        def later():
            if self.quitting or pane.get_parent() is None:
                return False
            tab = pane.tab
            if len(tab.panes()) > 1:
                session.scrollback_path(pane.pane_id).unlink(missing_ok=True)
                tab.remove_pane(pane)
                tab.refresh_title()
                self.schedule_save()
            elif tab.page is not None:
                self.view.close_page(tab.page)
            return False

        GLib.timeout_add(CLOSE_DELAY_MS, later)

    def _on_selected(self, *_):
        self._update_chrome()
        self._clear_attention()
        p = self.pane()
        if p:
            p.term.grab_focus()

    def _check_sizes(self):
        if self.quitting:
            return False
        for p in self.all_panes():
            p.check_size()
        return True

    def _update_chrome(self):
        tab = self.current_tab()
        if tab:
            home = os.path.expanduser("~")
            path = "~" + tab.cwd[len(home):] if tab.cwd.startswith(home) else tab.cwd
            n = self.view.get_n_pages()
            np_ = len(tab.panes())
            extra = f"  ·  {np_} painéis" if np_ > 1 else ""
            self.wtitle.set_title(f"Matiz — {tab.display_title()}" if tab.custom_title or tab.fg_name else "Matiz")
            self.wtitle.set_subtitle(f"{path}  ·  {n} aba{'s' if n != 1 else ''}{extra}")
            self.set_title(f"{tab.display_title()} — Matiz")
        if tab:
            self.tree.follow(tab.active)
        color = tab.color if tab else None
        if color:
            head = mix("#2b2f38", color, 0.42)
            bar = mix("#2b2f38", color, 0.22)
            css = (
                f"window.matiz-window headerbar {{ background-color: {head}; }}"
                f"window.matiz-window tabbar .box {{ background-color: {bar}; }}"
            )
        else:
            css = ""
        self.css.load_from_string(css)

    # ================= salvar / restaurar =================
    def restore(self):
        state = session.load_state()
        win = state.get("window") or {}
        self.set_default_size(int(win.get("width", 1100)), int(win.get("height", 720)))
        if win.get("maximized"):
            self.maximize()
        self.split.set_position(int(win.get("arvore_largura", 260)))
        if win.get("arvore"):
            self.tree_btn.set_active(True)
        tabs = [t for t in state.get("tabs", []) if isinstance(t, dict)]
        for t in tabs:
            self.new_tab(state=t, select=False)
        if not tabs:
            self.new_tab(cwd=os.path.expanduser("~"))
        idx = int(state.get("active", 0) or 0)
        if 0 <= idx < self.view.get_n_pages():
            self.view.set_selected_page(self.view.get_nth_page(idx))
        self._update_chrome()
        p = self.pane()
        if p:
            p.term.grab_focus()

    def snapshot(self) -> dict:
        tabs = [t for t in self.tabs() if any(not p.exited for p in t.panes())]
        sel = self.view.get_selected_page()
        active = 0
        for i, t in enumerate(tabs):
            if t.page is sel:
                active = i
        return {
            "version": 2,
            "window": {
                "width": self.get_width() or 1100,
                "height": self.get_height() or 720,
                "maximized": self.is_maximized(),
                "arvore": self.tree.get_visible(),
                "arvore_largura": self.split.get_position(),
            },
            "active": active,
            "tabs": [dict(t.to_state(), pinned=bool(t.page and t.page.get_pinned())) for t in tabs],
        }

    def save(self, *, authoritative=False, scrollback=True, probe=True):
        panes = self.all_panes()
        if probe:
            for p in panes:
                p.probe(self.rerun, authoritative=authoritative)
            for t in self.tabs():
                t.refresh_title()
        if scrollback:
            for p in panes:
                if p.exited:
                    continue
                text = p.scrollback_text(self.max_lines)
                if text:
                    session.save_scrollback(p.pane_id, text)
                else:
                    session.scrollback_path(p.pane_id).unlink(missing_ok=True)
                p.dirty = False
        state = self.snapshot()
        text = session.dump_state(state)
        if text != self._last_state_text:
            session.save_state_text(text)
            self._last_state_text = text
        session.prune_scrollback({pid for t in state["tabs"] for pid in _layout_ids(t.get("layout"))})

    def schedule_save(self):
        if self.quitting:
            return

        def go():
            if not self.quitting:
                self.save(scrollback=False)
            return False

        GLib.timeout_add(300, go)

    def _on_tick(self):
        if self.quitting:
            return False
        self._tick += 1
        need_sb = self._tick % SCROLLBACK_EVERY == 0 and any(p.dirty for p in self.all_panes())
        try:
            self.save(scrollback=need_sb)
        except Exception as e:  # nunca derruba o terminal por erro de gravação
            print(f"matiz: falha ao salvar estado: {e}", file=sys.stderr)
        self._update_chrome()
        return True

    def _on_close_request(self, *_):
        if not self.quitting:
            # Fechar a janela NÃO fecha a sessão: tudo é salvo para reabrir igual.
            try:
                self.save(authoritative=True)
            finally:
                self.quitting = True
        return False

    def _debug_dump(self):
        d = os.environ.get("MATIZ_DEBUG_DUMP")
        if not d:
            return
        for p in self.all_panes():
            try:
                res = p.term.get_text_format(_vte().Format.TEXT)
                text = res[0] if isinstance(res, tuple) else res
                session.atomic_write(session.Path(d) / f"{p.pane_id}.screen.txt", text or "")
            except Exception as e:
                print(f"matiz: dump falhou: {e}", file=sys.stderr)

    def on_terminate_signal(self):
        """SIGTERM/SIGHUP (desligar, logout): salva sem 'autoridade' — os
        processos podem já estar morrendo — e sai."""
        self._debug_dump()
        if not self.quitting:
            try:
                self.save(authoritative=False)
            except Exception as e:
                print(f"matiz: falha ao salvar no encerramento: {e}", file=sys.stderr)
            self.quitting = True
        self.get_application().quit()


def _layout_ids(node) -> list[str]:
    if not isinstance(node, dict):
        return []
    if "split" in node:
        return _layout_ids(node.get("a")) + _layout_ids(node.get("b"))
    st = node.get("pane", node)
    return [st["id"]] if st.get("id") else []


def _fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def _ignore_finish(method):
    def cb(obj, res):
        try:
            getattr(obj, method)(res)
        except GLib.Error:
            pass
    return cb


EDITOR_IDS = [
    "code.desktop", "code-insiders.desktop", "cursor.desktop", "dev.zed.Zed.desktop",
    "zed.desktop", "sublime_text.desktop", "windsurf.desktop", "codium.desktop",
]


def folder_apps() -> list:
    """Aplicativos que abrem pastas: o padrão do sistema primeiro, depois os
    demais registrados para pastas e editores de código instalados."""
    seen, out = set(), []

    def add(app):
        if app and app.get_id() not in seen and app.should_show():
            seen.add(app.get_id())
            out.append(app)

    add(Gio.AppInfo.get_default_for_type("inode/directory", False))
    for app in Gio.AppInfo.get_all_for_type("inode/directory"):
        add(app)
    for app_id in EDITOR_IDS:
        try:
            add(Gio.DesktopAppInfo.new(app_id))
        except TypeError:
            pass
    return out


def _vte():
    from gi.repository import Vte
    return Vte


class MatizApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=os.environ.get("MATIZ_APP_ID", APP_ID),
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: MatizWindow | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        focar = Gio.SimpleAction.new("focar", GLib.VariantType.new("s"))
        focar.connect("activate", lambda _a, v: self.window and self.window.focus_pane(v.get_string()))
        self.add_action(focar)

    def _on_signal(self):
        if self.window:
            self.window.on_terminate_signal()
        else:
            self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        if self.window is None:
            self.window = MatizWindow(self)
            self.window.restore()
        self.window.present()


def main(argv=None) -> int:
    return MatizApp().run(argv if argv is not None else sys.argv)
