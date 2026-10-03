"""Debug builds of the v1.0 compiler (the game's Cities, CityCampaign and MissionTeamHunt).

That compiler numbered the engine functions differently: functions were added and removed before SDK 1.3,
so the index of a call means something else (0x38a is CreateHero there, GetWorldHeight in 1.3). Everything
else in this package works in SDK 1.3 numbering, so such a build is translated on loading: every import
index is replaced by the 1.3 index of the same function (`data/v10_native_map.json`).

How the map is made (`build`), strongest evidence first:

1. measured: a library routine (TraceText, _TraceText, ... from the SDK's include files) that is the same
   program in a v1.0 build and in an SDK 1.3 debug build - same name, parameters, size and code, compared
   with dbgcompare - calls the same engine functions at the same places: 148 of Cities/CityCampaign's 200
   indices and 161 of MissionTeamHunt's 212, without one contradiction (the line hook is 0x2f7 in v1.0 and
   0x2f4 in 1.3, TraceDbg(string) 0x2f3/0x2f0).
2. named: the v1.0 build's own debug info names every call written in the source. The 1.3 indices of that
   name (native_sigs, native_map) are filtered by the arity solved from the v1.0 builds and the one closest
   to the offset of the measured neighbours is taken.
3. unnamed (calls the compiler makes itself): the offset of the measured neighbours, where both agree.

Emitting works by name either way - a call is written with the name the debug info gives and the 1.3
compiler picks the overload from the argument types - so a wrong 1.3 index for a named function shows up as
a difference in the round trip (dbgcompare compares the import tables after this map), not as a wrong
program.

  py v10.py <old .eco ...> -- <SDK 1.3 debug builds ...>   -> data/v10_native_map.json
  (with data/v10_observed.json from a round trip as further evidence, see observed_pairs)
"""
import bisect
import collections
import json
import pathlib
import sys

import ecodbg

HERE = pathlib.Path(__file__).resolve().parent
MAP = HERE / 'data' / 'v10_native_map.json'
_map = None


def _json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def names_of(paths):
    """{index: {names}} and a Counter of uses over the debug builds at `paths`"""
    used, named = collections.Counter(), collections.defaultdict(set)
    for p in paths:
        f, b = ecodbg.parse_file(p)
        if not b:
            continue
        loc2fn = dict(f['imports'])
        used.update(fn for _l, fn in f['imports'])
        for r in b['routines']:
            for ref in r['refs']:
                fn = loc2fn.get(ref['addr'] - 4)
                if fn is not None:
                    named[fn].add(ref['name'])
    return named, used


def measured_pairs(old_paths, new_paths):
    """{old index: 1.3 index} from library routines that are the same program in both builds"""
    import dbgcompare
    cs = dbgcompare._cs()

    def prep(p):
        B = dbgcompare.Build(str(p))
        B.imp = dict(B.f['imports'])
        B.locs = sorted(B.imp)
        B.by_name = collections.defaultdict(list)
        for r in B.routines:
            B.by_name[r['name']].append(r)
        return B

    def calls(B, r):
        a, z = bisect.bisect_left(B.locs, r['start']), bisect.bisect_right(B.locs, r['end'])
        return [(loc - r['start'], B.imp[loc]) for loc in B.locs[a:z]]

    def sig(r):
        # overloads share name and code: TraceText(int) and TraceText(string) differ only in the TraceDbg
        # overload they call, so routines match only with the same parameter list
        return [(p['kind'], p['type'], p['sub']) for p in r['params']], r.get('kind')

    news = [prep(p) for p in new_paths]
    pairs = collections.defaultdict(collections.Counter)
    for p in old_paths:
        A = prep(p)
        for r in A.routines:
            for B in news:
                hit = None
                for rb in B.by_name.get(r['name'], ()):
                    if rb['end'] - rb['start'] != r['end'] - r['start'] or sig(rb) != sig(r):
                        continue
                    ca, cb = calls(A, r), calls(B, rb)
                    if [c[0] for c in ca] == [c[0] for c in cb] and \
                            dbgcompare.compare_routine(A, B, r, rb, mask_lines=True, cs=cs) is None:
                        hit = (ca, cb)
                        break
                if hit:
                    for (_l, o), (_l2, n) in zip(*hit):
                        pairs[o][n] += 1
                    break
    bad = {o: dict(c) for o, c in pairs.items() if len(c) > 1}
    if bad:
        raise RuntimeError(f'measured pairs contradict each other: {bad}')
    return {o: next(iter(c)) for o, c in pairs.items()}


