"""Test window, bug report and known issues for every TW1 tool.

Standard library (tkinter) plus ``foxfeedback.py`` next to it. A tool needs
four lines to take part (see the skill ``tw1-testfenster``):

    import foxfeedback_ui
    fb = foxfeedback_ui.FeedbackUI(root, 'pareditor', VERSION,
                                   cfg_get, cfg_set, lang='de')
    fb.add_menu_items(help_menu)             # test / known issues / bug
    fb.start()                               # summary + "new in" window

and ``fb.report_bug(error_text=..., error_key=...)`` behind a "Report a bug"
button of its error dialogs, ``fb.log.add(...)`` for what the user did.

Everything here was first built and reviewed twice in the Quest Creator
(4.0.0/4.0.1, questforge2/feedbackwin.py): no Tk call from a thread (the
UI polls), nothing is sent before the user saw it, the public bug title is
made by the tool (never typed by the user), tests the server does not know
are not offered, the body is cut in BYTES.
"""

import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import foxfeedback

UI_VERSION = 3
CONFIRM_NEEDED = 2          # the server confirms a test after 2 passes from different PCs
NL = chr(10)

TEXTS = {
    'de': {
        'menu.test': 'Ungetestetes testen',
        'menu.issues': 'Bekannte Probleme',
        'menu.bug': 'Bug melden',
        'fb.title': 'Rueckmeldung',
        'fb.sent': 'Gesendet, danke! Kennung: {id}',
        'fb.err.rate': 'Zu viele Einsendungen in kurzer Zeit. Bitte spaeter nochmal.',
        'fb.err.closed': 'Die Annahme ist gerade geschlossen.',
        'fb.err.too_large': 'Die Meldung ist zu gross.',
        'fb.err.offline': 'Der Server ist nicht erreichbar ({detail}).',
        'fb.err.invalid': 'Der Server hat die Meldung abgelehnt ({detail}).',
        'fb.savefile': 'Die Meldung stattdessen als Textdatei speichern?',
        'fb.preview.title': 'Das wird gesendet',
        'fb.preview.head': 'Genau das geht an alchemy-fox.de',
        'fb.preview.hint': 'Keine Namen, keine E-Mail, keine Projektdaten. Benutzernamen in Pfaden sind durch <user> ersetzt. Gesendet wird erst mit dem Knopf.',
        'fb.send': 'Senden',
        'cancel': 'Abbrechen',
        'close': 'Schliessen',
        'test.title': 'Testbereich',
        'test.head': 'Hilf mit: ungetestete Neuerungen',
        'test.sub': 'Such dir einen Test aus, folge den Schritten und sag am Ende, ob es funktioniert hat. Bestaetigen zwei Leute einen Test, gilt er fuer alle als bestanden.',
        'test.server.ok': 'Stand vom Server geladen.',
        'test.server.off': 'Server nicht erreichbar: Stand unbekannt, testen geht trotzdem.',
        'test.none': 'Gerade gibt es nichts zu testen. Danke!',
        'test.state.open': 'Neu in {since}. Offen: {ok} x funktioniert.',
        'test.state.unknown': 'Neu in {since}. Noch keine Rueckmeldung.',
        'test.state.confirmed': 'Neu in {since}. BESTAETIGT: {ok} x funktioniert.',
        'test.state.failed': 'Neu in {since}. FEHLER GEMELDET ({bad} x funktioniert nicht). Wird untersucht.',
        'test.state.closed': 'Neu in {since}. Abgeschlossen.',
        'test.bar.none': 'Noch keine Rueckmeldung. Deine waere die erste von {need}.',
        'test.bar.some': '{ok} von {need} Bestaetigungen. Es fehlt noch eine von einem anderen Rechner.',
        'test.bar.confirmed': 'Test bestanden: {ok} Bestaetigungen von verschiedenen Rechnern.',
        'test.bar.failed': 'Test gescheitert: {bad} x hat es nicht funktioniert. Wird untersucht.',
        'test.mine.pass': 'Von diesem Rechner schon als "funktioniert" gemeldet.',
        'test.mine.fail': 'Von diesem Rechner schon als "funktioniert nicht" gemeldet.',
        'test.steps': 'Schritte',
        'test.expect': 'So muss es am Ende aussehen',
        'test.run': 'Starten',
        'test.run.hint': 'Das Tool schreibt mit, was von aussen sichtbar ist. Das Protokoll geht mit dem Ergebnis mit.',
        'test.run.waiting': 'Wird gestartet ...',
        'test.run.running': 'Laeuft. Das Tool wartet, bis du es beendest.',
        'test.run.ended': 'Beendet, Protokoll geschrieben. Jetzt unten das Ergebnis waehlen.',
        'test.run.failed': 'Nicht gestartet. Von Hand starten und trotzdem melden.',
        'test.run.busy': 'Das Programm laeuft schon. Erst beenden.',
        'test.note': 'Anmerkung (bei "funktioniert nicht" bitte: was hast du erwartet, was ist passiert?)',
        'test.pass': 'Funktioniert',
        'test.fail': 'Funktioniert nicht',
        'test.steps.open': '{n} Schritte sind nicht abgehakt. Trotzdem als "funktioniert" melden?',
        'test.norun.q': 'Nicht ueber "Starten" gestartet, das Protokoll fehlt dann. Trotzdem melden?',
        'test.stillrun.q': 'Es laeuft noch. Ohne das Ende fehlt ein Teil des Protokolls. Trotzdem jetzt melden?',
        'test.fail.note': 'Bitte kurz beschreiben, was nicht funktioniert hat.',
        'test.close.q': 'Es laeuft noch, das Protokoll ginge verloren. Trotzdem schliessen?',
        'test.busy': 'Fuer diesen Test laeuft noch etwas. Erst beenden und melden.',
        'test.notopen': 'Dieser Test nimmt gerade keine Ergebnisse an. Bitte spaeter nochmal.',
        'bug.title': 'Bug melden',
        'bug.head': 'Das sieht nach einem Fehler im Tool aus?',
        'bug.sub': 'Die Meldung geht an den Entwickler und erscheint unter Hilfe > Bekannte Probleme. Oeffentlich wird nur eine Kurzzeile aus dem Fehler, dein Text bleibt privat.',
        'bug.known': 'Schon gemeldet:',
        'bug.error': 'Fehlermeldung',
        'bug.guide': 'Erst im Guide nachlesen',
        'bug.what': 'Was hast du gemacht, was hast du erwartet?',
        'bug.next': 'Weiter: ansehen, was gesendet wird',
        'bug.empty': 'Bitte kurz beschreiben, was passiert ist.',
        'issues.head': 'Gemeldet fuer {tool}',
        'issues.loading': 'Wird geladen ...',
        'issues.offline': 'Der Server ist nicht erreichbar.',
        'issues.sub': '{n} Meldungen. {tests} Neuerungen warten auf Tester.',
        'issues.col.status': 'Stand', 'issues.col.title': 'Problem',
        'issues.col.version': 'Version', 'issues.col.count': 'Anzahl',
        'issues.col.fixed': 'Behoben in',
        'issues.status.reported': 'gemeldet',
        'issues.status.confirmed': 'bestaetigt',
        'issues.status.fixed': 'behoben',
        'news.title': 'Neu in {version}',
        'news.head': 'Diese Version bringt Ungetestetes',
        'news.sub': 'Diese Neuerungen sind gemessen, aber noch nicht von Nutzern bestaetigt. Ein Test dauert ein paar Minuten und hilft allen.',
        'news.test': 'Ich teste das',
    },
    'en': {
        'menu.test': 'Test untested features',
        'menu.issues': 'Known issues',
        'menu.bug': 'Report a bug',
        'fb.title': 'Feedback',
        'fb.sent': 'Sent, thank you! Id: {id}',
        'fb.err.rate': 'Too many reports in a short time. Please try again later.',
        'fb.err.closed': 'Reports are not being accepted right now.',
        'fb.err.too_large': 'The report is too large.',
        'fb.err.offline': 'The server cannot be reached ({detail}).',
        'fb.err.invalid': 'The server rejected the report ({detail}).',
        'fb.savefile': 'Save the report as a text file instead?',
        'fb.preview.title': 'This will be sent',
        'fb.preview.head': 'Exactly this goes to alchemy-fox.de',
        'fb.preview.hint': 'No names, no e-mail, no project data. User names in paths are replaced by <user>. Nothing is sent before you press the button.',
        'fb.send': 'Send',
        'cancel': 'Cancel',
        'close': 'Close',
        'test.title': 'Test area',
        'test.head': 'Help out: untested news',
        'test.sub': 'Pick a test, follow the steps and say at the end whether it worked. Once two people confirm a test, it counts as passed for everybody.',
        'test.server.ok': 'State loaded from the server.',
        'test.server.off': 'Server not reachable: state unknown, testing works anyway.',
        'test.none': 'Nothing to test right now. Thank you!',
        'test.state.open': 'New in {since}. Open: {ok} x works.',
        'test.state.unknown': 'New in {since}. No feedback yet.',
        'test.state.confirmed': 'New in {since}. CONFIRMED: {ok} x works.',
        'test.state.failed': 'New in {since}. FAILURE REPORTED ({bad} x does not work). Being looked into.',
        'test.state.closed': 'New in {since}. Closed.',
        'test.bar.none': 'No feedback yet. Yours would be the first of {need}.',
        'test.bar.some': '{ok} of {need} confirmations. One more from another PC is missing.',
        'test.bar.confirmed': 'Test passed: {ok} confirmations from different PCs.',
        'test.bar.failed': 'Test failed: {bad} x did not work. Being looked into.',
        'test.mine.pass': 'Already reported as "works" from this PC.',
        'test.mine.fail': 'Already reported as "does not work" from this PC.',
        'test.steps': 'Steps',
        'test.expect': 'What it must look like at the end',
        'test.run': 'Start',
        'test.run.hint': 'The tool writes down what can be seen from outside. That log goes along with the result.',
        'test.run.waiting': 'Starting ...',
        'test.run.running': 'Running. The tool waits until you quit it.',
        'test.run.ended': 'Ended, log written. Now pick the result below.',
        'test.run.failed': 'Did not start. Start it by hand and report anyway.',
        'test.run.busy': 'It is already running. Quit it first.',
        'test.note': 'Note (for "does not work" please: what did you expect, what happened?)',
        'test.pass': 'Works',
        'test.fail': 'Does not work',
        'test.steps.open': '{n} steps are not ticked. Report "works" anyway?',
        'test.norun.q': 'Not started with "Start", so the log is missing. Report anyway?',
        'test.stillrun.q': 'It is still running. Without its end part of the log is missing. Report now anyway?',
        'test.fail.note': 'Please describe briefly what did not work.',
        'test.close.q': 'It is still running, the log would be lost. Close anyway?',
        'test.busy': 'Something is still running for this test. Quit it and report first.',
        'test.notopen': 'This test does not take results right now. Please try again later.',
        'bug.title': 'Report a bug',
        'bug.head': 'This looks like an error in the tool?',
        'bug.sub': 'The report goes to the developer and shows up under Help > Known issues. Only a short line made from the error becomes public, your text stays private.',
        'bug.known': 'Already reported:',
        'bug.error': 'Error message',
        'bug.guide': 'Read the guide first',
        'bug.what': 'What did you do, what did you expect?',
        'bug.next': 'Next: see what will be sent',
        'bug.empty': 'Please describe briefly what happened.',
        'issues.head': 'Reported for {tool}',
        'issues.loading': 'Loading ...',
        'issues.offline': 'The server cannot be reached.',
        'issues.sub': '{n} reports. {tests} new features are waiting for testers.',
        'issues.col.status': 'State', 'issues.col.title': 'Issue',
        'issues.col.version': 'Version', 'issues.col.count': 'Count',
        'issues.col.fixed': 'Fixed in',
        'issues.status.reported': 'reported',
        'issues.status.confirmed': 'confirmed',
        'issues.status.fixed': 'fixed',
        'news.title': 'New in {version}',
        'news.head': 'This version brings untested things',
        'news.sub': 'These features are measured but not yet confirmed by users. A test takes a few minutes and helps everybody.',
        'news.test': 'I will test this',
    },
}

