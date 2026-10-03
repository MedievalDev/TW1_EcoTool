"""Test results and bug reports of the TW1 tools (standard library only, so
every Python tool can take this one file along).

The server side is specified in FEEDBACK_ENDPUNKT_SPEZIFIKATION.md (Marco's
desktop, 2026-09-19) and lives at https://alchemy-fox.de/game/_feedback/:

- ``POST submit``       no key - a test result or a bug report, JSON
- ``GET summary.json``  no key - state of every test, known issues

There is NO key in here and there must never be one: everything a tool does
works without. Reading the reports is the maintainer's side.

Rules this module keeps for every tool:
- nothing is sent silently; the tool shows ``preview(payload)`` first
- user names in paths become ``<user>`` (``scrub``)
- logs are cut to the size the server takes, the END is kept (``clip``)
- a bug title is made by the tool from the error, never typed by the user
  (it becomes public in the known issues at once)
"""

import getpass
import hashlib
import json
import os
import platform
import re
import threading
import time
import uuid

BASE = 'https://alchemy-fox.de/game/_feedback/'
SCHEMA = 1
MAX_LOG = 120000
MAX_MESSAGE = 4000
MAX_TITLE = 120
MAX_BODY = 280 * 1024           # the server takes 300 KB, counted in BYTES
TIMEOUT = 15


# -- text ---------------------------------------------------------------------

# separators may come doubled: str(OSError) and JSON write C:\\Users\\X
_PROFILE = re.compile(
    r'(?i)([/\\]+(?:users|documents and settings|benutzer)[/\\]+)'
    r'[^/\\\r\n"<>|;:*?\']+')
# \\SERVER\share: both say who the user is (company, home folder)
_UNC = re.compile(r'(?<![\w\\/:])(?:\\\\|//)[^\\/\s"\'<>|]+[\\/]+'
                  r'[^\\/\s"\'<>|]+')
# "OneDrive - Firma GmbH": the company name
_ORG = re.compile(r'(?i)(onedrive|sharepoint) - [^/\\\r\n"<>|:*?]+')
_MAIL = re.compile(r'(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+')
_CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\ud800-\udfff]+')
_COMMON = {'user', 'users', 'admin', 'administrator', 'test', 'tester',
           'owner', 'guest', 'gast', 'benutzer', 'besitzer', 'default',
           'public', 'home', 'mail', 'pc', 'desktop', 'laptop', 'windows',
           'player', 'spieler', 'gamer', 'workgroup'}
_PLACEHOLDER = ('<user>', '<mail>', '<server>', '<org>', '<path>')


def _own_names():
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:                  # no user name in this environment
        pass
    home = os.path.expanduser('~')
    if home and home != '~':
        names.add(os.path.basename(home.rstrip('/' + chr(92))))
    names.add(os.environ.get('COMPUTERNAME') or '')
    names.add(os.environ.get('USERDOMAIN') or '')
    # default account names say nothing about the person but are everyday
    # words: replacing them would wreck the log ("test", "user", "admin")
    return sorted((n for n in names if n and len(n) > 2
                   and n.lower() not in _COMMON), key=len, reverse=True)


def scrub(text, names=True):
    """``text`` without the user: the profile folder in every path, e-mail
    addresses, and (``names``) the user and machine name as WHOLE words - a
    user called "mark" must not turn "marker" into "<user>er". Control
    characters and U+FFFD go too (a log in another code page would triple
    in size and the server counts bytes)."""
    text = _CONTROL.sub(' ', str(text or ''))
    text = _PROFILE.sub(lambda m: m.group(1) + '<user>', text)
    text = _UNC.sub(lambda m: m.group(0)[:2] + '<server>' + chr(92)
                    + '<share>', text)
    text = _ORG.sub(lambda m: m.group(1) + ' - <org>', text)
    text = _MAIL.sub('<mail>', text)
    if names:
        for name in _own_names():
            # never inside a placeholder we just wrote: a user called
            # "User" or "mail" must not turn <user> into <<user>>
            text = re.sub(r'(?<![A-Za-z0-9_<])' + re.escape(name)
                          + r'(?![A-Za-z0-9_>])', '<user>', text, flags=re.I)
    return text


