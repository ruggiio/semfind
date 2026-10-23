#!/usr/bin/python3
"""Spotlight per GNOME: finestra sempre in memoria, mostrata/nascosta con l'azione DBus "toggle".

Gira con il Python di sistema (PyGObject), non con il venv di torch: la ricerca usa
semfind.search, che parla con llama-server per il vettore della query.
"""
import ast
import math
import operator
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from semfind import search as sf  # noqa: E402

try:  # serve per export_handle() sulla superficie Wayland (anteprima agganciata a Spotlight)
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland  # noqa: F401
except (ValueError, ImportError):
    pass

try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

APP_ID = "io.github.semfind.Spotlight"
HOME = os.path.expanduser("~")
CSS_PATH = Path(__file__).with_name("spotlight.css")

CATEGORIES = [  # ordine delle sezioni, come in Spotlight
    ("top", "Risultato migliore"),
    ("calc", "Calcolatrice"),
    ("app", "Applicazioni"),
    ("doc", "Documenti"),
    ("pdf", "Documenti PDF"),
    ("img", "Immagini"),
    ("dev", "Sviluppo"),
    ("other", "Altro"),
]
CAT_TITLE = dict(CATEGORIES)
CAT_ORDER = {k: i for i, (k, _) in enumerate(CATEGORIES)}
EXT_CAT = {
    **dict.fromkeys(["pdf"], "pdf"),
    **dict.fromkeys(["md", "txt", "tex", "docx", "pptx", "epub", "rst", "org", "odt"], "doc"),
    **dict.fromkeys(["jpg", "jpeg", "png", "webp", "gif", "heic"], "img"),
    **dict.fromkeys(["py", "m", "ino", "ipynb", "c", "cpp", "h", "js", "sh", "slx"], "dev"),
}
PER_CAT = 4
FILTERS = [("app", "view-app-grid-symbolic", "Applicazioni"),
           ("file", "folder-symbolic", "File"),
           ("img", "image-x-generic-symbolic", "Immagini")]


@dataclass
class Result:
    cat: str
    title: str
    subtitle: str = ""
    right: str = ""
    gicon: Gio.Icon | None = None
    path: str | None = None
    page: int | None = None
    app: Gio.AppInfo | None = None
    value: str | None = None  # risultato della calcolatrice


# --------------------------------------------------------------------------- calcolatrice

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "asin", "acos", "atan")}
_CONSTS = {"pi": math.pi, "e": math.e}


def calc(q: str) -> str | None:
    s = q.strip().replace(",", ".").replace("^", "**").replace("×", "*").replace("÷", "/")
    if not any(c.isdigit() for c in s) or not any(c in s for c in "+-*/%(") and not any(f in s for f in _FUNCS):
        return None

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 1000:
                raise ValueError
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and len(n.args) == 1:
            return _FUNCS[n.func.id](ev(n.args[0]))
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        raise ValueError
    try:
        v = ev(ast.parse(s, mode="eval"))
    except Exception:
        return None
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            v = int(v)
        else:
            return f"{v:.10g}".replace(".", ",")
    return str(v)


# --------------------------------------------------------------------------- app

class Apps:
    def __init__(self):
        self.load()
        self._mon = Gio.AppInfoMonitor.get()
        self._mon.connect("changed", lambda *_: self.load())

    def load(self):
        self.apps = []
        for a in Gio.AppInfo.get_all():
            if not a.should_show():
                continue
            extra = []
            if isinstance(a, DesktopAppInfo):
                extra = [a.get_generic_name() or "", *(a.get_keywords() or [])]
            self.apps.append((a, a.get_display_name().lower(), " ".join(extra).lower()))

    def search(self, q: str, n: int = 6):
        q = q.lower().strip()
        if not q:
            return []
        scored = []
        for a, name, extra in self.apps:
            if name.startswith(q):
                s = 4
            elif any(w.startswith(q) for w in name.split()):
                s = 3
            elif q in name:
                s = 2
            elif len(q) >= 3 and q in extra:
                s = 1
            else:
                continue
            scored.append((-s, len(name), a))
        scored.sort(key=lambda t: t[:2])
        return [(-s, a) for s, _, a in scored[:n]]


# --------------------------------------------------------------------------- righe

