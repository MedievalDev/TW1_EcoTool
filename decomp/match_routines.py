"""Match release-build routines to their debug-build counterparts.

Release builds carry no names or types; debug builds do, but are a different program
(`-D _DEBUG` pulls in extra macros). Commands, states and events pair up through the
header arrays. From there the call graph carries the mapping further: the internal
calls of a release routine correspond, in order, to the internal call records of its
debug counterpart.

The debug build has extra internal calls from TRACE macros. Those go to routines
defined in Debug.ech, and the line table says which source file a routine came from,
so they can be filtered out.

  py match_routines.py <release.eco> <debug.eco>
"""
import pathlib, struct, sys, collections, itertools
import eco, ecodbg, disasm, lifter
from disasm import CS

DEBUG_SOURCES = ('debug.ech',)


def debug_only(dbg):
    """routines that exist only because of the debug build"""
    out = set()
    for r in dbg['routines']:
        files = {ln[0] for ln in r['lines']}
        srcs = {dbg['files'][i].lower().rsplit('\\', 1)[-1]
                for i in files if i < len(dbg['files'])}
        if srcs and srcs <= set(DEBUG_SOURCES):
            out.add(r['name'])
    return out


def internal_calls(code, r):
    """target offsets of internal calls, in code order"""
    out = []
    for ins in disasm.CS.disasm(code[r['start']:r['end'] + 1], r['start']):
        if ins.mnemonic == 'call' and code[ins.address] == 0xE8:
            rel = struct.unpack_from('<i', code, ins.address + 1)[0]
            if rel != 0:
                out.append(ins.address + ins.size + rel)
    return out


def native_calls(code, imports, r):
    """the native ids a routine calls, in code order

    A native call is `E8 00 00 00 00` — a relative call to itself — and the import
    table maps the displacement's address to the native's id. Both builds use the
    same ids, so this is a build-independent fingerprint of what a routine does.
    """
    out = []
    for ins in CS.disasm(code[r['start']:r['end'] + 1], r['start']):
        if ins.mnemonic == 'call' and code[ins.address] == 0xE8 \
                and struct.unpack_from('<i', code, ins.address + 1)[0] == 0:
            idx = imports.get(ins.address + 1)
            if idx is not None:
                out.append(idx)
    return out


def string_refs(code, cptr, data, r):
    """the string literals a routine references, as a multiset

    The Code Pointers table marks every code position that holds a data-segment
    offset, so the `mov eax, <imm32>` operands it points at are string addresses.
    The texts are the same in both builds even though the data layout is not, which
    makes them a build-independent fingerprint — and a much sharper one than the
    native list for the attribute getters: GetMainState, GetDailyState and
    GetActivity (Campaigns/Missions/PInc) compile to the very same three natives and
    differ only in the attribute name they pass.
    """
    out = collections.Counter()
    for ins in CS.disasm(code[r['start']:r['end'] + 1], r['start']):
        if len(ins.bytes) == 5 and ins.bytes[0] == 0xB8 and ins.address + 1 in cptr:
            off = struct.unpack_from('<I', code, ins.address + 1)[0]
            end = data.find(b'\x00', off) if 0 <= off < len(data) else -1
            if end >= 0 and end - off < 200:
                s = data[off:end]
                if all(9 <= c < 127 for c in s):
                    out[bytes(s)] += 1
    return out


def const_refs(code, cptr, r):
    """the integer literals a routine loads, as a multiset

    `mov eax, <imm32>` where the operand is NOT marked in the Code Pointers table
    is a constant from the source, so both builds load the same ones. Together with
    the string literals this separates routines that compile to the very same
    natives — the release build of TwoWorldsCampaign has a 21-byte, argument-less
    routine that passed every other filter as both `GetHero` and `EscapeUnitCheck`.
    """
    out = collections.Counter()
    for ins in CS.disasm(code[r['start']:r['end'] + 1], r['start']):
        if len(ins.bytes) == 5 and ins.bytes[0] == 0xB8 \
                and ins.address + 1 not in cptr:
            out[struct.unpack_from('<I', code, ins.address + 1)[0]] += 1
    return out


def own_calls(rdbg, imports, skip_names):
    """names the debug routine calls inside the script, in code order"""
    return [x['name'] for x in rdbg['refs']
            if (x['addr'] - 4) not in imports and x['name'] not in skip_names]


def own_arity(code, r):
    e = r['end']
    if e >= 2 and code[e - 2] == 0xC2:
        return struct.unpack_from('<H', code, e - 1)[0] // 4
    return 0


