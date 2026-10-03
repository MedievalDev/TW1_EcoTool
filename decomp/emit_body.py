"""Translate lifted statements into EarthC source.

The lifter already reconstructs expressions and control flow; what it prints is
analysis pseudocode. This turns that into compilable EarthC:

  native_0012(a, i)        -> a[i]              ($GetAt in the compiler's API table)
  native_0010(a, i, v)     -> a[i] = v          ($SetAt)
  &x                       -> x                 (reference args are implicit)
  goto L_n                 -> reported as unsupported (EarthC has no goto)

Returns (lines, problems); a non-empty problems list means the body cannot be
emitted faithfully yet.
"""
import re, pathlib
import lifter, arity4, disasm

# The compiler-internal natives follow a block scheme: four array blocks of 23
# indices each (int@0x00, string@0x17, stringW@0x2e, unit@0x45) where offsets 16..22
# are always $SetAt / $GetPtrAt / $GetAt / $Serialize / $Destructor /
# $ConstructorSize / $Constructor. That is why 0x10/0x12 turned out to be array
# set/get: block int + 16 / + 18.
ARRAY_SET = {0x10, 0x27, 0x3e, 0x55}          # offset 16
ARRAY_PTR = {0x11, 0x28, 0x3f, 0x56}          # offset 17, reads like GET
ARRAY_GET = {0x12, 0x29, 0x40, 0x57}          # offset 18
# implicit string operations: assignment and the string/stringW conversions.
# Dropping the conversions is correct — the compiler re-inserts them from the types.
STR_ASSIGN = {0x73, 0x105}
STR_CAST = {0x74, 0x106}
# Pure lifecycle calls: constructors, destructors, serialisers, scope enter/leave and
# handle AddRef/Release. They have no counterpart in the source and must vanish when
# emitting, not block. (`$AddRef(new); $Release(old); var = new` collapses to the
# assignment.)
LIFECYCLE = {
    0x13, 0x14, 0x15, 0x16, 0x2a, 0x2b, 0x2c, 0x2d,          # array int / string
    0x41, 0x42, 0x43, 0x44, 0x58, 0x59, 0x5a, 0x5b,          # array stringW / unit
    0x71, 0x72, 0x75, 0x76, 0x77,                            # string
    0x103, 0x104, 0x107, 0x108, 0x109,                       # stringW
    0x2f6, 0x2f9,                                            # int serialize
    0x578, 0x3be, 0x3b4, 0x805, 0x35f, 0x8b1, 0x770,         # handle $Serialize
    0x2f3,                                                   # `object` destructor
}
# 0x02f3 is the destructor of a plain `object` handle. It is the one bookkeeping
# native that lifter.load_internal() does not see (compiler_calls counts 510 hits at
# a source line for it), so extra_handle_pass() never picked it up and RPGCompute
# lifted `native_02f3(pChangedParams);` as a statement. Measured, not guessed:
# roundtrip/mytest6/t_obj.ec — a function whose only local is `object a;` and whose
# body is `if (g == 1) g = 2;` — compiles to exactly one call, 0x02f3, and to nothing
# else. Only RPGCompute declares `object` locals, so no other file is affected.
# $AddRef / $Release wrap a handle assignment: `$AddRef(new); $Release(old); v = new`
# collapses to `v = new`, so these pass their value through instead of vanishing.
HANDLE_PASS = {
    0x579, 0x57a, 0x3bf, 0x3c0, 0x806, 0x807,
    0x360, 0x361, 0x8b2, 0x8b3, 0x771, 0x772,
    # SpecialArtefactParamsSet, CustomArtefactParams, CustomArtefactParamsSet.
    # extra_handle_pass() below finds every other pair but not these three: it asks
    # whether the neighbouring index is compiler-generated, and 0x0830 / 0x083c /
    # 0x084a occur in no build of the whole corpus at all, so there is nothing to
    # ask about. Measured instead — roundtrip/probe/anchors_rpg.ec f9/f10/f11 declare
    # exactly one handle local each, which puts that class's destructor alone in the
    # epilogue block and gives the index and the arity in one step.
    0x830, 0x831, 0x83c, 0x83d, 0x84a, 0x84b,
}
_extra_pass = None


