"""Native function index -> name, rebuilt from the debug .eco (cached in ../out)."""
import pathlib, json, collections
import eco, ecodbg

CACHE = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_map.json')


def build(roots=(pathlib.Path('../roundtrip/ref'), pathlib.Path('../eco/Scripts_wd'))):
    """Index -> name from every debug build available.

    Order matters: the freshly compiled SDK builds come first. Three v1.0 scripts
    (Cities, CityCampaign, MissionTeamHunt) were translated against an OLDER native
    table — all 16 name conflicts between the two sets come from those files (e.g.
    0x038a is CreateHero there but GetWorldHeight everywhere else). Mixing both index
    spaces would put wrong names on the majority of files, so later sources only fill
    gaps and never override a name the SDK-era builds already established.
    """
    tab = collections.defaultdict(collections.Counter)
    OLD_INDEX_SPACE = {'Cities', 'CityCampaign', 'MissionTeamHunt'}
    files = []
    for root in roots:
        if root.exists():
            files += [p for p in sorted(root.rglob('*.eco'))
                      if p.stem not in OLD_INDEX_SPACE
                      and not p.name.endswith('.release.eco')]
    for p in files:
        try:
            f, b = ecodbg.parse_file(p)
        except Exception:
            continue
        if not b:
            continue
        loc2fn = dict(f['imports'])
        for r in b['routines']:
            for ref in r['refs']:
                fn = loc2fn.get(ref['addr'] - 4)
                if fn is not None:
                    tab[fn][ref['name']] += 1
    out = {}
    for idx, c in tab.items():
        name, hits = c.most_common(1)[0]
        out[str(idx)] = {'name': name, 'hits': hits,
                         'alt': [n for n, _ in c.most_common()[1:]]}
    return out


RET = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_ret.json')


def ret_kinds():
    """native index -> return kind, from the same call-site records as the names.

    A ref record carries the callee's return kind (see Lifter.return_kinds); for a
    native call the Function Imports array says which index it is. Kind 0 is "handle
    to an object", and that is what decides whether a comparison against zero has to
    be written `!= null` — `GetCampaign().GetPlayerHeroUnit(n) != 0` does not compile
    (Network/MissionTeamAssault.ec and friends).
    """
    if RET.exists():
        return {int(k): v for k, v in json.loads(RET.read_text()).items()}
    votes = collections.defaultdict(collections.Counter)
    for root in (pathlib.Path('../roundtrip/ref'), pathlib.Path('../eco/Scripts_wd')):
        if not root.exists():
            continue
        for p in sorted(root.rglob('*.eco')):
            if p.name.endswith('.release.eco'):
                continue
            try:
                f, b = ecodbg.parse_file(p)
            except Exception:
                continue
            if not b:
                continue
            loc2fn = dict(f['imports'])
            for r in b['routines']:
                for ref in r['refs']:
                    fn = loc2fn.get(ref['addr'] - 4)
                    if fn is not None:
                        votes[fn][ref['kind']] += 1
    out = {i: c.most_common(1)[0][0] for i, c in votes.items()}
    RET.parent.mkdir(exist_ok=True)
    RET.write_text(json.dumps({str(k): v for k, v in sorted(out.items())}, indent=1))
    return out


ARGS = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_argkind.json')
_building_args = False

# Positions the call-site evidence below cannot reach, filled from the SDK's own
# declaration. `null` is not only the object spelling: EarthC accepts it for a
# `string` parameter too, and there the corpus rule works against itself — every
# call site that does pass a real string at that position counts as evidence that it
# is NOT a handle position, so it is excluded and the lifted `0` stays a `0`.
#   0x0595 unit::CreateAlchemyResult(string pszObjectID, int nCount,
#          int bCreateChangedParams, string strCopyFromObjectID,
#          EquipmentValues pCopyFromEquipmentValues, object pCopyFromChangedParams)
#          (Documentation/EarthC/unit.htm:584; positions here count the receiver, so
#          the three that take null are 4, 5 and 6.) RPGCompute/Alchemy.ech:1037
#          writes `uHero.CreateAlchemyResult(MakeBottle(), 1, true, null, null, null)`
#          and without this the recompile fails with "Cannot find suitable".
DOCUMENTED_NULL = {
    0x595: (4, 5, 6),
}


def arg_handles():
    """native index -> the argument positions that hold an object handle

    There is no parameter table for natives anywhere in a .eco, so the types are
    read off the call sites of the debug builds: lift every routine and ask each
    argument expression what it is declared as (Lifter.arg_evidence). A position
    counts as a handle when at least one call site passes something declared with
    an object type and no call site ever passes something declared int/string.
    That is what turns `CanMakeMagic(0, n, 1, 1)` into `CanMakeMagic(null, …)`,
    which is the spelling EarthC accepts (Units/Magic.ech:768).

    Positions where the evidence disagrees are left out — never guessed.
    """
    global _building_args
    if _building_args:
        return {}                     # the learning pass itself must lift plainly
    if ARGS.exists():
        return _with_documented(
            {int(k): v for k, v in json.loads(ARGS.read_text()).items()})
    import lifter
    sink = collections.defaultdict(collections.Counter)
    _building_args = True
    lifter.ARGKIND_SINK = sink
    try:
        for root in (pathlib.Path('../roundtrip/ref'),
                     pathlib.Path('../eco/Scripts_wd')):
            if not root.exists():
                continue
            for p in sorted(root.rglob('*.eco')):
                if p.name.endswith('.release.eco'):
                    continue
                try:
                    lf = lifter.Lifter(p)
                except Exception:
                    continue
                if not lf.b:
                    continue
                for r in lf.routines:
                    try:
                        lf.simulate(r)
                    except Exception:
                        pass
    finally:
        lifter.ARGKIND_SINK = None
        _building_args = False
    out = collections.defaultdict(list)
    for (idx, pos), c in sorted(sink.items()):
        if c['obj'] and not c['int']:
            out[idx].append(pos)
    ARGS.parent.mkdir(exist_ok=True)
    ARGS.write_text(json.dumps({str(k): v for k, v in sorted(out.items())},
                               indent=1))
    return _with_documented(dict(out))


def _with_documented(d):
    """merge DOCUMENTED_NULL in — the cache file stays pure measurement"""
    for idx, pos in DOCUMENTED_NULL.items():
        d[idx] = sorted(set(d.get(idx, ())) | set(pos))
    return d


def load():
    if CACHE.exists():
        return {int(k): v for k, v in json.loads(CACHE.read_text()).items()}
    m = build()
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(m, indent=1))
    return {int(k): v for k, v in m.items()}


def name_of(m, idx):
    e = m.get(idx)
    if not e:
        return f'native_{idx:04x}'
    return e['name'] + ('?' if e['alt'] else '')


if __name__ == '__main__':
    m = load()
    print(f'{len(m)} native names cached in {CACHE}')