COLORS = {'bg': '#15120e', 'ink': '#ece7db', 'mut': '#9a938a',
          'gold': '#d19a3d', 'ok': '#43b563', 'err': '#e06c60'}


def vkey(version):
    """'4.0.1' -> (4, 0, 1); leading digits of each part ("0-m1" is 0)."""
    out = []
    for part in str(version).split('.'):
        digits = ''
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits or 0))
    return tuple(out)


def _style(widget_style, fallback=''):
    """The tool's ttk style when it exists (PY_TOOL_DESIGN theme), else the
    plain one - a missing style would raise in Tk."""
    try:
        ttk.Style().layout(widget_style)
        return widget_style
    except tk.TclError:
        return fallback


# ---------------------------------------------------------------------------
# watching a program the test starts (the game, an editor ...)

def process_names():
    """Names of the running processes (Windows snapshot API, 5-20 ms; the
    ``tasklist`` fallback takes 0.4 s), or None when they cannot be read."""
    try:
        import ctypes
        from ctypes import wintypes

        class ENTRY(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD),
                        ('cntUsage', wintypes.DWORD),
                        ('th32ProcessID', wintypes.DWORD),
                        ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', wintypes.DWORD),
                        ('cntThreads', wintypes.DWORD),
                        ('th32ParentProcessID', wintypes.DWORD),
                        ('pcPriClassBase', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD),
                        ('szExeFile', ctypes.c_wchar * 260)]
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRY)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRY)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (None, wintypes.HANDLE(-1).value):
            raise OSError('no snapshot')
        try:
            e = ENTRY()
            e.dwSize = ctypes.sizeof(ENTRY)
            names = []
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                names.append(e.szExeFile)
                ok = k32.Process32NextW(snap, ctypes.byref(e))
            if names:
                return names
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    try:
        out = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess,
                                                   'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return None
    names = [ln.split(',')[0].strip('"') for ln in out.stdout.splitlines()]
    return names or None