class Row(Gtk.ListBoxRow):
    def __init__(self, r: Result, show_header: bool):
        super().__init__()
        self.result = r
        self.show_header = show_header
        self.add_css_class("sf-row")
        box = Gtk.Box(spacing=10)
        self.icon = Gtk.Image(pixel_size=32 if r.cat != "top" else 40)
        self.icon.add_css_class("sf-icon")
        if r.gicon:
            self.icon.set_from_gicon(r.gicon)
        box.append(self.icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        t = Gtk.Label(label=r.title, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        t.add_css_class("sf-title")
        texts.append(t)
        if r.subtitle:
            s = Gtk.Label(label=r.subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            s.add_css_class("sf-sub")
            texts.append(s)
        box.append(texts)
        if r.right:
            rl = Gtk.Label(label=r.right, xalign=1)
            rl.add_css_class("sf-right")
            box.append(rl)
        self.set_child(box)
        if r.path:
            self.set_tooltip_text(r.path.replace(HOME, "~", 1))


def thumbnail(path: str, size: int):
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size * 2, size * 2, True)
        return Gdk.Texture.new_for_pixbuf(pb)
    except GLib.Error:
        return None


# --------------------------------------------------------------------------- finestra

class Spotlight(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Spotlight", decorated=False)
        self.add_css_class("sf-overlay")
        # ~/.config/gtk-4.0/gtk.css dà a .background il colore (semitrasparente) del tema:
        # sulla finestra a tutto schermo oscurerebbe l'intero desktop
        self.remove_css_class("background")
        self.apps = Apps()
        self.seq = 0
        self.timer = 0
        self.filter: str | None = None
        self.app_results: list[Result] = []
        self.file_results: list[Result] = []
        self.calc_result: Result | None = None
        self.was_active = False
        self.previewing = False
        self.preview_sub = 0
        self.handle = ""

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.outer = outer
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_outer_click)
        outer.add_controller(click)

        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                             valign=Gtk.Align.START, width_request=680)
        self.panel.add_css_class("sf-panel")
        outer.append(self.panel)

        bar = Gtk.Box(spacing=12)
        bar.add_css_class("sf-bar")
        mag = Gtk.Image.new_from_icon_name("system-search-symbolic")
        mag.add_css_class("sf-mag")
        bar.append(mag)
        self.entry = Gtk.Text(hexpand=True, placeholder_text="Ricerca Spotlight")
        self.entry.add_css_class("sf-entry")
        self.entry.connect("changed", self.on_changed)
        bar.append(self.entry)
        self.filter_buttons = {}
        for key, icon, tip in FILTERS:
            b = Gtk.ToggleButton(icon_name=icon, tooltip_text=f"{tip}", valign=Gtk.Align.CENTER)
            b.add_css_class("sf-filter")
            b.connect("toggled", self.on_filter, key)
            self.filter_buttons[key] = b
            bar.append(b)
        self.panel.append(bar)

        self.sep = Gtk.Separator()
        self.sep.add_css_class("sf-sep")
        self.panel.append(self.sep)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True)
        self.list.add_css_class("sf-list")
        self.list.set_header_func(self.header_func)
        self.list.connect("row-activated", lambda _l, row: self.open(row.result))
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         propagate_natural_height=True, max_content_height=470)
        self.scroll.set_child(self.list)
        self.scroll.add_css_class("sf-scroll")
        self.panel.append(self.scroll)
        self.set_expanded(False)
        self.set_child(outer)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.connect("notify::is-active", self.on_active)
        self.connect("notify::default-height", lambda *_: self.place_panel())
        self.connect("close-request", lambda *_: (self.hide_spotlight(), True)[1])

    # -- posizione: circa a un quarto dell'altezza dello schermo, come su macOS
    def place_panel(self):
        h = self.get_height() or 1080
        self.panel.set_margin_top(int(h * 0.24))

    def set_expanded(self, on: bool):
        self.sep.set_visible(on)
        self.scroll.set_visible(on)
        if on:
            self.panel.add_css_class("expanded")
        else:
            self.panel.remove_css_class("expanded")

    # -- mostra / nascondi
    def toggle(self):
        if self.get_visible() and self.is_active():
            self.hide_spotlight()
        else:
            self.show_spotlight()

    def show_spotlight(self):
        self.previewing = False
        # massimizzata, non fullscreen: per xdg-shell sotto una finestra fullscreen il
        # compositor deve nascondere tutto il resto (Mutter mette il nero)
        self.maximize()
        self.present()
        self.place_panel()
        if not self.handle:
            GLib.idle_add(lambda: self.export_handle() and False)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)  # come macOS: l'ultima ricerca resta, selezionata
        if os.environ.get("SEMFIND_DEBUG"):
            GLib.timeout_add(400, lambda: print("attivo dopo 400 ms:", self.is_active(), flush=True) and False)

    def hide_spotlight(self):
        self.was_active = False
        if self.previewing:
            try:
                self.previewer_call("Close")
            except GLib.Error:
                pass
            self.previewing = False
        if self.handle:
            surf = self.get_surface()
            if surf is not None and hasattr(surf, "drop_exported_handle"):
                surf.drop_exported_handle(self.handle.removeprefix("wayland:"))
            self.handle = ""
        self.set_visible(False)

    def on_active(self, *_):
        if self.is_active():
            self.was_active = True
            self.previewing = False
        elif self.was_active and self.get_visible() and not self.previewing:
            self.hide_spotlight()

    def on_outer_click(self, gesture, _n, x, y):
        ok, b = self.panel.compute_bounds(self.outer)
        if ok and not (b.origin.x <= x <= b.origin.x + b.size.width and b.origin.y <= y <= b.origin.y + b.size.height):
            self.hide_spotlight()

    # -- tastiera
    def on_key(self, _c, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if keyval == Gdk.KEY_Escape:
            if self.entry.get_text():
                self.entry.set_text("")
            else:
                self.hide_spotlight()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self.move(1 if keyval == Gdk.KEY_Down else -1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            row = self.list.get_selected_row()
            if row:
                self.reveal(row.result) if ctrl else self.open(row.result)
            return True
        if ctrl and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            row = self.list.get_selected_row()
            if row and row.result.path:
                self.quicklook(row.result.path)
            return True
        if ctrl and keyval in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3):
            key = FILTERS[keyval - Gdk.KEY_1][0]
            b = self.filter_buttons[key]
            b.set_active(not b.get_active())
            return True
        if not self.entry.has_focus() and keyval < 0xff00 and not ctrl:
            self.entry.grab_focus()
        return False

    def move(self, d):
        rows = []
        r = self.list.get_row_at_index(0)
        i = 0
        while r:
            rows.append(r)
            i += 1
            r = self.list.get_row_at_index(i)
        if not rows:
            return
        cur = self.list.get_selected_row()
        idx = rows.index(cur) if cur in rows else -1
        nxt = rows[max(0, min(len(rows) - 1, idx + d))]
        self.list.select_row(nxt)
        nxt.grab_focus()
        self.entry.grab_focus_without_selecting()

    # -- filtri
    def on_filter(self, button, key):
        if button.get_active():
            for k, b in self.filter_buttons.items():
                if k != key and b.get_active():
                    b.set_active(False)
            self.filter = key
        elif self.filter == key:
            self.filter = None
        self.on_changed()

    # -- ricerca
    def on_changed(self, *_):
        q = self.entry.get_text().strip()
        self.seq += 1
        seq = self.seq
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.file_results = []
        if not q:
            self.app_results, self.calc_result = [], None
            self.render()
            return
        v = calc(q) if self.filter is None else None
        self.calc_result = Result("calc", f"= {v}", "Invio per copiare il risultato", value=v,
                                  gicon=Gio.ThemedIcon.new("accessories-calculator")) if v else None
        self.app_results = []
        if self.filter in (None, "app"):
            for score, a in self.apps.search(q, 6 if self.filter == "app" else 4):
                self.app_results.append(Result("app", a.get_display_name(), a.get_description() or "",
                                               gicon=a.get_icon(), app=a, value=str(score)))
        self.render()
        if self.filter != "app" and len(q) >= 3 and not v:
            self.timer = GLib.timeout_add(90, self.start_file_search, q, seq)

    def start_file_search(self, q, seq):
        self.timer = 0
        kinds = ("image",) if self.filter == "img" else ("text", "image", "name")
        threading.Thread(target=self.file_worker, args=(q, seq, kinds), daemon=True).start()
        return False

    def file_worker(self, q, seq, kinds):
        try:
            hits = sf.search(q, n=24, kinds=kinds)
        except Exception as ex:  # indice in aggiornamento, server giù, ...
            print("ricerca fallita:", ex, file=sys.stderr)
            hits = []
        GLib.idle_add(self.on_file_hits, seq, hits)

    def on_file_hits(self, seq, hits):
        if seq != self.seq:
            return False
        out = []
        for h in hits:
            name = os.path.basename(h.path)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            cat = EXT_CAT.get(ext, "other")
            folder = os.path.dirname(h.path).replace(HOME, "~", 1)
            sub = folder
            if h.snippet and cat in ("doc", "pdf"):
                sub = h.snippet
                if sub.startswith("…"):  # niente mezze parole all'inizio
                    sub = "…" + sub[1:].split(" ", 1)[-1]
            ctype, _ = Gio.content_type_guess(h.path, None)
            out.append(Result(cat, name, sub, f"p. {h.page}" if h.page else "",
                              gicon=Gio.content_type_get_icon(ctype), path=h.path, page=h.page))
        self.file_results = out
        self.render()
        return False

    def ordered(self) -> list[Result]:
        res: list[Result] = []
        apps, files = list(self.app_results), list(self.file_results)
        # risultato migliore: un'app se il nome inizia con la query, altrimenti il primo file
        top = None
        if apps and int(apps[0].value) >= 3:
            top = apps.pop(0)
        elif files:
            top = files.pop(0)
        elif apps:
            top = apps.pop(0)
        if self.calc_result:
            res.append(self.calc_result)
        if top:
            res.append(Result("top", top.title, top.subtitle, top.right, top.gicon, top.path,
                              top.page, top.app, top.value))
        res += apps
        counts: dict[str, int] = {}
        limit = 12 if self.filter else PER_CAT
        for r in sorted(files, key=lambda r: CAT_ORDER[r.cat]):
            if counts.get(r.cat, 0) < limit:
                counts[r.cat] = counts.get(r.cat, 0) + 1
                res.append(r)
        return res

    def render(self):
        self.list.remove_all()
        results = self.ordered()
        prev = None
        first = None
        for r in results:
            row = Row(r, r.cat != prev)
            prev = r.cat
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda _c, _x, _y, row=row: self.list.select_row(row))
            row.add_controller(hover)
            self.list.append(row)
            first = first or row
            if r.cat == "img" or (r.cat == "top" and r.path and EXT_CAT.get(r.path.rsplit(".", 1)[-1].lower()) == "img"):
                self.load_thumb(row)
        self.set_expanded(bool(results))
        if first:
            self.list.select_row(first)
            self.scroll.get_vadjustment().set_value(0)

    def load_thumb(self, row: Row):
        path, size = row.result.path, row.icon.get_pixel_size()

        def work():
            tex = thumbnail(path, size)
            if tex:
                GLib.idle_add(row.icon.set_from_paintable, tex)
        threading.Thread(target=work, daemon=True).start()

    def header_func(self, row, before):
        if not row.show_header:
            row.set_header(None)
            return
        lab = Gtk.Label(label=CAT_TITLE[row.result.cat], xalign=0)
        lab.add_css_class("sf-header")
        row.set_header(lab)

    # -- azioni
    def launch_ctx(self):
        return self.get_display().get_app_launch_context()

    def open(self, r: Result):
        if r.value is not None and r.cat == "calc":
            self.get_display().get_clipboard().set(r.value)
        elif r.app:
            r.app.launch([], self.launch_ctx())
        elif r.path:
            uri = Gio.File.new_for_path(r.path).get_uri()
            default = Gio.AppInfo.get_default_for_type(Gio.content_type_guess(r.path, None)[0], False)
            if r.page and default and default.get_id() == "org.gnome.Papers.desktop":
                # apre il PDF alla pagina del pezzo trovato
                Gio.AppInfo.create_from_commandline(f"papers -i {r.page}", "Papers",
                                                    Gio.AppInfoCreateFlags.NONE).launch([Gio.File.new_for_path(r.path)], self.launch_ctx())
            else:
                Gio.AppInfo.launch_default_for_uri(uri, self.launch_ctx())
        self.hide_spotlight()

    def reveal(self, r: Result):
        if not r.path:
            return
        uri = Gio.File.new_for_path(r.path).get_uri()
        Gio.bus_get_sync(Gio.BusType.SESSION).call(
            "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1",
            "ShowItems", GLib.Variant("(ass)", ([uri], "")), None, Gio.DBusCallFlags.NONE, -1)
        self.hide_spotlight()

    # -- anteprima rapida (Sushi), come Quick Look: Ctrl+Y apre/chiude, le frecce dentro
    #    l'anteprima scorrono i risultati (segnale SelectionEvent, come fa Nautilus)
    PREVIEWER = ("org.gnome.NautilusPreviewer", "/org/gnome/NautilusPreviewer", "org.gnome.NautilusPreviewer2")

    def previewer_call(self, method, params=None):
        Gio.bus_get_sync(Gio.BusType.SESSION).call(*self.PREVIEWER, method, params, None,
                                                   Gio.DBusCallFlags.NONE, -1, None, None)

    def quicklook(self, path: str, close_if_shown=True):
        if not self.preview_sub:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            self.preview_sub = bus.signal_subscribe(
                self.PREVIEWER[0], self.PREVIEWER[2], "SelectionEvent", self.PREVIEWER[1], None,
                Gio.DBusSignalFlags.NONE, self.on_preview_selection)
        self.previewing = True
        uri = Gio.File.new_for_path(path).get_uri()
        token = self.launch_ctx().get_startup_notify_id(None, []) or ""
        self.previewer_call("ShowFile", GLib.Variant("(ssbs)", (uri, self.handle, close_if_shown, token)))

    def on_preview_selection(self, _bus, _sender, _path, _iface, _signal, params):
        (direction,) = params.unpack()
        if direction in (Gtk.DirectionType.UP, Gtk.DirectionType.LEFT, Gtk.DirectionType.TAB_BACKWARD):
            step = -1
        else:
            step = 1
        # salta app e calcolatrice: in anteprima vanno solo i file
        before = self.list.get_selected_row()
        for _ in range(50):
            self.move(step)
            row = self.list.get_selected_row()
            if row is before or row is None:
                return
            if row.result.path:
                self.quicklook(row.result.path, close_if_shown=False)
                return
            before = row

    def export_handle(self):
        """Handle xdg-foreign: Sushi si apre come finestra figlia di Spotlight, sopra il pannello."""
        surf = self.get_surface()
        if surf is None or not hasattr(surf, "export_handle"):
            return

        def done(_toplevel, handle, *_):
            self.handle = f"wayland:{handle}"
        try:
            surf.export_handle(done)
        except TypeError:
            pass


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()  # resta in memoria anche senza finestre visibili
        css = Gtk.CssProvider()
        css.load_from_path(str(CSS_PATH))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 10)  # sopra gtk.css dell'utente
        self.win = Spotlight(self)
        sm = Adw.StyleManager.get_default()
        sm.connect("notify::dark", self.on_dark)
        self.on_dark(sm)
        act = Gio.SimpleAction.new("toggle", None)
        act.connect("activate", lambda *_: self.win.toggle())
        self.add_action(act)

    def on_dark(self, sm, *_):
        for c in ("dark", "light"):
            self.win.remove_css_class(c)
        self.win.add_css_class("dark" if sm.get_dark() else "light")

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--toggle" in args or "--show" in args:
            self.win.show_spotlight() if "--show" in args else self.win.toggle()
        if "--preview" in args:  # debug: cerca e apre l'anteprima del primo file
            query = args[args.index("--preview") + 1]
            self.win.show_spotlight()
            self.win.entry.set_text(query)

            def go():
                row = self.win.list.get_selected_row()
                if row and row.result.path:
                    self.win.quicklook(row.result.path)
                    print("anteprima:", row.result.path, "handle:", self.win.handle or "(nessuno)", flush=True)
                return False
            GLib.timeout_add(1500, go)
        if "--snapshot" in args:  # debug: salva il pannello in PNG
            out, query = args[args.index("--snapshot") + 1], args[args.index("--snapshot") + 2]
            self.win.show_spotlight()
            self.win.entry.set_text(query)
            GLib.timeout_add(1500, self.snapshot, out)
        return 0

    def snapshot(self, out):
        w = self.win if out.endswith("-full.png") else self.win.panel
        paint = Gtk.WidgetPaintable.new(w)
        snap = Gtk.Snapshot()
        paint.snapshot(snap, w.get_width(), w.get_height())
        node = snap.to_node()
        if node:
            tex = self.win.get_renderer().render_texture(node, None)
            tex.save_to_png(out)
            print("snapshot:", out, w.get_width(), "x", w.get_height())
        self.win.hide_spotlight()
        return False


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