def extra_handle_pass():
    """the $AddRef/$Release pairs of the script classes nobody listed by hand

    `lifter.load_internal()` is the corpus-wide test "this native has no ref record
    at any call site in any debug build", i.e. the compiler emits it on its own and
    it has no source form. Every script class has its own handle pair, and the SDK
    builds contain classes the shipped scripts do not (RPGCompute), so scanning
    ../roundtrip/ref as well turns up twelve more pairs than the twelve above:
    0x071a/0x071b, 0x074c/0x074d, 0x075a/0x075b, 0x0762/0x0763, … Structurally they
    are the same thing — arity 1, consecutive indices, compiler-generated — and
    they were the four most frequent unnamed natives in emit_survey. Written out,
    RPGCompute.ec:829 read `native_074c(pUnit.GetEquipmentValuesOnIndex(nIndex));`
    and :1766 `native_07d3(unPar);`.

    Anything already classified above keeps its classification.
    """
    global _extra_pass
    if _extra_pass is None:
        import lifter, arity4
        d = lifter.load_internal()
        ar = arity4.load()
        known = LIFECYCLE | HANDLE_PASS | STR_ASSIGN | STR_CAST
        _extra_pass = {i for i in d
                       if i not in known and ar.get(i) == 1
                       and ((i + 1 in d and ar.get(i + 1) in (1, None))
                            or (i - 1 in d and ar.get(i - 1) in (1, None)))}
    return _extra_pass
NATIVE = re.compile(r'\bnative_([0-9a-f]{4})\(')
DROP = '$$DROP$$'     # marker for statements that must not be emitted at all
_HANDLE_HEAD = re.compile(r'^\x02?native_([0-9a-f]{4})\(')


def handle_wrapper(expr):
    """statement that is nothing but the AddRef/Release of a handle assignment

    `uHero = GetHeroMulti()` compiles to `$AddRef(new); $Release(old); uHero = new`.
    The two wrapper results are never read, so the lifter drops them into statements
    of their own, and HANDLE_PASS then unwrapped them into a bare `GetHeroMulti();`
    and `uHero;` in front of the assignment that repeats them (TwoWorldsLights
    state_1). As statements they have no source form at all.
    """
    mo = _HANDLE_HEAD.match(str(expr))
    return bool(mo) and int(mo.group(1), 16) in (HANDLE_PASS | extra_handle_pass())


def method_names():
    """Which natives are called as `obj.Name(...)` rather than `Name(...)`.

    Primary source is the SDK's own EarthC code: every `.Name(` in the .ec/.ech
    sources is a method call by definition — 562 distinct names, ground truth
    (`out/method_names.txt`, produced by grep over src_sdk).

    Fallback is EarthC.exe's API table, which maps script name -> C++ handler class;
    anything not implemented by CStaticEC is a member of an object type. That uses
    the NAME->class mapping, which is solid — the index->name mapping from the same
    binary is only 92 % monotonic and is deliberately not used.
    """
    names = set()
    p = (pathlib.Path(__file__).resolve().parent / 'data' / 'method_names.txt')
    if p.exists():
        names |= {l.strip() for l in p.read_text(encoding='utf8').splitlines()
                  if l.strip()}
    api = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_api.txt')
    if api.exists():
        for line in api.read_text(encoding='utf8').splitlines():
            mo = re.match(r'\s*\d+\s+(\S+)\s+(C\w+)::', line)
            if mo and mo.group(2) != 'CStaticEC':
                names.add(mo.group(1).lstrip('$'))
    return names


_METHODS = None
# names the current routine declares as int / string / stringW; see body()
_NOTOBJ = frozenset()


def is_method(name):
    global _METHODS
    if _METHODS is None:
        _METHODS = method_names()
    return name in _METHODS


REGS = {'ebx', 'esi', 'edi', 'ecx', 'edx', 'eax'}


def is_context(a):
    """The prologue loads VM context registers (`ebx = [esi-12]`, `edi = [esi+4]`).

    Whether such a value reaches the argument list as the register name or as the
    slot it was loaded from depends on the block: `push ebx` after the register was
    tracked yields `$vm12`, an untracked one yields `ebx`. Both are the same implicit
    argument. Missing the `$vmN` spelling left `$vm12.SetLevelsHorizon(...)` in the
    lift of MainMenuCampaign_1, which is not valid EarthC.
    """
    a = a.strip()
    return a in REGS or re.fullmatch(r'\$vm\d+', a) is not None


