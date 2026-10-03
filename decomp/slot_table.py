"""The complete command / event slot table of every script class, read out of the SDK compiler (EarthC.exe).

entries.py learns the slots from debug builds, which only shows the slots some script happens to use (the
class `hero` has a single script). The compiler itself registers every slot at start-up:

    obj = new(0x48); ctor(obj, "Name", ret, p1, ..., pn, 0)

with ctor 0x46c320 for a command and 0x480a20 for an event. One routine per part of the class tree, slots
numbered in reverse code order (the list is built front to back):

    0x40dc40  commands 0..2 of every class ($Constructor, $Destructor, $Serialize)
    0x410b70  script base: commands from 3, events from 0          -> global (4); mission and campaign add:
    0x41a860  mission (2): commands from 22, events from 33
    0x413ca0  campaign (3): commands from 22, events from 33
    0x41e0a0  unit (0) and hero (1): commands from 3, events from 0
    0x447d00  RPGCompute (25): commands from 3

All six are called from one routine (0x405380), which first builds the type objects and hands them over:
the address of a global for the plain types (`push 0x555660`), the address of a local for the classes
(`lea eax,[ebp-0xa8] ; push eax`, made by `ctor(obj, "unit", ...)`) and the arrays (made by
`ctor(obj, 6, element type)`, one each for int, string, stringW and unit). Inside a part, a type argument is
`mov r,[ebp+X] ; mov r2,[r] ; push r2` - argument X of the part, i.e. one of those type objects. So every
parameter type is one of a few dozen SOURCES, the same in every part. Each source's type is voted from the
slots the debug builds know (entries.py), over all parts at once; a class source no debug build reaches takes
the class name it was created with, an array source kind 6 with its element's kind as sub (as every voted
array has it: int 1, string 2, stringW 3). Eight slots are registered by 0x405380 itself (RPGCompute 79..85, the
campaign event 35) and appended to a part's list; their numbers follow the order of the appends.

Measured against SDK 1.3's EarthC.exe (sha 66729e68); `build()` refuses a compiler whose layout differs.

  py slot_table.py <EarthC.exe>     -> data/slot_table.json (with a check against data/entry_table.json)
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / 'data' / 'slot_table.json'
BASE = 0x400000
CTORS = {0x46c320: 'command', 0x480a20: 'event'}
TYPE_CTOR, TYPE_WRAP = 0x464900, 0x4908b0          # named class type object; storing it into a local
LIST_ADD = (0x4a1600, 0x4a0b40)                     # list.Add(a).Add(b) for the extra slots
ARRAY_CTOR, ARRAY_KIND = 0x4a1000, 6                # ctor(obj, 6, element type): the array type objects
# routine -> (classes it belongs to, first command slot, first event slot)
PARTS = {
    0x40dc40: ((0, 1, 2, 3, 4, 25), 0, None),
    0x410b70: ((2, 3, 4), 3, 0),
    0x41a860: ((2,), 22, 33),
    0x413ca0: ((3,), 22, 33),
    0x41e0a0: ((0, 1), 3, 0),
    0x447d00: ((25,), 3, None),
}
_cache = None
_scanned = {}


def _scan(exe):
    """memoised per compiler file (the leave-out runs rebuild the table often)"""
    if exe not in _scanned:
        _scanned[exe] = _scan_uncached(exe)
    return _scanned[exe]


def _src_of(insns, j):
    """the type source an instruction pushes: ('g', global address) or ('l', local offset) of the caller,
    ('a', argument offset) inside a part, or None"""
    import re
    p = insns[j]
    if p.mnemonic != 'push' or p.op_str.startswith('0x'):
        return None
    a = insns[j - 1]
    if a.mnemonic == 'lea' and a.op_str.startswith(p.op_str + ', [ebp - '):
        return ('l', int(a.op_str.split('[ebp - ')[1].rstrip(']'), 0))          # caller: address of a local
    if a.mnemonic != 'mov' or not a.op_str.startswith(p.op_str + ', dword ptr ['):
        return None
    inner = a.op_str.split('[', 1)[1].rstrip(']')
    if re.fullmatch(r'0x[0-9a-f]+', inner):
        return ('g', int(inner, 16))                                             # value of a global
    mo = re.fullmatch(r'ebp - (0x[0-9a-f]+|\d+)', inner)
    if mo:
        return ('l', int(mo.group(1), 0))                                        # value of a local
    b = insns[j - 2]                                                             # mov r,[ebp+X] ; mov r2,[r]
    if b.mnemonic == 'mov' and b.op_str.startswith(inner + ', dword ptr [ebp + '):
        return ('a', int(b.op_str.split('[ebp + ')[1].rstrip(']'), 0))
    return None


def _scan_uncached(exe):
    import bisect
    import collections
    import struct
    import capstone
    with open(exe, 'rb') as f:
        data = f.read()
    cs = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    nsec = struct.unpack_from('<H', data, pe + 6)[0]
    optsz = struct.unpack_from('<H', data, pe + 20)[0]
    o = pe + 24 + optsz
    text = None
    for _ in range(nsec):
        if data[o:o + 5] == b'.text':
            _vsz, va, rsz, raw = struct.unpack_from('<IIII', data, o + 8)
            text = (va, raw, rsz)
        o += 40
    va, raw, rsz = text
    insns, pos = [], raw
    while pos < raw + rsz:
        got = list(cs.disasm(data[pos:raw + rsz], BASE + va + pos - raw))
        if not got:
            pos += 1
            continue
        insns += got
        pos = got[-1].address - BASE - va + raw + got[-1].size

    def cstr(v):
        off = v - BASE
        end = data.find(b'\0', off) if 0 <= off < len(data) else -1
        s = data[off:end] if end > 0 else b''
        return s.decode() if 1 <= len(s) < 80 and all(32 <= c < 127 for c in s) else None

    def target(ins):
        return int(ins.op_str, 16) if ins.mnemonic == 'call' and ins.op_str.startswith('0x') else None

    starts = [insns[i].address for i in range(len(insns) - 1)
              if insns[i].mnemonic == 'push' and insns[i].op_str == 'ebp'
              and insns[i + 1].mnemonic == 'mov' and insns[i + 1].op_str == 'ebp, esp']
    regs = collections.defaultdict(list)     # routine -> [(addr, kind, name, [source], object local)]
    calls = {}                               # part -> [source of argument 0, 1, ...]
    names = {}                               # caller local -> class name it holds
    arrays = {}                              # caller local -> source of the element type of the array it holds
    pending_array = None
    adds = []                                # (list local, [object locals in push order])
    pending_name = None
    for k, ins in enumerate(insns):
        t = target(ins)
        if t is None:
            continue
        if t == TYPE_CTOR:
            # the script-visible name is the string pushed last before the call (`push "unit"`); the
            # strings further back are the C++ side ("CEquipmentValues*&", "RStoreable&")
            pending_name = None
            for j in range(k - 1, max(k - 14, -1), -1):
                p = insns[j]
                if p.mnemonic == 'push' and p.op_str.startswith('0x'):
                    nm = cstr(int(p.op_str, 16))
                    if nm:
                        pending_name = nm if nm.isidentifier() else None
                        break
                if target(p) is not None:
                    break
            continue
        if t == ARRAY_CTOR:
            # mov ecx, G | lea ecx,[ebp-X] ; call 0x4a1600 ; push eax ; push 6 ; mov ecx, obj ; call ctor
            a, b, c, d = insns[k - 5:k - 1]
            if (b.mnemonic == 'call' and c.op_str == 'eax' and d.op_str == str(ARRAY_KIND)
                    and a.op_str.startswith('ecx, ')):
                v = a.op_str[5:]
                pending_array = (('g', int(v, 16)) if a.mnemonic == 'mov' and v.startswith('0x') else
                                 ('l', int(v.split('[ebp - ')[1].rstrip(']'), 0)) if a.mnemonic == 'lea' else None)
            continue
        if t == TYPE_WRAP and pending_array:
            prev = insns[k - 1]
            if prev.mnemonic == 'lea' and prev.op_str.startswith('ecx, [ebp - '):
                arrays[int(prev.op_str.split('[ebp - ')[1].rstrip(']'), 0)] = pending_array
            pending_array = None
            continue
        if t == TYPE_WRAP and pending_name:
            prev = insns[k - 1]
            if prev.mnemonic == 'lea' and prev.op_str.startswith('ecx, [ebp - '):
                names[int(prev.op_str.split('[ebp - ')[1].rstrip(']'), 0)] = pending_name
            pending_name = None
            continue
        if t in PARTS and t not in calls:
            srcs, j = [], k - 1
            while j > 0 and len(srcs) < 40 and target(insns[j]) is None:
                if insns[j].mnemonic == 'push':
                    if insns[j].op_str.startswith('0x'):
                        srcs.append(('g', int(insns[j].op_str, 16)))     # address of a global type object
                    else:
                        srcs.append(_src_of(insns, j))
                j -= 1
            calls[t] = srcs                                               # argument 0 first
            continue
        if t == LIST_ADD[0] and insns[k - 1].mnemonic == 'lea' and insns[k - 1].op_str.startswith('ecx, [ebp - '):
            lst = int(insns[k - 1].op_str.split('[ebp - ')[1].rstrip(']'), 0)
            objs, j = [], k - 2
            while j > 0 and insns[j].mnemonic in ('push', 'mov') and len(objs) < 4:
                if insns[j].mnemonic == 'push':
                    s = _src_of(insns, j)
                    if s and s[0] == 'l':
                        objs.append(s[1])
                j -= 1
            adds.append((lst, objs[::-1]))                                # push order
            continue
        kind = CTORS.get(t)
        if kind is None:
            continue
        args, j = [], k - 1
        while j >= 0 and len(args) < 40:
            p = insns[j]
            if p.mnemonic == 'push':
                if p.op_str == '0':
                    args.append(None)
                    break
                args.append(('imm', int(p.op_str, 16)) if p.op_str.startswith('0x') else _src_of(insns, j))
            j -= 1
        if len(args) < 3 or args[-1] is not None or not args[1] or args[1][0] != 'imm':
            continue
        name = cstr(args[1][1])
        if not name:
            continue
        srcs = args[2:-1]
        # where the object ends up: the last `mov [ebp - V], reg` within the next few instructions
        obj = None
        for q in insns[k + 1:k + 10]:
            if q.mnemonic == 'mov' and q.op_str.startswith('dword ptr [ebp - ') and ', e' in q.op_str:
                obj = int(q.op_str.split('[ebp - ')[1].split(']')[0], 0)
        r = starts[bisect.bisect_right(starts, ins.address) - 1]
        regs[r].append((ins.address, kind, name, srcs, obj))
    return {'regs': regs, 'calls': calls, 'names': names, 'adds': adds, 'arrays': arrays}


def _ptype(p):
    # A string parameter is a pointer either way, and the compiler registers `string` and `string&` with the
    # same descriptor (campaign: FillNetworkChannelLevelsList's `string& strMapTexture` next to plain strings).
    # The `&` is the script's choice and shows in its code (typeinfer strref), so it is not part of the slot.
    ref = p.get('flags', 0) & 1 if p['kind'] not in (2, 3) else 0
    return [p['kind'], p['type'] if isinstance(p.get('type'), str) else 0, p.get('sub', 0), ref]


def _resolve(scan, r, src):
    """a source as seen by the caller: ('a', X) inside part r is argument (X - 8) / 4 of the call to r"""
    if src and src[0] == 'a':
        args = scan['calls'].get(r, [])
        i = (src[1] - 8) // 4
        return args[i] if 0 <= i < len(args) else None
    return src


def build(exe, known=None):
    """{class: {kind: {slot: {'name', 'params': [record] | None}}}} and a small report"""
    import collections
    import entries
    known = known if known is not None else entries.load()
    scan = _scan(exe)
    regs = scan['regs']
    missing = [hex(r) for r in PARTS if r not in regs or r not in scan['calls']]
    if missing:
        raise RuntimeError(f'this EarthC.exe registers its slots elsewhere ({", ".join(missing)} missing)')
    # slot lists per part, in slot order
    lists = {}
    for r, (classes, cfirst, efirst) in PARTS.items():
        for kind, first in (('command', cfirst), ('event', efirst)):
            got = [x for x in regs[r] if x[1] == kind][::-1]
            if got and first is None:
                raise RuntimeError(f'{hex(r)} registers {kind}s where none are expected')
            if got:
                lists[(r, kind)] = (classes, first, got)
    # the extra slots: appended to the list a part filled, in the order of the appends
    caller = next(iter({x for x in regs if x not in PARTS} | {None}))
    by_obj = {x[4]: x for x in regs.get(caller, []) if x[4] is not None}
    list_part = {}
    for r, srcs in scan['calls'].items():
        for s in srcs:
            if s and s[0] == 'l':
                list_part.setdefault(s[1], set()).add(r)
    extra = collections.defaultdict(list)       # (part, kind) -> [registration] in slot order
    for lst, objs in scan['adds']:
        for o in reversed(objs):                 # the last pushed is added first
            x = by_obj.get(o)
            if x is None:
                continue
            # a list handed to several parts (the event list goes to the base and to campaign) continues
            # in the most derived part that fills slots of this kind: the one starting highest
            parts = [r for r in list_part.get(lst, ()) if (r, x[1]) in lists]
            if not parts:
                continue
            r = max(parts, key=lambda r: lists[(r, x[1])][1])
            extra[(r, x[1])].append(x)
    # every type source voted from the slots the debug builds know, over all parts at once
    votes = collections.defaultdict(collections.Counter)

    def vote(r, kind, slot, x):
        for cls in PARTS[r][0]:
            e = known.get(str(cls), {}).get(kind, {}).get(str(slot))
            if not e or e['name'] != x[2] or len(e['params']) != len(x[3]) - 1:
                continue
            for s, p in zip(x[3][1:], e['params']):
                s = _resolve(scan, r, s)
                if s:
                    votes[s][json.dumps(_ptype(p))] += 1
    slots = {}
    for (r, kind), (classes, first, got) in lists.items():
        for i, x in enumerate(got):
            slots[(r, kind, first + i)] = x
            vote(r, kind, first + i, x)
        for i, x in enumerate(extra.get((r, kind), ())):
            slots[(r, kind, first + len(got) + i)] = (*x[:3], x[3], x[4], 'extra')
            vote(r, kind, first + len(got) + i, x)
    src_type = {}
    for s, c in votes.items():
        if len(c) > 1:
            raise RuntimeError(f'type source {s} means {len(c)} different types: {dict(c)}')
        src_type[s] = json.loads(next(iter(c)))
    for off, name in scan['names'].items():                 # a class no debug build reaches: its name
        src_type.setdefault(('l', off), [0, name, 0, 0])
    for off, elem in scan['arrays'].items():                # an array: kind 6, sub = the element's kind
        e = src_type.get(elem)
        if e is None or e[1]:                                 # element unknown or a class (no voted example)
            continue
        t = [ARRAY_KIND, 0, e[0], 0]
        if src_type.setdefault(('l', off), t) != t:
            raise RuntimeError(f'array source {off:#x} voted {src_type[("l", off)]}, its element says {t}')
    table, unknown = {}, 0
    for (r, kind, slot), x in slots.items():
        params = []
        for n, s in enumerate(x[3][1:]):
            t = src_type.get(_resolve(scan, r, s))
            if t is None:
                unknown += 1
                params = None
                break
            params.append({'name': f'p{n + 1}', 'kind': t[0], 'type': t[1], 'sub': t[2], 'flags': t[3],
                           'addr': 4 * (n + 1), 'tidx': 0xFFFFFFFF})
        for cls in PARTS[r][0]:
            table.setdefault(str(cls), {}).setdefault(kind, {})[str(slot)] = {'name': x[2], 'params': params}
    return table, {'unknown_types': unknown, 'sources': len(src_type), 'voted': len(votes),
                   'extra': sum(len(v) for v in extra.values())}


def check(table, known):
    """every slot a debug build knows must come out with the same name and parameter types"""
    bad = []
    for cls, kinds in known.items():
        for kind, slots in kinds.items():
            for idx, e in slots.items():
                t = table.get(cls, {}).get(kind, {}).get(idx)
                if t is None:
                    bad.append((cls, kind, idx, e['name'], 'missing'))
                elif t['name'] != e['name']:
                    bad.append((cls, kind, idx, e['name'], 'name ' + t['name']))
                elif t['params'] is not None and [_ptype(p) for p in t['params']] != [_ptype(p) for p in e['params']]:
                    bad.append((cls, kind, idx, e['name'], 'types'))
    return bad


def load():
    global _cache
    if _cache is None:
        _cache = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    return _cache


def lookup(cls, kind, idx):
    """{'name', 'params'} of a slot, parameter names synthesised (p1, p2, ...), or None"""
    e = load().get(str(cls), {}).get(kind, {}).get(str(idx))
    return e if e and e.get('params') is not None else None


if __name__ == '__main__':
    import entries
    known = entries.load()
    table, info = build(sys.argv[1], known)
    bad = check(table, known)
    n = sum(len(s) for k in table.values() for s in k.values())
    print(f'{n} slots, {info}; disagreements with the debug builds: {len(bad)}')
    for b in bad[:30]:
        print('   ', b)
    OUT.write_text(json.dumps(table, indent=0, sort_keys=True), encoding='utf-8')