def clip(text, limit=MAX_LOG):
    """At most ``limit`` characters, the end kept (the newest lines)."""
    text = str(text or '')
    if len(text) <= limit:
        return text
    if limit < 40:
        return ''
    head = '[... cut ...]' + chr(10)
    return head + text[-(limit - len(head)):]


def fingerprint(tool, error_key, message):
    """The same bug gives the same 40 hex characters on every PC: numbers,
    paths and quoted names are taken out of the message first."""
    msg = scrub(message, names=False).lower()
    # a path ends at a quote, a line end or ": " / ", " - so "x.wd: [Errno
    # 13] ..." and "x.wd: [Errno 28] ..." stay two different bugs
    msg = re.sub(r'(?:[a-z]:[/\\]|[/\\]{2})(?:(?![:,] )[^"\'\r\n])*',
                 '<path>', msg)
    msg = re.sub(r'"[^"]*"|\'[^\']*\'', '<s>', msg)
    msg = re.sub(r'\d+', '#', msg)
    msg = re.sub(r'\s+', ' ', msg).strip()[:300]
    raw = f'{tool}|{error_key or ""}|{msg}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def os_name():
    try:
        return f'{platform.system()} {platform.release()} ' \
               f'{platform.version()}'[:80]
    except Exception:
        return 'unknown'


def new_client_id():
    """Random per installation, says nothing about the person."""
    return uuid.uuid4().hex


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# -- session log -----------------------------------------------------------------

class SessionLog:
    """What the tool did in this session, newest last; goes along with a
    report. Thread safe, bounded."""

    def __init__(self, limit=400):
        self.limit = limit
        self.lines = []
        self._lock = threading.Lock()

    def add(self, text):
        line = f'{time.strftime("%H:%M:%S")}  {str(text)[:500]}'
        with self._lock:
            self.lines.append(line)
            del self.lines[:-self.limit]

    def text(self):
        with self._lock:
            return chr(10).join(self.lines)


# -- payloads ----------------------------------------------------------------------

def _base(kind, tool, version, client_id, lang, message, log, game_log):
    return {'schema': SCHEMA, 'kind': kind, 'tool': tool, 'version': version,
            'client_id': client_id, 'lang': 'de' if lang == 'de' else 'en',
            'os': os_name(), 'created': now_iso(),
            'message': scrub(message)[:MAX_MESSAGE],
            'log': clip(scrub(log)), 'game_log': clip(scrub(game_log))}


def test_payload(tool, version, client_id, lang, test_id, passed,
                 message='', log='', game_log=''):
    p = _base('test', tool, version, client_id, lang, message, log, game_log)
    p.update(test_id=test_id, result='pass' if passed else 'fail')
    return fit(p)


def bug_payload(tool, version, client_id, lang, title, error_key='',
                error_text='', guide_ref='', message='', log='', game_log='',
                fp_text=None):
    """``title`` comes from the tool (error text in English or the key),
    ``message`` is what the user typed. ``fp_text``: what makes two reports
    the same bug - without project data and not translated (the English
    template of the message); default the error text."""
    p = _base('bug', tool, version, client_id, lang, message, log, game_log)
    key = re.sub(r'[^A-Za-z0-9._-]', '', error_key or '')[:80]
    p.update(error_key=key, title=scrub(title).replace(chr(10), ' ')
             .strip()[:MAX_TITLE] or 'bug',
             fingerprint=fingerprint(tool, key, fp_text if fp_text is not None
                                     else (error_text or title)),
             guide_ref=str(guide_ref or '')[:80])
    return fit(p)