def drop_context_args(args):
    """implicit context arguments are not written in EarthC source and are
    re-inserted by the compiler, so they must not be emitted"""
    return [a for a in args if not is_context(a)]


def _split_args(s):
    """split a call's argument list at top level.

    String literals are skipped whole. A format string is full of commas and
    brackets — `strName.Format("WP_STAFF_%d%d(%d,0)", …)` (Common/Quest.ech:232)
    was split inside the literal and rejoined with the canonical ", ", which
    rewrote the literal itself to "WP_STAFF_%d%d(%d, 0)". Every string behind it
    then moved and the whole data segment shifted (TwoWorldsContainers: 17 strings
    changed, 31 data bytes, and the string offsets in 30 of 44 routines).
    """
    out, depth, cur, quote, esc = [], 0, '', False, False
    for ch in s:
        if quote:
            cur += ch
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                quote = False
            continue
        if ch == '"':
            quote = True
            cur += ch
            continue
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _match_paren(s, start):
    """index just past the `)` that closes the bracket opened before `start`.

    Brackets inside string literals do not count — see _split_args."""
    depth, i, quote, esc = 1, start, False, False
    while i < len(s) and depth:
        ch = s[i]
        if quote:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        else:
            depth += (ch == '(') - (ch == ')')
        i += 1
    return i if not depth else None


def _in_string(s, pos):
    """does `pos` fall inside a "…" literal?"""
    q, esc, i = False, False, 0
    while i < pos and i < len(s):
        ch = s[i]
        if q:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                q = False
        elif ch == '"':
            q = True
        i += 1
    return q


def to_earthc(expr):
    """rewrite one expression string into EarthC syntax"""
    s = str(expr)
    # array accessors, innermost first
    for _ in range(8):
        mo = NATIVE.search(s)
        if not mo:
            break
        idx = int(mo.group(1), 16)
        start = mo.end()
        depth, i = 1, start
        while i < len(s) and depth:
            depth += (s[i] == '(') - (s[i] == ')')
            i += 1
        args = _split_args(s[start:i - 1])
        if idx in (ARRAY_GET | ARRAY_PTR) and len(args) == 2:
            rep = f'{args[0]}[{args[1]}]'
        elif idx in ARRAY_SET and len(args) == 3:
            rep = f'{args[0]}[{args[1]}] = {args[2]}'
        elif idx in STR_ASSIGN and len(args) == 2:
            rep = f'{args[0]} = {args[1]}'
        elif idx in STR_CAST and args:
            rep = args[0]
        elif (idx in HANDLE_PASS or idx in extra_handle_pass()) and args:
            rep = args[-1]                 # AddRef/Release: keep the handle value
        elif idx in LIFECYCLE:
            rep = DROP                     # ctor/dtor/serialize/scope: no source form
        else:
            break                      # unknown native: leave it, flagged below
        s = s[:mo.start()] + rep + s[i:]
    # reference arguments are implicit in EarthC — but only drop the address-of
    # prefix, never the bitwise AND operator (`x & 32` must survive)
    s = re.sub(r'&(?=[A-Za-z_$])', '', s)
    # compiler temporaries are named $1, $4, … which is not a legal identifier
    s = re.sub(r'\$(\d+)', r'_t\1', s)
    s = to_method_calls(s)
    # calls to this script's own routines were marked so the method rewrite above
    # would leave them alone (lifter.INTERNAL_MARK); the marker goes no further
    return s.replace('\x02', '').replace('\x03', '')


_TMP_NAME = r'_t\d+(?:_\d+)*'
_TMP_SET = re.compile(rf'^(\s*)({_TMP_NAME}) = (.+);$')
# lvalue: a plain name or one array element
_LVALUE = r'[A-Za-z_]\w*(?:\[[^\[\]]*\])?'
_OP_ASSIGN = re.compile(
    rf'^(\s*)({_LVALUE}) = ({_TMP_NAME}) (<<|>>|[-+|&^*/%]) (.+);$')
# same order as lifter.binop: smaller binds tighter
_OP_PRIO = {'*': 3, '/': 3, '%': 3, '+': 4, '-': 4, '<<': 5, '>>': 5,
            '&': 8, '^': 9, '|': 10}
