"""Engine function signatures, measured: the SDK documentation names every function with its parameter types,
the SDK compiler says which native index each one is.

For every documented signature a probe function calls it once with variables of the documented types; the
probes are compiled with the SDK compiler, and the native a probe calls (minus the bookkeeping natives and
the ones that build the receiver) is that signature's index. Signatures the compiler rejects are peeled off
one by one at the reported line. Result: data/native_sigs.json

    {index: {'name', 'cls', 'ret', 'params': [[type, ref], ...]}}

with the types spelled like the documentation (int, string, stringW, unit, mission, array, ...). Positions in
the lifter count the receiver as argument 0, so parameter k is argument k + 1.

  py native_sigs.py <SDK folder>        (needs Documentation\\EarthC and Tools)
"""
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / 'data' / 'native_sigs.json'
SIG = re.compile(r'^\s*([A-Za-z_]\w*\s*&?)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*$')
# doc file -> (script class to probe in, receiver expression for public functions or None = call directly)
RECEIVER = {
    'unit': ('mission', 'u'), 'mission': ('mission', 'm'), 'campaign': ('mission', 'GetCampaign()'),
    'global': ('mission', 'g'), 'playerinterface': ('mission', 'GetCampaign().GetPlayerInterface(0)'),
    'string': ('mission', 's'), 'stringW': ('mission', 'w'), 'array': ('mission', 'ai'),
    'basescript': ('mission', None), 'object': ('mission', None), 'hero': ('hero', None),
    'RPGCompute': ('RPGCompute', None),
}
ARRAY_ELEMS = ('int', 'string', 'stringW', 'unit')
PRIVATE_CLASS = {'unit': 'unit', 'mission': 'mission', 'campaign': 'campaign', 'global': 'global',
                 'hero': 'hero', 'RPGCompute': 'RPGCompute'}
LOCAL_OF = {'int': 'int', 'string': 'string', 'stringW': 'stringW', 'unit': 'unit', 'mission': 'mission',
            'object': 'object', 'array': 'int', 'global': 'global'}


def parse_docs(doc_dir):
    """[(doc class, section, ret, name, [(type, ref)])] - section 'public' / 'private'"""
    import html            # measuring only; the tool itself just load()s the result
    out = []
    for f in sorted(pathlib.Path(doc_dir).glob('*.htm')):
        cls = f.stem
        if cls in ('all', 'index'):
            continue
        text = html.unescape(re.sub(r'<[^>]+>', '\n', f.read_text(encoding='latin-1')))
        section = 'public'
        for line in text.splitlines():
            s = line.strip()
            if s in ('public:', 'private:'):
                section = s[:-1]
                continue
            mo = SIG.match(s)
            if not mo:
                continue
            params = []
            for p in [x.strip() for x in mo.group(3).split(',') if x.strip()]:
                t = p.split()[0] if ' ' in p else p
                params.append((t.rstrip('&'), t.endswith('&')))
            out.append((cls, section, mo.group(1).strip(), mo.group(2), params))
    # array.htm documents its methods once with TYPE for the element; there are four array classes
    # (int, string, stringW, unit), each with its own natives, so every one is probed four times
    expanded = []
    for cls, section, ret, name, params in out:
        if cls != 'array':
            expanded.append((cls, section, ret, name, params))
            continue
        for elem in ARRAY_ELEMS:
            ps = [(elem if t == 'TYPE' else f'array:{elem}' if t == 'array' else t, r) for t, r in params]
            expanded.append((f'array:{elem}', section, elem if ret == 'TYPE' else ret, name, ps))
    return expanded


