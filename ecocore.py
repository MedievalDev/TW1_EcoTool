"""TW1 EcoTool core (no GUI): the game's scripts, the SDK sources, the index between them, export, SDK update.

Facts this rests on (measured 03.10.2026, see STATUS.md):
- The game (v1.7) mounts its .wd archives in a fixed order; the newest copy of a script wins.
- The SDK compiler (cpp + EarthC.exe) is deterministic: the body of a compiled .eco only depends on the source.
- SDK 1.3 reproduces 30 of the 42 shipped scripts byte for byte, its _Scripts_old_1.5_ folder (= SDK 1.2) another 5.
Nothing here writes into an SDK, the EcoAnalysis folder or the game: compiling always happens in a temp copy.
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SEP = chr(92)
INDEX_FORMAT = 2


class Cancelled(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ .eco --

def eco_body(raw):
    """The ECO body of a stored .eco: plain ('ECO\\0...') or the two zlib streams of a loose file."""
    if raw[:4] == b'ECO' + bytes(1):
        return raw
    d = zlib.decompressobj()
    head = d.decompress(raw)
    rest = d.unused_data
    if rest:
        return zlib.decompress(rest)
    return head


def eco_is_debug(body):
    """True when the body carries the debug blob (HasDebug word after the code segment)."""
    try:
        o = 12
        n = struct.unpack_from('<I', body, o)[0]
        o += 4 + n
        for _ in range(2):                                  # data pointers, code pointers
            n = struct.unpack_from('<I', body, o)[0]
            o += 4 + 4 * n
        n = struct.unpack_from('<I', body, o)[0]
        o += 4 + 8 * n                                      # imports
        n = struct.unpack_from('<I', body, o)[0]
        o += 4 + 4 * n                                      # states
        n = struct.unpack_from('<I', body, o)[0]
        o += 4 + 16 * n                                     # commands
        n = struct.unpack_from('<I', body, o)[0]
        o += 4 + 4 * n                                      # events
        n = struct.unpack_from('<I', body, o)[0]
        o += 4 + n                                          # code
        return struct.unpack_from('<I', body, o)[0] != 0
    except struct.error:
        return False


# ------------------------------------------------------------------- WD --

class Archive:
    """Directory of one .wd (the layout of the game's and the packer's archives) and reading of entries."""

    def __init__(self, path, label=None):
        self.path = path
        self.label = label or os.path.basename(path)
        self.entries = {}
        with open(path, 'rb') as f:
            f.seek(-4, 2)
            size = f.tell() + 4
            dir_len = struct.unpack('<I', f.read(4))[0]
            f.seek(size - dir_len)
            table = zlib.decompressobj().decompress(f.read(dir_len))
        off = 8
        count = struct.unpack_from('<H', table, off)[0]
        off += 2
        for _ in range(count):
            nlen = table[off]
            off += 1
            name = table[off:off + nlen].decode('latin-1')
            off += nlen
            flags, foff, clen, rlen = struct.unpack_from('<BIII', table, off)
            off += 13
            if flags & 0x08:
                off += 1 + table[off]
            if flags & 0x10:
                off += 4
            if flags & 0x20:
                off += 16
            self.entries[name.lower()] = (name, flags, foff, clen, rlen)

    def read(self, key):
        name, flags, foff, clen, rlen = self.entries[key.lower()]
        with open(self.path, 'rb') as f:
            f.seek(foff)
            blob = f.read(clen)
        if flags & 0x01:
            return zlib.decompressobj().decompress(blob)
        return blob


def _layer(name):
    """Mount order of the base game (TW1_WD_LADEREIHENFOLGE.md): base, GraphicsUpdate*, Update11-15, Update16, Language*."""
    b = name.lower()
    if b.startswith('update16'):
        return 3
    if b.startswith('update11'):
        return 2
    if b.startswith('graphicsupdate'):
        return 1
    if b.startswith('language16') or b.startswith('languagegog'):
        return 4
    return 0


def find_game_dir(hint=None):
    """Two Worlds folder (with WDFiles): the setting, the registry, then the Steam libraries."""
    cands = []
    if hint:
        cands.append(hint)
    try:
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, r'SOFTWARE\Reality Pump\TwoWorlds'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Reality Pump\TwoWorlds'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Reality Pump\TwoWorlds')):
            try:
                with winreg.OpenKey(root, key) as k:
                    for value in ('DataDir', 'InstallPath', 'Path'):
                        try:
                            cands.append(winreg.QueryValueEx(k, value)[0])
                        except OSError:
                            pass
            except OSError:
                pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'SOFTWARE\Valve\Steam') as k:
                steam = winreg.QueryValueEx(k, 'SteamPath')[0].replace('/', SEP)
            libs = [steam]
            vdf = os.path.join(steam, 'steamapps', 'libraryfolders.vdf')
            if os.path.isfile(vdf):
                with open(vdf, encoding='utf-8', errors='replace') as f:
                    libs += [p.replace(SEP + SEP, SEP) for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
            for lib in libs:
                common = os.path.join(lib, 'steamapps', 'common')
                if os.path.isdir(common):
                    for n in os.listdir(common):
                        if n.lower().startswith('two worlds') and 'ii' not in n.lower():
                            cands.append(os.path.join(common, n))
        except OSError:
            pass
    except ImportError:
        pass
    for c in cands:
        c = (c or '').rstrip(SEP)
        if c and os.path.isdir(os.path.join(c, 'WDFiles')):
            return c
    return None


def active_mods():
    """Mods\\*.wd switched on in the registry (HKCU\\...\\TwoWorlds\\Mods, value 1)."""
    out = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'SOFTWARE\Reality Pump\TwoWorlds\Mods') as k:
            i = 0
            while True:
                try:
                    name, value, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                if value == 1:
                    out.append(name)
                i += 1
    except (ImportError, OSError):
        pass
    return out


class Script:
    """One .eco as the game sees it: the winning copy plus the copies it hides."""

    def __init__(self, key, name, layer, body, older):
        self.key = key                     # inner path, lower case: scripts\campaigns\twoworldscampaign.eco
        self.name = name                   # file name as stored: TwoWorldsCampaign.eco
        self.layer = layer                 # label of the winning archive
        self.body = body
        self.sha = sha(body)
        self.debug = eco_is_debug(body)
        self.older = older                 # [(label, sha)] of hidden copies, oldest first
        self.match = None                  # index entry (dict) when a source compiles to these bytes
        self.related = []                  # index entries with the same file name that do not match
        self.from_mod = False

    @property
    def stem(self):
        return os.path.splitext(self.name)[0]


def read_game(game_dir, with_mods=True, extra_files=()):
    """All .eco of the game (and active mods, and extra .wd / .eco files): {key: Script}."""
    wd = os.path.join(game_dir, 'WDFiles') if game_dir else None
    archives = []
    problems = []
    if wd and os.path.isdir(wd):
        names = sorted((n for n in os.listdir(wd) if n.lower().endswith('.wd')), key=lambda n: (_layer(n), n.lower()))
        for n in names:
            try:
                archives.append((Archive(os.path.join(wd, n)), False))
            except Exception as e:
                problems.append(f'{n}: {e}')
        if with_mods:
            mods_dir = os.path.join(game_dir, 'Mods')
            for n in active_mods():
                p = os.path.join(mods_dir, n)
                if os.path.isfile(p):
                    try:
                        archives.append((Archive(p, 'Mods' + SEP + n), True))
                    except Exception as e:
                        problems.append(f'Mods{SEP}{n}: {e}')
            for n in sorted(os.listdir(game_dir)):
                if n.lower().endswith('.wd') and os.path.isfile(os.path.join(game_dir, n)):
                    try:
                        archives.append((Archive(os.path.join(game_dir, n), n), True))
                    except Exception as e:
                        problems.append(f'{n}: {e}')
    seen = {}
    for a, is_mod in archives:
        for k, (stored, *_rest) in a.entries.items():
            if k.endswith('.eco'):
                seen.setdefault(k, []).append((a, stored, is_mod))
    scripts = {}
    for k, copies in seen.items():
        older = []
        for a, stored, _m in copies[:-1]:
            try:
                older.append((a.label, sha(eco_body(a.read(k)))))
            except Exception as e:
                problems.append(f'{a.label} {k}: {e}')
        a, stored, is_mod = copies[-1]
        try:
            body = eco_body(a.read(k))
        except Exception as e:
            problems.append(f'{a.label} {k}: {e}')
            continue
        s = Script(k, os.path.basename(stored), a.label, body, older)
        s.from_mod = is_mod
        scripts[k] = s
    for p in extra_files:
        try:
            if p.lower().endswith('.wd'):
                a = Archive(p)
                for k, (stored, *_r) in a.entries.items():
                    if k.endswith('.eco'):
                        s = Script('file:' + p + '|' + k, os.path.basename(stored), os.path.basename(p),
                                   eco_body(a.read(k)), [])
                        s.from_mod = True
                        scripts[s.key] = s
            else:
                with open(p, 'rb') as f:
                    s = Script('file:' + p, os.path.basename(p), os.path.basename(p), eco_body(f.read()), [])
                s.from_mod = True
                scripts[s.key] = s
        except Exception as e:
            problems.append(f'{os.path.basename(p)}: {e}')
    return scripts, problems


# ------------------------------------------------------------------- SDK --

class Sdk:
    """A local Two Worlds SDK: Scripts (sources) and Tools (cpp + EarthC.exe)."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.scripts = os.path.join(self.root, 'Scripts')
        self.tools = os.path.join(self.root, 'Tools')
        self.version = '?'
        info = os.path.join(self.root, '_info_.txt')
        if os.path.isfile(info):
            with open(info, encoding='latin-1') as f:
                m = re.search(r'SDK ver\.\s*([0-9.]+)', f.read(400))
            if m:
                self.version = m.group(1)

    @staticmethod
    def looks_like(root):
        return os.path.isdir(os.path.join(root, 'Scripts')) and os.path.isfile(os.path.join(root, 'Tools', 'EarthC.exe'))

    @property
    def compiler_ok(self):
        return (os.path.isfile(os.path.join(self.tools, 'EarthC.exe'))
                and os.path.isfile(os.path.join(self.tools, 'gcc', 'bin', 'cpp.exe')))

    def source_roots(self):
        """(id, label, path): the SDK's Scripts and, if present, the older set it keeps in _Scripts_old_1.5_."""
        out = [(f'sdk{self.version}', f'SDK {self.version}', self.scripts)]
        old = os.path.join(self.scripts, '_Scripts_old_1.5_')
        if os.path.isdir(old):
            out.append((f'sdk{self.version}-old15', f'SDK {self.version} _Scripts_old_1.5_', old))
        return out

    def compiler_id(self):
        with open(os.path.join(self.tools, 'EarthC.exe'), 'rb') as f:
            return sha(f.read())[:16]


