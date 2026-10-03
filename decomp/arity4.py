"""Native arity, take 4 — basic-block balance with proper save/restore detection
and majority voting instead of single-derivation propagation.

Saves are identified from the epilogue: whatever `pop reg` run sits before
`pop ebp; ret` names the registers that were saved, and the matching first
`push reg` instructions in the prologue are the saves. Everything else that is
pushed is an argument (Hero.sub_0 pushes ebx as an argument and never pops it).

Each basic block yields  #push - #pop - sum(arity(callee)) == 0.
Blocks with one unknown vote for a value; a value is accepted when it holds a
>=70% majority. Repeat until nothing new is accepted, then re-check every block.
"""
import pathlib, collections, struct, re, json
import capstone
import eco, ecodbg, disasm
from arity3 import own_arity, decode, blocks, SAVE_REGS

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
CACHE = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_arity.json')


def mark_saves(insns):
    """returns the index set of push/pop instructions that are frame bookkeeping"""
    skip = set()
    # epilogue: ... pop reg* ; pop ebp ; ret
    j = len(insns) - 1
    while j >= 0 and insns[j][0] in ('ret', 'nop'):
        j -= 1
    if j >= 0 and insns[j][0] == 'pop' and insns[j][3] and insns[j][3].op_str == 'ebp':
        skip.add(j)
        j -= 1
        if j >= 0 and insns[j][0] == 'mov' and insns[j][3] \
                and insns[j][3].op_str == 'esp, ebp':
            j -= 1
        saved = []
        while j >= 0 and insns[j][0] == 'pop' and insns[j][3] \
                and insns[j][3].op_str in SAVE_REGS:
            skip.add(j)
            saved.append(insns[j][3].op_str)
            j -= 1
        # `saved` was read backwards through the epilogue, so it already is the
        # prologue push order (pops are LIFO): push esi,ebx,ecx,edi
        # -> pop edi,ecx,ebx,esi -> read backwards: esi,ebx,ecx,edi.
        want = saved
        k = 0
        for i, (m, a, s, ins) in enumerate(insns):
            if k >= len(want):
                break
            if m == 'push' and ins is not None and ins.op_str == want[k]:
                skip.add(i)
                k += 1
    # the frame setup itself
    if insns and insns[0][0] == 'push' and insns[0][3] and insns[0][3].op_str == 'ebp':
        skip.add(0)
    return skip


def file_eqs(p):
    """block equations of one .eco"""
    eqs = []
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
        insns = decode(code, r)
        skip = mark_saves(insns)
        pos_of = {ins[1]: i for i, ins in enumerate(insns)}
        # the folded debug hook is `push eax; push line; push file; call; pop eax`
        # -> the callee pops exactly 2, otherwise the pop would not restore eax
        for m, a, s, ins in insns:
            if m == 'hook':
                idx = loc2fn.get(a + 14)
                if idx is not None:
                    eqs.append((collections.Counter({idx: 1}), 2, False))
        for blk in blocks(insns, r['start'], r['end']):
            net, row, bad = 0, collections.Counter(), False
            lead = blk[0][1] != r['start']       # block is a jump target
            for m, a, s, ins in blk:
                i = pos_of[a]
                if i in skip:
                    continue
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
                eqs.append((row, net, lead))
    return eqs


def collect(root=pathlib.Path('../eco')):
    eqs = []
    for p in sorted(root.rglob('*.eco')):
        eqs += file_eqs(p)
    return eqs


