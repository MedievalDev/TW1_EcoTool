"""Dark theme: colours, fonts, ttk styles, node palette.

Palette and style setup are transcribed from quest_creator_gui.py (same
look as the TW1MP server panel / website) so the two tools match.
"""

import tkinter as tk
from tkinter import ttk

BG = '#14110e'
PANEL = '#1c1813'
FIELD = '#231e17'
INK = '#ece7db'
MUT = '#9a938a'
LINE = '#352f26'
GOLD = '#d2a044'
GOLD_HI = '#e3b45c'
SEL = '#3a3122'
OK = '#43b563'
ERR = '#e06c60'
CANVAS_BG = '#0f0d0a'
GRID = '#1a1611'

# Distinct, dark-theme-legible colours cycled through for NPC speakers
# (one colour per speaker, see plan section 5.2).
SPEAKER_COLORS = ['#e06c60', '#7fbf7f', '#e0a050', '#c090e0',
                  '#5fc7c7', '#d4796b', '#a0b060', '#d0a0a0']
PLAYER_COLOR = '#6ca0e0'
ENTRY_COLOR = '#43b563'
COMMENT_COLOR = '#4a453d'
DIM = '#5c564c'
SCROLL_THUMB = '#7a7061'     # scrollbar thumb, readable on BG
# light blue is reserved for content from mods (update 5b): frames, dots,
# list entries. The journal group palette below keeps away from it.
MOD = '#56c8ff'
MOD_DOT = '\u25cf '
# one colour per journal group so the quest lines are told apart
CARD_COLORS = ('#d2a044', '#6ca0e0', '#7fbf7f', '#e06c60', '#c090e0',
               '#5fc7c7', '#e0a050', '#a0b060', '#d4796b', '#d0a0c0',
               '#c7a86b', '#9a8fe0')