def _probe_text(script_cls, entries):
    """one probe function per signature; returns (text, {line: entry index})"""
    lines = [f'{script_cls} "probe"', '{', '    state Initialize;']
    owner = {}
    for n, (cls, section, _ret, name, params) in enumerate(entries):
        decl, args = [], []
        for k, (t, _ref) in enumerate(params):
            if t == 'array':
                decl.append(f'int x{k}[];')
            elif t.startswith('array:'):
                decl.append(f'{t[6:]} x{k}[];')
            elif t in ('TYPE',):
                return None
            else:
                decl.append(f'{LOCAL_OF.get(t, t)} x{k};')
            args.append(f'x{k}')
        recv = ''
        if cls.startswith('array:'):
            decl.append(f'{cls[6:]} ai[];')
            recv = 'ai.'
        elif section == 'public' and RECEIVER.get(cls, ('', None))[1]:
            r = RECEIVER[cls][1]
            if r in ('u', 'm', 'g', 's', 'w', 'ai'):
                t = {'u': 'unit', 'm': 'mission', 'g': 'global', 's': 'string', 'w': 'stringW', 'ai': 'int'}[r]
                decl.append(f'{t} {r}{"[]" if r == "ai" else ""};')
            recv = r + '.'
        elif cls not in RECEIVER and cls not in PRIVATE_CLASS and not cls.startswith('array:'):
            decl.append(f'{cls} hv;')          # the *Params / *Values classes
            recv = 'hv.'
        start = len(lines) + 1
        lines.append(f'    function void p{n}()')
        lines.append('    {')
        lines += [f'        {d}' for d in decl]
        lines.append(f'        {recv}{name}({", ".join(args)});')
        lines.append('    }')
        for ln in range(start, len(lines) + 1):
            owner[ln] = n
    lines.append('    state Initialize')
    lines.append('    {')
    lines += [f'        p{n}();' for n in range(len(entries))]
    lines.append('        return Initialize;')
    lines.append('    }')
    lines.append('}')
    return '\n'.join(lines) + '\n', owner


def _compile_peeling(tools, script_cls, entries, work, log):
    """compile, dropping the entry at each reported error line; -> (eco body, kept entry indices)"""
    import ecocore
    keep = list(range(len(entries)))
    for _ in range(len(entries) + 1):
        if not keep:
            return None, []
        got = _probe_text(script_cls, [entries[i] for i in keep])
        if got is None:
            return None, []
        text, owner = got
        src = os.path.join(work, 'probe.ec')
        with open(src, 'w', encoding='latin-1') as f:
            f.write(text)
        body, msg = ecocore.compile_file(tools, src)
        if body is not None:
            return body, keep
        mo = re.search(r'\((\d+),\d+\) : error', msg)
        if not mo or int(mo.group(1)) not in owner:
            log.append(f'{script_cls}: unexpected error {msg.strip()[-160:]}')
            return None, []
        bad = keep[owner[int(mo.group(1))]]
        log.append(f'rejected {entries[bad][0]}.{entries[bad][3]}: {msg.strip().splitlines()[-1][-80:]}')
        keep.remove(bad)
    return None, []


def measure(sdk_root, chunk=120):
    import collections
    import shutil
    import tempfile
    import ecocore
    import decomp  # noqa: F401
    import eco
    import disasm
    import emit_body
    tools = os.path.join(sdk_root, 'Tools')
    entries = [e for e in parse_docs(os.path.join(sdk_root, 'Documentation', 'EarthC'))
               if not any(t == 'TYPE' for t, _r in e[4])]      # array templates: no single signature
    bookkeeping = (emit_body.LIFECYCLE | emit_body.HANDLE_PASS | emit_body.extra_handle_pass()
                   | emit_body.STR_CAST | emit_body.STR_ASSIGN)
    groups = collections.defaultdict(list)
    for e in entries:
        cls, section = e[0], e[1]
        script_cls = PRIVATE_CLASS.get(cls) if section == 'private' else RECEIVER.get(cls, ('mission', None))[0]
        groups[script_cls or 'mission'].append(e)
    import arity4
    ar = arity4.load()
    out, log = {}, []
    work = tempfile.mkdtemp(prefix='ecotool_sigs_')
    try:
        # baseline: what the receivers and the locals cost on their own
        for script_cls, lst in groups.items():
            for c0 in range(0, len(lst), chunk):
                part = lst[c0:c0 + chunk]
                body, keep = _compile_peeling(tools, script_cls, part, work, log)
                if body is None:
                    continue
                p = os.path.join(work, 'probe.eco')
                with open(p, 'wb') as f:
                    f.write(body)
                f_ = eco.parse(p)
                code = bytes(f_['code'])
                imports = dict(f_['imports'])
                routines = disasm.scan_routines(code)
                # code order: p0, p1, ... then the state
                for k, i in enumerate(keep):
                    r = routines[k]
                    called = [imports[a + 1] for a in range(r['start'], r['end'] + 1)
                              if code[a] == 0xE8 and (a + 1) in imports]
                    called = [x for x in called if x not in bookkeeping and x not in RECEIVER_NATIVES]
                    cls, section, ret, name, params = part[i]
                    if len(set(called)) == 1:
                        idx = called[0]
                        n_args = _arity_at(code, imports, r, idx, ar)
                        out.setdefault(str(idx), {'name': name, 'cls': cls, 'section': section, 'ret': ret,
                                                  'params': [list(x) for x in params], 'arity': n_args,
                                                  'receiver': None if n_args is None else n_args - len(params)})
                    else:
                        log.append(f'ambiguous {cls}.{name}: {sorted(set(called))}')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out, log