def find_sdks(extra=()):
    roots = []
    for c in list(extra) + [r'C:\TwoWorldsSDK', r'D:\TwoWorldsSDK', r'D:\Games\TwoWorldsSDK',
                            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'TwoWorldsSDK'),
                            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'TwoWorldsSDK')]:
        if c and Sdk.looks_like(c) and os.path.abspath(c).lower() not in [r.lower() for r in roots]:
            roots.append(os.path.abspath(c))
    return [Sdk(r) for r in roots]


def _env(tools):
    return dict(os.environ, GCC_EXEC_PREFIX=os.path.join(tools, 'gcc'),
                PATH=os.path.join(tools, 'gcc', 'libexec', 'gcc', 'mingw32', '3.4.2') + ';' + os.environ.get('PATH', ''))


_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def compile_file(tools, path, debug=False):
    """cpp + EarthC on ``path`` (inside a work copy). Returns (body or None, message)."""
    folder, name = os.path.split(path)
    pp = path + '-pp'
    out = os.path.splitext(path)[0] + '.eco'
    if os.path.exists(out):
        os.remove(out)
    env = _env(tools)
    with open(pp, 'wb') as fh:
        r = subprocess.run([os.path.join(tools, 'gcc', 'bin', 'cpp.exe'), name] + (['-D', '_DEBUG'] if debug else []),
                           cwd=folder, stdout=fh, stderr=subprocess.PIPE, env=env, creationflags=_NOWIN)
    if r.returncode != 0:
        _rm(pp)
        return None, 'cpp: ' + r.stderr.decode('latin-1', 'replace').strip()[:600]
    cmd = [os.path.join(tools, 'EarthC.exe'), '-w-', '-nologo', '-noresult', '-echofilename']
    if debug:
        cmd.append('-debug')
    cmd += [os.path.basename(pp), os.path.basename(out)]
    r = subprocess.run(cmd, cwd=folder, capture_output=True, env=env, creationflags=_NOWIN)
    _rm(pp)
    msg = (r.stdout + r.stderr).decode('latin-1', 'replace').strip()
    if not os.path.isfile(out):
        return None, 'EarthC: ' + msg[:600]
    with open(out, 'rb') as f:
        body = eco_body(f.read())
    return body, msg