def embed(rel_targets, dbg_names, fits):
    """the one order-preserving embedding of `rel_targets` into `dbg_names`

    The debug build never calls *less* than the release build, but it does call
    more, and not only into Debug.ech: `TRACE("Time %d", GetDayPhase())`
    (TwoWorldsSounds.ec:619) puts an ordinary script call inside a debug-only
    macro, so the two call lists differ in length while still describing the same
    program. Dropping the whole block on that (the old "shapes differ" bail) cost
    TwoWorldsSounds four routine names and left `sub_70()` in the output.

    Returns the aligned name list, or None when the embedding is not unique -
    ambiguity is reported, never resolved by guessing. `fits(i, name)` decides
    whether release call i can be the debug call `name`.
    """
    R, D = len(rel_targets), len(dbg_names)
    if R > D:
        return None
    ok = [[fits(i, dbg_names[j]) for j in range(D)] for i in range(R)]
    # ways[i][j] = number of embeddings of rel[i:] into dbg[j:], capped at 2
    ways = [[0] * (D + 1) for _ in range(R + 1)]
    for j in range(D + 1):
        ways[R][j] = 1
    for i in range(R - 1, -1, -1):
        for j in range(D - 1, -1, -1):
            w = ways[i][j + 1] + (ways[i + 1][j + 1] if ok[i][j] else 0)
            ways[i][j] = min(w, 2)
    if ways[0][0] != 1:
        return None
    out, i, j = [], 0, 0
    while i < R:
        if ways[i][j + 1] == 1:           # skipping keeps the unique embedding
            j += 1
        else:
            out.append(dbg_names[j])
            i += 1
            j += 1
    return out


def forced_positions(ok):
    """for each row of `ok`, the only column it can take in ANY embedding

    `ok[i][j]` says release item i may be debug item j. An order-preserving
    embedding picks a strictly increasing column per row. A row whose valid
    columns (columns that occur in at least one complete embedding) number
    exactly one is determined without a choice being made anywhere; every other
    row stays None. This is strictly weaker than demanding the whole embedding be
    unique, and still never guesses: a row with two possible partners is left out.

    Returns a list with one debug index (or None) per release item.
    """
    R = len(ok)
    D = len(ok[0]) if R else 0
    if R == 0 or R > D:
        return [None] * R
    # pre[i][j] : R[:i] fits into D[:j]      suf[i][j] : R[i:] fits into D[j:]
    pre = [[False] * (D + 1) for _ in range(R + 1)]
    for j in range(D + 1):
        pre[0][j] = True
    for i in range(1, R + 1):
        row, prev = pre[i], pre[i - 1]
        for j in range(1, D + 1):
            row[j] = row[j - 1] or (ok[i - 1][j - 1] and prev[j - 1])
    suf = [[False] * (D + 2) for _ in range(R + 1)]
    for j in range(D + 2):
        suf[R][j] = True
    for i in range(R - 1, -1, -1):
        row, nxt = suf[i], suf[i + 1]
        for j in range(D - 1, -1, -1):
            row[j] = row[j + 1] or (ok[i][j] and nxt[j + 1])
    if not suf[0][0]:
        return [None] * R                 # no embedding at all
    out = []
    for i in range(R):
        cols = [j for j in range(D)
                if ok[i][j] and pre[i][j] and suf[i + 1][j + 1]]
        out.append(cols[0] if len(cols) == 1 else None)
    return out


def rising(anchors):
    """the longest strictly ascending sub-list of (release index, debug index)

    Patience sorting; O(n log n). The anchors come from the call graph and are
    almost always already ascending — this only removes the few that contradict
    the shared source order, so one bad pair no longer blocks every gap.
    """
    import bisect
    tails, back, idx = [], [None] * len(anchors), []
    for k, (_i, j) in enumerate(anchors):
        p = bisect.bisect_left(tails, j)
        back[k] = idx[p - 1] if p else None
        if p == len(tails):
            tails.append(j)
            idx.append(k)
        else:
            tails[p], idx[p] = j, k
    out, k = [], idx[-1] if idx else None
    while k is not None:
        out.append(anchors[k])
        k = back[k]
    return out[::-1]


