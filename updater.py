"""Update check and self-update from GitHub Releases.

On start (and via Help > Check for updates) the tool asks the GitHub API for
the latest release of the repository. A newer version shows a small window
with the release notes: update now, later, or skip this version.

Updating (only the exe, a source checkout just opens the release page):

1. Download the asset ``TW1_EcoTool.exe`` of that release next to the
   running exe as ``TW1_EcoTool.exe.new``.
2. Check its SHA-256 against the ``digest`` GitHub stores for the asset.
   Without a digest, or when it does not match, nothing is installed.
3. A small batch file waits until the process of the tool has ended (by its
   process id: Windows lets a running exe be renamed, so waiting for the file
   alone swapped while the old tool still ran, measured 2026-09-16), then
   moves the old exe to ``.old``, the new one in its place and starts it.
   ``.old`` is removed on the next start.

Transcribed from TW1ParEditor/updater.py (PY_TOOL_DESIGN.md
section 9); only REPO, ASSET and the user agent differ.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

from version import VERSION

REPO = 'MedievalDev/TW1_EcoTool'
API_LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
LATEST_PAGE = f'https://github.com/{REPO}/releases/latest'
ASSET = 'TW1_EcoTool.exe'
LATEST_DOWNLOAD = f'https://github.com/{REPO}/releases/latest/download/{ASSET}'
USER_AGENT = f'TW1EcoTool/{VERSION}'
TIMEOUT = 6


def parse_version(text):
    """'v1.5.0' / '1.5' -> (1, 5, 0); None when it is not a version."""
    m = re.fullmatch(r'v?(\d+)\.(\d+)(?:\.(\d+))?', (text or '').strip())
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(tag, current=VERSION):
    new, cur = parse_version(tag), parse_version(current)
    return bool(new and cur and new > cur)


def release_info(payload):
    """What the tool needs from a GitHub release JSON."""
    tag = payload.get('tag_name') or ''
    asset = next((a for a in payload.get('assets') or []
                  if a.get('name') == ASSET), None) or {}
    digest = asset.get('digest') or ''
    return {'version': tag.lstrip('v'), 'tag': tag,
            'page': payload.get('html_url') or LATEST_PAGE,
            'notes': payload.get('body') or '',
            'url': asset.get('browser_download_url'),
            'size': asset.get('size') or 0,
            'sha256': digest[7:].lower() if digest.startswith('sha256:')
            else None,
            'draft': bool(payload.get('draft')),
            'prerelease': bool(payload.get('prerelease'))}


def fetch_latest(timeout=TIMEOUT):
    import urllib.request          # here, so a broken build still starts
    req = urllib.request.Request(API_LATEST, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return release_info(json.loads(r.read().decode('utf-8')))


def check_async(callback):
    """Ask GitHub in a thread; callback(info or None, error or None) runs in
    that thread, the caller hands it to the Tk thread."""
    def work():
        try:
            callback(fetch_latest(), None)
        except Exception as e:           # offline, rate limit, bad JSON
            callback(None, e)
    threading.Thread(target=work, daemon=True).start()


def frozen_exe():
    """Path of the running exe, None when running from source."""
    if getattr(sys, 'frozen', False):
        return os.path.abspath(sys.executable)
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def download(info, dest, progress=None, opener=None):
    """Download the exe to ``dest`` and verify it. Returns ``dest``; raises
    ValueError on a missing digest or a mismatch (the file is removed)."""
    if not info.get('url'):
        raise ValueError('release has no exe')
    if not info.get('sha256'):
        raise ValueError('release has no SHA-256 digest')
    import urllib.request
    opener = opener or urllib.request.urlopen
    tmp = dest + '.part'
    req = urllib.request.Request(info['url'], headers={'User-Agent': USER_AGENT})
    total = info.get('size') or 0
    done = 0
    with opener(req, timeout=60) as r, open(tmp, 'wb') as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    got = sha256_file(tmp)
    if got != info['sha256']:
        os.remove(tmp)
        raise ValueError(f'SHA-256 mismatch: {got} != {info["sha256"]}')
    os.replace(tmp, dest)
    return dest


def swap_script(exe, new, pid):
    """Batch that waits until process ``pid`` has ended (at most about two
    minutes), keeps the old exe as .old, moves the new one in place and
    starts it."""
    old = exe + '.old'
    return '\r\n'.join([
        '@echo off',
        'for /l %%i in (1,1,120) do (',
        f'  "%SystemRoot%\\System32\\tasklist.exe" /FI "PID eq {pid}" /NH'
        f' | "%SystemRoot%\\System32\\find.exe" " {pid} " >nul'
        ' || goto closed',
        '  ping -n 2 127.0.0.1 >nul',
        ')',
        'exit /b 1',
        ':closed',
        'for /l %%i in (1,1,20) do (',
        f'  move /y "{exe}" "{old}" >nul 2>&1 && goto moved',
        '  ping -n 2 127.0.0.1 >nul',
        ')',
        'exit /b 1',
        ':moved',
        f'move /y "{new}" "{exe}" >nul 2>&1 || move /y "{old}" "{exe}" >nul',
        f'start "" "{exe}"',
        '(goto) 2>nul & del "%~f0"',
        ''])


def start_swap(exe, new, pid=None):
    """Write the batch into the temp folder and start it detached."""
    fd, path = tempfile.mkstemp(prefix='modmanager_update_', suffix='.bat')
    with os.fdopen(fd, 'w', encoding='mbcs' if os.name == 'nt' else 'utf-8',
                   newline='') as f:
        f.write(swap_script(exe, new, pid or os.getpid()))
    flags = 0
    if os.name == 'nt':
        # CREATE_NO_WINDOW alone: with DETACHED_PROCESS the batch had no
        # console and tasklist/find never ran (measured 2026-09-16)
        flags = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                 | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    subprocess.Popen(['cmd', '/c', path], creationflags=flags,
                     close_fds=True, cwd=os.path.dirname(exe))
    return path


def cleanup_old(exe=None):
    """Remove the ``.old`` exe and a stale ``.new``/``.part`` of an earlier
    update (called on start)."""
    exe = exe or frozen_exe()
    if not exe:
        return
    for suffix in ('.old', '.new.part'):
        try:
            os.remove(exe + suffix)
        except OSError:
            pass
