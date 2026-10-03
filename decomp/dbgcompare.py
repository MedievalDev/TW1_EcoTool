"""Compare two debug builds of a script routine by routine, the way the compiler laid them out.

A debug build (`EarthC -debug`) embeds the full path of every source file in its data segment. Two builds of
the same source made in two folders therefore differ wherever something holds a data address behind such a
path, because every item after it moves: compiling the SDK's TwoWorldsWeather.ec in a temp folder against the
round-trip reference differs only in `mov eax, imm32` data addresses (0x1fe vs 0x260, ...) and in the data
segment's own pointers. So the data segments are aligned first and addresses are compared by what they point
at, never as numbers:

- data segment: split into its items (the header's data pointer list holds their starts). Source paths
  (`X:\\...`) are set aside; every other item must be the same, in the same order, except where it holds a
  data address itself (an item of TwoWorldsWeather holds two): those compare by what they point at.
- code: the instructions of a routine are compared one by one. Where the bytes differ, only the last four
  (the immediate or the call target) may: two data addresses of corresponding items, two paths with the same
  file name, two code addresses of routines with the same name, or a call to routines with the same name.
- engine calls carry rel32 = 0 in the code; their indices (the import table) are compared after `native_map`
  (index of the original -> index of ours).
- `mask_lines`: the line and the file of the debug line hook (`push eax; mov eax, LINE; push eax; mov eax,
  FILE; push eax; call hook; pop eax`) and any address of a source path are not compared - a decompiled
  source is one file with its own line numbers.

  py dbgcompare.py ours.eco original.eco [--mask-lines]
"""
import re
import sys

import ecodbg

DBG_HOOK = re.compile(rb'\x50\xb8(....)\x50\xb8(....)\x50\xe8(....)\x58', re.S)
LINE_CALL = re.compile(rb'\xb8(....)\x50\xe8\x00\x00\x00\x00', re.S)
LINE_NATIVES = {0x2f5}       # SDK 1.3's one-argument line call (131 in PTown's debug build)
_PATH = re.compile(rb'^(?:[A-Za-z]:\\|\\\\)')


def _norm_path(s):
    return s.rstrip(b'\0').replace(b'/', b'\\').rsplit(b'\\', 1)[-1].lower()


class Build:
    def __init__(self, path):
        self.f, self.b = ecodbg.parse_file(path)
        if not self.b:
            raise ValueError(f'{path}: no debug info')
        self.code = bytes(self.f['code'])
        self.data = bytes(self.f['data_seg'])
        self.routines = sorted(self.b['routines'], key=lambda r: r['start'])
        self.by_start = {r['start']: r for r in self.routines}
        self._items()

    def _items(self):
        """data items: (offset, length, kind, bytes), kind 'path' (a source file's full path) or 'data'.
        The header's data pointer list is exactly the item starts (TwoWorldsWeather: 0, 85, 172, ... are the
        paths of its source files); `self.rank` maps the offset of every non-path item to its rank."""
        d = self.data
        starts = sorted({p for p in self.f['data_ptrs'] if 0 <= p < len(d)} | {0})
        items = []
        for k, o in enumerate(starts):
            end = starts[k + 1] if k + 1 < len(starts) else len(d)
            chunk = d[o:end]
            items.append((o, end - o, 'path' if _PATH.match(chunk) else 'data', chunk))
        self.items = items
        self.starts = [it[0] for it in items]
        rank, k = {}, 0
        for it in items:
            if it[2] != 'path':
                rank[it[0]] = k
                k += 1
        self.rank = rank

    def item_at(self, off):
        """(item, offset inside it) holding data address `off`, or None"""
        import bisect
        i = bisect.bisect_right(self.starts, off) - 1
        if i < 0:
            return None
        it = self.items[i]
        return (it, off - it[0]) if off < it[0] + it[1] or (it[1] == 0 and off == it[0]) else None

    def routine_at(self, off):
        for r in self.routines:
            if r['start'] <= off <= r['end']:
                return r
        return None