def gather_votes(eqs, known, skip=None):
    """one pass of block equations -> candidate values per unknown index"""
    votes = collections.defaultdict(collections.Counter)
    for row, net, lead in eqs:
        rest, unk = net, collections.Counter()
        for idx, mult in row.items():
            if idx in known and idx != skip:
                rest -= known[idx] * mult
            else:
                unk[idx] += mult
        if rest == 0 and len(unk) > 1:
            # arities cannot be negative, so a balance of zero with only positive
            # coefficients forces every unknown in this block to 0
            for idx in unk:
                votes[idx][0] += 1
            continue
        if len(unk) != 1:
            continue
        idx, mult = next(iter(unk.items()))
        if rest % mult == 0 and 0 <= rest // mult <= 32:
            votes[idx][rest // mult] += 1
    return votes


def reduce_eqs(eqs, known, exact_only=True):
    """block equations with the known arities substituted out

    -> {sorted unknown (idx, mult) tuple: set of right-hand sides}. The value is a
    set on purpose: an unknown set that appears with two different balances means
    at least one of the two blocks is not a clean equation, and neither may be used
    as evidence.

    `exact_only` drops jump-target blocks. Their balance is not an equation at all:
    a jump target can inherit pushes from its predecessor (`&&` / `||` evaluate
    across the branch), so the count is short. Subtracting two such blocks
    multiplies the error — with them in, the difference rule read SetHorizonOffset
    as 6 from a standalone jump-target block that balances to 6 while the routine's
    entry block says 3.
    """
    red = {}
    for row, net, lead in eqs:
        if exact_only and lead:
            continue
        rest, unk = net, collections.Counter()
        for idx, mult in row.items():
            if idx in known:
                rest -= known[idx] * mult
            else:
                unk[idx] += mult
        if unk:
            red.setdefault(tuple(sorted(unk.items())), set()).add(rest)
    return red


def gather_pair_votes(eqs, known):
    """Difference votes: two blocks whose unknowns differ by exactly one call.

    The single-unknown rule cannot reach a native that never appears alone.
    `GetPlayerInterface` (0x0322) is the extreme case: it is the receiver of every
    UI call, so all 574 of its blocks carry at least two unknowns and it stayed
    unsolved — and with it SetConsoleText, PlayDialog, GetPlayerName,
    XBOXGetAchievement, which only ever occur behind it.

    Two blocks whose unknown parts differ by one call give that call's arity by
    subtraction, e.g. `{0x0322, 0x042a} = 7` minus `{0x0322} = 2` -> 0x042a = 5.
    Looking the complement up in a dict keeps this linear instead of quadratic.
    """
    red = reduce_eqs(eqs, known)
    votes = collections.defaultdict(collections.Counter)
    for key, rests in red.items():
        if len(rests) != 1:
            continue
        rest = next(iter(rests))
        unk = dict(key)
        for idx, mult in unk.items():
            for take in range(1, mult + 1):
                sub = [(k, v - take if k == idx else v) for k, v in unk.items()]
                other = red.get(tuple(sorted((k, v) for k, v in sub if v)))
                if other is None or len(other) != 1:
                    continue
                diff = rest - next(iter(other))
                if diff % take == 0 and 0 <= diff // take <= 32:
                    votes[idx][diff // take] += 1
    return votes


ANCHORS = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_arity_anchors.json')


def load_anchors():
    """Arities measured directly, by compiling a probe source with the SDK compiler
    (see arity_probe.py). They are first-hand evidence and outrank the corpus vote:
    the vote cannot reach a native that never stands alone in a basic block, and it
    gets some values plainly wrong (GetWorldWidth voted 2 over 33 jump-target
    blocks; the compiler emits 1)."""
    if not ANCHORS.exists():
        return {}
    return {int(k, 0): v for k, v in json.loads(ANCHORS.read_text()).items()}


def solve(eqs, strict=0.9, loose=0.55, revisions=3):
    """Two phases plus revision.

    Accepting a value from thin evidence early and never revisiting it was wrong:
    0x0603 ended up as 4 although, once everything else was known, 2 had the
    majority. So: accept only clear majorities first, then the weaker ones, then
    re-derive every value against full knowledge and replace it where the evidence
    now points elsewhere. Returns (values, confidence).
    """
    anchors = load_anchors()
    known = dict(anchors)
    conf = {k: 1.0 for k in anchors}
    for quorum in (strict, loose):
        while True:
            votes = gather_votes(eqs, known)
            new = 0
            for idx, c in votes.items():
                if idx in known:
                    continue
                val, n = c.most_common(1)[0]
                tot = sum(c.values())
                if n / tot >= quorum:
                    known[idx], conf[idx] = val, n / tot
                    new += 1
            if new:
                continue
            # nothing left that stands alone in a block — fall back to the
            # difference of two blocks (see gather_pair_votes)
            for idx, c in gather_pair_votes(eqs, known).items():
                if idx in known:
                    continue
                val, n = c.most_common(1)[0]
                tot = sum(c.values())
                if n / tot >= quorum:
                    known[idx], conf[idx] = val, n / tot
                    new += 1
            if not new:
                break
    for _ in range(revisions):
        changed = 0
        for idx in list(known):
            if idx in anchors:
                continue                  # measured, not voted on
            votes = gather_votes(eqs, known, skip=idx).get(idx)
            if not votes:
                continue
            val, n = votes.most_common(1)[0]
            tot = sum(votes.values())
            if val != known[idx] and n / tot > conf.get(idx, 0):
                known[idx], conf[idx] = val, n / tot
                changed += 1
            else:
                conf[idx] = max(conf.get(idx, 0),
                                votes[known[idx]] / tot if tot else 0)
        if not changed:
            break
    return known, conf


def check(eqs, known):
    ok = bad = skip = 0
    culprits = collections.Counter()
    for row, net, lead in eqs:
        if all(k in known for k in row):
            if sum(known[k] * v for k, v in row.items()) == net:
                ok += 1
            else:
                bad += 1
                for k in row:
                    culprits[k] += 1
        else:
            skip += 1
    return ok, bad, skip, culprits


def store(known, conf):
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(
        {str(k): [v, round(conf.get(k, 0), 3)] for k, v in sorted(known.items())},
        indent=1))


def load_full():
    """-> {idx: (arity, confidence)}"""
    if not CACHE.exists():
        known, conf = solve(collect())
        store(known, conf)
        return {k: (v, conf.get(k, 0)) for k, v in known.items()}
    raw = json.loads(CACHE.read_text())
    out = {}
    for k, v in raw.items():
        out[int(k)] = (v[0], v[1]) if isinstance(v, list) else (v, 1.0)
    return out


def load():
    return {k: v[0] for k, v in load_full().items()}


if __name__ == '__main__':
    eqs = collect()
    print(f'{len(eqs)} basic-block equations')
    known, conf = solve(eqs)
    ok, bad, skip, culprits = check(eqs, known)
    tot = ok + bad
    print(f'{len(known)} arities solved; blocks: ok={ok} ({100 * ok / max(tot, 1):.1f}%) '
          f'contradicting={bad} incomplete={skip}')
    if bad:
        print('  most frequent indices in contradicting blocks:', culprits.most_common(6))
    solid = sum(1 for i in known if conf.get(i, 0) >= 0.9)
    print(f'confidence: {solid} solid (>=90% agreement), '
          f'{len(known) - solid} uncertain')
    print('arity histogram:', sorted(collections.Counter(known.values()).items()))
    store(known, conf)
    print('written', CACHE)