_ASSOC = {'+', '|', '&', '^'}


def _top_ops(s):
    """binary operators of `s` that are not inside brackets, a call or a string"""
    out, depth, quote, esc, i = set(), 0, False, False, 0
    while i < len(s):
        ch = s[i]
        if quote:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        elif depth == 0 and s[i:i + 2] in ('<<', '>>'):
            out.add(s[i:i + 2])
            i += 2
            continue
        elif depth == 0 and ch in '+-*/%&^|' and i and s[i - 1] == ' ':
            out.add(ch)                  # spaced: a binary operator, not a sign
        i += 1
    return out


def _compound_safe(op, rest):
    """is `x = t OP rest` the same expression as `x OP= rest`?

    Only when nothing in `rest` binds looser than OP — otherwise OP is not the
    top-level operator at all. `nX = _t * 60 / 100` is `(nX * 60) / 100`, and
    writing it `nX *= 60 / 100` computes `nX * (60 / 100)`, a different program:
    TwoWorldsEnemies and Enemies16 fell out of byte-identical on exactly that
    (the `imul` moved to the other side of the division guard). An operator equal
    to OP is fine only for the associative ones.
    """
    p = _OP_PRIO[op]
    for o in _top_ops(rest):
        q = _OP_PRIO[o]
        if q > p or (q == p and (op not in _ASSOC or o != op)):
            return False
    return True


def compound_assign(lines):
    """`x += e` back from `$t = x; x = $t + e`.

    `+=` is a code shape of its own, not sugar: EarthC parks the left-hand side in
    a temporary before it evaluates the right-hand side, so `nX += -e1m + Rand(e2m)`
    (Network/TownCampaign.ec:96) reserves two slots and ends with
    `add eax,$rhs / add eax,$nX`, while `nX = nX + (-e1m + Rand(e2m))` folds the
    load into the first `add` and needs one slot less. Measured in
    roundtrip/mytest6/r9.ec: f1 (`+=`) 62 code bytes and two slots, f2 (the
    explicit form) 53 and one.

    Every compound operator behaves that way, not just `+=` — measured in
    roundtrip/mytest6/rb.ec: `|=`, `&=`, `^=`, `-=` are 45 code bytes each and
    `*=` 43, against 29 for the written-out `x = x | (…)`. `nSearchType |= …`
    (Units/Alarm.ech:35) was the last 5 bytes missing from UnitBase.

    Must run before inline_temps, which would otherwise substitute the parked value
    and hide the shape.
    """
    out = list(lines)
    pend = {}                        # temp -> (line index, value text)
    for i, ln in enumerate(out):
        mo = _TMP_SET.match(ln)
        if mo:
            pend[mo.group(2)] = (i, mo.group(3))
            continue
        mo = _OP_ASSIGN.match(ln)
        if mo and mo.group(3) in pend:
            k, val = pend.pop(mo.group(3))
            # the temporary has to hold exactly the assignment's own target, and
            # nothing else may read it in between
            if val == mo.group(2) and _compound_safe(mo.group(4), mo.group(5)) \
                    and not any(
                        re.search(rf'\b{mo.group(3)}\b', out[j])
                        for j in range(k + 1, i) if out[j] is not None):
                out[i] = f'{mo.group(1)}{mo.group(2)} {mo.group(4)}= {mo.group(5)};'
                out[k] = None
    return [l for l in out if l is not None]


_FOR_HEAD = re.compile(r'^(\s*)for \(; (.+);\) \{$')