def _same_data(A, B, va, vb, mask_paths):
    """True if data address va of A and vb of B mean the same item"""
    ha, hb = A.item_at(va), B.item_at(vb)
    if not ha or not hb:
        return None                                   # not data addresses
    (ia, oa), (ib, ob) = ha, hb
    if ia[2] == 'path' and ib[2] == 'path':
        # the start of the path, or the same distance from its end (a record right behind the main file's
        # path points 4 bytes before the path's end: 57 of 61 in our Cities build, 50 of 54 in the original)
        at = oa == ob == 0 or ia[1] - oa == ib[1] - ob
        return at and (mask_paths or _norm_path(ia[3]) == _norm_path(ib[3]))
    if ia[2] == 'path' or ib[2] == 'path':
        return False
    # the same item: same rank, or (when an item is missing in one build and the ranks behind it moved) the
    # same bytes - CityCampaign's two v1.0 commands SDK 1.3 cannot declare leave out their debug records
    return oa == ob and (A.rank[ia[0]] == B.rank[ib[0]] or ia[3] == ib[3])


def compare_data(A, B, mask_paths=False):
    """None if the data segments hold the same items, else a reason"""
    xa = [it for it in A.items if it[2] != 'path']
    xb = [it for it in B.items if it[2] != 'path']
    if len(xa) != len(xb):
        return f'{len(xa)} vs {len(xb)} data items'
    for p, q in zip(xa, xb):
        if len(p[3]) != len(q[3]):
            return f'data at {p[0]}/{q[0]}: {p[3][:24]!r} vs {q[3][:24]!r}'
        k = 0
        while k < len(p[3]):
            if p[3][k] == q[3][k]:
                k += 1
                continue
            # an item can hold data addresses itself (TwoWorldsWeather 6708: two string addresses 0x1a2f and
            # 0x1a33 that move with the paths before them)
            s_ = next((s_ for s_ in range(max(0, k - 3), k + 1) if s_ + 4 <= len(p[3]) and _same_data(
                A, B, int.from_bytes(p[3][s_:s_ + 4], 'little'), int.from_bytes(q[3][s_:s_ + 4], 'little'),
                mask_paths)), None)
            if s_ is None:
                return f'data at {p[0] + k}/{q[0] + k}: {p[3][k:k + 8]!r} vs {q[3][k:k + 8]!r}'
            k = s_ + 4
    if not mask_paths:
        pa = [_norm_path(it[3]) for it in A.items if it[2] == 'path']
        pb = [_norm_path(it[3]) for it in B.items if it[2] == 'path']
        if pa != pb:
            return f'source files {pa[:3]} vs {pb[:3]}'
    return None


def _cs():
    import capstone
    return capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)


def compare_routine(A, B, ra, rb, mask_lines=False, cs=None):
    """None if the two routines are the same program, else a short reason"""
    a = A.code[ra['start']:ra['end'] + 1]           # a routine's end is its last byte (the ret's)
    b = B.code[rb['start']:rb['end'] + 1]
    if len(a) != len(b):
        return f'size {len(a)} vs {len(b)}'
    if a == b:
        return None
    masked = set()
    if mask_lines:
        for mo in DBG_HOOK.finditer(a):
            if DBG_HOOK.match(b, mo.start()):
                masked.update(range(mo.start(1), mo.end(2)))
        # a loop's line marker: `mov eax, LINE; push eax; call <0x2f5>` (SDK 1.3 numbering; v1.0 0x2f8)
        imp = dict(A.f['imports'])
        for mo in LINE_CALL.finditer(a):
            if imp.get(ra['start'] + mo.end() - 4) in LINE_NATIVES and LINE_CALL.match(b, mo.start()):
                masked.update(range(mo.start(1), mo.end(1)))
    cs = cs or _cs()
    ia = list(cs.disasm(a, ra['start']))
    ib = list(cs.disasm(b, rb['start']))
    if sum(x.size for x in ia) != len(a) or len(ia) != len(ib):
        return 'instructions do not line up'
    for x, y in zip(ia, ib):
        if x.bytes == y.bytes:
            continue
        rel = x.address - ra['start']
        if x.size != y.size:
            return f'{rel}: {x.mnemonic} {x.op_str} vs {y.mnemonic} {y.op_str}'
        diff = [k for k in range(x.size) if x.bytes[k] != y.bytes[k] and rel + k not in masked]
        if not diff:
            continue
        if x.size != y.size or x.mnemonic != y.mnemonic or diff[0] < x.size - 4:
            return f'{rel}: {x.mnemonic} {x.op_str} vs {y.mnemonic} {y.op_str}'
        if x.mnemonic == 'call':
            ta, tb = A.routine_at(int(x.op_str, 16)), B.routine_at(int(y.op_str, 16))
            if ta and tb and ta['name'] == tb['name']:
                continue
            return f'{rel}: call {ta and ta["name"]} vs {tb and tb["name"]}'
        va = int.from_bytes(x.bytes[-4:], 'little')
        vb = int.from_bytes(y.bytes[-4:], 'little')
        same = _same_data(A, B, va, vb, mask_lines)
        if same:
            continue
        if same is None:
            pa, pb = A.by_start.get(va), B.by_start.get(vb)
            if pa and pb and pa['name'] == pb['name']:
                continue
        return f'{rel}: {x.mnemonic} {x.op_str} vs {y.mnemonic} {y.op_str}'
    return None