def decode_log(raw):
    """Bytes of a log cut anywhere: UTF-16 (with or without BOM), UTF-8 (a
    cut character at either end dropped), else the system code page."""
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', 'replace')
    odd, even = raw[1::2], raw[0::2]
    if len(raw) >= 16 and odd.count(0) > len(odd) * 0.6:
        return raw[len(raw) % 2 and 1:].decode('utf-16-le', 'replace')
    if len(raw) >= 16 and even.count(0) > len(even) * 0.6:
        return raw[1:len(raw) - (len(raw) - 1) % 2].decode('utf-16-le',
                                                            'replace')
    body = raw
    for _ in range(3):
        if body[:1] and 0x80 <= body[0] < 0xC0:
            body = body[1:]
    try:
        return body.decode('utf-8')
    except UnicodeDecodeError as e:
        if e.start >= len(body) - 3:
            return body[:e.start].decode('utf-8', 'replace')
    return raw.decode('mbcs' if os.name == 'nt' else 'latin-1', 'replace')


def read_part(path, offset=0, limit=40000, head=False):
    """At most ``limit`` bytes of a file: its START (``head``, a crash
    report) or the END of what came after ``offset`` (a log)."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(offset if head else max(offset, size - limit))
            return decode_log(f.read(limit))
    except OSError as e:
        return f'(unreadable: {e})'


def listing(folder):
    """{name: (size, mtime)} of the files directly in ``folder``."""
    out = {}
    try:
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                st = os.stat(p)
                out[n] = (st.st_size, st.st_mtime)
    except OSError:
        pass
    return out


def _stamp(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


class RunSession:
    """One program started from the test window and watched from outside.
    ``before()`` and ``after(session)`` return text lines for the log;
    ``is_it(name)`` tells whether a process name is the program (a launcher
    may end at once and the real program appear later)."""

    APPEAR_SECONDS = 90

    def __init__(self, exe, cwd, is_it, before=None, after=None):
        self.exe, self.cwd, self.is_it = exe, cwd, is_it
        self._before, self._after = before, after
        self.lines = []
        self.state = 'idle'              # idle waiting running ended failed
        self.started = self.ended = None
        self._proc = None

    def add(self, text=''):
        self.lines.append(str(text))

    def text(self):
        return NL.join(self.lines)

    def _running(self):
        names = process_names()
        if names is None:
            return None
        return any(self.is_it(n) for n in names)

    def start(self):
        self.add(f'== before the start  {_stamp(time.time())}')
        self.add(f'program: {os.path.basename(self.exe)}')
        if self._before:
            try:
                for ln in self._before() or []:
                    self.add(ln)
            except Exception as e:           # the log never stops the start
                self.add(f'(log before the start incomplete: {e!r})')
        try:
            self._proc = subprocess.Popen([self.exe], cwd=self.cwd)
        except OSError as e:                 # "run as administrator" ...
            self.add(f'direct start refused ({e}), asking the shell')
            try:
                os.startfile(self.exe, cwd=self.cwd)
            except (OSError, AttributeError, TypeError) as e2:
                self.add(f'could not start: {e2}')
                self.state = 'failed'
                return False
        self.started = time.time()
        self.state = 'waiting'
        self.add(f'== started  {_stamp(self.started)}')
        threading.Thread(target=self._watch, daemon=True).start()
        return True

    def _watch(self):
        def alive():
            return self._proc is not None and self._proc.poll() is None
        seen = False
        while time.time() - self.started < self.APPEAR_SECONDS:
            if self._running():
                seen = True
                break
            if self._proc is not None and not alive() and \
                    time.time() - self.started > 20:
                break
            time.sleep(1.5)
        if not seen and alive():
            seen = True
        if not seen:
            self.add('the program never appeared')
            self.state = 'failed'
            self._finish()
            return
        self.state = 'running'
        misses = 0                           # one failed look is not the end
        while misses < 3:
            state = self._running()
            if state or (state is None and alive()):
                misses = 0
            else:
                misses += 1
            time.sleep(2 if misses == 0 else 1)
        self.ended = time.time()
        self._finish()
        self.state = 'ended'

    def _finish(self):
        end = self.ended or time.time()
        self.add(f'== after  {_stamp(end)}')
        if self.started:
            self.add(f'run time: {int(end - self.started)} s')
        code = self._proc.poll() if self._proc else None
        if code is not None:
            self.add(f'exit code of the started program: {code}')
        if self._after:
            try:
                for ln in self._after(self) or []:
                    self.add(ln)
            except Exception as e:
                self.add(f'(log after the end incomplete: {e!r})')


class Launcher:
    """What the test window may start. ``programs()`` lists names,
    ``session(name)`` makes a RunSession."""

    def __init__(self, folder, names, is_it, before=None, after=None):
        self.folder, self.names = folder, list(names)
        self.is_it, self.before, self.after = is_it, before, after

    def programs(self):
        return [n for n in self.names
                if os.path.isfile(os.path.join(self.folder, n))]

    def session(self, name):
        return RunSession(os.path.join(self.folder, name), self.folder,
                          self.is_it, self.before, self.after)


def tw1_launcher(game_dir, extra_before=None):
    """Two Worlds as the program to test: TwoWorlds*.exe of the game folder
    (not the Mod Selector), the mods switched on, new saves and
    screenshots, new crash reports (TWSECrashLogs), new lines of .log files
    in the game folder. ``extra_before()`` adds the tool's own lines (e.g.
    what it exported)."""
    saves = os.path.join(os.path.expanduser('~'), 'Saved Games',
                         'Two Worlds Saves')
    crash_dirs = ('TWSECrashLogs',)
    snap = {}

    def is_game(name):
        low = name.lower()
        return low.startswith('twoworlds') and low.endswith('.exe') \
            and 'mod selector' not in low

    def before():
        snap['logs'] = {n: s for n, (s, _m) in listing(game_dir).items()
                        if n.lower().endswith('.log')}
        snap['saves'] = listing(saves)
        snap['crash'] = {d: set(listing(os.path.join(game_dir, d)))
                         for d in crash_dirs}
        out = []
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r'SOFTWARE\Reality Pump\TwoWorlds\Mods')
            on, i = [], 0
            with key:
                while True:
                    try:
                        name, value, _k = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    if str(value).strip() == '1':
                        on.append(name)
            files = listing(os.path.join(game_dir, 'Mods'))
            out.append(f'mods switched on: {len(on)}')
            for n in sorted(on, key=str.lower):
                size, mt = files.get(n, (None, None))
                out.append(f'  {n}  ' + (f'{size} bytes  {_stamp(mt)}'
                                         if size is not None
                                         else 'NOT in the Mods folder'))
        except Exception as e:
            out.append(f'mods: unknown ({e})')
        lhc = os.path.join(game_dir, 'Levels', 'Map_LevelHeaders.lhc')
        if os.path.isfile(lhc):
            out.append(f'level header cache: {os.path.getsize(lhc)} bytes, '
                       f'{_stamp(os.path.getmtime(lhc))}')
        else:
            out.append('level header cache: no loose file')
        if extra_before:
            out += list(extra_before() or [])
        return out

    def after(_session):
        out = []
        old = snap.get('saves') or {}
        new = {n: v for n, v in listing(saves).items()
               if n not in old or v[1] > old[n][1]}
        out.append(f'new saves and screenshots: {len(new)}')
        for n, (_s, mt) in sorted(new.items(), key=lambda kv: kv[1][1])[-20:]:
            out.append(f'  {n}  {_stamp(mt)}')
        crashed = False
        for d, before_names in (snap.get('crash') or {}).items():
            folder = os.path.join(game_dir, d)
            for n in sorted(set(listing(folder)) - before_names):
                crashed = True
                out.append(f'NEW CRASH REPORT {d}/{n}')
                out.append(read_part(os.path.join(folder, n), 0, 3000,
                                     head=True))
        out.append('crash report: ' + ('YES' if crashed else 'none'))
        for n, (size, _m) in sorted(listing(game_dir).items()):
            if not n.lower().endswith('.log'):
                continue
            prev = (snap.get('logs') or {}).get(n, 0)
            if size < prev:
                prev = 0
            if size > prev:
                out.append(f'-- new in {n} --')
                out.append(read_part(os.path.join(game_dir, n), prev))
        return out

    try:
        names = sorted((n for n in os.listdir(game_dir) if is_game(n)),
                       key=lambda n: (n.lower() != 'twoworlds.exe',
                                      n.lower()))
    except OSError:
        names = []
    return Launcher(game_dir, names, is_game, before, after)


# ---------------------------------------------------------------------------
# the manager a tool keeps

class FeedbackUI:
    """``cfg_get(key, default)`` / ``cfg_set(key, value)`` store the random
    client id and the last version the "new in" window was shown for (the
    tool saves its config itself - call its save in ``cfg_set``).
    ``tests_file``: the tool's untested.json. ``open_guide(chapter)``: shows
    the tool's guide (optional). ``launcher``: a Launcher for "Start"
    (optional, e.g. ``tw1_launcher(game_dir)``)."""

    def __init__(self, root, tool, version, cfg_get, cfg_set, lang='de',
                 tests_file=None, open_guide=None, launcher=None,
                 tool_name=None, base=None):
        self.root = root
        self.tool, self.version = tool, version
        self.tool_name = tool_name or tool
        self.cfg_get, self.cfg_set = cfg_get, cfg_set
        self.lang = 'de' if lang == 'de' else 'en'
        self.open_guide, self.launcher = open_guide, launcher
        self.base = base or foxfeedback.BASE
        self.log = foxfeedback.SessionLog()
        self.summary = None                 # None: server not reached (yet)
        self.tests = self._load_tests(tests_file)
        self.test_window = None
        self.log.add(f'{tool} {version} started, lang {self.lang}')

    # -- basics -----------------------------------------------------------

    def t(self, key, **fmt):
        text = TEXTS[self.lang].get(key) or TEXTS['en'].get(key) or key
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text

    def loc(self, d):
        if isinstance(d, dict):
            return d.get(self.lang) or d.get('en') or ''
        return d or ''

    @staticmethod
    def _load_tests(path):
        if not path:
            return []
        try:
            with open(path, encoding='utf-8') as f:
                tests = json.load(f).get('tests') or []
        except (OSError, ValueError, AttributeError):
            return []
        return [x for x in tests if isinstance(x, dict) and x.get('id')]

    def client_id(self):
        cid = self.cfg_get('client_id', None)
        if not isinstance(cid, str) or not re.fullmatch('[0-9a-f]{32}', cid):
            cid = foxfeedback.new_client_id()
            self.cfg_set('client_id', cid)
        return cid

    def _bg(self, work, done):
        """``work()`` in a thread, ``done(result, error)`` in the UI thread
        (the UI polls: no Tk call from the worker)."""
        box = []

        def run():
            try:
                box.append((work(), None))
            except Exception as e:
                box.append((None, e))

        def poll():
            if not box:
                try:
                    self.root.after(80, poll)
                except (tk.TclError, RuntimeError):
                    pass
                return
            try:
                done(*box[0])
            except tk.TclError:
                pass
        threading.Thread(target=run, daemon=True).start()
        self.root.after(80, poll)

    # -- server state --------------------------------------------------------

    def refresh(self, then=None):
        def done(s, _err):
            if s is not None:
                self.summary = s
            if then:
                then()
        self._bg(lambda: foxfeedback.fetch_summary(self.tool, self.version,
                                                    base=self.base), done)

    def state(self, test_id):
        tests = (self.summary or {}).get('tests') or {}
        return (tests.get(test_id) or {}).get('status') or 'unknown'

    def counts(self, test_id):
        rec = ((self.summary or {}).get('tests') or {}).get(test_id) or {}
        return int(rec.get('pass') or 0), int(rec.get('fail') or 0)

    def mine(self, test_id):
        """What this PC reported for the test: 'pass', 'fail' or ''."""
        sent = self.cfg_get('test_sent', None) or {}
        return sent.get(test_id, '') if isinstance(sent, dict) else ''

    def remember(self, test_id, passed):
        sent = self.cfg_get('test_sent', None) or {}
        if not isinstance(sent, dict):
            sent = {}
        sent[test_id] = 'pass' if passed else 'fail'
        self.cfg_set('test_sent', sent)

    def can_report(self, test_id):
        """False when the server was reached and does not take the test
        (not created there yet, or closed) - it would answer 400."""
        if self.summary is None:
            return True
        return self.state(test_id) in ('open', 'failed', 'confirmed')

    def untested(self):
        return [x for x in self.tests
                if vkey(x.get('since', '0')) <= vkey(self.version)
                and self.state(x['id']) not in ('confirmed', 'closed')
                and self.can_report(x['id'])]

    def experimental(self, label):
        """True while the test behind an "experimental" label is not
        confirmed (offline: still experimental)."""
        for x in self.tests:
            if x.get('experimental') == label:
                return self.state(x['id']) not in ('confirmed', 'closed')
        return False

    def issues(self):
        return (self.summary or {}).get('issues') or []

    # -- what a tool calls ------------------------------------------------------

    def start(self):
        """At the end of the tool's start: read the server state, then show
        "new in" once per version when there are untested things."""
        self.refresh(self._maybe_news)

    def add_menu_items(self, menu):
        menu.add_command(label=self.t('menu.test'),
                         command=lambda: self.show_tests())
        menu.add_command(label=self.t('menu.issues'),
                         command=lambda: IssuesWindow(self))
        menu.add_command(label=self.t('menu.bug'),
                         command=lambda: self.report_bug())

    def show_tests(self, test_id=None):
        w = self.test_window
        if w is not None:
            try:
                w.win.lift()
                if test_id:
                    w.select(test_id)
                return w
            except tk.TclError:
                self.test_window = None
        self.test_window = TestWindow(self, test_id)
        return self.test_window

    def report_bug(self, parent=None, error_text='', error_key='', guide='',
                   title=None, fp_text=None):
        """``error_key``: the text key of the message in the tool (its
        English template makes the public title and the fingerprint - never
        pass text with project data as ``title`` or ``fp_text``)."""
        return BugWindow(self, parent, error_text, error_key, guide, title,
                         fp_text)

    def _maybe_news(self):
        seen = self.cfg_get('news_seen', None)
        if seen == self.version or self.summary is None:
            return None
        first = not isinstance(seen, str)
        self.cfg_set('news_seen', self.version)
        fresh = [x for x in self.untested()
                 if first or vkey(x['since']) > vkey(seen)]
        return NewsWindow(self, fresh) if fresh else None

    # -- sending ------------------------------------------------------------------

    def send(self, payload, parent, on_ok=None):
        def done(rid, err):
            try:
                par = parent if parent.winfo_exists() else self.root
            except tk.TclError:
                par = self.root
            if err is None:
                self.log.add(f'report sent: {rid}')
                messagebox.showinfo(self.t('fb.title'),
                                    self.t('fb.sent', id=rid), parent=par)
                self.refresh(on_ok)
                return
            code = getattr(err, 'code', 'offline')
            detail = getattr(err, 'detail', str(err))
            self.log.add(f'report not sent: {code}')
            key = 'fb.err.' + (code if code in ('rate', 'closed', 'too_large',
                                                'offline') else 'invalid')
            if messagebox.askyesno(self.t('fb.title'),
                                   self.t(key, detail=detail) + NL + NL
                                   + self.t('fb.savefile'), parent=par):
                self.save_file(payload, par)
        self._bg(lambda: foxfeedback.submit(payload, base=self.base), done)

    def save_file(self, payload, parent):
        path = filedialog.asksaveasfilename(
            parent=parent, defaultextension='.txt',
            initialfile=f"{payload.get('kind')}_report.txt",
            filetypes=[('Text', '*.txt')])
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(foxfeedback.preview(payload))
            except OSError as e:
                messagebox.showerror(self.t('fb.title'), str(e),
                                     parent=parent)

    def preview(self, parent, payload, on_send):
        t = self.t
        win = tk.Toplevel(parent)
        win.title(t('fb.preview.title'))
        win.geometry('760x560')
        win.transient(parent)
        f = ttk.Frame(win, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=t('fb.preview.head'),
                  style=_style('Brand.TLabel')).pack(anchor='w')
        ttk.Label(f, text=t('fb.preview.hint'), style=_style('Muted.TLabel'),
                  wraplength=720, justify='left').pack(anchor='w',
                                                       pady=(2, 8))
        btns = ttk.Frame(f)
        btns.pack(side='bottom', fill='x', pady=(8, 0))
        box = ttk.Frame(f)
        box.pack(fill='both', expand=True)
        txt = tk.Text(box, wrap='none', font=('Consolas', 9))
        sb = ttk.Scrollbar(box, orient='vertical', command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        txt.pack(fill='both', expand=True)
        txt.insert('1.0', foxfeedback.preview(payload))
        txt.configure(state='disabled')

        def go():
            win.destroy()
            on_send()
        ttk.Button(btns, text=t('cancel'), command=win.destroy
                   ).pack(side='right')
        ttk.Button(btns, text=t('fb.send'), style=_style('Accent.TButton'),
                   command=go).pack(side='right', padx=6)
        win.grab_set()
        return win


# ---------------------------------------------------------------------------
# windows

def _needs_run(test):
    # 'needs_game' is the Quest Creator's older name for the same thing
    return bool(test.get('needs_run') or test.get('needs_game'))


class TestWindow:
    def __init__(self, fb, test_id=None):
        self.fb, t = fb, fb.t
        self.test = None
        self.session = None
        self.checks = []
        self.items = []
        self.win = tk.Toplevel(fb.root)
        self.win.title(t('test.title'))
        self.win.geometry('1100x700')
        self.win.minsize(880, 540)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text=t('test.head'), style=_style('Brand.TLabel')
                  ).pack(anchor='w')
        ttk.Label(outer, text=t('test.sub'), style=_style('Muted.TLabel'),
                  wraplength=1040, justify='left').pack(anchor='w',
                                                        pady=(2, 8))
        body = ttk.Frame(outer)
        body.pack(fill='both', expand=True)
        left = ttk.Frame(body, width=400)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        self.lst = tk.Listbox(left, activestyle='none', exportselection=False)
        self.lst.pack(fill='both', expand=True)
        self.lst.bind('<<ListboxSelect>>', lambda e: self._pick())
        self.server_lbl = ttk.Label(left, text='', style=_style('Muted.TLabel'),
                                    wraplength=390, justify='left')
        self.server_lbl.pack(anchor='w', pady=(6, 0))
        self.right = ttk.Frame(body, padding=(14, 0, 0, 0))
        self.right.pack(side='left', fill='both', expand=True)
        self._fill_list()
        fb.refresh(self._fill_list)
        if test_id:
            self.select(test_id)
        elif self.items:
            self.lst.selection_set(0)
            self._pick()

    def _running(self):
        return self.session is not None and self.session.state in (
            'waiting', 'running')

    def close(self):
        if self._running() and not messagebox.askyesno(
                self.fb.t('test.title'), self.fb.t('test.close.q'),
                parent=self.win):
            return
        self.fb.test_window = None
        self.win.destroy()

    def _fill_list(self):
        fb = self.fb
        try:
            keep = self.test['id'] if self.test else None
            self.lst.delete(0, 'end')
        except tk.TclError:
            return
        self.items = [x for x in fb.tests
                      if vkey(x.get('since', '0')) <= vkey(fb.version)
                      and (fb.can_report(x['id'])
                           or fb.state(x['id']) == 'closed')]
        order = {'failed': 0, 'open': 1, 'unknown': 1, 'confirmed': 2,
                 'closed': 3}
        self.items.sort(key=lambda x: (order.get(fb.state(x['id']), 1),
                                       tuple(-v for v in vkey(x['since']))))
        colours = {'confirmed': COLORS['ok'], 'failed': COLORS['err'],
                   'closed': COLORS['mut']}
        for i, x in enumerate(self.items):
            st = fb.state(x['id'])
            mark = {'confirmed': '✓', 'failed': '!', 'closed': '-'
                    }.get(st, '○')
            ok, bad = fb.counts(x['id'])
            need = max(CONFIRM_NEEDED, ok)
            tail = f'  ({ok}/{need})' if st in ('open', 'confirmed') and ok                 else (f'  ({bad} x !)' if bad else '')
            self.lst.insert('end', f' {mark}  {fb.loc(x["title"])}{tail}')
            if st in colours:
                self.lst.itemconfigure(i, foreground=colours[st])
        text = fb.t('test.server.ok' if fb.summary is not None
                    else 'test.server.off')
        if not self.items:
            text = fb.t('test.none') + NL + text
        self.server_lbl.configure(text=text)
        if keep:
            self.select(keep, rebuild=False)

    def select(self, test_id, rebuild=True):
        for i, x in enumerate(self.items):
            if x['id'] == test_id:
                self.lst.selection_clear(0, 'end')
                self.lst.selection_set(i)
                self.lst.see(i)
                if rebuild:
                    self._pick()
                return

    def _pick(self):
        sel = self.lst.curselection()
        if not sel:
            return
        x = self.items[sel[0]]
        if self.test is x:
            return
        if self._running():
            messagebox.showinfo(self.fb.t('test.title'),
                                self.fb.t('test.busy'), parent=self.win)
            self.select(self.test['id'], rebuild=False)
            return
        self.test, self.session = x, None
        self._build(x)

    def _build(self, x):
        fb, t = self.fb, self.fb.t
        for w in self.right.winfo_children():
            w.destroy()
        r = self.right
        ok, bad = fb.counts(x['id'])
        ttk.Label(r, text=fb.loc(x['title']), style=_style('Brand.TLabel'),
                  wraplength=640, justify='left').pack(anchor='w')
        ttk.Label(r, text=t('test.state.' + fb.state(x['id']),
                            since=x.get('since', '?'), ok=ok, bad=bad),
                  style=_style('Muted.TLabel')).pack(anchor='w', pady=(2, 6))
        self._bar(r, x)
        ttk.Label(r, text=fb.loc(x.get('why')), wraplength=640,
                  justify='left').pack(anchor='w', pady=(0, 8))
        bottom = ttk.Frame(r)
        bottom.pack(side='bottom', fill='x')
        ttk.Label(r, text=t('test.steps'), style=_style('Brand.TLabel')
                  ).pack(anchor='w')
        self.checks = []
        steps = x.get('steps') or []
        if isinstance(steps, dict):
            steps = steps.get(fb.lang) or steps.get('en') or []
        for i, step in enumerate(steps):
            v = tk.BooleanVar(value=False)
            row = ttk.Frame(r)
            row.pack(fill='x', pady=1)
            ttk.Checkbutton(row, variable=v, command=lambda i=i, v=v:
                            fb.log.add(f'test {x["id"]} step {i + 1} '
                                       + ('done' if v.get() else 'undone'))
                            ).pack(side='left', anchor='n')
            ttk.Label(row, text=f'{i + 1}. {step}', wraplength=600,
                      justify='left').pack(side='left', anchor='w')
            self.checks.append(v)
        ttk.Label(r, text=t('test.expect'), style=_style('Brand.TLabel')
                  ).pack(anchor='w', pady=(10, 0))
        ttk.Label(r, text=fb.loc(x.get('expect')), wraplength=640,
                  justify='left', foreground=COLORS['gold']
                  ).pack(anchor='w')
        self.run_btn = None
        if _needs_run(x) and fb.launcher is not None:
            g = ttk.Frame(r)
            g.pack(fill='x', pady=(12, 0))
            progs = fb.launcher.programs()
            last = fb.cfg_get('test_program', None)
            self.prog = tk.StringVar(value=last if last in progs
                                     else (progs[0] if progs else ''))
            self.run_btn = ttk.Button(g, text=t('test.run'),
                                      style=_style('Accent.TButton'),
                                      command=self.start_run)
            self.run_btn.pack(side='left')
            if len(progs) > 1:
                ttk.Combobox(g, textvariable=self.prog, values=progs,
                             width=26, state='readonly').pack(side='left',
                                                              padx=6)
            if not progs:
                self.run_btn.state(['disabled'])
            self.run_lbl = ttk.Label(g, text=t('test.run.hint'),
                                     style=_style('Muted.TLabel'),
                                     wraplength=300, justify='left')
            self.run_lbl.pack(side='left', padx=8)   # 380 ran past the right edge of the 1100 px window
        ttk.Label(bottom, text=t('test.note'), style=_style('Muted.TLabel')
                  ).pack(anchor='w', pady=(8, 0))
        self.note = tk.Text(bottom, height=3, wrap='word')
        self.note.pack(fill='x')
        btns = ttk.Frame(bottom)
        btns.pack(fill='x', pady=(8, 0))
        ttk.Button(btns, text='✓ ' + t('test.pass'),
                   command=lambda: self.result(True)).pack(side='left')
        ttk.Button(btns, text='✗ ' + t('test.fail'),
                   command=lambda: self.result(False)).pack(side='left',
                                                           padx=8)
        ttk.Button(btns, text=t('close'), command=self.close
                   ).pack(side='right')

    def _bar(self, parent, x):
        """How far the test is: one bar tick per confirmation (design 11a)."""
        fb, t = self.fb, self.fb.t
        ok, bad = fb.counts(x['id'])
        state = fb.state(x['id'])
        need = max(CONFIRM_NEEDED, ok)
        if state == 'failed':
            key, colour, filled = 'test.bar.failed', COLORS['err'], need
        elif state in ('confirmed', 'closed'):
            key, colour, filled = 'test.bar.confirmed', COLORS['ok'], need
        elif ok:
            key, colour, filled = 'test.bar.some', COLORS['gold'], ok
        else:
            key, colour, filled = 'test.bar.none', COLORS['mut'], 0
        box = ttk.Frame(parent)
        box.pack(anchor='w', fill='x', pady=(2, 6))
        w, h, gap = 150, 12, 4
        c = tk.Canvas(box, width=w, height=h, highlightthickness=0,
                      background=COLORS['bg'], bd=0)
        c.pack(side='left')
        step = (w - gap * (need - 1)) / need if need else w
        for i in range(need):
            x0 = i * (step + gap)
            c.create_rectangle(x0, 0, x0 + step, h, width=0,
                               fill=colour if i < filled else '#2a241c')
        ttk.Label(box, text=t(key, ok=ok, bad=bad, need=need),
                  style=_style('Muted.TLabel'), wraplength=470,
                  justify='left').pack(side='left', padx=8)
        mine = fb.mine(x['id'])
        if mine:
            ttk.Label(parent, text=t('test.mine.' + mine),
                      foreground=COLORS['ok'] if mine == 'pass'
                      else COLORS['err']).pack(anchor='w', pady=(0, 4))

    def start_run(self):
        fb = self.fb
        name = self.prog.get()
        fb.cfg_set('test_program', name)
        earlier = self.session.text() if self.session is not None else ''
        session = fb.launcher.session(name)
        running = session._running()
        if running:
            messagebox.showwarning(fb.t('test.title'), fb.t('test.run.busy'),
                                   parent=self.win)
            return
        if earlier:
            session.add(earlier)
            session.add('== started again')
        self.session = session
        fb.log.add(f'test {self.test["id"]}: started {name}')
        if not session.start():
            self._run_state('failed')
            return
        self.run_btn.state(['disabled'])
        self._watch(session, None)

    def _watch(self, session, shown):
        if session is not self.session:
            return
        if session.state != shown:
            self._run_state(session.state)
        if session.state in ('waiting', 'running'):
            try:
                self.win.after(500, lambda: self._watch(session,
                                                        session.state))
            except tk.TclError:
                pass

    def _run_state(self, state):
        try:
            self.run_lbl.configure(text=self.fb.t('test.run.' + state))
            if state in ('ended', 'failed'):
                self.run_btn.state(['!disabled'])
                self.win.lift()
        except (tk.TclError, AttributeError):
            pass
        self.fb.log.add(f'run: {state}')

    def result(self, passed):
        fb, t, x = self.fb, self.fb.t, self.test
        if x is None:
            return
        if not fb.can_report(x['id']):
            messagebox.showinfo(t('test.title'), t('test.notopen'),
                                parent=self.win)
            return
        open_steps = sum(1 for v in self.checks if not v.get())
        if passed and open_steps and not messagebox.askyesno(
                t('test.title'), t('test.steps.open', n=open_steps),
                parent=self.win):
            return
        if self._running() and not messagebox.askyesno(
                t('test.title'), t('test.stillrun.q'), parent=self.win):
            return
        if _needs_run(x) and fb.launcher is not None and \
                self.session is None and not messagebox.askyesno(
                    t('test.title'), t('test.norun.q'), parent=self.win):
            return
        note = self.note.get('1.0', 'end').strip()
        if not passed and not note:
            messagebox.showinfo(t('test.title'), t('test.fail.note'),
                                parent=self.win)
            self.note.focus_set()
            return
        fb.log.add(f'test {x["id"]}: result {"pass" if passed else "fail"}, '
                   f'{len(self.checks) - open_steps}/{len(self.checks)} '
                   'steps ticked')
        payload = foxfeedback.test_payload(
            fb.tool, fb.version, fb.client_id(), fb.lang, x['id'], passed,
            message=note, log=fb.log.text(),
            game_log=self.session.text() if self.session else '')
        def sent():
            fb.remember(x['id'], passed)

            def shown():
                self._fill_list()
                self.test = None          # rebuild the panel with the new count
                self.select(x['id'])
            fb.refresh(shown)
        fb.preview(self.win, payload, lambda: fb.send(
            payload, self.win, on_ok=sent))


class BugWindow:
    def __init__(self, fb, parent=None, error_text='', error_key='',
                 guide='', title=None, fp_text=None):
        self.fb, t = fb, fb.t
        self.error_text, self.error_key = str(error_text or ''), error_key
        self.guide, self.fp_text = guide, fp_text
        self.title = title or error_key or 'general report'
        parent = parent or fb.root
        self.win = tk.Toplevel(parent)
        self.win.title(t('bug.title'))
        self.win.geometry('640x520')
        self.win.transient(parent)
        f = ttk.Frame(self.win, padding=14)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=t('bug.head'), style=_style('Brand.TLabel')
                  ).pack(anchor='w')
        ttk.Label(f, text=t('bug.sub'), style=_style('Muted.TLabel'),
                  wraplength=600, justify='left').pack(anchor='w', pady=(2, 8))
        known = [i for i in fb.issues() if i.get('status') != 'fixed'][:4]
        if known:
            ttk.Label(f, text=t('bug.known'), style=_style('Brand.TLabel')
                      ).pack(anchor='w')
            for i in known:
                ttk.Label(f, text=f"  {i.get('title')}  ({i.get('version')}, "
                                  f"{i.get('count')}x)",
                          style=_style('Muted.TLabel'), wraplength=600,
                          justify='left').pack(anchor='w')
        if self.error_text:
            ttk.Label(f, text=t('bug.error'), style=_style('Brand.TLabel')
                      ).pack(anchor='w', pady=(8, 0))
            ttk.Label(f, text=self.error_text[:600], wraplength=600,
                      justify='left').pack(anchor='w')
        if guide and fb.open_guide:
            ttk.Button(f, text=t('bug.guide'),
                       command=lambda: fb.open_guide(guide)
                       ).pack(anchor='w', pady=(6, 0))
        btns = ttk.Frame(f)
        btns.pack(side='bottom', fill='x', pady=(8, 0))
        ttk.Label(f, text=t('bug.what'), style=_style('Brand.TLabel')
                  ).pack(anchor='w', pady=(10, 0))
        self.note = tk.Text(f, height=6, wrap='word')
        self.note.pack(fill='both', expand=True)
        ttk.Button(btns, text=t('cancel'), command=self.win.destroy
                   ).pack(side='right')
        ttk.Button(btns, text=t('bug.next'), style=_style('Accent.TButton'),
                   command=self.next).pack(side='right', padx=6)
        self.note.focus_set()

    def next(self):
        fb = self.fb
        note = self.note.get('1.0', 'end').strip()
        if not note and not self.error_text:
            messagebox.showinfo(fb.t('bug.title'), fb.t('bug.empty'),
                                parent=self.win)
            return
        payload = foxfeedback.bug_payload(
            fb.tool, fb.version, fb.client_id(), fb.lang, self.title,
            error_key=self.error_key, error_text=self.error_text,
            guide_ref=self.guide, fp_text=self.fp_text,
            message=(note + NL + NL + self.error_text[:1500]).strip(),
            log=fb.log.text())

        def send():
            fb.send(payload, fb.root)
            self.win.destroy()
        fb.preview(self.win, payload, send)


class IssuesWindow:
    def __init__(self, fb):
        self.fb, t = fb, fb.t
        self.win = tk.Toplevel(fb.root)
        self.win.title(t('menu.issues'))
        self.win.geometry('720x460')
        self.win.transient(fb.root)
        f = ttk.Frame(self.win, padding=14)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=t('issues.head', tool=fb.tool_name),
                  style=_style('Brand.TLabel')).pack(anchor='w')
        self.sub = ttk.Label(f, text=t('issues.loading'),
                             style=_style('Muted.TLabel'), wraplength=680,
                             justify='left')
        self.sub.pack(anchor='w', pady=(2, 8))
        btns = ttk.Frame(f)
        btns.pack(side='bottom', fill='x', pady=(8, 0))
        cols = ('status', 'title', 'version', 'count', 'fixed')
        self.tree = ttk.Treeview(f, columns=cols, show='headings')
        for c, w in zip(cols, (90, 360, 70, 60, 80)):
            self.tree.heading(c, text=t('issues.col.' + c))
            self.tree.column(c, width=w, anchor='w', stretch=c == 'title')
        self.tree.pack(fill='both', expand=True)
        ttk.Button(btns, text=t('close'), command=self.win.destroy
                   ).pack(side='right')
        ttk.Button(btns, text=t('menu.bug'),
                   command=lambda: fb.report_bug(self.win)).pack(side='left')
        ttk.Button(btns, text=t('menu.test'),
                   command=lambda: fb.show_tests()).pack(side='left', padx=6)
        self.fill()
        fb.refresh(self.fill)

    def fill(self):
        fb = self.fb
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            return
        if fb.summary is None:
            self.sub.configure(text=fb.t('issues.offline'))
            return
        rows = fb.issues()
        self.sub.configure(text=fb.t('issues.sub', n=len(rows),
                                     tests=len(fb.untested())))
        for i in rows:
            self.tree.insert('', 'end', values=(
                fb.t('issues.status.' + str(i.get('status'))),
                i.get('title'), i.get('version'), i.get('count'),
                i.get('fixed_in') or ''))


class NewsWindow:
    def __init__(self, fb, tests):
        self.fb, t = fb, fb.t
        self.win = tk.Toplevel(fb.root)
        self.win.title(t('news.title', version=fb.version))
        self.win.geometry('640x420')
        self.win.transient(fb.root)
        f = ttk.Frame(self.win, padding=16)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=t('news.head'), style=_style('Brand.TLabel')
                  ).pack(anchor='w')
        ttk.Label(f, text=t('news.sub'), style=_style('Muted.TLabel'),
                  wraplength=600, justify='left').pack(anchor='w',
                                                       pady=(2, 10))
        ttk.Button(f, text=t('close'), command=self.win.destroy
                   ).pack(side='bottom', anchor='e', pady=(8, 0))
        for x in tests[:6]:
            row = ttk.Frame(f)
            row.pack(fill='x', pady=3)
            ttk.Button(row, text=t('news.test'),
                       style=_style('Accent.TButton'),
                       command=lambda i=x['id']: (self.win.destroy(),
                                                  fb.show_tests(i))
                       ).pack(side='right')
            ttk.Label(row, text=f"{fb.loc(x['title'])}  ({x.get('since')})",
                      wraplength=440, justify='left').pack(side='left')