def for_headers(lines):
    """`for (; c;) { b; i++; }` with a `continue` in it -> `for (; c; i++) { b; }`.

    The three forms `for (i=0; c; i++) {b}`, `for (; c; i++) {b}` and
    `for (; c;) {b; i++;}` compile to the same bytes (roundtrip/mytest5:
    e3.eco == e5.eco == e6.eco), so structure2 emits the increment where the
    compiler put it — at the end of the body. A `continue` breaks that equivalence:
    in the body-end form it would skip the increment, so `go()` copies the
    increment in front of every `continue`, and those copies are 9 code bytes each
    that the reference does not have (its `continue` is a plain `jmp <latch>`, e.g.
    MissionCommon.ech's ShowEarthNetGuildPointsText). Moving the increment into the
    header makes `continue` reach it on its own and the copies go away.

    Applied only when EVERY `continue` in the body is directly preceded by exactly
    that copy — otherwise the increment does not run on all paths and the shapes are
    not interchangeable.
    """
    out = list(lines)
    i = 0
    while i < len(out):
        mo = _FOR_HEAD.match(out[i])
        if not mo:
            i += 1
            continue
        indent, cond = mo.group(1), mo.group(2)
        close = f'{indent}}}'
        end = next((j for j in range(i + 1, len(out)) if out[j] == close), None)
        body_ind = indent + '    '
        if end is None or end == i + 1:
            i += 1
            continue
        inc = out[end - 1]
        conts = [j for j in range(i + 1, end) if out[j].strip() == 'continue;']
        if (conts and inc.startswith(body_ind) and inc.strip().endswith(';')
                and not inc.strip().endswith('{')
                and all(out[j - 1].strip() == inc.strip() for j in conts)):
            out[i] = f'{indent}for (; {cond}; {inc.strip()[:-1]}) {{'
            for j in sorted(conts, reverse=True):
                del out[j - 1]
            del out[end - 1 - len(conts)]
        i += 1
    return out


def inline_temps(lines):
    """Compiler temporaries ($n -> _tn) must not be declared — the compiler creates
    its own on recompile. Where one is assigned and then used, substitute it and drop
    the assignment: `_t5 = nPosX; a.Add(x + _t5);` -> `a.Add(x + nPosX);`"""
    out, pending = [], {}
    for ln in lines:
        # `_t4_2` is the second store into the same compiler slot within a block
        # (Lifter.run_block versions them), so the name is not just digits
        mo = re.match(r'(\s*)(_t\d+(?:_\d+)*) = (.+);$', ln)
        if mo and '_t' not in mo.group(3):
            # `_atom`, exactly as in the step-wise branch below: the temporary is
            # one operand of the expression it gets folded into, so a value that is
            # itself an operator expression needs its brackets back.  Without them
            # `_t4 = nValue + 1; nValue = _t4 % arr.GetSize();` came out as
            # `nValue = nValue + 1 % arr.GetSize()` — a different program, and the
            # recompile spilled a temporary of its own for it
            # (TwoWorldsMusic.PlayRandom, Music.ec:713, +28 code bytes).
            pending[mo.group(2)] = _atom(mo.group(3))
            continue
        if mo:
            # The temporary is built up in steps: `_t0 = 5; _t0 = (n-1)*3 + _t0;`
            # for `(5+(n-1)*3)*(20+n)/10` (RPGCompute/Equipment.ech:12). Falling
            # through to the substitution below rewrote the *target* as well and
            # produced `5 = 5 + (nLevel - 1) * 3;`, which is not even an lvalue.
            rhs = mo.group(3)
            for name, val in pending.items():
                rhs = re.sub(rf'\b{name}\b', _atom(val), rhs)
            if '_t' not in rhs:
                pending[mo.group(2)] = _atom(rhs)
                continue
            for name in [n for n in pending if re.search(rf'\b{n}\b', rhs)]:
                pending.pop(name)           # cannot fold: leave the assignment
        for name, val in pending.items():
            ln = re.sub(rf'\b{name}\b', val, ln)
        out.append(ln)
    return out


def _atom(v):
    """parenthesize unless the text already binds tighter than any operator"""
    if re.fullmatch(r'-?[\w.]+|"[^"]*"|\([^()]*\)|[\w.]+\([^()]*\)', v):
        return v
    return f'({v})'


# the zeroing of an object or string slot reads `x = null;` / `x = "";`
# (lifter.as_null and the empty-string fix) — still the same prologue
PROLOGUE_INIT = re.compile(r'\s*([A-Za-z_]\w*) = (?:0|null|"");$')
SLOT = re.compile(r'loc\d+$')          # mem_name's fallback for an unnamed frame slot


