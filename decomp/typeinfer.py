"""Types for a release build that has no debug info.

A release .eco names nothing and types nothing, but the code still says a lot: which native a value is
passed to and at which position, which native or routine it was returned by, which variable it is copied
into. The natives' parameter and return types are the same in every script, so they are learned once
from debug builds (where every variable carries its declaration) and then applied to the release build:

  learn(debug .eco paths)       -> data/type_tables.json  {native, position -> type}, {native -> return type}
  infer(lifter, routines, ...)  -> {variable key: type} for globals, parameters, locals and return values

Values that flow into each other (x = y, f(x) with f's parameter, return x) must have one type, so the
variables are merged with union-find and every group takes the type most of its evidence names.

Type values are tuples: ('int',) ('str',) ('strW',) ('h', <class name>) ('arr', 'int'|'str'|'strW'|'unit').
"""
import collections
import json
import pathlib

import lifter

TABLES = pathlib.Path(__file__).resolve().parent / 'data' / 'type_tables.json'
ELEM = {0: 'unit', 1: 'int', 2: 'str', 3: 'strW'}
ELEM_SUB = {'unit': 0, 'int': 1, 'str': 2, 'strW': 3}
# array natives: four blocks of 23 (int, string, stringW, unit), the array itself is argument 0
ARRAY_BLOCKS = ('int', 'str', 'strW', 'unit')
ELEM_T = {'int': ('int',), 'str': ('str',), 'strW': ('strW',), 'unit': ('h', 'unit')}
# (offset in the block, argument position) that holds an element: $SetAt (16), SetAt (9), Add (11)
ARRAY_ELEM_POS = {(16, 2), (9, 2), (11, 1)}
# string conversion (0x74 string, 0x106 stringW) and assignment (0x73, 0x105)
STR_REF_NATIVES = {0x74, 0x106, 0x73, 0x105}
# scripts translated against an older native table (see nativemap.build)
OLD_INDEX_SPACE = {'cities', 'citycampaign', 'missionteamhunt'}


def rec_type(v):
    """type tuple of a debug record (global, parameter, local)"""
    k = v['kind']
    t = v.get('type')
    if k == 0:
        return ('h', t if isinstance(t, str) and t else 'unit')
    if k == 1:
        return ('int',)
    if k == 2:
        return ('str',)
    if k == 3:
        return ('strW',)
    if k == 6:
        if isinstance(t, str) and t:
            return ('arr', 'unit')             # the only array of objects there is
        return ('arr', ELEM.get(v.get('sub', 1), 'int'))
    return None


def to_record(t, v):
    """write type tuple `t` into the record `v` (a copy)"""
    v = dict(v)
    if t is None:
        return v
    if t[0] == 'h':
        v.update(kind=0, type=t[1], sub=0)
    elif t[0] == 'int':
        v.update(kind=1, type=0, sub=0)
    elif t[0] == 'str':
        v.update(kind=2, type=0, sub=0)
    elif t[0] == 'strW':
        v.update(kind=3, type=0, sub=0)
    elif t[0] == 'arr':
        if t[1] == 'unit':
            v.update(kind=6, type='unit', sub=0)
        else:
            v.update(kind=6, type=0, sub=ELEM_SUB[t[1]])
    return v


def type_name(t):
    """EarthC spelling of a return type"""
    if t is None:
        return None
    return {'int': 'int', 'str': 'string', 'strW': 'stringW', 'void': 'void'}.get(
        t[0], t[1] if t[0] == 'h' else None)


def _enc(t):
    return '|'.join(t)


def _dec(s):
    return tuple(s.split('|'))