def mix(colour, other, f):
    """Blend two #rrggbb colours, f = share of ``colour``."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(int(round(x * f + y * (1 - f)))
                                   for x, y in zip(a, b))


def group_color(group):
    """Colour of a journal group: every quest line gets its own."""
    return CARD_COLORS[int(group) % len(CARD_COLORS)]
# One colour per conversation level (state band on the node header).
STATE_COLORS = {'first': '#d2a044', 'known': '#b08850', 'running': '#6ca0e0',
                'taken': '#5a88c0', 'solved': '#43b563', 'closed': '#9a938a',
                'failed': '#e06c60', 'lowrep': '#c090e0',
                'neutral': '#7a746a'}

FONT = ('Segoe UI', 9)
FONT_BOLD = ('Segoe UI', 9, 'bold')
FONT_SMALL = ('Segoe UI', 8)
FONT_BRAND = ('Georgia', 12, 'bold')
FONT_MONO = ('Consolas', 9)
FONT_H1 = ('Segoe UI Semibold', 15)
FONT_H2 = ('Segoe UI Semibold', 11)
# Menus (menu bar labels and dropdowns): one size larger than body text.
FONT_MENU = ('Segoe UI', 10)


def dark_titlebar(window):
    """Dark title bar on Windows.

    Immersive dark mode alone loses against the user's "accent colour on
    title bars" setting, so the caption colour is set explicitly too
    (Windows 11 22000+); both calls are no-ops on older builds.
    """
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        dwm = ctypes.windll.dwmapi
        flag = ctypes.c_int(1)
        for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE, pre-20H1
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(flag),
                                         ctypes.sizeof(flag)) == 0:
                break

        def bgr(hex_colour):
            r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
            return ctypes.c_int(b << 16 | g << 8 | r)
        for attr, colour in ((35, PANEL), (36, INK)):     # caption, text
            value = bgr(colour)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                      ctypes.sizeof(value))
    except Exception:
        pass


def apply_dark_theme(root):
    root.configure(background=BG)
    dark_titlebar(root)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BG, foreground=INK, bordercolor=LINE,
                    darkcolor=BG, lightcolor=BG, troughcolor=PANEL,
                    fieldbackground=FIELD, selectbackground=SEL,
                    selectforeground=GOLD_HI, insertcolor=INK, font=FONT)
    style.configure('TLabelframe', bordercolor=LINE)
    style.configure('TLabelframe.Label', foreground=GOLD)
    style.configure('TButton', background=PANEL, padding=(12, 5),
                    borderwidth=1, focusthickness=1, focuscolor=LINE)
    style.map('TButton', background=[('pressed', SEL), ('active', '#282219')],
              foreground=[('disabled', '#5c564c')])
    # the Menubutton of the timeline ("Sources"): clam paints it near white
    # on hover, the text vanished (Marco 2026-09-16)
    style.configure('TMenubutton', background=PANEL, foreground=INK,
                    arrowcolor=MUT, padding=(10, 4), relief='flat',
                    borderwidth=1)
    style.map('TMenubutton',
              background=[('pressed', SEL), ('active', '#282219')],
              foreground=[('active', GOLD_HI), ('disabled', '#5c564c')],
              arrowcolor=[('active', GOLD_HI)])
    style.configure('Accent.TButton', background=GOLD, foreground='#17130b')
    style.map('Accent.TButton',
              background=[('pressed', '#b88d3c'), ('active', GOLD_HI),
                          ('disabled', '#6d5b31')],
              foreground=[('disabled', '#3a3226')])
    style.configure('TNotebook', bordercolor=LINE, tabmargins=(8, 6, 8, 0))
    style.configure('TNotebook.Tab', background=PANEL, foreground=MUT,
                    padding=(16, 6), bordercolor=LINE)
    style.map('TNotebook.Tab', background=[('selected', BG)],
              foreground=[('selected', GOLD)])
    style.configure('Treeview', background=FIELD, fieldbackground=FIELD,
                    rowheight=22, bordercolor=LINE)
    style.map('Treeview', background=[('selected', SEL)],
              foreground=[('selected', GOLD_HI)])
    style.configure('Treeview.Heading', background=PANEL, foreground=MUT,
                    bordercolor=LINE, relief='flat', padding=(6, 4))
    style.map('Treeview.Heading',
              background=[('active', '#282219'), ('pressed', SEL)],
              foreground=[('active', INK)])
    # clam draws radio/check hover as a light fill; keep it dark and only
    # let the indicator dot pick up the gold accent.
    for cls in ('TCheckbutton', 'TRadiobutton'):
        style.configure(cls, background=BG, foreground=INK,
                        indicatorbackground=FIELD, indicatorforeground=GOLD,
                        bordercolor=LINE, focuscolor=BG, padding=(2, 2))
        style.map(cls,
                  background=[('active', BG), ('pressed', BG),
                              ('selected', BG)],
                  foreground=[('active', GOLD_HI), ('disabled', '#5c564c')],
                  indicatorbackground=[('selected', GOLD), ('pressed', SEL),
                                       ('active', FIELD)],
                  indicatorforeground=[('selected', '#17130b')])
    # Read-only comboboxes otherwise render their text in the *selection*
    # colours, which on a dark theme means dark-on-dark.
    style.configure('TCombobox', arrowcolor=MUT, foreground=INK,
                    fieldbackground=FIELD, background=PANEL,
                    selectbackground=FIELD, selectforeground=INK,
                    bordercolor=LINE, padding=(6, 3))
    style.map('TCombobox',
              fieldbackground=[('readonly', FIELD), ('disabled', PANEL)],
              foreground=[('readonly', INK), ('disabled', '#5c564c')],
              selectbackground=[('readonly', FIELD)],
              selectforeground=[('readonly', INK)],
              arrowcolor=[('active', GOLD)])
    # thumb in a light warm grey on the dark trough, gold while hovered or
    # dragged (a PANEL thumb on BG was almost invisible)
    for cls in ('Vertical.TScrollbar', 'Horizontal.TScrollbar'):
        style.configure(cls, background=SCROLL_THUMB, troughcolor=BG,
                        bordercolor=LINE, arrowcolor=MUT,
                        darkcolor=SCROLL_THUMB, lightcolor=SCROLL_THUMB,
                        gripcount=0, relief='flat', arrowsize=13)
        style.map(cls, background=[('pressed', GOLD), ('active', GOLD_HI)],
                  arrowcolor=[('active', GOLD)])
    style.configure('TPanedwindow', background=LINE)
    style.configure('Sash', sashthickness=5, gripcount=0)
    style.configure('TProgressbar', background=GOLD, troughcolor=FIELD,
                    bordercolor=LINE, lightcolor=GOLD, darkcolor=GOLD)
    style.configure('Brand.TLabel', foreground=GOLD, font=FONT_BRAND)
    style.configure('Panel.TFrame', background=PANEL)
    style.configure('Panel.TLabel', background=PANEL, foreground=INK)
    style.configure('PanelTitle.TLabel', background=PANEL, foreground=GOLD,
                    font=FONT_BOLD, padding=(8, 5))
    style.configure('Muted.TLabel', foreground=MUT)
    style.configure('PanelMuted.TLabel', background=PANEL, foreground=MUT)
    style.configure('Panel.TCheckbutton', background=PANEL, foreground=INK,
                    indicatorbackground=FIELD, indicatorforeground=GOLD,
                    focuscolor=PANEL)
    style.map('Panel.TCheckbutton',
              background=[('active', PANEL), ('pressed', PANEL),
                          ('selected', PANEL)],
              foreground=[('active', GOLD_HI)],
              indicatorbackground=[('selected', GOLD), ('pressed', SEL),
                                   ('active', FIELD)],
              indicatorforeground=[('selected', '#17130b')])
    style.configure('Status.TFrame', background=PANEL)
    style.configure('Status.TLabel', background=PANEL, foreground=MUT,
                    padding=(8, 3), font=FONT_SMALL)
    style.configure('StatusOk.TLabel', background=PANEL, foreground=OK,
                    padding=(8, 3), font=FONT_SMALL)
    style.configure('StatusErr.TLabel', background=PANEL, foreground=ERR,
                    padding=(8, 3), font=FONT_SMALL)
    style.configure('StatusSep.TLabel', background=PANEL, foreground=LINE,
                    padding=(0, 3), font=FONT_SMALL)
    # Themed stand-in for the native menu bar, which Windows always draws
    # light and which no ttk style can reach.
    style.configure('Menubar.TFrame', background=PANEL)
    style.configure('Menubar.TLabel', background=PANEL, foreground=INK,
                    padding=(12, 5), font=FONT_MENU)
    style.map('Menubar.TLabel', background=[('active', SEL)],
              foreground=[('active', GOLD_HI)])
    for pattern, value in (
            ('*Text.background', CANVAS_BG), ('*Text.foreground', INK),
            ('*Text.insertBackground', INK), ('*Text.selectBackground', SEL),
            ('*Text.borderWidth', 0), ('*Text.highlightThickness', 1),
            ('*Text.highlightBackground', LINE),
            ('*Text.highlightColor', GOLD),
            ('*Listbox.background', FIELD), ('*Listbox.foreground', INK),
            ('*Listbox.selectBackground', SEL),
            ('*Listbox.borderWidth', 0), ('*Listbox.highlightThickness', 1),
            ('*Listbox.highlightBackground', LINE),
            ('*Menu.background', PANEL), ('*Menu.foreground', INK),
            ('*Menu.activeBackground', SEL),
            ('*Menu.activeForeground', GOLD_HI),
            ('*Menu.disabledForeground', DIM),
            ('*Menu.selectColor', GOLD),
            ('*Menu.font', FONT_MENU),
            ('*Menu.borderWidth', 1),
            ('*Menu.activeBorderWidth', 0),
            ('*Menu.relief', 'flat'),
            ('*TCombobox*Listbox.background', FIELD),
            ('*TCombobox*Listbox.foreground', INK),
            ('*TCombobox*Listbox.selectBackground', SEL),
            ('*Toplevel.background', BG)):
        root.option_add(pattern, value)


class Menu(tk.Menu):
    """Dropdown / context menu with readable disabled entries.

    On Windows Tk draws disabled menu entries with an embossed white shadow
    that is hard to read on a dark background. Entries added with
    ``state='disabled'`` are therefore kept technically enabled, drawn in the
    dim colour, without hover highlight and without a command.
    """

    def __init__(self, master=None, **kw):
        kw.setdefault('tearoff', 0)
        kw.setdefault('font', FONT_MENU)
        kw.setdefault('background', PANEL)
        kw.setdefault('foreground', INK)
        kw.setdefault('activebackground', SEL)
        kw.setdefault('activeforeground', GOLD_HI)
        kw.setdefault('disabledforeground', DIM)
        kw.setdefault('relief', 'flat')
        kw.setdefault('activeborderwidth', 0)
        super().__init__(master, **kw)

    @staticmethod
    def _soft_disable(kind, kw):
        if kw.get('state') == 'disabled':
            kw['state'] = 'normal'
            kw['foreground'] = DIM
            kw['activeforeground'] = DIM
            kw['activebackground'] = PANEL
            if kind != 'cascade':
                kw['command'] = lambda: None
        return kw

    def add_command(self, cnf=None, **kw):
        super().add_command(cnf or {}, **self._soft_disable('command', kw))

    def add_checkbutton(self, cnf=None, **kw):
        super().add_checkbutton(cnf or {}, **self._soft_disable('check', kw))

    def add_radiobutton(self, cnf=None, **kw):
        super().add_radiobutton(cnf or {}, **self._soft_disable('radio', kw))

    def add_cascade(self, cnf=None, **kw):
        super().add_cascade(cnf or {}, **self._soft_disable('cascade', kw))


class FloatTip:
    """A tooltip that follows the mouse over parts of one widget (canvas
    items, list rows); the caller decides the text."""

    def __init__(self, master):
        self.master = master
        self.tip = None
        self.text = None

    def show(self, text, x_root, y_root):
        if not text:
            self.hide()
            return
        if self.tip is not None and text == self.text:
            self.tip.wm_geometry(f'+{x_root + 14}+{y_root + 14}')
            return
        self.hide()
        self.text = text
        self.tip = tk.Toplevel(self.master)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes('-topmost', True)
        tk.Label(self.tip, text=text, bg=PANEL, fg=INK, bd=1, relief='solid',
                 justify='left', padx=6, pady=3, font=FONT_SMALL).pack()
        self.tip.wm_geometry(f'+{x_root + 14}+{y_root + 14}')

    def hide(self):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
        self.tip = None
        self.text = None


class Tooltip:
    """Small hover tooltip for any widget."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind('<Enter>', self._show, add='+')
        widget.bind('<Leave>', self._hide, add='+')

    def _show(self, ev):
        if self.tip or not self.text:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes('-topmost', True)
        tk.Label(self.tip, text=self.text, bg=PANEL, fg=INK, bd=1,
                 relief='solid', justify='left', padx=6, pady=3,
                 font=FONT_SMALL).pack()
        self.tip.wm_geometry(f'+{ev.x_root + 14}+{ev.y_root + 12}')

    def _hide(self, ev=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ------------------------------------------------ TW1 EcoTool: script view --
# Syntax colours for EarthC source (keywords gold like the accent, types blue,
# strings green, numbers orange, comments in the comment grey) and the status
# colours of the script list (design 2.2 category colours).
SYNTAX = {'keyword': GOLD, 'type': '#6ca0e0', 'string': '#7fbf7f', 'number': '#e0a050',
          'comment': '#7a746a', 'preproc': '#c090e0', 'native': '#5fc7c7'}
STATUS_SOURCE = OK            # a source compiles to the game's bytes
STATUS_DIFFERS = '#e0a050'    # same-named source, other bytes (mod or other version)
STATUS_NONE = '#d4796b'       # no source at all: decompiler only