def _hook_index(f):
    """the import index of the debug line hook: differs between compiler versions (v1.0 Cities 759,
    MissionTeamHunt 756 like SDK 1.3)"""
    import dbgcompare
    imp = dict(f['imports'])
    c = collections.Counter(imp.get(mo.start(3)) for mo in dbgcompare.DBG_HOOK.finditer(bytes(f['code'])))
    return c.most_common(1)[0][0] if c else None


def groups(old_paths):
    """the files grouped by numbering: same line hook index and no index named two ways"""
    out = []
    for p in old_paths:
        f, _b = ecodbg.parse_file(p)
        named, _u = names_of([p])
        hook = _hook_index(f)
        for g in out:
            if g['hook'] == hook and all(named[i] & g['named'][i] for i in set(named) & set(g['named'])):
                g['paths'].append(p)
                for i, ns in named.items():
                    g['named'][i] |= ns
                break
        else:
            out.append({'hook': hook, 'paths': [p], 'named': collections.defaultdict(set, named)})
    return out


def observed_pairs(ours, original):
    """{old index: 1.3 index} seen in a round trip: in every routine that came out the same size with the
    engine calls at the same places, the original's call and ours call the same function. Our source names
    each call as the original's own records do and the 1.3 compiler picks the index, so this is measured -
    it settles the overloads the name alone leaves open (Cities' GetFillChannelLevelsListCommonPlayerAttribute
    is 0x329 in 1.3, not the 0x327 the order suggested)."""
    import dbgcompare
    A, B = dbgcompare.Build(str(ours)), dbgcompare.Build(str(original))
    ia, ib = dict(A.f['imports']), dict(B.f['imports'])
    seen = collections.defaultdict(collections.Counter)
    for ra, rb in zip(A.routines, B.routines):
        if ra['name'] != rb['name'] or ra['end'] - ra['start'] != rb['end'] - rb['start']:
            continue
        ca = [(loc - ra['start'], fn) for loc, fn in sorted(ia.items()) if ra['start'] <= loc <= ra['end']]
        cb = [(loc - rb['start'], fn) for loc, fn in sorted(ib.items()) if rb['start'] <= loc <= rb['end']]
        if [c[0] for c in ca] != [c[0] for c in cb]:
            continue
        for (_l, n), (_l2, o) in zip(ca, cb):
            seen[o][n] += 1
    return {o: c.most_common(1)[0][0] for o, c in seen.items() if len(c) == 1}


def learned_arities(paths, indices, current=None):
    """{old index: arity} measured at the call sites: the lifter runs every routine with these indices
    unknown and reads each one's arity off a basic block that starts and ends with an empty argument stack
    (Lifter.infer_arity) - first-hand, where the corpus-wide solver over two files guesses (it gives 2 for
    CityCampaign's `GetCampaign().CommandMessage(33, 27)`, which pushes two arguments and the receiver)"""
    import lifter
    global synthetic, override
    out = {}
    old, synthetic = synthetic, set(indices)
    old_ov, override = override, current
    try:
        for p in paths:
            lf = lifter.Lifter(str(p))
            if not lf.f.get('v10'):
                continue
            for r in lf.routines:
                try:
                    lf.simulate(r)
                except Exception:
                    continue
            for idx, n in lf.learned.items():
                if idx >= SYNTH:
                    out.setdefault(idx - SYNTH, n)
    finally:
        synthetic, override = old, old_ov
    return out