def prologue_zeroes(lf, r):
    """how many locals the entry code zeroes — `xor eax,eax` + N stores

    The compiler spells the two `= 0` forms differently: the implicit clearing of
    the frame reuses one `xor eax, eax` and then stores eax into every slot, while
    a written `i = 0` loads the constant first (`mov eax, 0`). Counting the stores
    is therefore exact, and it has to be counted: `for (i = 0; …)` over an already
    zeroed slot produced two identical `i = 0;` lines, the second of which was
    dropped as prologue (Quest.ech's GetHeroMulti, 8 code bytes short).
    """
    insns = lf.decode(r)
    start = next((i for i, (m, a, s, ins) in enumerate(insns[:16])
                  if ins is not None and m == 'xor' and ins.op_str == 'eax, eax'),
                 None)
    if start is None:
        return 0
    n = 0
    for m, a, s, ins in insns[start:]:
        if ins is None:
            break
        if m == 'xor' and ins.op_str == 'eax, eax':
            continue                       # the clearing is re-issued per group
        if m == 'mov' and ins.op_str.startswith('dword ptr [ebp - ') \
                and ins.op_str.endswith('], eax'):
            n += 1
            continue
        break
    return n


def strip_prologue_inits(lines, locals_, nzero=None):
    """The compiler zeroes every local on entry; it does that again on recompile,
    so those leading `x = 0;` assignments must not be emitted.

    `nzero` (from prologue_zeroes) caps how many lines belong to that prologue;
    without it a written `i = 0;` right after the declarations was eaten too."""
    names = {l['name'].replace('$', '_t') for l in locals_}
    i = 0
    # A release routine whose debug record did not match keeps the synthesized slot
    # names (mem_name's `locN`). Those are just as much prologue zeroing, and they
    # are not declared anywhere, so leaving them in cannot compile.
    while i < len(lines) and (nzero is None or i < nzero):
        mo = PROLOGUE_INIT.match(lines[i])
        if mo and (mo.group(1) in names or SLOT.match(mo.group(1))):
            i += 1
            continue
        break
    return lines[i:]


_OBJ_ELEM_ZERO = re.compile(r'(\b([A-Za-z_]\w*)\[[^\[\]]*\]) = 0;$')


def body(lf, r, local_names=None):
    """-> (lines, problems)"""
    global _NOTOBJ
    order, info, _preds, _ends = lf.simulate(r)
    _NOTOBJ = frozenset(getattr(lf, 'intnames', ()))
    out = lf.structure_best(r, order, info, base=0)

    lines, problems = [], []
    chain = None          # (line index, depth, value node) of the last assignment
    # The compiler's prologue zeroes every local from one `xor eax,eax`, so those
    # stores all share the value node too and would chain into one nonsense
    # `strM = nIndex = nLayer = nRow = nCol = 0;`. They are the leading run of
    # `<local> = 0;` that strip_prologue_inits drops anyway.
    slots = {l['name'].replace('$', '_t') for l in r.get('locals', ())}
    prologue = True
    for kind, depth, payload in out:
        pad = '    ' * depth
        if kind != 'stmt' or payload[0] != 'assign':
            chain = None
        if kind == 'stmt':
            if payload[0] == 'hookmark':
                continue
            if payload[0] == 'assign':
                rhs = to_earthc(payload[2])
                if DROP in rhs:
                    chain = None
                    continue
                lhs = to_earthc(payload[1])
                line = f'{pad}{lhs} = {rhs};'
                mo = PROLOGUE_INIT.match(line)
                if prologue and mo and (mo.group(1) in slots
                                        or SLOT.match(mo.group(1))):
                    lines.append(line)
                    chain = None
                    continue
                prologue = False
                # `nCol = nRow = 1;` (Common/Levels.ech:80) stores the SAME value
                # node twice in a row. Written as two statements it recompiles to two
                # `mov eax,1` sequences and the code segment grows — folding them
                # back into the chain is what the source had.
                if chain is not None and chain[1] == depth and chain[2] is payload[2]:
                    lines[chain[0]] = f'{pad}{lhs} = {lines[chain[0]].strip()}'
                    continue
                lines.append(line)
                chain = (len(lines) - 1, depth, payload[2])
            elif payload[0] == 'expr':
                if handle_wrapper(payload[1]):
                    continue          # AddRef/Release around a handle assignment
                txt = to_earthc(payload[1])
                if DROP in txt:
                    continue          # pure lifecycle call
                lines.append(f'{pad}{txt};')
            elif payload[0] == 'raw2':
                lines.append(f'{pad}{to_earthc(payload[1])}')
        else:
            txt = str(payload)
            if 'goto' in txt:
                problems.append(txt.strip())
            lines.append(f'{pad}{to_earthc(txt)}')
    # `auUnit[nUnit] = 0;` is an "Invalid type" (Campaigns/Missions/PTown.ec):
    # the null object is spelled `null`. The scalar case is handled while the
    # instruction is read, but an array store is a native call at that point and
    # only becomes `arr[i] = v` in to_earthc, where the declaration is out of reach.
    objarrays = getattr(lf, 'objarrays', ())
    if objarrays:
        lines = [_OBJ_ELEM_ZERO.sub(
            lambda mo: f'{mo.group(1)} = null;'
            if mo.group(2) in objarrays else mo.group(0), l) for l in lines]
    for l in lines:
        if 'native_' in l:
            problems.append(f'unresolved native: {l.strip()[:60]}')
        if '/*…*/' in l or '/* arity ? */' in l:
            problems.append(f'incomplete expression: {l.strip()[:60]}')
    # A call to a routine the debug build did not name (`sub_14941()`) is NOT
    # reported here on purpose. It is not an identifier the compiler knows, so an
    # entry point carrying one has to fall back to a stub — but that is a decision
    # for emit_ec (see UNNAMED_ROUTINE there); counting it as a translation problem
    # dropped emit_survey from 95.6 % to 81.9 % on ../eco/Scripts_wd, where the
    # shipped builds simply have fewer symbols and `sub_` is common and harmless.
    return lines, problems

