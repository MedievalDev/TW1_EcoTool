"""TW1 EcoTool - the scripts of Two Worlds 1 as real, compilable source.

Reads every compiled script (.eco) of the installed game and of active mods, finds the SDK source that compiles
to exactly the same bytes, and exports those sources so that they compile again. Scripts without a source are
shown and exported as readable decompiler output. It can also bring an old SDK (1.2) up to the game's state.
Core: ecocore.py; decompiler: decomp\\ (from the EcoAnalysis of 07/2026). Design after PY_TOOL_DESIGN.md.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tkinter as tk                                   # noqa: E402
from tkinter import ttk, filedialog, messagebox        # noqa: E402

import theme                                            # noqa: E402
import guidebook                                        # noqa: E402
import updater                                          # noqa: E402
import ecocore                                          # noqa: E402
from version import VERSION                             # noqa: E402

APP_NAME = 'TW1 ECOTOOL'
GITHUB_URL = 'https://github.com/MedievalDev/TW1_EcoTool'
SITE_URL = 'https://alchemy-fox.de/'
GUIDE_URL = 'https://alchemy-fox.de/game/TW1_EcoTool/'
COMMUNITY_URL = 'https://twmp.alchemy-fox.de/'
LINKS = (('GitHub-Repo', GITHUB_URL), ('Alchemy Fox', SITE_URL),
         ('Guide-Seite', GUIDE_URL), ('Community', COMMUNITY_URL))
SEP = chr(92)
NL = chr(10)

# status of a script -> (label, colour); the guide reads the same table
STATUS_INFO = {
    'source': ('Source', 'A source of your SDK compiles to exactly the bytes the game uses. Export gives you that '
               'source with all its include files; it compiles again to the same file.'),
    'rebuilt': ('Rebuilt', 'No SDK has a source for this script (or only another version), but the decompiler '
                'rebuilt one from the compiled script that compiles to exactly the game\'s bytes - checked.'),
    'v10': ('Rebuilt (v1.0)', 'A debug build of the 2007 compiler, no SDK source. The decompiler rebuilt it; compiled '
            'with SDK 1.3 it is the same program as the game\'s, routine by routine - engine function numbers '
            'translated, source paths and line numbers aside. Checked.'),
    'differs': ('Changed', 'There is a source with the same name, but it compiles to other bytes: the game or a mod '
                'uses another version, and the decompiler could not rebuild it exactly yet.'),
    'none': ('No source', 'No SDK has a source for this script and the decompiler could not rebuild it exactly '
             '(the v1.0 debug builds CityCampaign and MissionTeamHunt call functions SDK 1.3 no longer has). You '
             'see the rebuilt source with what differs, or the decompiler output.'),
}
STATUS_COLOR = {'source': theme.STATUS_SOURCE, 'rebuilt': theme.STATUS_REBUILT, 'v10': theme.STATUS_REBUILT,
                'differs': theme.STATUS_DIFFERS,
                'none': theme.STATUS_NONE}


# ------------------------------------------------------------------ language --

_LANG = 'en'


def system_is_german():
    try:
        import ctypes
        return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x07
    except Exception:
        return False


def tr(text):
    if _LANG == 'de':
        return DE.get(text, text)
    return text


# ---------------------------------------------------------------- settings --

def data_dir():
    if getattr(sys, 'frozen', False):
        d = os.path.join(os.environ.get('LOCALAPPDATA', HERE), 'TW1EcoTool')
        os.makedirs(d, exist_ok=True)
        return d
    return HERE


class Config(dict):
    def __init__(self):
        super().__init__()
        self.path = os.path.join(data_dir(), 'eco_tool_settings.json')
        try:
            with open(self.path, encoding='utf-8') as f:
                self.update(json.load(f))
        except Exception:
            pass

    def save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self, f, indent=2)
        except Exception:
            pass


def cache_dir():
    return os.path.join(data_dir(), 'cache')


def backup_dir():
    return os.path.join(data_dir(), 'backup')


def default_export_dir():
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser('~'), 'TW1_EcoTool_Export')


def help_mark(parent, text, chapter, app, style=None, side='left'):
    lbl = ttk.Label(parent, text='?', foreground=theme.GOLD, cursor='hand2', style=style or 'TLabel')
    lbl.pack(side=side, padx=(6, 0) if side == 'left' else (4, 4))
    theme.Tooltip(lbl, text)
    lbl.bind('<Button-1>', lambda ev: app.show_help(text, chapter))
    return lbl


# ------------------------------------------------------------- highlighting --

_KEYWORDS = ('function', 'state', 'event', 'command', 'if', 'else', 'while', 'for', 'return', 'break', 'continue',
             'switch', 'case', 'default', 'global', 'mission', 'campaign', 'hero', 'RPGCompute', 'true', 'false',
             'null', 'hidden', 'button', 'item', 'priority', 'enum', 'consts')
_TYPES = ('int', 'float', 'string', 'stringW', 'unit', 'mission', 'object', 'void', 'player', 'UnitValues')
_TOKEN = re.compile(r'(?P<comment>//[^\n]*|/\*.*?\*/)|(?P<string>"(?:\\.|[^"\\])*")|(?P<preproc>^\s*#[^\n]*)'
                    r'|(?P<number>\b\d+(?:\.\d+)?\b|\b0x[0-9a-fA-F]+\b)|(?P<word>\b[A-Za-z_]\w*\b)',
                    re.S | re.M)


def highlight(text_widget, text):
    """Fill a read-only Text with EarthC source and colour it."""
    w = text_widget
    w.configure(state='normal')
    w.delete('1.0', 'end')
    w.insert('1.0', text)
    for tag in theme.SYNTAX:
        w.tag_remove(tag, '1.0', 'end')
    if len(text) < 1500000:
        for m in _TOKEN.finditer(text):
            kind = m.lastgroup
            if kind == 'word':
                word = m.group(0)
                kind = 'keyword' if word in _KEYWORDS else 'type' if word in _TYPES else None
                if kind is None:
                    continue
            w.tag_add(kind, f'1.0+{m.start()}c', f'1.0+{m.end()}c')
    w.configure(state='disabled')


# -------------------------------------------------------------------- tour --

GUIDE_STEPS = [
    {'title': 'Welcome', 'widget': None, 'text':
     'TW1 EcoTool shows the scripts of Two Worlds as source code. Where a source of your SDK compiles to exactly '
     'the game\'s bytes, you get that source; everything else is shown as readable decompiler output.'},
    {'title': 'Game and SDK', 'widget': 'head', 'text':
     'Up here: which game and which SDKs the tool found. Read game reads the scripts again (also from active mods). '
     'The first time the SDK sources are compiled once and remembered - that takes about half a minute.'},
    {'title': 'The scripts', 'widget': 'tree', 'text':
     'Every script of the game with its status. Green: a source compiles to the same bytes. Orange: a source '
     'exists, but the game or a mod uses another version. Red: no source, decompiler only.'},
    {'title': 'Source code', 'widget': 'nb', 'text':
     'The selected script: its source or the decompiler output, the include files it needs, and the details '
     '(which archive, which hash, which older copies the game hides).'},
    {'title': 'Export', 'widget': 'btn_export', 'text':
     'Export writes the sources into a folder, in the SDK layout, with a compile_all.bat. Every exported script is '
     'compiled once more and compared with the game before you get the result.'},
    {'title': 'Update an SDK', 'widget': 'btn_update', 'text':
     'Update SDK brings an old SDK (1.2) to the state of the game: it shows every file first, makes a backup, and '
     'Tools > Undo an SDK update puts everything back.'},
    {'title': 'Help', 'widget': 'menubar', 'text':
     'F1 opens the guide. The gold ? marks jump to the matching chapter. Help also checks for updates and lets '
     'you report a bug.'},
]


class Guide:
    def __init__(self, app):
        self.app, self.i, self.frames, self.win = app, 0, [], None

    def start(self):
        self.i = 0
        if self.win:
            self.win.destroy()
        self.win = tk.Toplevel(self.app.root)
        self.win.title(tr('Tour'))
        self.win.configure(background=theme.PANEL)
        self.win.transient(self.app.root)
        self.win.protocol('WM_DELETE_WINDOW', lambda: self.finish(False))
        theme.dark_titlebar(self.win)
        f = ttk.Frame(self.win, style='Panel.TFrame', padding=14)
        f.pack(fill='both', expand=True)
        self.head = ttk.Label(f, style='PanelTitle.TLabel')
        self.head.pack(anchor='w')
        self.title = ttk.Label(f, style='Panel.TLabel', font=('Segoe UI Semibold', 11), foreground=theme.GOLD)
        self.title.pack(anchor='w', pady=(4, 6))
        self.text = ttk.Label(f, style='Panel.TLabel', wraplength=340, justify='left')
        self.text.pack(anchor='w')
        self.dont = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text=tr("Don't show at startup"), variable=self.dont,
                        style='Panel.TCheckbutton').pack(anchor='w', pady=(14, 8))
        b = ttk.Frame(f, style='Panel.TFrame')
        b.pack(fill='x')
        self.back = ttk.Button(b, text=tr('Back'), command=self.prev)
        self.back.pack(side='left')
        self.next = ttk.Button(b, text=tr('Next'), style='Accent.TButton', command=self.nxt)
        self.next.pack(side='left', padx=8)
        ttk.Button(b, text=tr('Quit tour'), command=lambda: self.finish(self.dont.get())).pack(side='right')
        self.win.bind('<Escape>', lambda e: self.finish(self.dont.get()))
        self.win.bind('<Return>', lambda e: self.nxt())
        self.show()
        self.place()
        try:
            self.win.lift()
            self.win.attributes('-topmost', True)
        except tk.TclError:
            pass

    def place(self):
        r = self.app.root
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        x, y = r.winfo_rootx() + 24, r.winfo_rooty() + 110
        x = min(x, max(0, r.winfo_screenwidth() - w - 10))
        y = min(y, max(0, r.winfo_screenheight() - h - 40))
        self.win.geometry(f'+{x}+{y}')

    def show(self):
        s = GUIDE_STEPS[self.i]
        self.head.configure(text=tr('Step {n} of {m}').format(n=self.i + 1, m=len(GUIDE_STEPS)))
        self.title.configure(text=tr(s['title']))
        self.text.configure(text=tr(s['text']))
        self.back.state(['!disabled'] if self.i > 0 else ['disabled'])
        self.next.configure(text=tr('Next') if self.i < len(GUIDE_STEPS) - 1 else tr('Finish'))
        self.highlight(getattr(self.app, s['widget'], None) if s['widget'] else None)

    def prev(self):
        if self.i > 0:
            self.i -= 1
            self.show()

    def nxt(self):
        if self.i < len(GUIDE_STEPS) - 1:
            self.i += 1
            self.show()
        else:
            self.finish(True)

    def highlight(self, widget):
        for f in self.frames:
            f.destroy()
        self.frames = []
        if widget is None:
            return
        root = self.app.root
        root.update_idletasks()
        x = widget.winfo_rootx() - root.winfo_rootx()
        y = widget.winfo_rooty() - root.winfo_rooty()
        w, h, t = widget.winfo_width(), widget.winfo_height(), 3
        for fx, fy, fw, fh in ((x, y, w, t), (x, y + h - t, w, t), (x, y, t, h), (x + w - t, y, t, h)):
            f = tk.Frame(root, background=theme.GOLD)
            f.place(x=fx, y=fy, width=fw, height=fh)
            self.frames.append(f)

    def finish(self, dont_show):
        self.highlight(None)
        if dont_show or self.i == len(GUIDE_STEPS) - 1:
            self.app.cfg['guide_seen'] = True
            self.app.cfg.save()
        if self.win:
            self.win.destroy()
            self.win = None


# --------------------------------------------------------------------- App --

class App:
    def __init__(self, carry=None):
        global _LANG
        self.cfg = Config()
        self._carry = carry or {}
        self.selftest = os.environ.get('ECOTOOL_SELFTEST')
        self._drops = []                   # dropped files waiting for the worker
        self._drop_ok = False
        _LANG = self.cfg.get('lang') or ('de' if system_is_german() else 'en')
        self.root = tk.Tk()
        self.root.withdraw()
        theme.apply_dark_theme(self.root)
        self.root.title(f'TW1 EcoTool {VERSION}')
        self._icon()
        self.restart = False
        self.busy = False
        self._cancel = False
        self._q = queue.Queue()
        self.scripts = {}
        self.index = None
        self.sdks = []
        self.game_dir = None
        self.extra_files = []
        self.problems = []
        self.current = None
        self._decomp_cache = {}
        self.filter_vars = {k: tk.BooleanVar(value=True) for k in STATUS_INFO}
        self.filter_mods = tk.BooleanVar(value=False)
        self.update_var = tk.BooleanVar(value=bool(self.cfg.get('update_check', True)))
        self.guide = Guide(self)
        self._init_feedback()
        self.build()
        self.place_window()
        self.root.deiconify()
        self.root.after(150, self._startup)

    # ---- feedback (tw1-testfenster) ----
    def _init_feedback(self):
        import foxfeedback_ui
        base = getattr(sys, '_MEIPASS', HERE)

        def cfg_set(key, value):
            self.cfg[key] = value
            self.cfg.save()
        self.fb = foxfeedback_ui.FeedbackUI(
            self.root, 'ecotool', VERSION,
            cfg_get=lambda k, d=None: self.cfg.get(k, d), cfg_set=cfg_set,
            lang=_LANG, tests_file=os.path.join(base, 'untested.json'),
            open_guide=self.show_guide, tool_name='TW1 EcoTool', launcher=None)
        self.root.report_callback_exception = self._crash

    def _crash(self, exc, val, tb):
        frames = traceback.extract_tb(tb)
        mine = [f for f in frames if os.path.dirname(os.path.abspath(f.filename)) in (HERE, getattr(sys, '_MEIPASS', HERE))]
        where = mine[-1] if mine else (frames[-1] if frames else None)
        spot = f'{os.path.basename(where.filename)}:{where.lineno}' if where else '?'
        shown = ''.join(traceback.format_exception(exc, val, tb))[-3000:]
        try:
            self.fb.log.add(f'crash {exc.__name__} at {spot}')
            ErrorDialog(self, 'crash', f'{exc.__name__} at {spot}', shown, None, title='crash: ' + exc.__name__)
        except Exception:
            sys.__excepthook__(exc, val, tb)

    def error(self, key, message, shown, guide=None):
        ErrorDialog(self, key, message, shown, guide)

    def _icon(self):
        base = getattr(sys, '_MEIPASS', HERE)
        ico = os.path.join(base, 'eco_tool.ico')
        if os.path.exists(ico):
            try:
                self.root.iconbitmap(default=ico)
            except Exception:
                pass

    def place_window(self):
        if self._carry.get('geometry'):
            self.root.geometry(self._carry['geometry'])
            return
        w, h = 1220, 780
        self.root.update_idletasks()
        w = min(w, self.root.winfo_screenwidth() - 40)
        h = min(h, self.root.winfo_screenheight() - 80)
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 2 - 30)
        self.root.geometry(f'{w}x{h}+{x}+{y}')
        self.root.minsize(900, 560)

    # ---- start ----
    def _startup(self):
        if getattr(self, '_started', False):
            return
        self._started = True
        updater.cleanup_old()
        self.root.protocol('WM_DELETE_WINDOW', self._close)
        try:
            import dropfiles
            self._drop_ok = dropfiles.enable(self.root, self.on_drop) > 0
        except Exception:
            self._drop_ok = False
        if self.selftest:
            self._run_selftest()
            return
        self.reload(first=True)
        # a file dropped on the exe's icon arrives as an argument; it waits until the game is read
        args = [a for a in sys.argv[1:] if os.path.isfile(a)]
        if args:
            self.on_drop(args)
        if not self._carry.get('geometry'):
            if self.cfg.get('update_check', True):
                self.root.after(1500, self.check_updates)
            if not self.cfg.get('guide_seen'):
                self.root.after(900, self.guide.start)
            self.root.after(2500, self.fb.start)

    def _run_selftest(self):
        """Frozen build probe: decompiler data present, a tiny made-up script decompiles, https modules there."""
        note = []
        try:
            import decomp  # noqa: F401
            import lifter  # noqa: F401
            import emit_ec, typeinfer, entries  # noqa: F401  (the rebuild path)
            data = os.path.join(os.path.dirname(decomp.__file__), 'data', 'native_api.txt')
            note.append('decomp=' + ('ok' if os.path.isfile(data) else 'nodata'))
            import slot_table, native_sigs  # noqa: E401
            note.append('rebuild=' + ('ok' if typeinfer.tables().get('arg') and entries.load() and slot_table.load()
                                      and native_sigs.load() and native_sigs.load_tree()
                                      and native_sigs.load_lifecycle() else 'notables'))
            import v10, dbgcompare  # noqa: E401,F401  (the v1.0 debug builds)
            note.append('v10=' + ('ok' if v10.load('Cities')[0] and v10.load('MissionTeamHunt')[0] else 'nomap'))
        except Exception as e:
            note.append(f'decomp=failed:{type(e).__name__}')
        https = 'ok'
        try:
            import http.client  # noqa: F401
            import ssl  # noqa: F401
            import urllib.request  # noqa: F401
        except ImportError as e:
            https = f'missing:{e.name}'
        note.append(f'drop={self._drop_ok}')
        game = ecocore.find_game_dir(self.cfg.get('game_dir'))
        if game and os.environ.get('ECOTOOL_SELFTEST_V10'):
            # the rebuild of the game's v1.0 debug builds, run inside the built exe:
            # Cities=equivalent:2/2 CityCampaign=differs:287/290 MissionTeamHunt=differs:343/344
            try:
                sdk = next((s for s in ecocore.find_sdks(self.cfg.get('sdk_dirs', [])) if s.version == '1.3'), None)
                scripts, _p = ecocore.read_game(game, with_mods=False)
                for s in sorted(scripts.values(), key=lambda s: s.stem):
                    if s.debug and s.stem in ('Cities', 'CityCampaign', 'MissionTeamHunt'):
                        r = ecocore.reconstruct(s, sdk.tools if sdk else None)
                        note.append(f'{s.stem}={r["status"]}:' + '/'.join(map(str, r.get('routines') or ())))
            except Exception as e:
                note.append(f'v10run=failed:{type(e).__name__}:{e}')
        try:
            with open(self.selftest, 'w', encoding='utf-8') as f:
                f.write(f'version={VERSION} {" ".join(note)} game={"yes" if game else "no"} '
                        f'sdks={len(ecocore.find_sdks(self.cfg.get("sdk_dirs", [])))} chapters={len(guidebook.CHAPTERS)} '
                        f'tests={len(self.fb.tests)} https={https} frozen={getattr(sys, "frozen", False)}' + NL)
        finally:
            self.root.after(50, self.root.destroy)

    # ---- window ----
    def build(self):
        self.build_menubar()
        self.statusbar = ttk.Frame(self.root, style='Status.TFrame')
        self.statusbar.pack(fill='x', side='bottom')
        self.lbl_status = ttk.Label(self.statusbar, text=tr('ready'), style='Status.TLabel')
        self.lbl_status.pack(side='left')
        self.bar = ttk.Progressbar(self.statusbar, maximum=100, length=220)
        self.btn_cancel = ttk.Button(self.statusbar, text=tr('Cancel'), command=self.cancel)
        self.lbl_count = ttk.Label(self.statusbar, text='', style='Status.TLabel')
        self.lbl_count.pack(side='right')

        self.head = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        self.head.pack(fill='x')
        top = ttk.Frame(self.head)
        top.pack(fill='x')
        ttk.Label(top, text=tr('Two Worlds scripts as source code'), style='Brand.TLabel').pack(side='left')
        help_mark(top, tr('Which script has a source, export, SDK update. Chapter "Getting started".'), 'start', self)
        help_mark(top, tr('Brings an old SDK to the game\'s state, with preview and backup. Chapter "Update an SDK".'),
                  'update', self, side='right')
        self.btn_update = ttk.Button(top, text=tr('Update SDK...'), command=self.open_sdk_update)
        self.btn_update.pack(side='right')
        self.btn_export = ttk.Button(top, text=tr('Export...'), style='Accent.TButton', command=self.open_export)
        self.btn_export.pack(side='right', padx=(0, 10))
        self.btn_open = ttk.Button(top, text=tr('Open .eco / .wd...'), command=self.pick_files)
        self.btn_open.pack(side='right', padx=(0, 10))
        self.btn_reload = ttk.Button(top, text=tr('Read game'), command=self.reload)
        self.btn_reload.pack(side='right', padx=(0, 10))
        info = ttk.Frame(self.head)
        info.pack(fill='x', pady=(6, 0))
        self.lbl_game = ttk.Label(info, text='', style='Muted.TLabel', cursor='hand2')
        self.lbl_game.pack(side='left')
        self.lbl_game.bind('<Button-1>', lambda e: self.choose_game_dir())
        ttk.Label(info, text='  |  ', style='Muted.TLabel').pack(side='left')
        self.lbl_sdk = ttk.Label(info, text='', style='Muted.TLabel', cursor='hand2')
        self.lbl_sdk.pack(side='left')
        self.lbl_sdk.bind('<Button-1>', lambda e: self.add_sdk())
        help_mark(info, tr('Click the game or SDK text to choose another folder. Chapter "Game, mods and SDKs".'),
                  'read', self)
        help_mark(info, tr('Drop files on the window or on the exe. Chapter "Getting started".'), 'start', self,
                  side='right')
        ttk.Label(info, text=tr('Drop a .eco here to decompile it, a .ec to compile it.'),
                  style='Muted.TLabel').pack(side='right')

        self.paned = ttk.PanedWindow(self.root, orient='horizontal')
        self.paned.pack(fill='both', expand=True, padx=10, pady=(4, 8))
        left = ttk.Frame(self.paned, style='Panel.TFrame')
        right = ttk.Frame(self.paned)
        self.paned.add(left, weight=1)
        self.paned.add(right, weight=3)

        lh = ttk.Frame(left, style='Panel.TFrame')
        lh.pack(fill='x')
        ttk.Label(lh, text=tr('Scripts'), style='PanelTitle.TLabel').pack(side='left')
        help_mark(lh, tr('Green: source found. Orange: another version. Red: decompiler only. Chapter "Status".'),
                  'status', self, style='Panel.TLabel')
        self.btn_filter = ttk.Menubutton(lh, text=tr('Filter'))
        self.filter_menu = theme.Menu(self.btn_filter, postcommand=self._fill_filter)
        self.btn_filter['menu'] = self.filter_menu
        self.btn_filter.pack(side='right', padx=6, pady=4)
        sb = ttk.Frame(left, style='Panel.TFrame', padding=(6, 0, 6, 6))
        sb.pack(fill='x')
        self.q = tk.StringVar()
        self.q.trace_add('write', lambda *a: self.fill_tree())
        self.search = ttk.Entry(sb, textvariable=self.q)
        self.search.pack(fill='x')
        theme.Tooltip(self.search, tr('Search by name, e.g. Containers'))
        tf = ttk.Frame(left, style='Panel.TFrame')
        tf.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tf, columns=('status', 'source', 'layer'), show='tree headings', selectmode='extended')
        self.tree.heading('#0', text=tr('Script'))
        self.tree.heading('status', text=tr('Status'))
        self.tree.heading('source', text=tr('Source'))
        self.tree.heading('layer', text=tr('Archive'))
        self.tree.column('#0', width=190)
        self.tree.column('status', width=80, stretch=False)
        self.tree.column('source', width=120)
        self.tree.column('layer', width=100)
        for k, col in STATUS_COLOR.items():
            self.tree.tag_configure(k, foreground=col)
        ys = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.on_select())
        self.tree.bind('<Button-3>', self._tree_menu)
        self.empty = ttk.Label(tf, text='', style='Panel.TLabel', wraplength=260, justify='left')

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill='both', expand=True)
        src = ttk.Frame(self.nb)
        self.nb.add(src, text=tr('Source code'))
        self.src_head = ttk.Label(src, text=tr('Select a script on the left.'), style='Muted.TLabel',
                                  wraplength=820, justify='left', padding=(6, 6))
        self.src_head.pack(fill='x')
        self.src_head.bind('<Configure>', lambda e: self.src_head.configure(wraplength=max(200, e.width - 16)))
        srow = ttk.Frame(src)
        srow.pack(fill='both', expand=True)
        self.text = tk.Text(srow, wrap='none', font=theme.FONT_MONO, undo=False, padx=8, pady=6)
        for tag, col in theme.SYNTAX.items():
            self.text.tag_configure(tag, foreground=col)
        ys2 = ttk.Scrollbar(srow, orient='vertical', command=self.text.yview)
        xs2 = ttk.Scrollbar(src, orient='horizontal', command=self.text.xview)
        self.text.configure(yscrollcommand=ys2.set, xscrollcommand=xs2.set, state='disabled')
        self.text.pack(side='left', fill='both', expand=True)
        ys2.pack(side='right', fill='y')
        xs2.pack(fill='x')
        self.text.bind('<Button-3>', self._text_menu)
        inc = ttk.Frame(self.nb)
        self.nb.add(inc, text=tr('Include files'))
        ttk.Label(inc, text=tr('Every file the script needs to compile. Double-click shows it.'), style='Muted.TLabel',
                  padding=(6, 6)).pack(fill='x')
        self.inc = ttk.Treeview(inc, columns=('root',), show='tree headings')
        self.inc.heading('#0', text=tr('File'))
        self.inc.heading('root', text=tr('From'))
        self.inc.column('root', width=200, stretch=False)
        self.inc.pack(fill='both', expand=True)
        self.inc.bind('<Double-1>', lambda e: self.show_include())
        det = ttk.Frame(self.nb)
        self.nb.add(det, text=tr('Details'))
        self.details = tk.Text(det, wrap='word', font=theme.FONT_MONO, padx=10, pady=8, state='disabled')
        self.details.pack(fill='both', expand=True)

    def build_menubar(self):
        bar = ttk.Frame(self.root, style='Menubar.TFrame')
        bar.pack(fill='x')
        self.menubar = bar
        for key, filler in ((tr('File'), self._fill_file), (tr('Tools'), self._fill_tools), (tr('View'), self._fill_view),
                            (tr('Help'), self._fill_help)):
            item = ttk.Label(bar, text=key, style='Menubar.TLabel')
            item.pack(side='left')
            item.bind('<Button-1>', lambda ev, f=filler, w=item: self._popup(f, w))
            item.bind('<Enter>', lambda ev, w=item: w.state(['active']))
            item.bind('<Leave>', lambda ev, w=item: w.state(['!active']))
        ttk.Label(bar, text=APP_NAME, style='Menubar.TLabel').pack(side='right', padx=(0, 6))
        box = ttk.Frame(bar, style='Menubar.TFrame')
        for i, code in enumerate(('de', 'en')):
            if i:
                ttk.Label(box, text='·', style='Menubar.TLabel', padding=(2, 5)).pack(side='left')
            lbl = ttk.Label(box, text=code.upper(), style='Menubar.TLabel', padding=(4, 5), cursor='hand2',
                            foreground=theme.GOLD if code == _LANG else theme.MUT)
            lbl.pack(side='left')
            lbl.bind('<Button-1>', lambda ev, c=code: self.set_lang(c))
        box.pack(side='right', padx=(0, 10))

    def _popup(self, filler, widget):
        menu = theme.Menu(self.root)
        filler(menu)
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            menu.grab_release()

    def _fill_file(self, m):
        st = 'disabled' if self.busy else 'normal'
        m.add_command(label=tr('Read game'), accelerator='F5', command=self.reload, state=st)
        m.add_command(label=tr('Open .eco / .wd...'), accelerator='Ctrl+O', command=self.pick_files, state=st)
        m.add_command(label=tr('Export...'), accelerator='Ctrl+E', command=self.open_export, state=st)
        m.add_separator()
        m.add_command(label=tr('Save shown text as...'), accelerator='Ctrl+S', command=self.save_text)
        m.add_command(label=tr('Open export folder'), command=self.open_export_dir)
        m.add_separator()
        m.add_command(label=tr('Settings...'), command=self.open_settings)
        m.add_separator()
        m.add_command(label=tr('Exit'), accelerator='Alt+F4', command=self._close)

    def _fill_tools(self, m):
        st = 'disabled' if self.busy else 'normal'
        m.add_command(label=tr('Update SDK...'), command=self.open_sdk_update, state=st)
        m.add_command(label=tr('Undo an SDK update...'), command=self.open_restore, state=st)
        m.add_separator()
        m.add_command(label=tr('Add SDK folder...'), command=self.add_sdk, state=st)
        m.add_command(label=tr('Rebuild SDK index'), command=self.rebuild_index, state=st)
        m.add_command(label=tr('Open backup folder'), command=self.open_backups)

    def _fill_view(self, m):
        sub = theme.Menu(m)
        lang = tk.StringVar(value=_LANG)
        for code, name in (('de', 'Deutsch'), ('en', 'English')):
            sub.add_radiobutton(label=name, value=code, variable=lang, command=lambda c=code: self.set_lang(c))
        m.add_cascade(label=tr('Language'), menu=sub)

    def _fill_help(self, m):
        m.add_command(label=tr('Guide'), accelerator='F1', command=self.show_guide)
        m.add_command(label=tr('Start tour'), command=self.guide.start)
        m.add_command(label=tr('Documentation'), command=lambda: webbrowser.open(GUIDE_URL))
        m.add_separator()
        self.fb.add_menu_items(m)
        m.add_separator()
        for name, url in LINKS:
            m.add_command(label=f'{name}  ({url})', command=lambda u=url: webbrowser.open(u))
        m.add_separator()
        m.add_command(label=tr('Check for updates'), command=lambda: self.check_updates(manual=True))
        m.add_checkbutton(label=tr('Check for updates on start'), variable=self.update_var,
                          command=self._toggle_update_check)
        m.add_command(label=tr('Latest version on GitHub'), command=lambda: webbrowser.open(updater.LATEST_PAGE))
        m.add_separator()
        m.add_command(label=tr('About'), command=self.show_about)

    def _fill_filter(self):
        m = self.filter_menu
        m.delete(0, 'end')
        for k in STATUS_INFO:
            m.add_checkbutton(label=tr(STATUS_INFO[k][0]), variable=self.filter_vars[k], command=self.fill_tree)
        m.add_separator()
        m.add_checkbutton(label=tr('Only scripts from mods / opened files'), variable=self.filter_mods,
                          command=self.fill_tree)

    def _bind_keys(self):
        r = self.root

        def key(fn):
            def handler(ev):
                if isinstance(r.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox)):
                    return None
                fn()
                return 'break'
            return handler
        r.bind('<F5>', lambda e: self.reload())
        r.bind('<Control-o>', key(self.pick_files))
        r.bind('<Control-e>', key(self.open_export))
        r.bind('<Control-s>', key(self.save_text))
        r.bind('<Control-f>', lambda e: (self.search.focus_set(), 'break')[1])
        r.bind('<F1>', lambda e: self.show_guide())
        r.bind('<Escape>', lambda e: self.cancel() if self.busy else None)

    def status(self, text, error=False, ok=False):
        self.lbl_status.configure(text=text, style='StatusErr.TLabel' if error else 'StatusOk.TLabel' if ok else 'Status.TLabel')

    def show_guide(self, chapter='start'):
        guidebook.GuideWindow.show(self, chapter)

    def show_help(self, text, chapter):
        self.status(text.split(NL)[0][:160])
        self.show_guide(chapter)

    # ---- background work ----
    def run_task(self, label, work, done):
        """Run ``work(progress, cancelled)`` in a thread; ``done(result, error)`` back on the Tk thread."""
        if self.busy:
            self.status(tr('Wait until the work is done.'), error=True)
            return
        self.busy = True
        self._cancel = False
        self.status(label)
        self.bar.configure(value=0)
        self.lbl_count.pack_forget()
        self.btn_cancel.pack(side='right', padx=4)
        self.bar.pack(side='right', padx=8)
        self.lbl_count.pack(side='right')
        for b in (self.btn_reload, self.btn_open, self.btn_export, self.btn_update):
            b.state(['disabled'])
        q = self._q

        def progress(text, done_n, total):
            q.put(('p', text, done_n, total))

        def run():
            try:
                q.put(('done', work(progress, lambda: self._cancel), None))
            except ecocore.Cancelled:
                q.put(('done', None, 'cancelled'))
            except Exception as e:
                q.put(('done', None, e))
        threading.Thread(target=run, daemon=True).start()

        def poll():
            last = end = None
            while True:
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    break
                if msg[0] == 'p':
                    last = msg
                else:
                    end = msg
            if last:
                _p, text, n, total = last
                self.bar.configure(value=100 * n / max(1, total))
                self.status(f'{label}  {text}'[:150])
            if end is None:
                self.root.after(100, poll)
                return
            self.busy = False
            self.bar.pack_forget()
            self.btn_cancel.pack_forget()
            for b in (self.btn_reload, self.btn_open, self.btn_export, self.btn_update):
                b.state(['!disabled'])
            if end[2] == 'cancelled':
                self.status(tr('Cancelled.'))
                return
            done(end[1], end[2])
        self.root.after(100, poll)

    def cancel(self):
        if self.busy:
            self._cancel = True
            self.status(tr('Stopping ...'))

    # ---- reading ----
    def reload(self, first=False):
        self.game_dir = ecocore.find_game_dir(self.cfg.get('game_dir'))
        self.sdks = ecocore.find_sdks(self.cfg.get('sdk_dirs', []))
        self._paint_head()
        game_dir, sdks, extra = self.game_dir, self.sdks, list(self.extra_files)
        with_mods = bool(self.cfg.get('with_mods', True))

        def work(progress, cancelled):
            progress(tr('reading the game'), 0, 1)
            scripts, problems = ecocore.read_game(game_dir, with_mods=with_mods, extra_files=extra)
            index = ecocore.build_index(sdks, cache_dir(), progress, cancelled) if sdks else None
            if index:
                ecocore.classify(scripts, index)
                # scripts without a source: decompile, compile, compare (cached per body)
                tools = ecocore.tree_tools(scripts.values(), index)
                todo = [s for s in scripts.values() if not s.match]
                for i, s in enumerate(todo):
                    if cancelled():
                        raise ecocore.Cancelled()
                    progress(tr('rebuilding {name}').format(name=s.name), i, len(todo))
                    try:
                        s.rebuild = ecocore.reconstruct(s, tools, cache_dir())
                    except Exception as e:
                        s.rebuild = {'status': 'failed', 'text': '', 'msg': f'{type(e).__name__}: {e}', 'size': None}
            return scripts, problems, index

        def done(res, err):
            if err:
                self.error('read.failed', 'Reading the game or the SDK failed',
                           tr('Reading failed:') + NL + f'{type(err).__name__}: {err}', 'trouble')
                return
            self.scripts, self.problems, self.index = res
            self._decomp_cache = {}
            self.fill_tree()
            n = len(self.scripts)
            src = sum(1 for s in self.scripts.values() if s.match)
            reb = sum(1 for s in self.scripts.values() if ecocore.status_of(s) in ('rebuilt', 'v10'))
            self.status(tr('{n} scripts read, {src} with a matching source, {reb} rebuilt by the decompiler.').format(
                n=n, src=src, reb=reb), ok=bool(n))
            if self.problems:
                self.status(tr('{n} scripts read; {p} archive(s) could not be read (see Details).').format(
                    n=n, p=len(self.problems)), error=True)
            self.fb.log.add(f'read {n} scripts, {src} source')
        self.run_task(tr('Reading ...') if not first else tr('Reading the game and compiling the SDK sources once ...'),
                      work, done)

    def _paint_head(self):
        if self.game_dir:
            self.lbl_game.configure(text=tr('Game: {path}').format(path=self.game_dir), foreground=theme.MUT)
        else:
            self.lbl_game.configure(text=tr('Game: not found - click to choose the Two Worlds folder'), foreground=theme.ERR)
        if self.sdks:
            text = ', '.join(f'{s.version} ({s.root})' for s in self.sdks)
            self.lbl_sdk.configure(text=tr('SDK: {list}').format(list=text), foreground=theme.MUT)
        else:
            self.lbl_sdk.configure(text=tr('SDK: none found - click to choose your Two Worlds SDK folder'),
                                   foreground=theme.ERR)

    def choose_game_dir(self):
        d = filedialog.askdirectory(parent=self.root, title=tr('Choose the Two Worlds folder (with WDFiles)'))
        if not d:
            return
        d = os.path.normpath(d)
        if not os.path.isdir(os.path.join(d, 'WDFiles')):
            self.status(tr('That folder has no WDFiles subfolder.'), error=True)
            return
        self.cfg['game_dir'] = d
        self.cfg.save()
        self.reload()

    def add_sdk(self):
        d = filedialog.askdirectory(parent=self.root, title=tr('Choose a Two Worlds SDK folder (with Scripts and Tools)'))
        if not d:
            return
        d = os.path.normpath(d)
        if not ecocore.Sdk.looks_like(d):
            self.status(tr('No SDK there: the folder needs Scripts and Tools\\EarthC.exe.'), error=True)
            return
        dirs = self.cfg.get('sdk_dirs', [])
        if d not in dirs:
            dirs.append(d)
        self.cfg['sdk_dirs'] = dirs
        self.cfg.save()
        self.reload()

    def rebuild_index(self):
        import shutil
        shutil.rmtree(cache_dir(), ignore_errors=True)
        self.reload()

    def pick_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title=tr('Open compiled scripts or mod archives'),
                                            initialdir=self.cfg.get('last_dir') or None,
                                            filetypes=[(tr('Scripts and archives'), '*.eco *.wd'), (tr('All files'), '*.*')])
        if not paths:
            return
        self.cfg['last_dir'] = os.path.dirname(paths[0])
        self.cfg.save()
        for p in paths:
            if p not in self.extra_files:
                self.extra_files.append(os.path.normpath(p))
        self.filter_mods.set(True)
        self.reload()

    # ---- list ----
    def visible(self):
        q = self.q.get().strip().lower()
        out = []
        for s in self.scripts.values():
            st = ecocore.status_of(s)
            if not self.filter_vars[st].get():
                continue
            if self.filter_mods.get() and not s.from_mod:
                continue
            if q and q not in s.name.lower() and q not in s.key:
                continue
            out.append(s)
        return sorted(out, key=lambda s: (not s.from_mod if self.filter_mods.get() else 0, s.name.lower()))

    def fill_tree(self):
        sel = self.current.key if self.current else None
        self.tree.delete(*self.tree.get_children())
        shown = self.visible()
        for s in shown:
            st = ecocore.status_of(s)
            src = ''
            if s.match:
                src = self.index['roots'][s.match['root']]['label']
            elif s.related:
                src = tr('other: ') + self.index['roots'][s.related[0]['root']]['label']
            self.tree.insert('', 'end', iid=s.key, text=s.name, values=(tr(STATUS_INFO[st][0]), src, s.layer), tags=(st,))
        self.lbl_count.configure(text=tr('{shown} of {n} scripts').format(shown=len(shown), n=len(self.scripts)))
        if not self.scripts:
            if not self.game_dir:
                self.empty.configure(text=tr('No game found. Click the red game line above to choose the Two Worlds folder, '
                                             'or open single .eco / .wd files with "Open".'))
            else:
                self.empty.configure(text=tr('Nothing read yet. Click "Read game".'))
            self.empty.place(relx=0.05, rely=0.1)
        elif not shown:
            self.empty.configure(text=tr('No script matches the search or the filter.'))
            self.empty.place(relx=0.05, rely=0.1)
        else:
            self.empty.place_forget()
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
            self.tree.see(sel)

    def selected(self):
        return [self.scripts[k] for k in self.tree.selection() if k in self.scripts]

    def on_select(self):
        sel = self.selected()
        if not sel:
            return
        s = sel[0]
        self.current = s
        st = ecocore.status_of(s)
        self.inc.delete(*self.inc.get_children())
        if s.match:
            root = self.index['roots'][s.match['root']]
            self.src_head.configure(text=tr('{name}: source {rel} from {root}. Compiles to exactly the game\'s bytes '
                                            '(checked when indexing; Export checks again).').format(
                name=s.name, rel=s.match['rel'], root=root['label']), foreground=theme.OK)
            highlight(self.text, ecocore.read_source(self.index, s.match['root'], s.match['rel']).decode('latin-1'))
            for rel in ecocore.closure(s.match):
                self.inc.insert('', 'end', text=rel, values=(root['label'],))
        elif st in ('rebuilt', 'v10') or (s.rebuild and s.rebuild.get('status') in ('differs', 'compile')
                                          and s.rebuild.get('text')):
            r = s.rebuild
            if st == 'v10':
                self.src_head.configure(text=tr('{name}: v1.0 debug build, no SDK source. Rebuilt by the decompiler; '
                                                'compiled with SDK 1.3 it is the same program as the game\'s ({msg}). '
                                                'Export writes it to Decompiled.').format(
                    name=s.name, msg=r.get('msg', '')[:160]), foreground=theme.STATUS_REBUILT)
            elif st == 'rebuilt':
                self.src_head.configure(text=tr('{name}: no SDK source. Rebuilt by the decompiler from the compiled script; '
                                                'compiles to exactly the game\'s bytes (checked). Export writes it to '
                                                'Scripts\\_TW1_Rebuilt.').format(name=s.name),
                                        foreground=theme.STATUS_REBUILT)
            else:
                self.src_head.configure(text=tr('{name}: rebuilt source, NOT exact yet: {msg}').format(
                    name=s.name, msg=r.get('msg', '')[:160]), foreground=STATUS_COLOR[st])
            highlight(self.text, r['text'])
        else:
            if st == 'differs':
                r = s.related[0]
                why = tr('The source {rel} ({root}) compiles to other bytes - the game or a mod uses another version. ').format(
                    rel=r['rel'], root=self.index['roots'][r['root']]['label'])
            else:
                why = tr('No SDK has a source for this script. ')
            self.src_head.configure(text=s.name + ': ' + why + tr('Shown: readable decompiler output, not checked to compile.'),
                                    foreground=STATUS_COLOR[st])
            self._show_decompiled(s)
        self._fill_details(s)

    def _show_decompiled(self, s):
        if s.sha in self._decomp_cache:
            highlight(self.text, self._decomp_cache[s.sha])
            return
        highlight(self.text, tr('// decompiling ...'))
        body = s.body

        def run():
            try:
                text = ecocore.decompiled_text(body, s.name)
            except Exception as e:
                text = f'// {tr("The decompiler failed")}: {type(e).__name__}: {e}'
            self.root.after(0, lambda: self._decompiled(s, text))
        threading.Thread(target=run, daemon=True).start()

    def _decompiled(self, s, text):
        self._decomp_cache[s.sha] = text
        if self.current is s:
            highlight(self.text, text)

    def _fill_details(self, s):
        st = ecocore.status_of(s)
        lines = [f'{s.name}', '',
                 f'{tr("Inner path")}:   {s.key}',
                 f'{tr("Archive")}:      {s.layer}' + (f'   ({tr("mod or opened file")})' if s.from_mod else ''),
                 f'{tr("Status")}:       {tr(STATUS_INFO[st][0])} - {tr(STATUS_INFO[st][1])}',
                 f'{tr("Body")}:         {len(s.body)} {tr("bytes")}, sha256 {s.sha[:16]}',
                 f'{tr("Build")}:        ' + (tr('debug build (carries names and line numbers)') if s.debug else tr('release build')),
                 '']
        if s.older:
            lines.append(tr('Older copies the game hides (archive, sha256):'))
            for label, h in s.older:
                lines.append(f'   {label:<22} {h[:16]}' + ('   = ' + tr('same') if h == s.sha else ''))
            lines.append('')
        if s.rebuild:
            lines.append(tr('Decompiler rebuild: {status}{msg}').format(
                status=s.rebuild.get('status'), msg=(' - ' + s.rebuild['msg']) if s.rebuild.get('msg') else ''))
        if s.match:
            lines.append(tr('Matching source: {rel} in {path}').format(rel=s.match['rel'],
                                                                       path=self.index['roots'][s.match['root']]['path']))
        for r in s.related:
            lines.append(tr('Other version: {rel} in {path} ({size} bytes)').format(
                rel=r['rel'], path=self.index['roots'][r['root']]['path'], size=r['size']))
        if self.problems:
            lines += ['', tr('Archives that could not be read:')] + ['   ' + p for p in self.problems]
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', NL.join(lines))
        self.details.configure(state='disabled')

    def show_include(self):
        it = self.inc.focus()
        if not it or not self.current or not self.current.match:
            return
        rel = self.inc.item(it, 'text')
        highlight(self.text, ecocore.read_source(self.index, self.current.match['root'], rel).decode('latin-1'))
        self.src_head.configure(text=tr('Include file {rel}').format(rel=rel), foreground=theme.MUT)
        self.nb.select(0)

    def _tree_menu(self, ev):
        it = self.tree.identify_row(ev.y)
        if it and it not in self.tree.selection():
            self.tree.selection_set(it)
        m = theme.Menu(self.root)
        m.add_command(label=tr('Export selected...'), command=lambda: self.open_export(only_selected=True),
                      state='normal' if self.tree.selection() else 'disabled')
        m.add_command(label=tr('Save shown text as...'), command=self.save_text)
        m.add_command(label=tr('Copy name'), command=lambda: self._copy(self.current.name if self.current else ''))
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _text_menu(self, ev):
        m = theme.Menu(self.root)
        m.add_command(label=tr('Copy'), command=lambda: self._copy(self._selected_text()))
        m.add_command(label=tr('Copy all'), command=lambda: self._copy(self.text.get('1.0', 'end-1c')))
        m.add_command(label=tr('Save shown text as...'), command=self.save_text)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _selected_text(self):
        try:
            return self.text.get('sel.first', 'sel.last')
        except tk.TclError:
            return ''

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status(tr('Copied.'))

    def save_text(self):
        if not self.current:
            return
        name = self.current.stem + '.ec'
        p = filedialog.asksaveasfilename(parent=self.root, initialfile=name, defaultextension='.ec',
                                         filetypes=[(tr('EarthC source'), '*.ec *.ech'), (tr('All files'), '*.*')])
        if p:
            with open(p, 'w', encoding='latin-1', errors='replace', newline='') as f:
                f.write(self.text.get('1.0', 'end-1c'))
            self.status(tr('Saved: {path}').format(path=p), ok=True)

    # ---- export ----
    def open_export(self, only_selected=False):
        if self.busy:
            return
        if not self.scripts:
            self.status(tr('Read the game first.'), error=True)
            return
        if not self.index:
            self.error('export.nosdk', 'Export needs an SDK', tr('Export needs a Two Worlds SDK (Scripts and Tools). '
                                                                 'Choose it with Tools > Add SDK folder.'), 'read')
            return
        ExportWindow(self, only_selected)

    def open_export_dir(self):
        d = self.cfg.get('export_dir') or default_export_dir()
        if os.path.isdir(d):
            os.startfile(d)
        else:
            self.status(tr('Nothing exported yet.'))

    # ---- SDK update ----
    def open_sdk_update(self):
        if self.busy:
            return
        if not self.index or not self.scripts:
            self.status(tr('Read the game and an SDK first.'), error=True)
            return
        SdkUpdateWindow(self)

    def open_restore(self):
        RestoreWindow(self)

    def open_backups(self):
        os.makedirs(backup_dir(), exist_ok=True)
        os.startfile(backup_dir())

    def open_settings(self):
        SettingsWindow(self)

    # ---- language / close ----
    def _close(self):
        if self.busy and not messagebox.askyesno(APP_NAME, tr('Still working. Stop and close?'), parent=self.root):
            return
        self._cancel = True
        self.root.destroy()

    def set_lang(self, code):
        global _LANG
        if code == _LANG:
            return
        if self.busy:
            self.status(tr('Wait until the work is done.'), error=True)
            return
        self.cfg['lang'] = code
        self.cfg.save()
        _LANG = code
        self.restart = True
        self.carry_out = {'geometry': self.root.geometry()}
        self.root.destroy()

    # ---- updates / about ----
    def _toggle_update_check(self):
        self.cfg['update_check'] = bool(self.update_var.get())
        self.cfg.save()

    def check_updates(self, manual=False):
        results = []
        updater.check_async(lambda info, err: results.append((info, err)))

        def poll():
            try:
                if not self.root.winfo_exists():
                    return
            except tk.TclError:
                return
            if not results:
                self.root.after(200, poll)
                return
            info, err = results[0]
            if err is not None or info is None:
                if manual:
                    messagebox.showwarning(tr('Update'), tr('GitHub was not reachable: {err}').format(err=err), parent=self.root)
                return
            if not updater.is_newer(info['tag']):
                if manual:
                    messagebox.showinfo(tr('Update'), tr('You have the latest version ({version}).').format(version=VERSION),
                                        parent=self.root)
                return
            if not manual and self.cfg.get('update_skip') == info['tag']:
                return
            UpdateWindow(self, info)
        self.root.after(200, poll)

    def show_about(self):
        win = tk.Toplevel(self.root)
        win.title(tr('About'))
        win.configure(background=theme.BG)
        win.transient(self.root)
        theme.dark_titlebar(win)
        win.bind('<Escape>', lambda e: win.destroy())
        f = ttk.Frame(win, padding=16)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=f'TW1 EcoTool {VERSION}', style='Brand.TLabel').pack(anchor='w')
        ttk.Label(f, text=tr('The compiled scripts of Two Worlds 1 as compilable source code.\n'
                             'Sources come from your own Two Worlds SDK; the tool contains none.\n'
                             'Decompiler: EcoAnalysis 2026. Disassembly: Capstone (BSD).'),
                  style='Muted.TLabel', justify='left').pack(anchor='w', pady=(6, 10))
        for name, url in LINKS:
            lnk = ttk.Label(f, text=f'{name}: {url}', style='Link.TLabel', cursor='hand2')
            lnk.pack(anchor='w', padx=(12, 0))
            lnk.bind('<Button-1>', lambda e, u=url: webbrowser.open(u))
        ttk.Button(f, text=tr('Close'), command=win.destroy).pack(anchor='e', pady=(12, 0))

    # ---- drag & drop ----
    def on_drop(self, paths):
        """A dropped .eco is decompiled next to it, a dropped .ec compiled next to it (release build)."""
        todo = [os.path.normpath(p) for p in paths
                if os.path.isfile(p) and os.path.splitext(p)[1].lower() in ('.eco', '.ec')]
        if not todo:
            self.status(tr('Only .eco files (decompile) and .ec files (compile) can be dropped here.'), error=True)
            return
        self._drops.extend(todo)
        try:
            self.root.lift()
        except tk.TclError:
            pass
        self._next_drop()

    def _next_drop(self):
        if not self._drops:
            return
        if self.busy:                      # reading the game first, or an export: after that
            self.root.after(500, self._next_drop)
            return
        paths, self._drops = self._drops, []
        index = self.index
        tools = ecocore.drop_tools(ecocore.find_sdks(self.cfg.get('sdk_dirs', [])))

        def work(progress, cancelled):
            out = []
            for i, p in enumerate(paths):
                if cancelled():
                    raise ecocore.Cancelled()
                progress(os.path.basename(p), i, len(paths))
                try:
                    if p.lower().endswith('.eco'):
                        res, kind, msg = ecocore.decompile_dropped(p, tools, index)
                    else:
                        res, msg, backup = ecocore.compile_dropped(p, tools)
                        kind = 'compiled' if res else 'failed'
                        if backup:
                            msg += ', ' + tr('old file kept as {name}').format(name=os.path.basename(backup))
                except Exception as e:
                    res, kind, msg = None, 'failed', f'{type(e).__name__}: {e}'
                out.append((p, res, kind, msg))
            return out

        def done(res, err):
            if err:
                if err != 'cancelled':
                    self.status(tr('Working on the dropped files failed:') + f' {type(err).__name__}: {err}', error=True)
                return
            words = {'source': tr('SDK source, compiles to exactly these bytes'),
                     'identical': tr('rebuilt, compiles to exactly these bytes'),
                     'equivalent': tr('rebuilt, with SDK 1.3 the same program (v1.0 debug build)'),
                     'differs': tr('rebuilt, compiles, but not exactly to these bytes'),
                     'readable': tr('readable decompiler output, not compilable'),
                     'compiled': tr('compiled (release build)'), 'failed': tr('failed')}
            lines = [f'{os.path.basename(p)} -> {os.path.basename(r) if r else "-"}: {words.get(k, k)}'
                     + (f' ({m})' if m and k in ('failed', 'compiled', 'differs') else '') for p, r, k, m in res]
            bad = [x for x in res if x[2] == 'failed']
            self.status((lines[0] if len(lines) == 1 else tr('{n} dropped files done, {bad} failed.').format(
                n=len(lines), bad=len(bad))), error=bool(bad), ok=not bad)
            self.fb.log.add('drop: ' + '; '.join(lines))
            shown = next((x for x in reversed(res) if x[1] and x[1].lower().endswith('.ec')), None)
            if shown:
                with open(shown[1], encoding='latin-1') as f:
                    highlight(self.text, f.read())
                self.src_head.configure(text=tr('Written: {path}').format(path=shown[1]),
                                        foreground=theme.OK if shown[2] != 'readable' else theme.STATUS_NONE)
            if len(lines) > 1 or bad:
                messagebox.showinfo(tr('Dropped files'), NL.join(lines), parent=self.root)
        self.run_task(tr('Working on the dropped files ...'), work, done)

    def run(self):
        self._bind_keys()
        self.root.mainloop()


# --------------------------------------------------------------- dialogs --

def _toplevel(app, title, size=None):
    win = tk.Toplevel(app.root)
    win.title(title)
    win.configure(background=theme.BG)
    win.transient(app.root)
    theme.dark_titlebar(win)
    win.bind('<Escape>', lambda e: win.destroy())
    if size:
        win.geometry(size)
    return win


class ExportWindow:
    """Choose the folder, export, see the check result."""

    def __init__(self, app, only_selected=False):
        self.app = app
        self.win = win = _toplevel(app, tr('Export'), '640x420')
        f = ttk.Frame(win, padding=16)
        f.pack(fill='both', expand=True)
        sel = app.selected() if only_selected else []
        self.items = sel or list(app.scripts.values())
        n_src = sum(1 for s in self.items if s.match)
        ttk.Label(f, text=tr('Export {n} scripts').format(n=len(self.items)), style='Brand.TLabel').pack(anchor='w')
        ttk.Label(f, text=tr('{src} with a source go to Scripts\\ (SDK layout, with include files and compile_all.bat). '
                             '{rest} without a source go to Decompiled\\ as readable text.').format(
            src=n_src, rest=len(self.items) - n_src), style='Muted.TLabel', wraplength=600, justify='left').pack(anchor='w', pady=(4, 10))
        row = ttk.Frame(f)
        row.pack(fill='x')
        ttk.Label(row, text=tr('Folder:')).pack(side='left')
        self.dir = tk.StringVar(value=os.path.join(app.cfg.get('export_dir') or default_export_dir(),
                                                   time.strftime('Export_%Y-%m-%d_%H-%M')))
        ttk.Entry(row, textvariable=self.dir).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(row, text=tr('Choose...'), command=self.choose).pack(side='left')
        self.verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text=tr('Compile every exported script again and compare it with the game (recommended)'),
                        variable=self.verify).pack(anchor='w', pady=(10, 0))
        self.result = tk.Text(f, height=8, wrap='word', font=theme.FONT_MONO, state='disabled')
        self.result.pack(fill='both', expand=True, pady=(10, 0))
        btns = ttk.Frame(f)
        btns.pack(fill='x', pady=(10, 0))
        self.go = ttk.Button(btns, text=tr('Export'), style='Accent.TButton', command=self.start)
        self.go.pack(side='right')
        ttk.Button(btns, text=tr('Close'), command=win.destroy).pack(side='right', padx=6)
        self.open_btn = ttk.Button(btns, text=tr('Open folder'), command=self.open_folder)
        win.bind('<Return>', lambda e: self.start())

    def choose(self):
        d = filedialog.askdirectory(parent=self.win, title=tr('Choose the export folder'))
        if d:
            self.dir.set(os.path.join(os.path.normpath(d), time.strftime('Export_%Y-%m-%d_%H-%M')))

    def _say(self, text, colour=None):
        self.result.configure(state='normal')
        self.result.delete('1.0', 'end')
        self.result.insert('1.0', text)
        self.result.configure(state='disabled', foreground=colour or theme.INK)

    def start(self):
        out = self.dir.get().strip()
        if not out:
            return
        if os.path.isdir(out) and os.listdir(out) and not messagebox.askyesno(
                APP_NAME, tr('{path} is not empty. Write into it anyway? Files with the same name are replaced.').format(path=out),
                parent=self.win):
            return
        self.app.cfg['export_dir'] = os.path.dirname(out)
        self.app.cfg.save()
        self.go.state(['disabled'])
        self._say(tr('Exporting ...'))
        items, index, verify = self.items, self.app.index, self.verify.get()

        def work(progress, cancelled):
            return ecocore.export(items, index, out, progress, cancelled, verify)

        def done(rep, err):
            try:
                if not self.win.winfo_exists():
                    return
            except tk.TclError:
                return
            self.go.state(['!disabled'])
            if err:
                self._say(tr('Export failed: {e}').format(e=err), theme.ERR)
                return
            ok = sum(1 for r in rep['source'] if r['verified'])
            bad = [r for r in rep['source'] + rep.get('rebuilt', []) if verify and not r['verified']]
            lines = [tr('Written to {path}').format(path=out), '',
                     tr('{n} scripts as source (Scripts\\, compile_all.bat).').format(n=len(rep['source']))]
            if verify:
                lines.append(tr('Checked: {ok} of {n} compile to exactly the game\'s bytes.').format(ok=ok, n=len(rep['source'])))
            if rep.get('rebuilt'):
                okr = sum(1 for r in rep['rebuilt'] if r['verified'])
                lines.append(tr('{n} scripts rebuilt by the decompiler (Scripts\\_TW1_Rebuilt\\); {ok} of them checked to '
                                'compile to exactly the game\'s bytes.').format(n=len(rep['rebuilt']), ok=okr))
            lines.append(tr('{n} scripts as decompiler text (Decompiled\\).').format(n=len(rep['decompiled'])))
            for r in bad:
                lines.append(f'  {tr("NOT OK")}: {r["name"]}: {r["error"][:200]}')
            for r in rep['failed']:
                lines.append(f'  {tr("failed")}: {r["name"]}: {r["error"]}')
            self._say(NL.join(lines), theme.OK if not bad and not rep['failed'] else theme.ERR)
            self.open_btn.pack(side='left')
            self.out = out
            self.app.status(tr('Export done: {path}').format(path=out), ok=not bad)
            self.app.fb.log.add(f'export {len(rep["source"])} source, {ok} verified, {len(rep["decompiled"])} decompiled')
        self.app.run_task(tr('Exporting ...'), work, done)

    def open_folder(self):
        if getattr(self, 'out', None) and os.path.isdir(self.out):
            os.startfile(self.out)


class SdkUpdateWindow:
    """Assistant: 1 choose the SDK, 2 see every file and the check, 3 back up and write."""

    def __init__(self, app):
        self.app = app
        self.plan = None
        self.win = win = _toplevel(app, tr('Update SDK'), '860x600')
        f = ttk.Frame(win, padding=16)
        f.pack(fill='both', expand=True)
        self.step = ttk.Label(f, style='Muted.TLabel')
        self.step.pack(anchor='w')
        ttk.Label(f, text=tr('Bring an SDK to the state of the game'), style='Brand.TLabel').pack(anchor='w', pady=(2, 6))
        ttk.Label(f, text=tr('The SDK\'s Scripts folder gets the sources that compile to exactly the game\'s scripts, '
                             'laid out like SDK 1.3 (older versions in Scripts\\_Scripts_old_1.5_). If the SDK\'s compiler '
                             'is too old for them, Tools\\EarthC.exe is replaced as well. Nothing is written before you '
                             'saw every file; every replaced file is backed up first.'),
                  style='Muted.TLabel', wraplength=820, justify='left').pack(anchor='w')
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(10, 0))
        ttk.Label(row, text=tr('SDK to update:')).pack(side='left')
        self.choice = tk.StringVar()
        names = [s.root for s in app.sdks]
        self.combo = ttk.Combobox(row, textvariable=self.choice, values=names, state='readonly', width=70)
        self.combo.pack(side='left', padx=6)
        old = [s.root for s in app.sdks if s.version != '1.3']
        self.choice.set(old[0] if old else (names[0] if names else ''))
        self.btn_plan = ttk.Button(row, text=tr('Show what changes'), style='Accent.TButton', command=self.make_plan)
        self.btn_plan.pack(side='left')
        self.summary = ttk.Label(f, text='', wraplength=820, justify='left')
        self.summary.pack(anchor='w', pady=(10, 4))
        tf = ttk.Frame(f)
        tf.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tf, columns=('action', 'from'), show='tree headings')
        self.tree.heading('#0', text=tr('File in the SDK'))
        self.tree.heading('action', text=tr('Action'))
        self.tree.heading('from', text=tr('Taken from'))
        self.tree.column('action', width=110, stretch=False)
        self.tree.column('from', width=220, stretch=False)
        self.tree.tag_configure('replace', foreground=theme.STATUS_DIFFERS)
        self.tree.tag_configure('add', foreground=theme.OK)
        self.tree.tag_configure('bad', foreground=theme.ERR)
        ys = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')
        btns = ttk.Frame(f)
        btns.pack(fill='x', pady=(10, 0))
        self.btn_apply = ttk.Button(btns, text=tr('Back up and write'), style='Accent.TButton', command=self.apply)
        self.btn_apply.pack(side='right')
        self.btn_apply.state(['disabled'])
        ttk.Button(btns, text=tr('Close'), command=win.destroy).pack(side='right', padx=6)
        self._step(1)

    def _step(self, n):
        self.step.configure(text=tr('Step {n} of 3').format(n=n))

    def target(self):
        for s in self.app.sdks:
            if s.root == self.choice.get():
                return s
        return None

    def make_plan(self):
        target = self.target()
        if not target:
            return
        self.btn_plan.state(['disabled'])
        self.btn_apply.state(['disabled'])
        self.summary.configure(text=tr('Building the future Scripts folder in a temp copy and compiling every script there ...'),
                               foreground=theme.MUT)
        game_dir, index = self.app.game_dir, self.app.index

        def work(progress, cancelled):
            # The SDK gets the game's own scripts, never those an active mod replaces.
            scripts, _problems = ecocore.read_game(game_dir, with_mods=False)
            ecocore.classify(scripts, index)
            return scripts, ecocore.plan_sdk_update(target, scripts.values(), index, progress, cancelled)

        def done(result, err):
            try:
                if not self.win.winfo_exists():
                    return
            except tk.TclError:
                return
            self.btn_plan.state(['!disabled'])
            if err:
                self.summary.configure(text=tr('Planning failed: {e}').format(e=err), foreground=theme.ERR)
                return
            self.scripts, plan = result
            self.plan = plan
            self._step(2)
            self.tree.delete(*self.tree.get_children())
            comp = plan['compiler']
            if comp:
                self.tree.insert('', 'end', text=comp['dest'],
                                 values=(tr(comp['action']), tr('{label} (compiler)').format(
                                     label=index['roots'][comp['root']]['label'])), tags=(comp['action'],))
            for item in plan['files']:
                self.tree.insert('', 'end', text=item['dest'],
                                 values=(tr('replace') if item['action'] == 'replace' else tr('add'),
                                         index['roots'][item['root']]['label']), tags=(item['action'],))
            checks = plan['checks']
            ok = sum(1 for v in checks.values() if v[0])
            bad = [k for k, v in checks.items() if not v[0]]
            n_rep = sum(1 for i in plan['files'] if i['action'] == 'replace')
            text = tr('{add} new files, {rep} replaced (backed up first), {same} already the same. Checked in a copy: '
                      '{ok} of {n} scripts compile to exactly the game\'s bytes.').format(
                add=len(plan['files']) - n_rep, rep=n_rep, same=plan['same'], ok=ok, n=len(checks))
            if comp:
                text += ' ' + tr('The SDK\'s compiler is too old for the game\'s scripts: Tools\\EarthC.exe is replaced by '
                                 'the one from {label} (backed up first).').format(label=index['roots'][comp['root']]['label'])
            if plan['missing']:
                text += ' ' + tr('{m} scripts have no source anywhere and stay as they are: {names}.').format(
                    m=len(plan['missing']), names=', '.join(plan['missing']))
            if bad:
                text += NL + tr('NOT written: {n} scripts would not compile to the game\'s bytes.').format(n=len(bad))
                for k in bad[:6]:
                    self.tree.insert('', 'end', text=self.scripts[k].name if k in self.scripts else k,
                                     values=(tr('check failed'), checks[k][1][:60]), tags=('bad',))
            if not ecocore.plan_count(plan):
                text = tr('Nothing to do: this SDK already has the game\'s state.') + (
                    ' ' + tr('{m} scripts have no source anywhere.').format(m=len(plan['missing'])) if plan['missing'] else '')
            self.summary.configure(text=text, foreground=theme.ERR if bad else theme.INK)
            if ecocore.plan_count(plan) and not bad:
                self.btn_apply.state(['!disabled'])
        self.app.run_task(tr('Planning the SDK update ...'), work, done)

    def apply(self):
        target, plan = self.target(), self.plan
        if not target or not plan or not ecocore.plan_count(plan):
            return
        n = ecocore.plan_count(plan)
        if not messagebox.askyesno(APP_NAME, tr('Write {n} files into {path}?\n\nEvery replaced file goes to the backup '
                                                'folder first; Tools > Undo an SDK update puts everything back.').format(
                n=n, path=target.root), parent=self.win):
            return
        self.btn_plan.state(['disabled'])
        self.btn_apply.state(['disabled'])
        scripts, index = list(self.scripts.values()), self.app.index

        def work(progress, cancelled):
            backup = ecocore.apply_sdk_update(target, plan, index, backup_dir())
            try:
                res = ecocore.verify_sdk(target, scripts, plan, progress, cancelled)
            except ecocore.Cancelled:
                res = None
            return backup, res

        def done(result, err):
            try:
                if not self.win.winfo_exists():
                    return
            except tk.TclError:
                return
            self.btn_plan.state(['!disabled'])
            if err:
                self.summary.configure(text=tr('Writing failed: {e}. Use Tools > Undo an SDK update.').format(e=err),
                                       foreground=theme.ERR)
                return
            backup, res = result
            self._step(3)
            text = tr('Done. {n} files written. Backup: {path}').format(n=n, path=backup)
            good = res is not None and all(ok for ok, _m in res.values())
            if res is None:
                text += NL + tr('The check after writing was cancelled.')
            else:
                text += NL + tr('Checked in the SDK itself, with its own compiler: {ok} of {n} scripts compile to exactly '
                                'the game\'s bytes.').format(ok=sum(1 for ok, _m in res.values() if ok), n=len(res))
                for k, (ok, msg) in res.items():
                    if not ok:
                        text += NL + f'  {tr("NOT OK")}: {self.scripts[k].name}: {msg[:160]}'
            self.summary.configure(text=text, foreground=theme.OK if good else theme.ERR)
            self.app.fb.log.add(f'sdk update {n} files, compiler {"yes" if plan["compiler"] else "no"}, '
                                f'check {"ok" if good else "not ok"}')
            self.app.status(tr('SDK updated. Backup: {path}').format(path=backup), ok=good)
        self.app.run_task(tr('Writing and checking the SDK ...'), work, done)


class RestoreWindow:
    """List the SDK update backups and undo one."""

    def __init__(self, app):
        self.app = app
        self.win = win = _toplevel(app, tr('Undo an SDK update'), '680x380')
        f = ttk.Frame(win, padding=16)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=tr('Undo an SDK update'), style='Brand.TLabel').pack(anchor='w')
        ttk.Label(f, text=tr('Choose a backup. Replaced files (the compiler too) go back, added files are removed.'),
                  style='Muted.TLabel').pack(anchor='w', pady=(4, 8))
        self.lst = ttk.Treeview(f, columns=('files',), show='tree headings', selectmode='browse')
        self.lst.heading('#0', text=tr('Backup'))
        self.lst.heading('files', text=tr('Files written'))
        self.lst.column('files', width=120, stretch=False)
        self.lst.pack(fill='both', expand=True)
        root = backup_dir()
        for n in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
            man = os.path.join(root, n, 'manifest.json')
            if os.path.isfile(man):
                try:
                    with open(man, encoding='utf-8') as fh:
                        m = json.load(fh)
                    self.lst.insert('', 'end', iid=os.path.join(root, n), text=f'{n}   ->   {m["target"]}',
                                    values=(len(m['written']) + len(m.get('tools_written', [])),))
                except Exception:
                    pass
        if not self.lst.get_children():
            ttk.Label(f, text=tr('No backups yet.'), style='Muted.TLabel').pack(anchor='w')
        btns = ttk.Frame(f)
        btns.pack(fill='x', pady=(10, 0))
        ttk.Button(btns, text=tr('Undo this update'), style='Accent.TButton', command=self.restore).pack(side='right')
        ttk.Button(btns, text=tr('Close'), command=win.destroy).pack(side='right', padx=6)

    def restore(self):
        b = self.lst.focus()
        if not b:
            return
        if not messagebox.askyesno(APP_NAME, tr('Put the SDK back to the state before this update?'), parent=self.win):
            return
        try:
            n = ecocore.restore_sdk_update(b)
        except Exception as e:
            messagebox.showerror(APP_NAME, tr('Undo failed: {e}').format(e=e), parent=self.win)
            return
        self.app.status(tr('Undone: {n} files.').format(n=n), ok=True)
        self.win.destroy()


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.win = win = _toplevel(app, tr('Settings'), '640x300')
        f = ttk.Frame(win, padding=16)
        f.pack(fill='both', expand=True)
        self.game = tk.StringVar(value=app.cfg.get('game_dir') or (app.game_dir or ''))
        self.mods = tk.BooleanVar(value=bool(app.cfg.get('with_mods', True)))
        self.sdks = tk.StringVar(value=';'.join(app.cfg.get('sdk_dirs', [])))
        for label, var, pick in ((tr('Two Worlds folder:'), self.game, self._pick_game),
                                 (tr('Extra SDK folders (; between them):'), self.sdks, self._pick_sdk)):
            ttk.Label(f, text=label).pack(anchor='w', pady=(6, 2))
            row = ttk.Frame(f)
            row.pack(fill='x')
            ttk.Entry(row, textvariable=var).pack(side='left', fill='x', expand=True)
            ttk.Button(row, text=tr('Choose...'), command=pick).pack(side='left', padx=(6, 0))
        ttk.Checkbutton(f, text=tr('Also read the active mods (Mods folder, switched on in the registry)'),
                        variable=self.mods).pack(anchor='w', pady=(12, 0))
        btns = ttk.Frame(f)
        btns.pack(fill='x', side='bottom')
        ttk.Button(btns, text=tr('Save'), style='Accent.TButton', command=self.save).pack(side='right')
        ttk.Button(btns, text=tr('Close'), command=win.destroy).pack(side='right', padx=6)

    def _pick_game(self):
        d = filedialog.askdirectory(parent=self.win)
        if d:
            self.game.set(os.path.normpath(d))

    def _pick_sdk(self):
        d = filedialog.askdirectory(parent=self.win)
        if d:
            parts = [p for p in self.sdks.get().split(';') if p.strip()]
            parts.append(os.path.normpath(d))
            self.sdks.set(';'.join(parts))

    def save(self):
        g = self.game.get().strip()
        if g and not os.path.isdir(os.path.join(g, 'WDFiles')):
            messagebox.showwarning(APP_NAME, tr('That folder has no WDFiles subfolder.'), parent=self.win)
            return
        self.app.cfg['game_dir'] = g
        self.app.cfg['with_mods'] = bool(self.mods.get())
        self.app.cfg['sdk_dirs'] = [p.strip() for p in self.sdks.get().split(';') if p.strip()]
        self.app.cfg.save()
        self.win.destroy()
        self.app.reload()


class ErrorDialog:
    """An error the user can report: message, OK, "Report a bug", optional guide."""

    def __init__(self, app, key, message, shown, guide=None, title=None):
        self.win = win = tk.Toplevel(app.root)
        win.title(tr('Error'))
        win.transient(app.root)
        theme.dark_titlebar(win)
        win.bind('<Escape>', lambda e: win.destroy())
        win.bind('<Return>', lambda e: win.destroy())
        f = ttk.Frame(win, padding=16)
        f.pack(fill='both', expand=True)
        box = tk.Text(f, wrap='word', height=min(14, max(3, shown.count(NL) + 2 + len(shown) // 80)),
                      width=76, background=theme.FIELD, foreground=theme.INK, relief='flat', font=theme.FONT_MONO,
                      highlightthickness=0, padx=8, pady=6)
        box.insert('1.0', shown)
        box.configure(state='disabled')
        box.pack(fill='both', expand=True)
        btns = ttk.Frame(f)
        btns.pack(fill='x', pady=(12, 0))
        ttk.Button(btns, text='OK', style='Accent.TButton', command=win.destroy).pack(side='right')
        ttk.Button(btns, text=tr('Report a bug...'),
                   command=lambda: app.fb.report_bug(parent=win, error_text=shown, error_key=key,
                                                     title=title or f'{key}: {message}', fp_text=message)
                   ).pack(side='right', padx=6)
        if guide:
            ttk.Button(btns, text=tr('Read in the guide'), command=lambda: app.show_guide(guide)).pack(side='left')
        win.update_idletasks()
        win.geometry(f'+{app.root.winfo_rootx() + 40}+{app.root.winfo_rooty() + 60}')
        win.focus_set()


class UpdateWindow:
    """A newer release exists: notes, update now, later, skip (design 9)."""

    def __init__(self, app, info):
        self.app = app
        self.info = info
        self.win = _toplevel(app, tr('Update'), '620x480')
        f = ttk.Frame(self.win, padding=16)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=tr('Version {version} is out').format(version=info['version']), style='Brand.TLabel').pack(anchor='w')
        n = len(app.fb.untested())
        extra = ('  ' + tr('{n} new thing(s) wait for testers (Help > Test what is untested).').format(n=n)) if n else ''
        ttk.Label(f, text=tr('You have {current}. The update downloads the exe from GitHub, checks its SHA-256 checksum, '
                             'closes the tool and starts version {version}. The old exe stays as .old until the next start.'
                             ).format(current=VERSION, version=info['version']) + extra,
                  style='Muted.TLabel', wraplength=580, justify='left').pack(anchor='w', pady=(2, 8))
        txt = tk.Text(f, wrap='word', font=theme.FONT, height=12)
        txt.pack(fill='both', expand=True)
        txt.insert('1.0', info['notes'].split(NL + '---')[0].strip() or info['page'])
        txt.configure(state='disabled')
        self.status = ttk.Label(f, text='', style='Muted.TLabel', wraplength=580, justify='left')
        self.status.pack(anchor='w', pady=(8, 0))
        self.bar = ttk.Progressbar(f, maximum=100)
        btns = ttk.Frame(f)
        btns.pack(fill='x', side='bottom', pady=(10, 0))
        ttk.Button(btns, text=tr('Later'), command=self.win.destroy).pack(side='right')
        ttk.Button(btns, text=tr('Skip this version'), command=self.skip).pack(side='right', padx=6)
        self.exe = updater.frozen_exe()
        self.go = ttk.Button(btns, text=tr('Update now') if self.exe else tr('Open release page'),
                             style='Accent.TButton', command=self.start)
        self.go.pack(side='right')
        ttk.Button(btns, text=tr('View on GitHub'), command=lambda: webbrowser.open(info['page'])).pack(side='left')
        if self.exe and not info.get('sha256'):
            self.status.configure(text=tr('This release has no checksum. Without one the tool installs nothing; '
                                          'Update now opens the release page.'))

    def skip(self):
        self.app.cfg['update_skip'] = self.info['tag']
        self.app.cfg.save()
        self.win.destroy()

    def start(self):
        if not self.exe or not self.info.get('sha256') or not self.info.get('url'):
            webbrowser.open(self.info['page'])
            if not self.exe:
                self.win.destroy()
            return
        if self.app.busy:
            self.status.configure(text=tr('Wait until the work is done.'))
            return
        self.go.state(['disabled'])
        self.bar.pack(fill='x', pady=(6, 0), before=self.status)
        self.status.configure(text=tr('Downloading ...'))
        new = self.exe + '.new'
        state = {}

        def progress(done, total):
            state['p'] = (done, total)

        def work():
            try:
                updater.download(self.info, new, progress)
                state['ok'] = True
            except Exception as e:
                state['err'] = e
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                if not self.win.winfo_exists():
                    return
            except tk.TclError:
                return
            done, total = state.get('p', (0, 0))
            if total:
                self.bar.configure(value=100 * done / total)
                self.status.configure(text=tr('Downloading {done} of {total} MB ...').format(
                    done=done // 1048576, total=max(1, total // 1048576)))
            if 'err' in state:
                self.go.state(['!disabled'])
                self.status.configure(text=tr('Update failed, nothing was changed: {err}').format(err=state['err']))
                return
            if not state.get('ok'):
                self.win.after(150, poll)
                return
            self.status.configure(text=tr('Checksum matches. The tool closes and starts the new version.'))
            try:
                updater.start_swap(self.exe, new)
            except OSError as e:
                self.status.configure(text=tr('Update failed, nothing was changed: {err}').format(err=e))
                self.go.state(['!disabled'])
                return
            self.app.root.after(300, self.app.root.destroy)
        self.win.after(150, poll)


# ------------------------------------------------------------------ Deutsch --

DE = {}   # filled from i18n_de.py (kept apart: it is long)
try:
    from i18n_de import DE as _DE
    DE.update(_DE)
except ImportError:
    pass


def _check_translations():
    """Every user text has a German version with the same placeholders (run by the tests)."""
    missing = []
    for k, v in DE.items():
        assert set(re.findall(r'\{\w+\}', k)) == set(re.findall(r'\{\w+\}', v)), k
    for s in GUIDE_STEPS:
        for t in (s['text'], s['title']):
            if t not in DE:
                missing.append(t)
    return missing


def run_gui():
    carry = None
    while True:
        app = App(carry)
        app.run()
        if not app.restart:
            break
        carry = getattr(app, 'carry_out', None) or {}


if __name__ == '__main__':
    run_gui()