def build_group(old_paths, new_paths, observed=None):
    import arity4
    named, used = names_of(old_paths)
    pairs = measured_pairs(old_paths, new_paths)
    eqs = []
    for p in old_paths:
        eqs += arity4.file_eqs(pathlib.Path(p))
    solved = arity4.solve(eqs)
    old_ari = solved[0] if isinstance(solved, tuple) else solved
    sigs = {int(k): v for k, v in _json(HERE / 'data' / 'native_sigs.json').items()}
    nmap = {int(k): v for k, v in _json(HERE / 'data' / 'native_map.json').items()}
    new_ari = arity4.load()
    by_name = collections.defaultdict(set)
    for i, s_ in sigs.items():
        by_name[s_['name']].add(i)
    for i, v in nmap.items():
        by_name[v['name'] if isinstance(v, dict) else v].add(i)

    def ari13(j):
        return sigs[j]['arity'] if j in sigs else new_ari.get(j)

    named13 = set().union(*by_name.values())
    life = {int(k) for k in _json(HERE / 'data' / 'class_lifecycle.json')}
    out, how = dict(pairs), {o: 'measured' for o in pairs}
    for o, n in (observed or {}).items():
        if o in used and o not in out:
            out[o], how[o] = n, 'roundtrip'
    # The offset new - old only grows or shrinks between registrations, so an index lies between the offsets
    # of its resolved neighbours; repeat while that pins down more of them.
    # Named indices first (their own name pins them), unnamed ones only between two resolved neighbours.
    changed, unnamed_too = True, False
    while changed or not unnamed_too:
        if not changed:
            unnamed_too = True
        changed = False
        done = sorted(o for o, v in out.items() if v is not None)
        for o in sorted(used):
            if out.get(o) is not None:
                continue
            k = bisect.bisect_left(done, o)
            if o not in named and (not unnamed_too or k == 0 or k == len(done)):
                continue
            lo = out[done[k - 1]] - done[k - 1] if k > 0 else 0
            hi = out[done[k]] - done[k] if k < len(done) else lo
            window = range(min(lo, hi), max(lo, hi) + 1)
            if o in named:
                cands = sorted(set().union(*(by_name.get(n, set()) for n in named[o])))
                a = old_ari.get(o)
                if len(cands) == 1:
                    # one 1.3 function of that name: it is the one (the arity solved from two files alone
                    # is too weak to overrule a name - it gives IsGate 2 instead of 1)
                    out[o], how[o] = cands[0], 'named'
                    changed = True
                    continue
                ok_ari = [c for c in cands if a is None or ari13(c) is None or ari13(c) == a]
                fit = [c for c in ok_ari if c - o in window]
                adj = [c for c in cands if any(out.get(o + s) is not None and out[o + s] - (o + s) == c - o
                                               for s in (-1, 1))]
                # past the last resolved neighbour the window is a single offset although the table goes on
                # growing: then the candidate closest to the neighbours' offsets, if it is clearly closest
                dist = lambda c: min(abs((c - o) - lo), abs((c - o) - hi))   # noqa: E731
                near = sorted(ok_ari, key=dist)
                close = near[0] if near and (len(near) == 1 or dist(near[0]) < dist(near[1])) else None
                pick = fit[0] if len(fit) == 1 else adj[0] if len(adj) == 1 else close
                how[o] = 'named'
            else:
                # A call without a name record is one the compiler wrote itself: mostly a handle's AddRef /
                # Release (CityCampaign's `uHero = pStartMission.CreateHero(...)` calls old 0x562/0x563, unit's
                # pair 0x579/0x57a in 1.3), so a pair of neighbouring unnamed indices lands on a lifecycle pair.
                pair = sorted({d for d in window if o + d in life and any(
                    (o + s) in used and (o + s) not in named and (o + s + d) in life for s in (-1, 1))})
                free = [o + d for d in window if o + d not in named13]
                pick = (o + pair[0] if len(pair) == 1 else o + lo if lo == hi else
                        free[0] if len(free) == 1 else None)
                how[o] = 'neighbours'
            out[o] = pick
            changed |= pick is not None
    # Overloads the name and the order leave open: measure their arity at the call sites, with everything else
    # translated, and keep the candidate of that arity. Then once more through the loop above.
    doubt = [o for o in used if o in named and how.get(o) != 'measured' and how.get(o) != 'roundtrip'
             and len(set().union(*(by_name.get(n, set()) for n in named[o]))) > 1]
    if doubt:
        cur = {o: v for o, v in out.items() if v is not None}
        learned = learned_arities(old_paths, doubt, cur)
        for o in doubt:
            a = learned.get(o)
            if a is None:
                continue
            cands = sorted(set().union(*(by_name.get(n, set()) for n in named[o])))
            fit = [c for c in cands if ari13(c) == a]
            if len(fit) == 1:
                out[o], how[o] = fit[0], 'named+arity'
            elif fit and out.get(o) not in fit:
                k = bisect.bisect_left(sorted(cur), o)
                ks = sorted(cur)
                lo = cur[ks[k - 1]] - ks[k - 1] if k > 0 else 0
                out[o], how[o] = min(fit, key=lambda c: abs((c - o) - lo)), 'named+arity'
    # what is still open gets the closest guess, marked as such: the round trip shows whether it holds
    done = sorted(o for o, v in out.items() if v is not None)
    for o in sorted(used):
        if out.get(o) is not None:
            continue
        k = bisect.bisect_left(done, o)
        lo = out[done[k - 1]] - done[k - 1] if k > 0 else 0
        cands = sorted(set().union(*(by_name.get(n, set()) for n in named.get(o, ()))))
        out[o] = min(cands, key=lambda c: abs((c - o) - lo)) if cands else o + lo
        how[o] = 'guess'
    return {'scripts': sorted(pathlib.Path(p).stem.lower() for p in old_paths),
            'map': {str(o): v for o, v in sorted(out.items())},
            'how': {str(o): v for o, v in sorted(how.items())},
            'names': {str(o): sorted(v) for o, v in sorted(named.items())}}