class Facts:
    """lifter.TYPE_SINK: records value flow while the lifter simulates routines.

    learn=True: the lifter runs on a debug build, names resolve to typed records, and what is recorded is
    the type each native position / native result carries. learn=False: names are synthesised, and what
    is recorded is which variables meet where."""

    def __init__(self, learn=False, tables=None):
        self.learn = learn
        self.natarg = collections.defaultdict(collections.Counter)    # (idx, pos) -> type votes
        self.natret = collections.defaultdict(collections.Counter)    # idx -> type votes
        self.strpos = collections.defaultdict(collections.Counter)    # (idx, pos) -> {ref flag: n}
        self.hier = collections.Counter()                             # (derived, base) handle classes
        self.tables = tables or {'arg': {}, 'ret': {}}
        self.ev = collections.defaultdict(collections.Counter)        # key -> type votes
        self.parent = {}
        self.refs = collections.Counter()                             # key passed as &x
        self.strref = collections.Counter()                           # string parameter used by reference
        self.alts = collections.defaultdict(list)                     # key -> type sets a position accepts
        self.fixed = {}                    # key -> declared type (command / event parameters)
        self.copies = []                   # (destination key, source key) of `x = y`
        self.argtypes = collections.defaultdict(set)   # parameter key -> classes of native results passed
        self.flows = []                    # (callee start, param index, 'ref'|'var', argument key)

    # ---- union-find
    def find(self, k):
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a, b):
        if a is None or b is None:
            return
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    # ---- names -> keys / records
    def _lookup(self, lf, name):
        """(key, record) for a variable name in the current routine"""
        if not name or name.startswith('$'):
            return None, None
        r = getattr(lf, 'cur_routine', None)
        if r is not None:
            for k, p in enumerate(r.get('params', ())):
                if p['name'] == name:
                    return (r['start'], 'p', k), p
            for k, p in enumerate(r.get('locals', ())):
                if p['name'] == name:
                    return (r['start'], 'l', k), p
        for addr, g in lf.globals.items():
            if g['name'] == name:
                return ('g', addr), g
        return None, None

    def _var(self, lf, e):
        """(key, record, is_ref) of an expression that names a variable"""
        if e is None:
            return None, None, False
        # `$AddRef(x)` around a handle that is stored (unit array $SetAt, handle assignment) is the
        # variable itself; the wrapper hid PQuestsMulti's `g18[a1] = a2` from the unit-array rule
        for _ in range(3):
            if e.kind == 'call' and getattr(e, 'nidx', None) in _handle_pass() and getattr(e, 'argv', None):
                e = e.argv[0]
            else:
                break
        if e.kind == 'var':
            k, rec = self._lookup(lf, e.text)
            return k, rec, False
        if e.kind == 'ref' and e.text.startswith('&'):
            k, rec = self._lookup(lf, e.text[1:])
            return k, rec, True
        return None, None, False

    # ---- sink interface (called by the lifter)
    def call(self, lf, nidx, tgt, args):
        if nidx is not None:
            for pos, a in enumerate(args):
                k, rec, is_ref = self._var(lf, a)
                if k is None:
                    continue
                if self.learn:
                    t = rec_type(rec)
                    if t:
                        self.natarg[(nidx, pos)][t] += 1
                    if a.kind == 'var' and k[1:2] == ('p',) and rec['kind'] in (2, 3):
                        # a string parameter handed over as it is: by reference only
                        # where the native writes (out argument, mutating method)
                        self.strpos[(nidx, pos)][rec.get('flags', 0) & 1] += 1
                    continue
                if is_ref:
                    self.refs[k] += 1
                wpos = self.tables.get('strw', {}).get(f'{nidx}:{pos}') or doc_strw(nidx, pos)
                if a.kind == 'var' and k[1:2] == ('p',) and wpos and wpos.get('1') and not wpos.get('0'):
                    self.strref[k] += 1        # handed to a writing position: `string& s`
                if pos == 0 and nidx in STR_REF_NATIVES and a.kind == 'var' and k[1:2] == ('p',):
                    # a string parameter read through 0x74/0x106 (or assigned through
                    # 0x73/0x105) straight from its slot holds the address of the
                    # caller's variable: `string& s`. A value parameter is used as it
                    # is and cannot be assigned at all ("Invalid type"); measured
                    # _spike/p2/probe sq_v.ec / sq_r.ec
                    self.strref[k] += 1
                if pos == 0 and nidx < 0x5c:
                    self.ev[k][('arr', ARRAY_BLOCKS[nidx // 0x17])] += 5
                    continue
                if nidx < 0x5c and (nidx % 0x17, pos) in ARRAY_ELEM_POS:
                    # the value stored into an array has the array's element type: `a[i] = v`
                    # ($SetAt), a.SetAt(i, v), a.Add(v) (Documentation/EarthC/array.htm: TYPE)
                    self.ev[k][ELEM_T[ARRAY_BLOCKS[nidx // 0x17]]] += 5
                    continue
                got = self.tables['arg'].get(f'{nidx}:{pos}', {})
                if not got:
                    got = doc_arg(nidx, pos)
                if not got and pos == 0 and nidx in _life():
                    # destructor / AddRef / Release of a handle class (native_sigs.class_lifecycle)
                    got = {_enc(('h', _life()[nidx])): 1}
                for t, n in got.items():
                    self.ev[k][_dec(t)] += n
                if got:
                    self.alts[k].append(frozenset(_dec(t) for t in got))
            return
        if tgt is None:
            return
        callee = lf.by_start.get(tgt)
        if not callee:
            return
        params = callee.get('params', ())
        pad = len(args) - len(params)
        if self.learn:
            for j, p in enumerate(params):
                if 0 <= j + pad < len(args):
                    _k, rec, _r = self._var(lf, args[j + pad])
                    ta, tp = rec_type(rec) if rec else None, rec_type(p)
                    if ta and tp and ta[0] == tp[0] == 'h' and ta != tp:
                        self.hier[(ta[1], tp[1])] += 1      # ta is passed where tp is declared
            return
        for j in range(len(params)):
            if j + pad < 0 or j + pad >= len(args):
                continue
            a = args[j + pad]
            k, _rec, _r = self._var(lf, a)
            if k is None and a.kind == 'call' and getattr(a, 'nidx', None) is not None:
                # a native's result handed straight on: its class is a lower bound of
                # the parameter (`f(…, GetMagicCardParams(s), …)`, RPGCompute)
                got = self.tables['ret'].get(str(a.nidx)) or doc_ret(a.nidx)
                if len(got) == 1:
                    t_ = _dec(next(iter(got)))
                    self.argtypes[(tgt, 'p', j)].add(t_)
                    self.ev[(tgt, 'p', j)][t_] += 1
            if k is not None:
                self.union(k, (tgt, 'p', j))
                how = 'ref' if a.kind == 'ref' else ('val' if getattr(a, 'deref', False) else 'var')
                self.flows.append((tgt, j, how, k))

    def assign(self, lf, name, val):
        k, rec = self._lookup(lf, name)
        if k is None or val is None:
            return
        nidx = getattr(val, 'nidx', None) if val.kind == 'call' else None
        callee = getattr(val, 'callee', None) if val.kind == 'call' else None
        if self.learn:
            t = rec_type(rec)
            if t and nidx is not None:
                self.natret[nidx][t] += 1
            return
        if self._through(lf, val, k):
            pass
        elif nidx is not None:
            self._natret(lf, nidx, k)
        elif callee is not None:
            self.union(k, (callee, 'ret'))
        else:
            k2, _rec2, _r = self._var(lf, val)
            if k2 is not None:
                self.union(k, k2)
                self.copies.append((k, k2))

    def _through(self, lf, v, key):
        """a string conversion (0x74 string / 0x106 stringW) passes its variable through:
        `return s;` of a stringW local compiles to `return 0x106(&s, 1)`"""
        if getattr(v, 'nidx', None) in (0x74, 0x106) and getattr(v, 'argv', None):
            self.ev[key][('str',) if v.nidx == 0x74 else ('strW',)] += 3
            k2, _rec, _r = self._var(lf, v.argv[0])
            if k2 is not None:
                self.union(key, k2)
            return True
        return False

    def _natret(self, lf, nidx, key):
        """the result type of native `nidx` as evidence for `key`: the learned table, else the
        return kind every call site agrees on (nativemap.ret_kinds; kind 0 = some handle)"""
        if nidx < 0x5c and nidx % 0x17 in (10, 17, 18):
            # $GetPtrAt / $GetAt: an element of the block's array class
            self.ev[key][ELEM_T[ARRAY_BLOCKS[nidx // 0x17]]] += 5
            return
        got = self.tables['ret'].get(str(nidx)) or doc_ret(nidx)
        if got:
            for t, n in got.items():
                self.ev[key][_dec(t)] += n
            return
        kind = lf.natret.get(nidx)
        t = {0: ('h', 'unit'), 1: ('int',), 2: ('str',), 3: ('strW',), 4: ('void',)}.get(kind)
        if t:
            self.ev[key][t] += 1

    def returns(self, lf, r, info):
        """`return <value>` of a simulated routine (after the return repair)"""
        if self.learn:
            return
        for _blk, (_st, exit_) in info.items():
            if exit_ and exit_[0] == 'ret' and exit_[1] is not None:
                v = exit_[1]
                key = (r['start'], 'ret')
                if v.kind == 'call':
                    nidx = getattr(v, 'nidx', None)
                    if self._through(lf, v, key):
                        pass
                    elif nidx is not None:
                        self._natret(lf, nidx, key)
                    elif getattr(v, 'callee', None) is not None:
                        self.union(key, (v.callee, 'ret'))
                elif v.kind == 'const' and v.text == 'null':
                    self.ev[key][('h', 'unit')] += 1
                elif v.kind == 'const' and v.text.startswith('"'):
                    self.ev[key][('str',)] += 1
                else:
                    k, _rec, _r = self._var(lf, v)
                    if k is not None:
                        self.union(key, k)
                    elif v.kind in ('const', 'bin', 'un'):
                        self.ev[key][('int',)] += 1

    def _base(self, ts):
        """the one type every type in ts may be passed as (learned 'sub' pairs, transitive)"""
        ts = set(ts)
        if len(ts) == 1:
            return next(iter(ts))
        up = collections.defaultdict(set)
        # learned from the debug builds' calls, plus what the compiler accepts (native_sigs.class_tree)
        try:
            import native_sigs
            measured = native_sigs.load_tree()
        except Exception:
            measured = []
        for a, b in list(self.tables.get('sub', ())) + measured:
            up[('h', a)].add(('h', b))

        def ancestors(t):
            seen, todo = {t}, [t]
            while todo:
                for u in up.get(todo.pop(), ()):
                    if u not in seen:
                        seen.add(u)
                        todo.append(u)
            return seen
        common = set.intersection(*(ancestors(t) for t in ts))
        if not common:
            return sorted(ts)[0]
        # the most specific common one: the candidate that is an ancestor of no other candidate
        for c in sorted(common):
            if not any(c != d and c in ancestors(d) for d in common):
                return c
        return sorted(common)[0]

    # ---- result
    def solve(self):
        """{key: type} for every key that has evidence in its group"""
        votes = collections.defaultdict(collections.Counter)
        alts = collections.defaultdict(list)
        for k in set(self.parent) | set(self.ev):
            votes[self.find(k)].update(self.ev.get(k, {}))
            alts[self.find(k)] += self.alts.get(k, [])
        # A native position can accept several handle classes (a base class and the
        # classes derived from it). The type has to satisfy EVERY position a variable
        # reaches, so the choice is made inside the intersection; a plain majority took
        # the base class EquipmentParams for a variable that is also the receiver of
        # the WeaponParams-only GetMinDamage (RPGCompute, "Unknown member or function").
        for root, sets in alts.items():
            both = frozenset.intersection(*sets) if sets else frozenset()
            if both and len(both) < len(votes[root]):
                votes[root] = collections.Counter({t: n for t, n in votes[root].items() if t in both})
        # a declared type (command / event parameter) is not evidence, it is the answer.
        # Two different declared types in one group (InitEquipment passes EquipmentParams,
        # InitBrokenWeapon WeaponParams, both into the same helper) mean the helper's
        # parameter is their common base class - the free keys of the group take that.
        fixed = collections.defaultdict(set)
        for k, t in self.fixed.items():
            fixed[self.find(k)].add(t)
        for root, ts in fixed.items():
            votes[root] = collections.Counter({self._base(ts): 1})
        poly = {root for root, c in votes.items()
                if len({t for t in c if t[0] == 'h'}) > 1 or len(fixed.get(root, ())) > 1}
        out = {}
        for k in set(self.parent) | set(self.ev):
            c = votes[self.find(k)]
            if len(c) > 1 and ('void',) in c:
                # `return f();` of a void native is the end of a void routine; any real
                # value returned elsewhere wins
                c = collections.Counter({t: n for t, n in c.items() if t != ('void',)})
            if c:
                out[k] = c.most_common(1)[0][0]
        if poly:
            out.update(self._refine_handles(poly))
        return out

    def _refine_handles(self, poly):
        """Handle classes inside groups that mix several of them (RPGCompute's *Params).

        Union-find puts every variable that meets another into one group, but a
        WeaponParams may be passed where an EquipmentParams is declared, so such a
        group holds a whole class hierarchy and one type for all of it is wrong.
        Each variable takes its own type instead: a declared one, else what its own
        native uses say (inside the classes all of them accept). A variable without
        such evidence takes it from the values flowing into it - a parameter the common
        base of every argument passed to it - and failing that from where it flows to."""
        keys = [k for k in set(self.parent) | set(self.ev) if self.find(k) in poly]
        own = {}
        for k in keys:
            if k in self.fixed:
                own[k] = self.fixed[k]
                continue
            c = collections.Counter({t: n for t, n in self.ev.get(k, {}).items() if t[0] == 'h'})
            sets = self.alts.get(k, [])
            both = frozenset.intersection(*sets) if sets else None
            if both:
                c = collections.Counter({t: n for t, n in c.items() if t in both}) or c
            if c:
                own[k] = c.most_common(1)[0][0]
        inc = collections.defaultdict(set)
        outg = collections.defaultdict(set)
        for tgt, j, how, src in self.flows:
            inc[(tgt, 'p', j)].add(src)
            outg[src].add((tgt, 'p', j))
        for dst, src in self.copies:
            inc[dst].add(src)
            outg[src].add(dst)
        typ = dict(own)
        for _ in range(len(keys) + 1):
            changed = False
            for k in keys:
                if k in self.fixed:
                    continue
                # everything that flows in has to fit: the common base of the variable's
                # own class and of every value assigned or passed to it
                ins = {typ[x] for x in inc.get(k, ()) if x in typ and typ[x][0] == 'h'}
                ins |= {t for t in self.argtypes.get(k, ()) if t[0] == 'h'}
                if k in own:
                    ins.add(own[k])
                t = self._base(ins) if ins else None
                if t is None:
                    outs = {typ[x] for x in outg.get(k, ()) if x in typ and typ[x][0] == 'h'}
                    t = self._most_specific(outs) if outs else None
                if t is not None and typ.get(k) != t:
                    typ[k] = t
                    changed = True
            if not changed:
                break
        return typ

    def _most_specific(self, ts):
        """the type in ts every other one is an ancestor of (a value passed to all of them)"""
        ts = set(ts)
        for t in sorted(ts):
            if all(u == t or self._base({t, u}) == u for u in ts):
                return t
        return sorted(ts)[0]


# ---- documented signatures (native_sigs): the fallback where no debug build shows a position
_DOC = None
_DOC_T = {'int': ('int',), 'string': ('str',), 'stringW': ('strW',), 'void': ('void',)}


def _doc():
    global _DOC
    if _DOC is None:
        try:
            import native_sigs
            _DOC = native_sigs.load()
        except Exception:
            _DOC = {}
    return _DOC


def _doc_t(t):
    if t in _DOC_T:
        return _DOC_T[t]
    if t.startswith('array:'):                 # native_sigs' expanded array classes
        return ('arr', {'int': 'int', 'string': 'str', 'stringW': 'strW', 'unit': 'unit'}[t[6:]])
    if t in ('array', 'TYPE', 'playerinterface', 'basescript'):
        return None
    return ('h', t)


def _doc_param(nidx, pos):
    s = _doc().get(nidx)
    if not s or s.get('receiver') is None:
        return None
    k = pos - s['receiver']
    if s['receiver'] and pos == 0:
        return (s['cls'], False) if s['cls'] not in ('basescript', 'object', 'hero', 'RPGCompute') else None
    if 0 <= k < len(s['params']):
        return tuple(s['params'][k])
    return None


_LIFE = None
_PASS = None


def _handle_pass():
    global _PASS
    if _PASS is None:
        import emit_body
        _PASS = emit_body.HANDLE_PASS | emit_body.extra_handle_pass()
    return _PASS


def _life():
    global _LIFE
    if _LIFE is None:
        try:
            import native_sigs
            _LIFE = native_sigs.load_lifecycle()
        except Exception:
            _LIFE = {}
    return _LIFE


def doc_arg(nidx, pos):
    """{encoded type: weight} for argument `pos` of native `nidx` from its documented signature"""
    p = _doc_param(nidx, pos)
    t = _doc_t(p[0]) if p else None
    return {_enc(t): 1} if t else {}


def doc_strw(nidx, pos):
    """a documented `string&` / `stringW&` parameter is a writing position, and so is the receiver of a
    `void` string method (Format, FormatTrl, Append, Copy ...: 10 of 10 where the debug builds are clear)"""
    s = _doc().get(nidx)
    if s and pos == 0 and s.get('receiver') and s['cls'] in ('string', 'stringW') and s['ret'] == 'void':
        return {'1': 1}
    p = _doc_param(nidx, pos)
    return {'1': 1} if p and p[1] and p[0] in ('string', 'stringW') else None


def doc_ret(nidx):
    s = _doc().get(nidx)
    t = _doc_t(s['ret']) if s else None
    return {_enc(t): 1} if t else {}


def _simulate_all(lf, routines, sink):
    old = lifter.TYPE_SINK
    lifter.TYPE_SINK = sink
    try:
        for r in routines:
            try:
                order, info, _preds, _ends = lf.simulate(r)
            except Exception:
                continue
            sink.returns(lf, r, info)
    finally:
        lifter.TYPE_SINK = old


def implicit_commands(lf):
    """Commands 0..2 the compiler writes on its own: construct, destroy and serialise every
    global. They carry no debug record, but they name every global with the lifecycle native
    of its exact type ($Constructor/$Destructor/$Serialize of int, string, the four array
    classes and each handle class) - the one place a global that the script never uses
    shows its type."""
    import disasm
    out = []
    have = {r['start'] for r in lf.routines}
    scanned = {r['start']: r for r in disasm.scan_routines(lf.code)}
    for c in lf.f['commands'][:3]:
        r = scanned.get(c[0])
        if c[0] in scanned and (not lf.b or c[0] not in have):
            out.append({**r, 'kind': 5, 'params': [], 'locals': [], 'name': f'$implicit_{c[0]}'})
    return out


def learn(paths):
    """native argument / return types from debug builds -> TABLES"""
    sink = Facts(learn=True)
    for p in paths:
        p = pathlib.Path(p)
        if p.stem.lower() in OLD_INDEX_SPACE:
            continue
        try:
            lf = lifter.Lifter(p)
        except Exception:
            continue
        if not lf.b:
            continue
        _simulate_all(lf, lf.routines + implicit_commands(lf), sink)
    out = {'arg': {f'{i}:{k}': {_enc(t): n for t, n in c.items()} for (i, k), c in sink.natarg.items()},
           'ret': {str(i): {_enc(t): n for t, n in c.items()} for i, c in sink.natret.items()},
           'strw': {f'{i}:{k}': {str(f): n for f, n in c.items()} for (i, k), c in sink.strpos.items()},
           'sub': sorted([a, b] for (a, b) in sink.hier)}
    TABLES.write_text(json.dumps(out, indent=0, sort_keys=True), encoding='utf-8')
    return out


_tables = None


def tables():
    global _tables
    if _tables is None:
        _tables = (json.loads(TABLES.read_text(encoding='utf-8')) if TABLES.exists()
                   else {'arg': {}, 'ret': {}})
    return _tables


def infer(lf, routines, rounds=2):
    """{key: type}: ('g', addr), (start, 'p'|'l', index), (start, 'ret')"""
    facts = None
    for _ in range(rounds):
        facts = Facts(tables=tables())
        # command and event parameters come typed from the class table (entries.py)
        for r in routines:
            if r.get('kind') in (4, 5):
                for k, p in enumerate(r.get('params', ())):
                    t = rec_type(p)
                    if t:
                        facts.ev[(r['start'], 'p', k)][t] += 50
                        facts.fixed[(r['start'], 'p', k)] = t
        _simulate_all(lf, routines, facts)
    return facts.solve(), facts


if __name__ == '__main__':
    import sys
    t = learn(sys.argv[1:])
    print(len(t['arg']), 'argument positions,', len(t['ret']), 'return types')