def _arity_at(code, imports, r, target, ar):
    """arguments the call to `target` takes: the pushes of the probe statement that no earlier call used.

    Arguments go right to left, the receiver last, and every inner call (GetCampaign, the string
    conversions, the constructors of the locals) takes its own from the stack first; their arity is the
    corpus table's (arity4). A native without a receiver (GetChar, sscanf, GetCampaign) shows as
    `arity == parameters`, a method as `parameters + 1`."""
    import disasm
    depth = 0
    for ins in disasm.CS.disasm(code[r['start']:r['end'] + 1], r['start']):
        if ins.mnemonic == 'push' and ins.op_str != 'ebp':
            depth += 1
        elif ins.mnemonic == 'pop' and ins.op_str != 'ebp':
            depth -= 1                 # `push eax ; <constructor> ; pop eax` around the locals' setup
        elif ins.mnemonic == 'call' and code[ins.address] == 0xE8:
            idx = imports.get(ins.address + 1)
            if idx == target:
                return depth
            n = ar.get(idx)
            if n is None:
                return None
            depth -= n
        elif ins.mnemonic == 'add' and ins.op_str.startswith('esp, '):
            depth -= int(ins.op_str.split(', ')[1], 0) // 4
    return None


HANDLE_CLASSES = ('unit', 'mission', 'global', 'object', 'CustomArtefactParams', 'CustomArtefactParamsSet',
                  'EquipmentParams', 'EquipmentValues', 'MagicCardParams', 'MagicClubParams', 'MissileParams',
                  'MissileParamsSet', 'MissileValues', 'PotionArtefactParams', 'PotionArtefactParamsSet',
                  'PotionValues', 'SpecialArtefactParams', 'SpecialArtefactParamsSet', 'TrapParams', 'TrapParamsSet',
                  'UnitParams', 'UnitValues', 'WeaponParams')


def measure_hierarchy(sdk_root):
    """[[derived, base]]: the handle classes a value may be passed as, asked of the compiler.

    One probe function per ordered pair takes a `base` parameter and is called with a `derived` variable;
    the calls the compiler rejects ("Cannot find suitable function") are peeled off at the reported line.
    The debug builds only show the pairs RPGCompute happens to use (and RPGCompute is the only script of
    its class), so without this its leave-one-out failed on `InitWeapon` passing WeaponParams on."""
    import ecocore
    import shutil
    import tempfile
    tools = os.path.join(sdk_root, 'Tools')
    pairs = [(a, b) for a in HANDLE_CLASSES for b in HANDLE_CLASSES if a != b]
    work = tempfile.mkdtemp(prefix='ecotool_tree_')
    keep = list(range(len(pairs)))
    try:
        while keep:
            lines = ['mission "probe"', '{', '    state Initialize;']
            owner = {}
            for n, i in enumerate(keep):
                a, b = pairs[i]
                lines.append(f'    function void f{n}({b} x) {{ }}')
                lines.append(f'    function void g{n}() {{ {a} y; f{n}(y); }}')
                owner[len(lines)] = i
            lines += ['    state Initialize', '    {'] + [f'        g{n}();' for n in range(len(keep))] + \
                ['        return Initialize;', '    }', '}']
            src = os.path.join(work, 'tree.ec')
            with open(src, 'w', encoding='latin-1') as f:
                f.write(chr(10).join(lines) + chr(10))
            body, msg = ecocore.compile_file(tools, src)
            if body is not None:
                break
            mo = re.search(r'\((\d+),\d+\) : error', msg)
            if not mo or int(mo.group(1)) not in owner:
                raise RuntimeError('unexpected compiler error: ' + msg.strip()[-200:])
            keep.remove(owner[int(mo.group(1))])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return sorted([list(pairs[i]) for i in keep])