def build(old_paths, new_paths, observed=None):
    """observed: {script name: {old: 1.3}} from earlier round trips (observed_pairs)"""
    out = []
    for g in groups(old_paths):
        obs = {}
        for p in g['paths']:
            obs.update((observed or {}).get(pathlib.Path(p).stem.lower(), {}))
        out.append(build_group(g['paths'], new_paths, obs))
    return {'groups': out}


def load(script):
    """(old index -> 1.3 index, old index -> names) for the script `script` (file name without .eco)"""
    global _map
    if _map is None:
        try:
            _map = _json(MAP)['groups']
        except (OSError, ValueError, KeyError):
            _map = []
    for g in _map:
        if script.lower() in g['scripts']:
            return ({int(k): v for k, v in g['map'].items()}, {int(k): v for k, v in g['names'].items()})
    return {}, {}


HOOK_13 = 0x2f4          # the debug line hook's index in SDK 1.3 (every round-trip debug ref, 3398 hooks in PTown)


def has_slot(cls, kind, name, params):
    """True if SDK 1.3 has a command / event `name` with these parameter kinds for the class `cls`. v1.0's
    campaign had FillNetworkMissionsList with four parameters (1.3: eight) and IsNetworkMissionAvailable
    (gone in 1.3): such a routine cannot be declared as a command any more."""
    import slot_table
    want = [(p['kind'], p['sub']) for p in params]
    return any(e['name'] == name and e.get('params') is not None and
               [(p['kind'], p['sub']) for p in e['params']] == want
               for e in slot_table.load().get(str(cls), {}).get(kind, {}).values())


def is_old(f, b):
    """a debug build of an older compiler: its line hook is not 1.3's (Cities, CityCampaign: 0x2f7), or its
    own call records contradict the 1.3 names at their indices (MissionTeamHunt: 70 of 1629)"""
    if not b:
        return False
    if _hook_index(f) not in (None, HOOK_13):
        return True
    import nativemap
    nat = nativemap.load()
    loc2fn = dict(f['imports'])
    seen = bad = 0
    for r in b['routines']:
        for ref in r['refs']:
            fn = loc2fn.get(ref['addr'] - 4)
            if fn is None:
                continue
            known = nativemap.name_of(nat, fn)
            if known and not known.startswith('native_'):
                seen += 1
                bad += known != ref['name']
    return bad >= 3


SYNTH = 0x10000           # translate() maps an index in `synthetic` to SYNTH + index: no 1.3 entry, unknown arity
synthetic = set()
override = None           # the map being built (learned_arities lifts with it before it is written)


def translate(f, script):
    """the import table in 1.3 numbering; raises on an index the map does not know"""
    m = override if override is not None else load(script)[0]
    if override is not None:                      # building: what is still open stays unknown
        m = {**{fn: SYNTH + fn for _l, fn in f['imports']}, **{o: v for o, v in m.items() if v is not None}}
    m = {**m, **{o: SYNTH + o for o in synthetic}}
    missing = sorted({fn for _l, fn in f['imports'] if m.get(fn) is None})
    if missing:
        raise KeyError(f'v1.0 engine functions without a 1.3 index: {[hex(i) for i in missing]}')
    f['imports'] = [(loc, m[fn]) for loc, fn in f['imports']]
    f['v10'] = True
    return f


def site_names(f, b):
    """{import location: name} from the build's own call records - the name of every call the source wrote"""
    out = {}
    locs = {loc for loc, _fn in f['imports']}
    for r in b['routines']:
        for ref in r['refs']:
            if ref['addr'] - 4 in locs:
                out[ref['addr'] - 4] = ref['name']
    return out