def depends(tools, path):
    """Files ``path`` includes (relative to its folder's tree), via cpp -M."""
    folder, name = os.path.split(path)
    r = subprocess.run([os.path.join(tools, 'gcc', 'bin', 'cpp.exe'), '-M', name], cwd=folder,
                       capture_output=True, env=_env(tools), creationflags=_NOWIN)
    if r.returncode != 0:
        return None
    text = r.stdout.decode('latin-1', 'replace').replace(SEP + chr(10), ' ').replace(SEP + chr(13) + chr(10), ' ')
    parts = text.split(':', 1)[1].split() if ':' in text else []
    out = []
    for p in parts:
        full = os.path.normpath(os.path.join(folder, p.replace('/', SEP)))
        if full not in out:
            out.append(full)
    return out


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def tree_fingerprint(root):
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != '_Scripts_old_1.5_' or dirpath != root)
        for n in sorted(files):
            if n.lower().endswith(('.ec', '.ech', '.h')):
                p = os.path.join(dirpath, n)
                h.update(os.path.relpath(p, root).lower().encode())
                with open(p, 'rb') as f:
                    h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()[:24]


# ----------------------------------------------------------------- Index --

def build_index(sdks, cache_dir, progress=None, cancel=None):
    """Compile every .ec of every source root once (in a temp copy) and remember body hash and includes.

    Cached per source root by the fingerprint of its .ec/.ech/.h files and the compiler. Returns
    {'roots': {id: {...}}, 'entries': [ {root, rel, sha, size, deps, error} ]}.
    """
    os.makedirs(cache_dir, exist_ok=True)
    index = {'roots': {}, 'entries': []}
    jobs = []
    for sdk in sdks:
        if not sdk.compiler_ok:
            continue
        for rid, label, path in sdk.source_roots():
            if not os.path.isdir(path):
                continue
            jobs.append((sdk, rid, label, path))
    seen = {}
    for n, (sdk, rid, label, path) in enumerate(jobs):
        fp = tree_fingerprint(path)
        cid = sdk.compiler_id()
        if (fp, cid) in seen:                  # a second copy of the same sources and compiler (two SDK 1.3 installs)
            continue
        if rid in index['roots']:
            rid = rid + '-' + fp[:6]
        seen[(fp, cid)] = rid
        cache = os.path.join(cache_dir, f'index_{rid}_{fp}_{cid}.json')
        root_info = {'id': rid, 'label': label, 'path': path, 'sdk': sdk.root, 'version': sdk.version,
                     'tools': sdk.tools, 'compiler': cid, 'fingerprint': fp}
        data = None
        if os.path.isfile(cache):
            try:
                with open(cache, encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('format') != INDEX_FORMAT:
                    data = None
            except Exception:
                data = None
        if data is None:
            data = {'format': INDEX_FORMAT, 'entries': _index_root(sdk, rid, path, progress, cancel, n, len(jobs))}
        for e in data['entries']:
            e['root'] = rid
        with open(cache, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
        index['roots'][rid] = root_info
        index['entries'] += data['entries']
    return index


def _index_root(sdk, rid, path, progress, cancel, n, total):
    work = tempfile.mkdtemp(prefix='ecotool_idx_')
    try:
        copy = os.path.join(work, 'src')
        shutil.copytree(path, copy, ignore=shutil.ignore_patterns('_Scripts_old_1.5_', '*.eco', '*-pp'))
        sources = []
        for dirpath, dirs, files in os.walk(copy):
            for f in sorted(files):
                if f.lower().endswith('.ec'):
                    sources.append(os.path.join(dirpath, f))
        out = []
        for i, src in enumerate(sorted(sources)):
            if cancel and cancel():
                raise Cancelled()
            rel = os.path.relpath(src, copy)
            if progress:
                progress(f'{rid}: {rel}', n + i / max(1, len(sources)), total)
            body, msg = compile_file(sdk.tools, src)
            deps = depends(sdk.tools, src) or []
            deps = [os.path.relpath(d, copy) for d in deps if os.path.abspath(d).lower().startswith(copy.lower())]
            out.append({'root': rid, 'rel': rel, 'sha': sha(body) if body else None, 'size': len(body) if body else 0,
                        'deps': deps, 'error': None if body else msg})
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


ROOT_PREFERENCE = ('sdk1.3', 'sdk1.3-old15', 'sdk1.2', 'sdk1.2-old15')


def _pref(rid):
    return ROOT_PREFERENCE.index(rid) if rid in ROOT_PREFERENCE else len(ROOT_PREFERENCE)


def classify(scripts, index):
    """Attach to every script the index entry that compiles to its bytes (best root first) and same-named ones."""
    by_sha = {}
    by_name = {}
    for e in index['entries']:
        if e['sha']:
            by_sha.setdefault(e['sha'], []).append(e)
        by_name.setdefault(os.path.splitext(os.path.basename(e['rel']))[0].lower(), []).append(e)
    for s in scripts.values():
        hits = sorted(by_sha.get(s.sha, []), key=lambda e: _pref(e['root']))
        s.match = hits[0] if hits else None
        s.related = [] if hits else sorted(by_name.get(s.stem.lower(), []), key=lambda e: _pref(e['root']))
    return scripts


def status_of(s):
    """'source' (a source compiles to exactly these bytes), 'differs' (same-named source, other bytes), 'none'."""
    if s.match:
        return 'source'
    if s.related:
        return 'differs'
    return 'none'


# ---------------------------------------------------------------- Layout --

OLD_DIR = '_Scripts_old_1.5_'


def closure(entry):
    """The .ec and every file it includes, relative to its source root."""
    return [entry['rel']] + [d for d in entry['deps'] if d.lower() != entry['rel'].lower()]


def _groups(scripts):
    groups = {}
    for s in scripts:
        if s.match:
            groups.setdefault(s.match['root'], []).append(s)
    return groups


def primary_root(scripts):
    """The source root that reproduces the most scripts (None without any match)."""
    groups = _groups(scripts)
    if not groups:
        return None
    return max(groups, key=lambda r: (len(groups[r]), -_pref(r)))


def tree_tools(scripts, index):
    """The compiler for a whole exported tree: that of the primary root, else any indexed one."""
    root = primary_root(scripts)
    if root:
        return index['roots'][root]['tools']
    for r in index['roots'].values():
        if os.path.isfile(os.path.join(r['tools'], 'EarthC.exe')):
            return r['tools']
    return None


def layout(scripts, index):
    """Where each matched script's files go, SDK 1.3 style.

    The source root that reproduces the most scripts becomes Scripts\\, the older set (SDK 1.2 or SDK 1.3's own
    _Scripts_old_1.5_) goes to Scripts\\_Scripts_old_1.5_\\, any further root to Scripts\\_TW1_<root>\\.
    Returns (files {dest rel: (root id, src rel)}, mains {script key: dest rel of its .ec}, clashes [text]).
    """
    groups = _groups(scripts)
    if not groups:
        return {}, {}, []
    primary = primary_root(scripts)
    files, mains, clashes, owner = {}, {}, [], {}
    used_old = False
    for root in sorted(groups, key=lambda r: (r != primary, _pref(r))):
        if root == primary:
            prefix = ''
        elif not used_old and (root.endswith('-old15') or root.startswith('sdk1.2')):
            prefix, used_old = OLD_DIR + SEP, True
        else:
            prefix = '_TW1_' + root.replace('.', '_') + SEP
        for s in groups[root]:
            for rel in closure(s.match):
                dest = prefix + rel
                src = (root, rel.lower())
                if dest.lower() in owner and owner[dest.lower()] != src:
                    clashes.append(f'{dest}: {owner[dest.lower()][0]} / {root}')
                    continue
                owner[dest.lower()] = src
                files[dest] = (root, rel)
            mains[s.key] = prefix + s.match['rel']
    return files, mains, clashes


def read_source(index, root, rel):
    with open(os.path.join(index['roots'][root]['path'], rel), 'rb') as f:
        return f.read()


def check_tree(tree_dir, scripts, mains, tools, progress=None, cancel=None):
    """Compile every script at its place in a temp copy of ``tree_dir`` with the compiler in ``tools``.

    One compiler for the whole tree: the one the user will compile with. {key: (ok, message)}."""
    work = tempfile.mkdtemp(prefix='ecotool_check_')
    out = {}
    try:
        copy = os.path.join(work, 'Scripts')
        shutil.copytree(tree_dir, copy, ignore=shutil.ignore_patterns('*.eco', '*-pp'))
        todo = [s for s in scripts if s.key in mains]
        for i, s in enumerate(todo):
            if cancel and cancel():
                raise Cancelled()
            if progress:
                progress(s.name, i, len(todo))
            body, msg = compile_file(tools, os.path.join(copy, mains[s.key]))
            if body is None:
                out[s.key] = (False, msg)
            elif sha(body) != s.sha:
                out[s.key] = (False, 'compiles, but the bytes differ from the game')
            else:
                out[s.key] = (True, '')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# ---------------------------------------------------------------- Export --

def decompiled_text(body, name=None):
    """Readable decompiler output (EcoAnalysis lifter): pseudo source, not checked for compiling."""
    import decomp  # noqa: F401  (puts the decompiler folder on sys.path)
    import lifter
    tmp = tempfile.NamedTemporaryFile(suffix='.eco', delete=False)
    try:
        tmp.write(body)
        tmp.close()
        _lf, text = lifter.run(tmp.name)
        if name:
            text = text.replace(os.path.basename(tmp.name), name, 1)
        return text
    finally:
        _rm(tmp.name)


def export(scripts, index, out_dir, progress=None, cancel=None, verify=True):
    """Sources of ``scripts`` into out_dir\\Scripts (layout()), the rest as decompiler text into out_dir\\Decompiled.

    With verify every exported script is compiled from a temp copy and compared with the game's bytes.
    Returns a report dict (also written as report.json)."""
    scripts = list(scripts)
    files, mains, clashes = layout(scripts, index)
    tree = os.path.join(out_dir, 'Scripts')
    for i, (dest, (root, rel)) in enumerate(sorted(files.items())):
        if cancel and cancel():
            raise Cancelled()
        if progress:
            progress(dest, i, len(files))
        p = os.path.join(tree, dest)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(read_source(index, root, rel))
    tools = tree_tools(scripts, index)
    report = {'out': out_dir, 'source': [], 'decompiled': [], 'failed': [], 'clashes': clashes, 'compiler': tools}
    for s in scripts:
        if s.key in mains:
            report['source'].append({'key': s.key, 'name': s.name, 'path': mains[s.key], 'root': s.match['root'],
                                     'sha': s.sha, 'verified': None, 'error': ''})
            continue
        try:
            text = decompiled_text(s.body, s.name)
            p = os.path.join(out_dir, 'Decompiled', s.stem + '.ec')
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f:
                f.write('// ' + s.name + ': no source compiles to the game\'s bytes. Readable decompiler output, '
                        'NOT a compilable script.' + chr(10) + text)
            report['decompiled'].append({'key': s.key, 'name': s.name, 'path': os.path.relpath(p, out_dir)})
        except Exception as e:
            report['failed'].append({'key': s.key, 'name': s.name, 'error': f'{type(e).__name__}: {e}'})
    if mains:
        _write_compile_bat(out_dir, sorted(mains.values()), tools)
    if verify and mains:
        res = check_tree(tree, scripts, mains, tools, progress, cancel)
        for r in report['source']:
            ok, msg = res.get(r['key'], (False, 'not checked'))
            r['verified'], r['error'] = ok, msg
    with open(os.path.join(out_dir, 'report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=1)
    return report


def _write_compile_bat(out_dir, mains, tools):
    tools = tools or r'C:\TwoWorldsSDK\Tools'
    lines = ['@echo off', 'rem Compiles the exported scripts with the SDK compiler (TW1 EcoTool).',
             'rem Change TOOLS if your SDK lies elsewhere. Each .eco lands next to its source.',
             'setlocal', f'set "TOOLS={tools}"', 'set "GCC_EXEC_PREFIX=%TOOLS%' + SEP + 'gcc"',
             'set "PATH=%TOOLS%' + SEP + 'gcc' + SEP + 'libexec' + SEP + 'gcc' + SEP + 'mingw32' + SEP + '3.4.2;%PATH%"',
             'set FAIL=0']
    for rel in mains:
        folder, name = os.path.split(os.path.join('Scripts', rel))
        base = os.path.splitext(name)[0]
        lines += [f'pushd "%~dp0{folder}"',
                  f'"%TOOLS%{SEP}gcc{SEP}bin{SEP}cpp.exe" "{name}" > "{name}-pp" && "%TOOLS%{SEP}EarthC.exe" -w- -nologo '
                  f'-noresult -echofilename "{name}-pp" "{base}.eco" || set FAIL=1',
                  f'del "{name}-pp" 2>nul', 'popd']
    lines += ['if %FAIL%==1 (echo SOME SCRIPTS FAILED & exit /b 1)', 'echo ALL COMPILED']
    with open(os.path.join(out_dir, 'compile_all.bat'), 'w', encoding='mbcs', newline='') as f:
        f.write((chr(13) + chr(10)).join(lines) + chr(13) + chr(10))


# ------------------------------------------------------------ SDK update --

COMPILER = 'EarthC.exe'


def _file_sha(path):
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        return sha(f.read())


def plan_sdk_update(target, scripts, index, progress=None, cancel=None, check=True):
    """Bring ``target`` (an Sdk) to the game's state: the layout() of all non-mod scripts written into its Scripts.

    Every planned file is 'add', 'replace' (backup first) or unchanged. The SDK 1.2 compiler is older than the
    game's scripts need (it lacks functions they call), so Tools\\EarthC.exe is replaced by the primary root's one
    when they differ (plan['compiler']). With check the future tree is built in a temp folder and every script
    compiled there with the future compiler; apply only when all match.
    Returns {'files': [ {dest, action, root, rel} ], 'compiler': None | {...}, 'tools': future compiler folder,
    'checks': {key: (ok, msg)}, 'missing': [names], ...}.
    """
    game = [s for s in scripts if not s.from_mod]
    files, mains, clashes = layout(game, index)
    plan = {'target': target.scripts, 'files': [], 'missing': sorted(s.name for s in game if not s.match),
            'clashes': clashes, 'checks': {}, 'mains': mains, 'same': 0, 'compiler': None, 'tools': target.tools}
    primary = primary_root(game)
    if primary:
        src = os.path.join(index['roots'][primary]['tools'], COMPILER)
        old = os.path.join(target.tools, COMPILER)
        if _file_sha(src) != _file_sha(old):
            plan['compiler'] = {'dest': 'Tools' + SEP + COMPILER, 'src': src, 'root': primary,
                                'action': 'replace' if os.path.isfile(old) else 'add'}
            plan['tools'] = index['roots'][primary]['tools']
    for dest, (root, rel) in sorted(files.items()):
        content = read_source(index, root, rel)
        p = os.path.join(target.scripts, dest)
        if os.path.isfile(p):
            with open(p, 'rb') as f:
                if sha(f.read()) == sha(content):
                    plan['same'] += 1
                    continue
            action = 'replace'
        else:
            action = 'add'
        plan['files'].append({'dest': dest, 'action': action, 'root': root, 'rel': rel})
    if check and mains:
        work = tempfile.mkdtemp(prefix='ecotool_plan_')
        try:
            future = os.path.join(work, 'Scripts')
            shutil.copytree(target.scripts, future, ignore=shutil.ignore_patterns('*.eco', '*-pp'))
            for item in plan['files']:
                p = os.path.join(future, item['dest'])
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, 'wb') as f:
                    f.write(read_source(index, item['root'], item['rel']))
            plan['checks'] = check_tree(future, game, mains, plan['tools'], progress, cancel)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return plan


def plan_count(plan):
    """Files the plan writes, the compiler included."""
    return len(plan['files']) + (1 if plan.get('compiler') else 0)


def apply_sdk_update(target, plan, index, backup_root):
    """Back up every file that changes into a dated folder, then write. Returns the backup folder."""
    stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
    backup = os.path.join(backup_root, f'{os.path.basename(target.root)}_{stamp}')
    os.makedirs(backup, exist_ok=True)
    manifest = {'target': target.scripts, 'tools': target.tools, 'written': [], 'replaced': [],
                'tools_written': [], 'tools_replaced': [], 'time': stamp}
    comp = plan.get('compiler')
    if comp and comp['action'] == 'replace':
        b = os.path.join(backup, 'Tools', COMPILER)
        os.makedirs(os.path.dirname(b), exist_ok=True)
        shutil.copy2(os.path.join(target.tools, COMPILER), b)
        manifest['tools_replaced'].append(COMPILER)
    for item in plan['files']:
        if item['action'] == 'replace':
            b = os.path.join(backup, 'Scripts', item['dest'])
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(os.path.join(target.scripts, item['dest']), b)
            manifest['replaced'].append(item['dest'])
    _write_manifest(backup, manifest)
    if comp:
        os.makedirs(target.tools, exist_ok=True)
        shutil.copy2(comp['src'], os.path.join(target.tools, COMPILER))
        manifest['tools_written'].append(COMPILER)
    for item in plan['files']:
        p = os.path.join(target.scripts, item['dest'])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(read_source(index, item['root'], item['rel']))
        manifest['written'].append(item['dest'])
    _write_manifest(backup, manifest)
    return backup


def _write_manifest(backup, manifest):
    with open(os.path.join(backup, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=1)


def verify_sdk(target, scripts, plan, progress=None, cancel=None):
    """After apply: compile every script in the SDK itself (temp copy) with the SDK's own compiler."""
    game = [s for s in scripts if not s.from_mod]
    return check_tree(target.scripts, game, plan['mains'], target.tools, progress, cancel)


def restore_sdk_update(backup):
    """Undo apply_sdk_update: put replaced files back, delete the added ones. Returns the number of files."""
    with open(os.path.join(backup, 'manifest.json'), encoding='utf-8') as f:
        manifest = json.load(f)
    target = manifest['target']
    replaced = {r.lower() for r in manifest['replaced']}
    for rel in manifest['written']:
        if rel.lower() not in replaced:
            _rm(os.path.join(target, rel))
    for rel in manifest['replaced']:
        shutil.copy2(os.path.join(backup, 'Scripts', rel), os.path.join(target, rel))
    tools = manifest.get('tools')
    for rel in manifest.get('tools_written', []):
        if rel not in manifest.get('tools_replaced', []):
            _rm(os.path.join(tools, rel))
    for rel in manifest.get('tools_replaced', []):
        shutil.copy2(os.path.join(backup, 'Tools', rel), os.path.join(tools, rel))
    return len(manifest['written']) + len(manifest.get('tools_written', []))
