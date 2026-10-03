"""Native arity, take 3 — one equation per basic block.

The code generator evaluates every expression to completion inside a basic block,
so at the end of a block the argument stack is empty again:

    #push - #pop - sum(arity(callee)) == 0        (per basic block)

Register saves/restores are excluded: the leading `push reg` run of the prologue and
the trailing `pop reg` run of the epilogue. A routine's own arity comes from its
`ret imm16`, so debug info is not required and all 107 files contribute.

Blocks with exactly one unknown arity are solved, the result is fed back, repeat.
Finally every block is re-checked, including those never used for solving.
"""
import pathlib, collections, struct, re, json
import capstone
import eco, ecodbg, disasm

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
HOOK = re.compile(rb'\x50\xb8(....)\x50\xb8(....)\x50\xe8(....)\x58', re.S)
CACHE = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_arity.json')
SAVE_REGS = ('esi', 'ebx', 'ecx', 'edi')


def own_arity(code, r):
    e = r['end']
    if e >= 2 and code[e - 2] == 0xC2:
        return struct.unpack_from('<H', code, e - 1)[0] // 4
    if code[e] == 0xC3:
        return 0
    return None


def decode(code, r):
    """instruction list of a routine, folded hooks replaced by a pseudo entry"""
    out, pos = [], r['start']
    while pos <= r['end']:
        mo = HOOK.match(code, pos, r['end'] + 1)
        if mo:
            out.append(('hook', pos, mo.end() - pos, None))
            pos = mo.end()
            continue
        ins = next(CS.disasm(code[pos:min(r['end'] + 1, pos + 16)], pos), None)
        if ins is None:
            break
        out.append((ins.mnemonic, ins.address, ins.size, ins))
        pos = ins.address + ins.size
    return out


def blocks(insns, start, end):
    """split at jump targets and after jumps -> list of instruction slices"""
    leaders = {start}
    for m, a, s, ins in insns:
        if m.startswith('j') and ins is not None and ins.op_str.startswith('0x'):
            t = int(ins.op_str, 16)
            if start <= t <= end:
                leaders.add(t)
            leaders.add(a + s)
    out, cur = [], []
    for item in insns:
        if item[1] in leaders and cur:
            out.append(cur)
            cur = []
        cur.append(item)
    if cur:
        out.append(cur)
    return out


def strip_frame(insns):
    """drop prologue register saves and epilogue restores"""
    i = 0
    if len(insns) >= 2 and insns[0][0] == 'push' and insns[1][0] == 'mov':
        i = 2
        if i < len(insns) and insns[i][0] == 'sub':
            i += 1
        while i < len(insns) and insns[i][0] == 'push' and insns[i][3] is not None \
                and insns[i][3].op_str in SAVE_REGS:
            insns[i] = ('skip',) + insns[i][1:]
            i += 1
    j = len(insns) - 1
    while j >= 0 and insns[j][0] in ('ret', 'pop', 'mov', 'nop'):
        if insns[j][0] == 'pop' and insns[j][3] is not None \
                and insns[j][3].op_str in SAVE_REGS + ('ebp',):
            insns[j] = ('skip',) + insns[j][1:]
        j -= 1
    return insns


def collect(root=pathlib.Path('../eco')):
    eqs = []
    for p in sorted(root.rglob('*.eco')):
        f = eco.parse(p)
        code = bytes(f['code'])
        b = ecodbg.parse_blob(f['tail'], len(code)) if f['has_debug'] else None
        routines = b['routines'] if b else disasm.scan_routines(code)
        loc2fn = dict(f['imports'])
        ar = {}
        for r in routines:
            a = own_arity(code, r)
            if a is not None:
                ar[r['start']] = a
        for r in routines:
            insns = strip_frame(decode(code, r))
            for blk in blocks(insns, r['start'], r['end']):
                net, row, bad = 0, collections.Counter(), False
                for m, a, s, ins in blk:
                    if m == 'push':
                        net += 1
                    elif m == 'pop':
                        net -= 1
                    elif m == 'call' and code[a] == 0xE8:
                        rel = struct.unpack_from('<i', code, a + 1)[0]
                        if rel == 0:
                            idx = loc2fn.get(a + 1)
                            if idx is None:
                                bad = True
                            else:
                                row[idx] += 1
                        else:
                            t = ar.get(a + s + rel)
                            if t is None:
                                bad = True
                            else:
                                net -= t
                    elif m == 'call':
                        bad = True
                if not bad and row:
                    eqs.append((row, net))
    return eqs


def solve(eqs):
    known, pending = {}, list(eqs)
    while True:
        progress, still = False, []
        for row, net in pending:
            rest, unk = net, collections.Counter()
            for idx, mult in row.items():
                if idx in known:
                    rest -= known[idx] * mult
                else:
                    unk[idx] += mult
            if not unk:
                continue
            if len(unk) == 1:
                idx, mult = next(iter(unk.items()))
                if rest % mult == 0 and 0 <= rest // mult <= 32:
                    known[idx] = rest // mult
                    progress = True
                    continue
            still.append((row, net))
        pending = still
        if not progress:
            return known, pending


def check(eqs, known):
    ok = bad = skip = 0
    worst = collections.Counter()
    for row, net in eqs:
        if all(k in known for k in row):
            if sum(known[k] * v for k, v in row.items()) == net:
                ok += 1
            else:
                bad += 1
                for k in row:
                    worst[k] += 1
        else:
            skip += 1
    return ok, bad, skip, worst


def load():
    if CACHE.exists():
        return {int(k): v for k, v in json.loads(CACHE.read_text()).items()}
    known, _ = solve(collect())
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({str(k): v for k, v in sorted(known.items())}, indent=1))
    return known


if __name__ == '__main__':
    eqs = collect()
    print(f'{len(eqs)} basic-block equations')
    known, left = solve(eqs)
    ok, bad, skip, worst = check(eqs, known)
    print(f'{len(known)} native arities solved, {len(left)} blocks still ambiguous')
    print(f'recheck over all blocks: ok={ok} contradicting={bad} incomplete={skip}')
    if bad:
        print('  indices in contradicting blocks:', worst.most_common(8))
    print('arity histogram:', sorted(collections.Counter(known.values()).items()))
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({str(k): v for k, v in sorted(known.items())}, indent=1))
    print('written', CACHE)
