"""Commands and events are fixed slots per script class, not names the script chooses.

A campaign has 29 command slots and 35 event slots, a mission 28 / 34, a unit 164 / 9 (emit_ec.CLASS);
slot 23 of a campaign IS `FillNetworkChannelLevelsList` whatever the script does with it (Network/Towns.ec).
A release build only says which slots it fills, so the name and the parameter list of every slot have to
come from somewhere else: from debug builds of any script of the same class, which carry both.

  py entries.py <debug.eco> [...]     harvest into data/entry_table.json (merged with what is there)
"""
import collections
import json
import pathlib
import sys

import ecodbg

TABLE = pathlib.Path(__file__).resolve().parent / 'data' / 'entry_table.json'
# v1.0 debug builds made by an older compiler whose slot lists differ (one event less in the script base,
# so RemovedUnit sits at 20 there instead of 21) - see slot_table.py
OLD_SPACE = {'cities', 'citycampaign', 'missionteamhunt'}
KINDS = {4: 'event', 5: 'command'}
_cache = None


def _param(p):
    t = p.get('type')
    return {'name': p['name'], 'kind': p['kind'], 'sub': p.get('sub', 0),
            'type': t if isinstance(t, str) else 0, 'flags': p.get('flags', 0), 'addr': p.get('addr', 0),
            'tidx': p.get('tidx', 0xFFFFFFFF)}


def harvest(paths, table=None):
    """{class id: {'command'|'event': {index: {'name', 'params'}}}} from debug builds; the most frequent
    signature wins where two builds disagree (a parameter renamed between SDK versions)."""
    votes = collections.defaultdict(collections.Counter)
    for p in paths:
        if pathlib.Path(p).stem.lower() in OLD_SPACE:
            continue
        try:
            f, b = ecodbg.parse_file(str(p))
        except Exception:
            continue
        if not b:
            continue
        cls = f['namerec'].get('num', 0)
        for r in b['routines']:
            kind = KINDS.get(r['kind'])
            if kind is None:
                continue
            key = (cls, kind, r['a'])
            votes[key][json.dumps({'name': r['name'], 'params': [_param(x) for x in r['params']]},
                                  sort_keys=True)] += 1
    out = {} if table is None else {c: {k: dict(v) for k, v in d.items()} for c, d in table.items()}
    for (cls, kind, idx), c in votes.items():
        out.setdefault(str(cls), {}).setdefault(kind, {})[str(idx)] = json.loads(c.most_common(1)[0][0])
    return out


def load():
    global _cache
    if _cache is None:
        _cache = json.loads(TABLE.read_text(encoding='utf-8')) if TABLE.exists() else {}
    return _cache


def lookup(cls, kind, idx):
    """the slot's {'name', 'params'} or None"""
    return load().get(str(cls), {}).get(kind, {}).get(str(idx))


if __name__ == '__main__':
    t = harvest(sys.argv[1:], load())
    TABLE.write_text(json.dumps(t, indent=1, sort_keys=True), encoding='utf-8')
    for cls, d in sorted(t.items()):
        print(cls, {k: len(v) for k, v in d.items()})