def compare_imports(A, B, native_map=None):
    """engine calls in code order where they differ: [(code offset, ours, original after native_map)]"""
    ia, ib = sorted(A.f['imports']), sorted(B.f['imports'])
    if len(ia) != len(ib):
        return [('count', len(ia), len(ib))]
    return [(la, fa, native_map.get(fb, fb) if native_map else fb)
            for (la, fa), (lb, fb) in zip(ia, ib)
            if la != lb or fa != (native_map.get(fb, fb) if native_map else fb)]


def entries(X):
    """what the script declares to the engine, by name (slot numbers differ between compiler versions):
    {(kind, name, parameter kinds): modifiers} for states (6), commands (5) and events (4), a command's
    modifiers being (item, priority, flags) - plus the size of its globals"""
    import emit_ec
    mods = emit_ec.command_modifiers(X.f)
    out = {}
    for r in X.routines:
        if r.get('kind') in (4, 5, 6):
            key = (r['kind'], r['name'], tuple((p['kind'], p['sub']) for p in r.get('params', ())))
            out[key] = mods.get(r.get('a')) if r['kind'] == 5 else None
    return out, X.f.get('mem_reserve')


def compare_entries(A, B):
    """None if both declare the same states, commands (with item, priority, hidden) and events and the same
    globals size, else a reason"""
    (ea, ma), (eb, mb) = entries(A), entries(B)
    if ma != mb:
        return f'globals size {ma} vs {mb}'
    if set(ea) != set(eb):
        gone = sorted(k[1] for k in set(eb) - set(ea))
        new = sorted(k[1] for k in set(ea) - set(eb))
        return f'entry points: missing {gone[:4]}, extra {new[:4]}'
    bad = sorted(k[1] for k in ea if ea[k] != eb[k])
    return f'command modifiers differ: {bad[:4]}' if bad else None


def compare(ours, original, native_map=None, mask_lines=False):
    """{'routines', 'same', 'differ': [(name, reason)], 'imports': [...], 'data': None or reason,
    'entries': None or reason}"""
    A, B = Build(ours), Build(original)
    cs = _cs()
    differ = []
    if len(A.routines) != len(B.routines):
        differ.append(('(routine count)', f'{len(A.routines)} vs {len(B.routines)}'))
    same = 0
    ia, ib = sorted(A.f['imports']), sorted(B.f['imports'])

    def calls(imports, r):
        return [(loc - r['start'], fn) for loc, fn in imports if r['start'] <= loc <= r['end']]

    for ra, rb in zip(A.routines, B.routines):
        why = None if ra['name'] == rb['name'] else f'name {ra["name"]} vs {rb["name"]}'
        why = why or compare_routine(A, B, ra, rb, mask_lines, cs)
        if not why:
            # the same code calls the same engine functions: rel32 is 0 in both, the index is in the table
            ca = calls(ia, ra)
            cb = [(o, native_map.get(fn, fn) if native_map else fn) for o, fn in calls(ib, rb)]
            if ca != cb:
                k = next((k for k, (x, y) in enumerate(zip(ca, cb)) if x != y), min(len(ca), len(cb)))
                why = f'engine call {k}: {ca[k] if k < len(ca) else "-"} vs {cb[k] if k < len(cb) else "-"}'
        if why:
            differ.append((rb['name'], why))
        else:
            same += 1
    return {'routines': len(B.routines), 'same': same, 'differ': differ,
            'imports': compare_imports(A, B, native_map), 'data': compare_data(A, B, mask_lines),
            'entries': compare_entries(A, B)}


if __name__ == '__main__':
    r = compare(sys.argv[1], sys.argv[2], mask_lines='--mask-lines' in sys.argv)
    print(f"{r['same']} of {r['routines']} routines the same, engine calls differing: {len(r['imports'])}, "
          f"data: {r['data'] or 'same'}, entry points: {r['entries'] or 'same'}")
    for name, why in r['differ'][:20]:
        print('  ', name, why)