def fit(payload, limit=MAX_BODY):
    """Cut the logs until the JSON body fits ``limit`` BYTES (umlauts,
    quotes and line ends cost more than one byte each)."""
    def size():
        return len(json.dumps(payload, ensure_ascii=False).encode(
            'utf-8', 'replace'))
    for _ in range(20):
        now = size()
        if now <= limit:
            break
        key = max(('log', 'game_log'), key=lambda k: len(payload.get(k) or ''))
        text = payload.get(key) or ''
        if len(text) < 200:
            payload['log'] = payload['game_log'] = ''
            payload['message'] = (payload.get('message') or '')[:1000]
            break
        # bytes per character differ, so shrink by the ratio and look again
        payload[key] = clip(text, int(len(text) * limit / now * 0.9))
    return payload


def preview(payload):
    """Exactly what will be sent, as text for the user to read."""
    short = dict(payload)
    out = []
    for key in ('log', 'game_log'):
        body = short.pop(key, '')
        if body:
            out.append(f'--- {key} ({len(body)} characters) ---')
            out.append(body)
    return json.dumps(short, indent=2, ensure_ascii=False) + chr(10) \
        + chr(10).join(out)


# -- network -------------------------------------------------------------------------

class FeedbackError(Exception):
    """``code``: 'rate', 'closed', 'too_large', 'invalid', 'offline'."""

    def __init__(self, code, detail=''):
        super().__init__(f'{code} {detail}'.strip())
        self.code = code
        self.detail = detail


def _agent(tool, version):
    return f'FoxFeedback/1 ({tool} {version})'


def submit(payload, base=BASE, timeout=TIMEOUT):
    """Send one report. Returns the id the server gave it."""
    import urllib.error
    import urllib.request
    try:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8',
                                                              'replace')
        req = urllib.request.Request(
            base + 'submit', data=body, method='POST',
            headers={'Content-Type': 'application/json; charset=utf-8',
                     'User-Agent': _agent(payload.get('tool'),
                                          payload.get('version'))})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            answer = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read().decode('utf-8'))
        except Exception:
            info = {}
        if not isinstance(info, dict):
            info = {}
        if e.code >= 500 and e.code != 503:
            raise FeedbackError('offline', str(e.code))  # server trouble
        code = info.get('error') or {413: 'too_large', 429: 'rate',
                                     503: 'closed'}.get(e.code, 'invalid')
        raise FeedbackError(str(code), str(info.get('field') or e.code))
    except Exception as e:             # no network, broken answer, ...
        raise FeedbackError('offline', str(e))
    if not isinstance(answer, dict) or not answer.get('ok'):
        raise FeedbackError((answer.get('error') if isinstance(answer, dict)
                             else None) or 'invalid')
    return answer.get('id')


def fetch_summary(tool, version, base=BASE, timeout=TIMEOUT):
    """{'tests': {...}, 'issues': [...], 'accept': bool} of one tool, or
    None when the server cannot be reached."""
    import urllib.request
    try:
        req = urllib.request.Request(
            base + 'summary.json',
            headers={'Cache-Control': 'no-cache',
                     'User-Agent': _agent(tool, version)})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8'))
    except Exception:                  # offline, proxy page, broken JSON
        return None
    if not isinstance(data, dict):
        return None
    tools = data.get('tools')
    mine = tools.get(tool) if isinstance(tools, dict) else None
    if not isinstance(mine, dict):
        mine = {}
    tests = mine.get('tests')
    tests = {str(k): v for k, v in tests.items() if isinstance(v, dict)} \
        if isinstance(tests, dict) else {}
    for rec in tests.values():
        for n in ('pass', 'fail'):
            try:
                rec[n] = int(rec.get(n) or 0)
            except (TypeError, ValueError):
                rec[n] = 0
    issues = mine.get('issues')
    issues = [i for i in issues if isinstance(i, dict)] \
        if isinstance(issues, list) else []
    return {'tests': tests, 'issues': issues,
            'accept': bool(data.get('accept', True)),
            'updated': data.get('updated')}