def fill_gaps(code, by_start, rel_routines, pairs, dbg_routines, skip_names,
              imports, calls_cache, covers, strings=None):
    """embed the still unmatched release routines into the debug routine list

    The call graph only carries names where it reaches; whole subtrees stay dark
    when a single block on the way has an ambiguous call list (PDialogUnits left
    33 of 109 release routines unnamed, TwoWorldsCampaign 17 of 43, and the
    lifter then emitted `sub_19988()` / `j = eax` for them).

    Both builds lay routines out in source order, so the release routine list is
    an ordered sub-list of the debug one. Measured over ../roundtrip/ref with the
    call-graph pairs only: 0 order violations in 32 of 36 files, 2 in the three
    PQuests* files. The already matched routines therefore cut both lists into
    aligned gaps, and inside a gap the assignment is an order-preserving
    embedding — decided per position by `forced_positions`, never guessed.

    Candidate filter: same `ret n` frame as the debug parameter list, not a
    Debug.ech routine, at least as many own calls in the debug body as the
    release body has, and every native the release body calls also called by the
    debug body, at least as often. Both counts were checked over every pair
    ../roundtrip/ref already produces: 0 counter-examples for the call count and
    18 of 3643 for the natives, all 18 inside PQuests / PQuestsMulti /
    PQuestsMulti16 — the only three files whose call-graph mapping already
    reports conflicts, so they are wrong pairs, not counter-examples.
    """
    didx = {r['start']: i for i, r in enumerate(dbg_routines)}
    seq = [didx.get(pairs[r['start']]['start']) if r['start'] in pairs else None
           for r in rel_routines]
    doc = [len(own_calls(x, imports, skip_names)) for x in dbg_routines]
    new = []
    # Only anchors that are in ascending order can bound a gap. Bailing on the
    # whole file instead (the first version did) cost PQuestsMulti /
    # PQuestsMulti16 every gap because two of their 419 call-graph pairs sit out
    # of order — the two that the native fingerprint also rejects.
    anchors = rising([(i, seq[i]) for i in range(len(seq)) if seq[i] is not None])
    pi, pj = -1, -1
    for i, j in anchors + [(len(rel_routines), len(dbg_routines))]:
        if i - pi > 1:
            R = rel_routines[pi + 1:i]
            lo, hi = pj + 1, j
            ra = [own_arity(code, r) for r in R]
            rc = [len(calls_cache(r)) for r in R]
            ok = [[(dbg_routines[c]['name'] not in skip_names
                    and len(dbg_routines[c]['params']) == ra[k]
                    and doc[c] >= rc[k]
                    and covers(dbg_routines[c], R[k])
                    and (strings is None or strings(dbg_routines[c], R[k])))
                   for c in range(lo, hi)] for k in range(len(R))]
            for k, col in enumerate(forced_positions(ok)):
                if col is None:
                    continue
                off = R[k]['start']
                if off not in pairs:
                    pairs[off] = dbg_routines[lo + col]
                    new.append(off)
        pi, pj = i, j
    return new


def cmd_arity_fits(code, by_start, off, d):
    """does the release routine at `off` have the frame of command `d`?

    The command table is a fixed-size array; absent commands hold a zero offset.
    Offset 0 is also a perfectly valid code address, so a command that only exists
    in the debug build (`command UserOneParam9` lives behind `#ifdef _DEBUG`,
    Units/CommonUnits.ech:25) used to be seeded onto whatever routine happens to
    start the release code segment. In Unit/UnitBase/Hero that is
    `function int StopCurrentAction()`, and every CHECK_STOP_CURR_ACTION() call
    site then lifted as `UserOneParam9()` - which does not compile.

    A command's own `ret n` always pops one word more than it has declared
    parameters (the implicit command handle). Checked over every seed in
    ../roundtrip/ref: 377 command seeds, 374 obey a == len(params) + 1, and the
    only three exceptions are exactly the bogus UserOneParam9 pairings.
    """
    r = by_start.get(off)
    if r is None:
        return False
    return own_arity(code, r) == len(d['params']) + 1


