"""Files dropped from Explorer onto the window (WM_DROPFILES).

Plain ctypes, no extra package - the same technique as the TW1 Mod Manager and the TW1 WD Packer
(mod_manager.enable_drop). Tk gives every widget its own HWND on Windows, so
each one is registered with DragAcceptFiles and its window procedure is
subclassed. The inspector rebuilds its widgets all the time, so ``refresh``
registers windows that appeared since; every HWND only once.

Measured on the Mod Manager: registering a window twice freed the first
callback while the window still pointed at it (0xc000001d in the old
procedure). Callbacks are therefore kept for the life of the process.

A window procedure runs inside Windows' own message handling, so it does no
Tk call at all: it only puts the paths into an inbox, and Tk empties that
inbox from its own event loop (``POLL_MS``). Calling Tk from inside the
procedure ended the process without a word on a real drop from Explorer
(19.09.2026); the simulated message never hit it because it came from the
Tk thread itself.
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4
POLL_MS = 120                   # how often Tk looks into the inbox
_KEEP = []                      # every callback ever handed to Windows
_state = {}                     # id(root) -> {'hwnds', 'olds', 'mine', 'cb', 'inbox'}
LOG = os.environ.get('ECOTOOL_DROPLOG')


def log(text):
    """Only when ECOTOOL_DROPLOG names a file - for tracking a drop down."""
    if not LOG:
        return
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%H:%M:%S")} {text}' + chr(10))
    except OSError:
        pass

if sys.platform == 'win32':
    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
    _user32, _shell32 = ctypes.windll.user32, ctypes.windll.shell32
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    _user32.CallWindowProcW.restype = ctypes.c_ssize_t
    _user32.CallWindowProcW.argtypes = (ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM)
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = (wintypes.HWND,)
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR,
                                        wintypes.UINT)
    _shell32.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
    _shell32.DragFinish.argtypes = (wintypes.HANDLE,)
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)


def _files(hdrop):
    n = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    out = []
    for i in range(n):
        ln = _shell32.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(ln + 1)
        _shell32.DragQueryFileW(hdrop, i, buf, ln + 1)
        out.append(buf.value)
    return out


def _register(root, st, hwnd):
    if not hwnd:
        return
    mine = st['mine'].get(hwnd)
    if mine is not None and _user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC) == mine:
        return                   # ours already (a reused HWND number is not)
    st['hwnds'].add(hwnd)
    olds = st['olds']

    def proc(h, msg, wp, lp):
        if msg == WM_DROPFILES:
            try:
                files = _files(wp)
                _shell32.DragFinish(wp)
                st['inbox'].append(files)     # no Tk call inside a window procedure
                log(f'drop {len(files)} path(s) on {h}')
            except Exception as e:            # an error must not leave the procedure
                log(f'drop failed: {type(e).__name__}: {e}')
            return 0
        old = olds.get(h)
        if old is None:                       # not ours (any more)
            return _user32.DefWindowProcW(h, msg, wp, lp)
        return _user32.CallWindowProcW(old, h, msg, wp, lp)
    cb = _WNDPROC(proc)
    _KEEP.append(cb)
    _shell32.DragAcceptFiles(hwnd, True)
    addr = ctypes.cast(cb, ctypes.c_void_p).value
    olds[hwnd] = _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, addr)
    st['mine'][hwnd] = addr


def refresh(root):
    """Register windows created since the last call. Returns how many HWNDs
    accept drops now (0 off Windows or before enable)."""
    st = _state.get(id(root))
    if st is None or sys.platform != 'win32':
        return 0
    try:
        root.update_idletasks()
        top = _user32.GetParent(root.winfo_id()) or root.winfo_id()
    except Exception:
        return len(st['hwnds'])
    found = [top]
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(h, _lp):
        found.append(h)
        return True
    cb = enum_proc(collect)
    _user32.EnumChildWindows(top, cb, 0)
    for h in found:
        _register(root, st, h)
    return len(st['hwnds'])


def _pump(root, st):
    """Tk's own event loop: hand over what the window procedures collected."""
    while st['inbox']:
        files = st['inbox'].pop(0)
        try:
            st['cb'](files)
        except Exception as e:
            log(f'callback failed: {type(e).__name__}: {e}')
            raise
    try:
        root.after(POLL_MS, _pump, root, st)
    except Exception:
        pass                     # window gone


def enable(root, callback):
    """Drops anywhere on ``root`` call ``callback(paths)`` on the Tk thread.
    Calling it again only changes the callback."""
    if sys.platform != 'win32':
        return 0
    first = id(root) not in _state
    st = _state.setdefault(id(root), {'hwnds': set(), 'olds': {}, 'mine': {},
                                      'cb': callback, 'inbox': []})
    st['cb'] = callback
    n = refresh(root)
    if first:
        root.after(POLL_MS, _pump, root, st)
    log(f'enable: {n} window(s) accept drops')
    return n