CALL_HEAD = re.compile(r'(?<![.\w\x02])([A-Za-z_]\w*)\(')
NOMETHOD = lifter.NOMETHOD


def to_method_calls(s):
    """rewrite `Add(a, b)` into `a.Add(b)` for natives that are object methods.

    Scans brackets by depth instead of using a regex for the argument list, so calls
    whose arguments contain parentheses — `Add(arr, (y << 16) + t)` — are handled.
    The lookbehind keeps an already rewritten `a.Add(b)` from being rewritten again.
    """
    pos = 0
    while True:
        mo = CALL_HEAD.search(s, pos)
        if not mo:
            return s
        if _in_string(s, mo.start()):
            # A format string looks like a call: `"WP_STAFF_%d%d(%d,0)"` matches
            # CALL_HEAD on the `d(` of `%d(`, and rewriting it rejoined the
            # literal's own commas with ", " — 17 strings of TwoWorldsContainers
            # changed and the whole data segment moved (Common/Quest.ech:232).
            pos = mo.start() + 1
            continue
        name = mo.group(1)
        start = mo.end()
        i = _match_paren(s, start)
        if i is None:
            return s
        raw = _split_args(s[start:i - 1])
        implicit = bool(raw) and is_context(raw[0])
        args = drop_context_args(raw)
        # A name can be BOTH a method and a free function: the SDK writes
        # `pArtefact.IsAlchemyFormulaArtefact()` on an object elsewhere and
        # `IsAlchemyFormulaArtefact(strObjectID)` on a string here
        # (RPGCompute/Alchemy.ech). Only an object can be a receiver — EarthC
        # answers a string with "Expected object before".
        # Earlier attempt (`_NOTOBJ`, reverted): decide it HERE by testing the first
        # argument's text against the int/string names. That drops the receiver from
        # every call whose first argument merely shares a name with an int
        # declaration — 27 of 36 files stopped compiling. The decision now happens in
        # the lifter, on the argument NODE, and arrives as lifter.NOMETHOD in front
        # of the name.
        plain = mo.start() > 0 and s[mo.start() - 1] == NOMETHOD
        if implicit or plain or not is_method(name) or not args:
            # receiver is a context register the compiler passes itself -> the source
            # writes this as a plain call
            rep = f'{name}({", ".join(args)})'
            moved = False
        else:
            rep = f'{args[0]}.{name}({", ".join(args[1:])})'
            moved = True
        s = s[:mo.start()] + rep + s[i:]
        # When the receiver moves to the front, the call that produced it now starts
        # exactly where this match started; advancing even by one character skipped
        # its head and left `GetPartiesNums(ebx).GetSize()` with the context register
        # still in it. Rescanning from the same position is safe because the receiver
        # rewrite only ever moves text leftwards, so the next pass matches a different
        # (shorter) call and the loop terminates.
        pos = mo.start() if moved else mo.start() + 1