def match(rel_path, dbg_path):
    frel = eco.parse(rel_path)
    code = bytes(frel['code'])
    rel_routines = disasm.scan_routines(code)
    by_start = {r['start']: r for r in rel_routines}

    fdbg, dbg = ecodbg.parse_file(dbg_path)
    dcode = bytes(fdbg['code'])
    # EarthC allows overloading: Common/Party.ech defines SetPartyEnemies twice
    # (2 and 3 parameters). Keeping only the first record made the second overload
    # unmatchable — its arity never agreed — and it lifted as `sub_485()`, which
    # does not compile. Keyed by name AND parameter count, both are reachable.
    dbg_by_name = collections.defaultdict(list)
    for r in dbg['routines']:
        dbg_by_name[r['name']].append(r)
    skip_names = debug_only(dbg)

    # seed: states / commands / events by index
    pairs, queue = {}, []
    for arr, kind in ((frel['states'], 6), (None, 5), (frel['events'], 4)):
        if kind == 5:
            entries = [(i, c[0]) for i, c in enumerate(frel['commands'])]
        else:
            entries = list(enumerate(arr))
        for idx, off in entries:
            if off not in by_start and not (kind == 6 and idx == 0 and off == 0):
                continue
            d = next((r for r in dbg['routines']
                      if r['kind'] == kind and r['a'] == idx), None)
            if d is not None and kind == 5 and not cmd_arity_fits(code, by_start, off, d):
                continue
            if d is not None and off in by_start and off not in pairs:
                pairs[off] = d
                queue.append(off)

    conflicts = 0
    imports = dict(fdbg['imports'])
    rel_imports = dict(frel['imports'])
    ic_cache = {}
    nat_cache = {}

    def calls_of(r):
        v = ic_cache.get(r['start'])
        if v is None:
            v = ic_cache[r['start']] = internal_calls(code, r)
        return v

    def dbg_natives(d):
        v = nat_cache.get(d['start'])
        if v is None:
            v = nat_cache[d['start']] = collections.Counter(
                native_calls(dcode, imports, d))
        return v

    def rel_natives(r):
        v = nat_cache.get(('r', r['start']))
        if v is None:
            v = nat_cache[('r', r['start'])] = collections.Counter(
                native_calls(code, rel_imports, r))
        return v

    def covers(d, r):
        """does the debug body call every native the release body calls?"""
        return not (rel_natives(r) - dbg_natives(d))

    dstr_cache, rstr_cache = {}, {}
    ddata, rdata = bytes(fdbg['data_seg']), bytes(frel['data_seg'])
    dcptr, rcptr = set(fdbg['code_ptrs']), set(frel['code_ptrs'])

    def strings_ok(d, r):
        """does the debug body reference every string literal the release body does?

        Sharper than the native fingerprint wherever a family of routines compiles
        to identical code and differs only in a constant — the attribute getters of
        PDialogUnits (GetMainState / GetDailyState / GetActivity) all passed the
        native filter, so `forced_positions` saw four candidates for one release
        routine and declined. The events that call them then fell back to a stub and
        22 of 109 routines went with them (EarthC drops what nothing reaches).
        """
        # NOT DONE: requiring the integer literals (const_refs) as well. They are not
        # build-invariant — the debug build reaches some of them differently, so the
        # subset test fails on pairs that are right, and the matcher LOSES ground:
        # unmatched release routines went from 3 to 6 in PDialogUnits, 4 to 10 in
        # PTown, 4 to 17 in TwoWorldsCampaign and 5 to 20 in PQuests. Only the
        # string literals survive `-D _DEBUG` unchanged.
        v = dstr_cache.get(d['start'])
        if v is None:
            v = dstr_cache[d['start']] = string_refs(dcode, dcptr, ddata, d)
        w = rstr_cache.get(r['start'])
        if w is None:
            w = rstr_cache[r['start']] = string_refs(code, rcptr, rdata, r)
        return not (w - v)

    def propagate(queue):
        bad = 0
        while queue:
            off = queue.pop(0)
            rrel, rdbg = by_start[off], pairs[off]
            rel_targets = calls_of(rrel)
            dbg_targets = own_calls(rdbg, imports, skip_names)
            if len(rel_targets) != len(dbg_targets):
                def fits(i, name, _t=None):
                    t = rel_targets[i]
                    if t not in by_start:
                        return True       # not a routine start: cannot judge
                    n = own_arity(code, by_start[t])
                    if t in pairs:
                        return pairs[t]['name'] == name
                    return any(len(x['params']) == n
                               for x in dbg_by_name.get(name, ()))
                dbg_targets = embed(rel_targets, dbg_targets, fits)
                if dbg_targets is None:
                    continue              # shapes differ, do not guess
            for t, name in zip(rel_targets, dbg_targets):
                if t not in by_start:
                    continue
                n = own_arity(code, by_start[t])
                cands = [x for x in dbg_by_name.get(name, ())
                         if len(x['params']) == n]
                if len(cands) > 1:
                    # Same name, same parameter count, different parameter *types*:
                    # `RespawnTeamHero(int)` / `RespawnTeamHero(unit)`
                    # (MissionTeamBase.ech:76-77). Taking the first put an `int`
                    # signature on the routine that is called with a unit. The body
                    # tells them apart — the debug build calls at least the natives
                    # and at least as many internal routines as the release build,
                    # so an overload that covers the native fingerprint wins, and
                    # among those the closest call count from above.
                    # `extra` is the third signal and the one that separates
                    # overloads with the same arity and no internal calls at all:
                    # how many natives the debug body calls that the release body
                    # does not. `GetStrength(unit)` does
                    # `pUnit.GetUnitValues().GetPoint(…)` and
                    # `GetStrength(UnitValues)` just `unVal.GetPoint(…)`
                    # (RPGCompute/Unit.ech:115/119) — same name, one parameter each,
                    # zero internal calls each, so the first two keys tie and the
                    # `unit` record won. Its `unit` parameter then made the emitted
                    # `pUnit.GetPoint(2)` an "Unknown member or function" and took
                    # the whole file down.
                    rt = len(calls_of(by_start[t]))
                    def extra(x, _r=by_start[t]):
                        return sum((dbg_natives(x) - rel_natives(_r)).values())
                    cands.sort(key=lambda x: (
                        not covers(x, by_start[t]),
                        len(own_calls(x, imports, skip_names)) < rt,
                        abs(len(own_calls(x, imports, skip_names)) - rt),
                        extra(x)))
                d = cands[0] if cands else None
                if d is None:
                    continue              # unknown, or no overload of that arity
                if t in pairs:
                    if pairs[t]['name'] != name:
                        bad += 1
                    continue
                pairs[t] = d
                queue.append(t)
        return bad

    conflicts += propagate(queue)
    # The call graph stops wherever a block's call list is ambiguous. The routine
    # layout is the second, independent source of order — alternate the two until
    # neither adds anything.
    while True:
        found = fill_gaps(code, by_start, rel_routines, pairs, dbg['routines'],
                          skip_names, imports, calls_of, covers, strings_ok)
        if not found:
            break
        conflicts += propagate(found)

    # Overloads that share a name AND a parameter count are still two different
    # routines — MissionTeamBase.ech:76-77 declares `RespawnTeamHero(int)` and
    # `RespawnTeamHero(unit)`. The lookup above always returns the first, so both
    # release routines were paired with the `int` record and the `unit` overload
    # was never emitted at all (`RespawnTeamHero(pHero)` then did not compile in
    # MissionTeamAssault / Deathmatch / MonsterHunt). Both builds lay routines out
    # in source order, so equally many claimants and candidates line up by address.
    claims = collections.defaultdict(list)
    for off, d in pairs.items():
        claims[(d['name'], len(d['params']))].append(off)
    for (nm, np), offs in claims.items():
        if len(offs) < 2:
            continue
        cands = [x for x in dbg_by_name.get(nm, ()) if len(x['params']) == np]
        if len(cands) != len(offs):
            continue
        offs = sorted(offs)
        cands = sorted(cands, key=lambda x: x['start'])
        # Address order is the rule, but the six `SendMessageToGlobalScripts`
        # overloads of PInc/PCommon.ech all have three parameters and are not laid
        # out in the same relative order in both builds: PQuests got the
        # `(int, unit, int)` record on the `(int, int, int)` body and lifted
        # `CommandMessage(nMessage, uUnit, nValue, 0)`, which has no overload.
        # The native fingerprint decides when it can: if address order contradicts
        # it and exactly one permutation does not, that permutation wins.
        if not all(covers(d, by_start[o]) for o, d in zip(offs, cands)):
            good = [p for p in itertools.permutations(cands)
                    if all(covers(d, by_start[o]) for o, d in zip(offs, p))] \
                if len(offs) <= 6 else []
            if len(good) == 1:
                cands = list(good[0])
        for off, d in zip(offs, cands):
            pairs[off] = d
    return frel, rel_routines, pairs, conflicts


if __name__ == '__main__':
    frel, routines, pairs, conflicts = match(sys.argv[1], sys.argv[2])
    print(f'release routines: {len(routines)}, matched: {len(pairs)}, '
          f'conflicts: {conflicts}')
    for r in routines:
        d = pairs.get(r['start'])
        sig = (', '.join(f'{p["kind"]}:{p["name"]}' for p in d['params'])
               if d else '')
        print(f'  {r["start"]:>6}..{r["end"]:<6} '
              f'{(d["name"] if d else "?"):<34} kind={d["kind"] if d else "-"} '
              f'({sig[:60]})')