# the natives that build a receiver: GetCampaign, GetPlayerInterface (measured in the probes of TW1_EcoTool
# _spike/p2/probe: 0x2de, 0x322)
RECEIVER_NATIVES = {0x2de, 0x322}


def load():
    return {int(k): v for k, v in json.loads(OUT.read_text(encoding='utf-8')).items()} if OUT.exists() else {}


TREE = HERE / 'data' / 'class_tree.json'
LIFE = HERE / 'data' / 'class_lifecycle.json'


def measure_lifecycle(sdk_root):
    """{native index: handle class} for the natives the compiler emits around a handle variable of each
    class: its destructor, AddRef / Release and $Serialize. A local of such a class that the script never
    uses is only visible through them (RPGCompute's unused UnitParams local: destructor 0x7d3), and only
    RPGCompute's debug build shows most of these classes."""
    import ecocore
    import eco
    import disasm
    import shutil
    import tempfile
    tools = os.path.join(sdk_root, 'Tools')
    work = tempfile.mkdtemp(prefix='ecotool_life_')
    out = {}
    try:
        # locals only: most of these classes cannot be globals ("Objects of this type cannot be used as
        # members"), and a local shows the destructor and, through `x = y`, AddRef / Release
        lines = ['mission "probe"', '{', '    state Initialize;']
        for n, c in enumerate(HANDLE_CLASSES):
            lines.append(f'    function void f{n}() {{ {c} x; {c} y; x = y; }}')
        lines += ['    state Initialize', '    {'] + [f'        f{n}();' for n in range(len(HANDLE_CLASSES))] + \
            ['        return Initialize;', '    }', '}']
        src = os.path.join(work, 'life.ec')
        with open(src, 'w', encoding='latin-1') as f:
            f.write(chr(10).join(lines) + chr(10))
        body, msg = ecocore.compile_file(tools, src)
        if body is None:
            raise RuntimeError('lifecycle probe: ' + msg.strip()[-200:])
        p = os.path.join(work, 'life.eco')
        with open(p, 'wb') as f:
            f.write(body)
        f_ = eco.parse(p)
        code = bytes(f_['code'])
        imports = dict(f_['imports'])
        routines = disasm.scan_routines(code)
        seen = {}
        for n, c in enumerate(HANDLE_CLASSES):
            r = routines[n]
            for a in range(r['start'], r['end'] + 1):
                if code[a] == 0xE8 and (a + 1) in imports:
                    seen.setdefault(imports[a + 1], set()).add(c)
        for idx, cs in seen.items():
            if len(cs) == 1:
                out[idx] = next(iter(cs))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def load_lifecycle():
    return {int(k): v for k, v in json.loads(LIFE.read_text(encoding='utf-8')).items()} if LIFE.exists() else {}


def load_tree():
    return json.loads(TREE.read_text(encoding='utf-8')) if TREE.exists() else []


if __name__ == '__main__':
    sys.path.insert(0, str(HERE.parent))
    tree = measure_hierarchy(sys.argv[1])
    TREE.write_text(json.dumps(tree, indent=0), encoding='utf-8')
    life = measure_lifecycle(sys.argv[1])
    LIFE.write_text(json.dumps({str(k): v for k, v in sorted(life.items())}, indent=0), encoding='utf-8')
    print(len(life), 'lifecycle natives with their class')
    print(len(tree), 'assignable class pairs:', tree)
    sigs, log = measure(sys.argv[1])
    OUT.write_text(json.dumps(sigs, indent=0, sort_keys=True), encoding='utf-8')
    print(len(sigs), 'native signatures measured;', len(log), 'notes')
    for l in log[:40]:
        print('   ', l)