_known13 = None


def known_in_13():
    """every engine function name SDK 1.3 knows: its documented signatures (each compiled with 1.3) and the
    names in its EarthC.exe. Not native_map: that one is filled from every debug build there is, the v1.0
    ones included, and so names GetObject, which SDK 1.3 has no more."""
    global _known13
    if _known13 is None:
        import re
        k = {v['name'] for v in _json(HERE / 'data' / 'native_sigs.json').values()}
        api = (HERE / 'data' / 'native_api.txt').read_text(encoding='utf-8', errors='replace')
        k |= set(re.findall(r'^\s*\d+\s+\$?(\w+)\s', api, re.M))
        _known13 = k
    return _known13


def _call_end(line, start):
    """index behind the `)` closing the call whose `(` is at `start`"""
    depth, q = 0, None
    for i in range(start, len(line)):
        c = line[i]
        if q:
            if c == q and line[i - 1] != chr(92):
                q = None
        elif c in ('"', "'"):
            q = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def comment_out_missing(lines, f):
    """A call of a function SDK 1.3 no longer has (CityCampaign's SetPlayerHeroUnit, MissionTeamHunt's
    GetObject) cannot be compiled. A statement of its own stays in the source as a comment; inside a
    condition the call is replaced by `null` / `0` (by the return kind the original's records give) with the
    call in a comment, so the file compiles and says what the original did."""
    import re
    missing = sorted({n for n in f.get('site_names', {}).values() if n not in known_in_13()})
    if not missing:
        return lines, 0
    kinds = f.get('site_kinds', {})
    pat = re.compile(r'(?<![\w.])((?:\w+\.)?(' + '|'.join(map(re.escape, missing)) + r'))\s*\(')
    out, n = [], 0
    for line in lines:
        if not pat.search(line) or line.lstrip().startswith('//'):
            out.append(line)
            continue
        n += 1
        pad = line[:len(line) - len(line.lstrip())]
        body = line.strip()
        if body.endswith(';') and '{' not in body and '}' not in body:
            out.append(f'{pad}// not in SDK 1.3 (v1.0 only): {body}')
            continue
        gone = []
        while True:
            mo = pat.search(line)
            if not mo:
                break
            end = _call_end(line, mo.end() - 1)
            if end is None:
                break
            gone.append(line[mo.start():end])
            line = line[:mo.start()] + ('null' if kinds.get(mo.group(2)) == 0 else '0') + line[end:]
        out.append(f'{line} // not in SDK 1.3 (v1.0 only): {"; ".join(gone)}')
    return out, n


def parse(path):
    """ecodbg.parse_file, with a v1.0 debug build translated to 1.3 numbering"""
    f, b = ecodbg.parse_file(path)
    if is_old(f, b) and fits(f, b, pathlib.Path(path).stem):
        f['site_names'] = site_names(f, b)
        f['site_kinds'] = {ref['name']: ref['kind'] for r in b['routines'] for ref in r['refs']}
        translate(f, pathlib.Path(path).stem)
    return f, b


def fits(f, b, script):
    """True if the map kept for `script` is this build's: every index it calls has a 1.3 entry, and every call
    it names carries a name the map knows for that index. A mod's own Cities.eco from another compiler, or a
    debug build the map has never seen, stays untranslated (the decompiler then shows what it can) instead of
    being read with another file's numbers."""
    m, names = load(script)
    if not m or any(m.get(fn) is None for _l, fn in f['imports']):
        return False
    loc2fn = dict(f['imports'])
    return all(n in names.get(loc2fn[loc], ()) for loc, n in site_names(f, b).items())


if __name__ == '__main__':
    k = sys.argv.index('--')
    obs_file = HERE / 'data' / 'v10_observed.json'      # written by the round trip (_spike/p2/v10_roundtrip.py)
    observed = ({n: {int(o): v for o, v in d.items()} for n, d in _json(obs_file).items()}
                if obs_file.exists() else None)
    res = build(sys.argv[1:k], sys.argv[k + 1:], observed)
    MAP.write_text(json.dumps(res, indent=0), encoding='utf-8')
    for g in res['groups']:
        n = sum(1 for v in g['map'].values() if v is not None)
        print(f'{g["scripts"]}: {n} of {len(g["map"])} indices mapped')
        for o, v in g['map'].items():
            if v is None:
                print('  unresolved', hex(int(o)), g['names'].get(o, '-'))
