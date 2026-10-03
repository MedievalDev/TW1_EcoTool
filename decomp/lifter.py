"""Lift compiled EarthC x86 back to .ec-like source.

The code generator is a template expander: expressions are evaluated through eax
with arguments pushed on the stack, one statement per source line, control flow via
cmp/jcc. So a symbolic execution of each basic block reconstructs the expressions,
and a structural pass over the block graph rebuilds if / else / while.

  py lifter.py <file.eco> [out.ec]
  py lifter.py --all
"""
import pathlib, sys, struct, collections, re
import capstone
import eco, ecodbg, disasm, nativemap, arity4

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
CS.detail = True
HOOK = disasm.DBG_HOOK
NOP_MARK = '//@nop'
TYPE = disasm.TYPE
CMP_OP = {'je': '==', 'jne': '!=', 'jge': '>=', 'jl': '<', 'jle': '<=', 'jg': '>'}
NEG = {'==': '!=', '!=': '==', '>=': '<', '<': '>=', '<=': '>', '>': '<='}
# same relation with the operands the other way round
MIRROR = {'==': '==', '!=': '!=', '>=': '<=', '<=': '>=', '<': '>', '>': '<'}
MAXEXPR = 400          # cap on expression text length, see E.__init__
# C precedence: comparisons bind TIGHTER than the bitwise operators, so x & 32 == 0
# parses as x & (32 == 0). Giving comparisons a lower number makes paren() wrap the
# bitwise operand, which is what the source means.
CMP_PRIO = 7
# Marker written in front of the name of a call to a routine defined in this very
# script. Script functions and object methods share names — Common/Levels.ech
# defines `function mission GetMission(int,int,int)` while `GetMission` is also a
# campaign method — so `emit_body.to_method_calls` cannot decide from the name
# alone and turned the internal call into `nCol.GetMission(nRow, 0)`. The marker is
# stripped again on the way out (emit_body.to_earthc, Lifter.text).
INTERNAL_MARK = '\x02'
# Marker written in front of a native whose first argument is provably NOT an object,
# so `emit_body.to_method_calls` has to keep the plain call form. A name can be both:
# the SDK writes `pArtefact.IsAlchemyFormulaArtefact()` on an object and
# `IsAlchemyFormulaArtefact(strObjectID)` on a string (RPGCompute/Alchemy.ech), and
# EarthC answers a string receiver with "Expected object before". Deciding this from
# the NAME failed (see the `_NOTOBJ` note in emit_body); the decision belongs here,
# where the argument is still an expression node with a known declaration kind.
# Stripped again on the way out, next to INTERNAL_MARK.
NOMETHOD = '\x03'
# Every native is registered with the C++ class it is a method of (out/native_api.txt,
# read out of EarthC.exe). That class is what says which receiver a name accepts:
# `EqualNoCase` is CECString and `strName.EqualNoCase(…)` is correct, while
# `IsAlchemyFormulaArtefact` is CUnitBase and the same shape on a string is not.
_NATIVE_CLASS = None
_STR_CLASS = {'CECString', 'CECStringW'}


def native_classes(name):
    """the C++ classes that declare a native of this name (may be several)"""
    global _NATIVE_CLASS
    if _NATIVE_CLASS is None:
        _NATIVE_CLASS = {}
        p = (pathlib.Path(__file__).resolve().parent / 'data' / 'native_api.txt')
        if p.exists():
            for line in p.read_text(encoding='utf8').splitlines():
                mo = re.match(r'\s*\d+\s+(\S+)\s+(C\w+)::', line)
                if mo:
                    _NATIVE_CLASS.setdefault(
                        mo.group(1).lstrip('$'), set()).add(mo.group(2))
    return _NATIVE_CLASS.get(name, ())
# set to a Counter-dict by nativemap.arg_handles while it learns which argument
# positions of a native hold object handles; None during normal lifting
ARGKIND_SINK = None
# set to a typeinfer.Facts while it collects how values flow (calls, assignments,
# returns) through a release build; None during normal lifting. The lift itself is
# unchanged by it.
TYPE_SINK = None
# `0x0074(&s, 0)` is how the compiler reads a string variable that is being passed
# as an argument. Measured with the SDK compiler on roundtrip/mytest4/t_str.ec:
# `sa.Format("%s", sb)` emits
#     mov eax,0 ; push eax ; lea eax,[sb] ; push eax ; call 0x0074 ; push eax
#     mov eax,0 ; push eax ; lea eax,[sa] ; push eax ; call Format
# so the call's value IS the string and it has no source form of its own. Written
# out it produced `strTMP.Format("%s…", …, native_0074(strQuiver, 0))`
# (TwoWorldsEnemies.ec:1866), which does not compile.
DEREF_ARG0 = {0x74}
# Array element access and string assignment DO have a source form (emit_body's
# ARRAY_SET / ARRAY_PTR / ARRAY_GET / STR_ASSIGN turn them into `a[i]` and `a = b`),
# so they must never be swallowed by the compiler-internal shortcut below, however
# `compiler_calls.py` voted. The vote only asks whether the corpus ever showed a
# debug ref record for the index, and a store into an object array never does:
# 0x55 (unit array $SetAt) came out as 'epilogue' and `m_arrTarget[nTargetOwner] =
# pTarget;` (Network/MissionTeamAssault.ec:178) vanished, 20 code bytes each.
SOURCE_FORM = {0x10, 0x27, 0x3e, 0x55,          # $SetAt   (int/string/stringW/unit)
               0x11, 0x28, 0x3f, 0x56,          # $GetPtrAt
               0x12, 0x29, 0x40, 0x57,          # $GetAt
               0x73, 0x105}                     # string / stringW assignment
# 8/16-bit operands name a part of a tracked 32-bit register
SUBREG = {'al': 'eax', 'ah': 'eax', 'ax': 'eax', 'bl': 'ebx', 'bh': 'ebx',
          'bx': 'ebx', 'cl': 'ecx', 'ch': 'ecx', 'cx': 'ecx', 'dl': 'edx',
          'dh': 'edx', 'dx': 'edx', 'si': 'esi', 'di': 'edi'}
# Every `/` and `%` is compiled with a zero-divisor guard around the idiv:
#     cdq ; mov ecx,<b> ; cmp ecx,0 ; jne L ; xor eax,eax ; xor edx,edx ; jmp M
#     L: idiv ecx ; M:
# The diamond is pure bookkeeping — semantically the whole thing is one division.
# Left as real control flow it splits the basic block and the quotient is lost at
# the merge, which is why `return 150 + ((i-50)/50)*50` (Common/Enums.ech:370)
# lifted to `return 150 + eax * 50` and failed to recompile. Collapsing it to the
# bare idiv keeps the block linear. 473 occurrences over ../eco/Scripts_wd, every
# one of them with ecx as the divisor and this exact byte layout.
DIVGUARD = re.compile(rb'\x83\xf9\x00\x0f\x85\x09\x00\x00\x00\x31\xc0\x31\xd2'
                      rb'\xe9\x02\x00\x00\x00(\xf7\xf9)', re.S)


def load_internal():
    """native indices the compiler emits on its own (scope enter/leave, string
    ctor/dtor, the debug line hook) — see compiler_calls.py"""
    import json
    p = (pathlib.Path(__file__).resolve().parent / 'data' / 'compiler_natives.json')
    if not p.exists():
        import compiler_calls
        where, _ = compiler_calls.scan()
        d = {i: max(('prologue', 'epilogue', 'inline'), key=lambda k: c[k])
             for i, c in where.items() if c['source'] == 0}
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({str(k): v for k, v in sorted(d.items())}, indent=1))
        return d
    return {int(k): v for k, v in json.loads(p.read_text()).items()}


# ---------------------------------------------------------------- expressions
class E:
    """expression node: kind in const/var/ref/call/bin/un/reg/raw

    Nested expressions are carried as text, so a value that keeps being folded into
    new expressions can grow exponentially (x = f(x, x) in a long block). Anything
    past MAXEXPR chars is truncated — the lifted code stays readable and the pass
    stays linear."""

    def __init__(self, kind, text, prio=0, args=None):
        if len(text) > MAXEXPR:
            text, kind, prio = text[:MAXEXPR] + ' /*…*/', 'raw', 99
        self.kind, self.text, self.prio, self.args = kind, text, prio, args or []

    def __str__(self):
        return self.text

    def paren(self, prio):
        return f'({self.text})' if self.prio > prio else self.text


def const(v):
    if v > 0x7FFFFFFF:
        v -= 1 << 32
    if v < 0:
        # A negative literal written with a minus sign is not the same code: EarthC
        # compiles `-1` to `mov eax,1 ; neg eax` and a *constant* whose value is -1
        # to a single `mov eax,0xffffffff`, two bytes less. The sources reach these
        # values through named constants (`eNoGroup = -1`, Common/Bandits.ech), and
        # the hex form of the same machine word compiles exactly like the constant.
        # Measured in roundtrip/mytest6/r3.ec: `return -1;` 17 code bytes,
        # `return eNeg;` and `return 0xffffffff;` 15 each.
        return E('const', f'0x{v & 0xFFFFFFFF:08x}')
    return E('const', str(v))


def var(name):
    return E('var', name)


def _pending(v):
    """value that still has to become a statement of its own.

    A call whose result nothing consumed is a void call. The string deref
    (DEREF_ARG0) is not a call node — it stays a variable so the expressions around
    it are unaffected — but `m_strMissionName;` is a statement all the same, so it
    is marked flushable when it is built."""
    if getattr(v, 'used', False):
        return False
    return v.kind == 'call' or getattr(v, 'flushable', False)


def binop(a, op, b, prio):
    # Every operator here is left-associative, so the RIGHT operand needs its
    # parentheses back at equal precedence as well — `paren(prio)` on both sides
    # reprinted `nLevelNum / (12 * 5)` (Common/Levels.ech:26) as
    # `nLevelNum / 12 * 5`, a different program. The compiler folds no constants,
    # so the grouping is observable in the generated code.
    return E('bin', f'{a.paren(prio)} {op} {b.paren(prio - 1)}', prio, [a, b])


def lookup_line(offs, at, addr):
    """source line for a code address: the last line record at or before it"""
    import bisect
    if not offs or addr is None or addr < offs[0]:
        return None
    k = bisect.bisect_right(offs, addr) - 1
    return at[offs[k]] if k >= 0 else None


class Lifter:
    @staticmethod
    def line_map(r):
        """(sorted offsets, offset -> (file, line)).

        Two records can share an offset; the isCall one wins, because that is the
        record a call statement anchors to (verified in line_check.py)."""
        at = {}
        for ln in r['lines']:
            if ln[2] not in at or ln[4] == 1:
                at[ln[2]] = (ln[0], ln[1])
        return sorted(at), at

    def __init__(self, path):
        import v10
        self.f, self.b = v10.parse(path)          # a v1.0 build in SDK 1.3 numbering
        self.path = pathlib.Path(path)
        self.code = bytes(self.f['code'])
        self.data = bytes(self.f['data_seg'])
        self.nat = nativemap.load()
        self.natret = nativemap.ret_kinds()
        self.natargs = nativemap.arg_handles()
        self.ari = arity4.load()
        self.internal = load_internal()
        self.imports = dict(self.f['imports'])
        self.site_names = self.f.get('site_names', {})
        self.cptr = set(self.f['code_ptrs'])
        self.routines = self.b['routines'] if self.b else disasm.scan_routines(self.code)
        self.by_start = {r['start']: r for r in self.routines}
        self.files = self.b['files'] if self.b else []
        self.globals = ({g['addr']: g for g in self.b['globals']} if self.b else {})
        self.stats = collections.Counter()
        self.ret_kind = self.return_kinds()
        self.cfg_exit = {}          # block addr -> exit before repair_returns
        self.learned = {}           # native idx -> arity recovered from a block
        self.spill_alias = {}       # `$n` -> live version inside the current block

    def return_kinds(self):
        """Return type per internal routine, taken from the call-site records of its
        callers (a ref carries the callee's return kind; 4 = void).

        Also fills self.ret_type with the type *name* for handle returns, which the
        kind alone cannot express — `unit` and `mission` are both kind 0.
        """
        self.ret_type = {}
        self.ret_kind_at = {}
        self.ret_type_at = {}
        if not self.b:
            return {}
        votes = collections.defaultdict(collections.Counter)
        tvotes = collections.defaultdict(collections.Counter)
        # Same votes, but keyed by the callee's start address instead of its name.
        # A name is not unique: MissionTeamRustling.ec:87/88 declares
        # `int GetTeamPaddockCenterPoint(int, int&, int&)` and
        # `void GetTeamPaddockCenterPoint(int[], int[], int&, int&)`. The name-keyed
        # vote made the int overload `void`, and using it inside `&&` was an
        # "Invalid type" that took the whole file down.
        avotes = collections.defaultdict(collections.Counter)
        atvotes = collections.defaultdict(collections.Counter)
        local = {r['name'] for r in self.routines}
        starts = {r['start'] for r in self.routines}
        for r in self.routines:
            for ref in r['refs']:
                if (ref['addr'] - 4) in self.imports:
                    continue                      # native, not one of ours
                if ref['name'] in local:
                    votes[ref['name']][ref['kind']] += 1
                    # kind 0 is "handle to a user type"; the record then carries the
                    # type name itself ('mission', 'unit', 'global', ...)
                    if ref['kind'] == 0 and isinstance(ref['type'], str):
                        tvotes[ref['name']][ref['type']] += 1
                    # `ref['addr']` is the byte behind the rel32 of the `call`, so
                    # the target is that address plus the displacement
                    try:
                        rel = struct.unpack_from('<i', self.code, ref['addr'] - 4)[0]
                    except Exception:
                        continue
                    tgt = ref['addr'] + rel
                    if tgt in starts:
                        avotes[tgt][ref['kind']] += 1
                        if ref['kind'] == 0 and isinstance(ref['type'], str):
                            atvotes[tgt][ref['type']] += 1
        self.ret_type = {n: c.most_common(1)[0][0] for n, c in tvotes.items()}
        self.ret_kind_at = {a: c.most_common(1)[0][0] for a, c in avotes.items()}
        self.ret_type_at = {a: c.most_common(1)[0][0] for a, c in atvotes.items()}
        return {n: c.most_common(1)[0][0] for n, c in votes.items()}

    # ------------------------------------------------------------ symbol names
    def frame(self, r):
        m = {8: 'this'} if r['kind'] != 7 else {}
        for p in r['params']:
            m[8 + p['addr']] = p['name']
        for l in r['locals']:
            m[-4 - l['addr']] = l['name']
        return m

    def mem_name(self, ins, names, alias=True):
        """symbolic name for a single memory operand, else None

        `alias` maps a compiler spill slot to the version currently live in this
        block (see run_block): the compiler reuses one slot for every spill of a
        statement, so `f(nX + j % 3 * 128, nY + j / 3 * 128)` stores into the same
        `$4` twice and both reads have to name different values. Pass alias=False
        to get the raw slot, which is what the store side needs."""
        op = ins.op_str
        mo = re.search(r'\[ebp ([+-]) (0x[0-9a-f]+|\d+)\]', op)
        if mo:
            off = int(mo.group(2), 0) * (1 if mo.group(1) == '+' else -1)
            if off in names:
                nm = names[off]
                return self.spill_alias.get(nm, nm) if alias else nm
            # release build: no debug names, derive slot names from the frame layout
            return (f'arg{(off - 8) // 4 + 1}' if off >= 8
                    else f'loc{(-off - 4) // 4 + 1}')
        mo = re.search(r'\[esi(?: \+ (0x[0-9a-f]+|\d+))?\]', op)
        if mo:
            off = int(mo.group(1), 0) if mo.group(1) else 0
            g = self.globals.get(off)
            return g['name'] if g else f'global_{off}'
        mo = re.search(r'\[esi - (0x[0-9a-f]+|\d+)\]', op)
        if mo:                               # VM fields in front of the globals
            return f'$vm{int(mo.group(1), 0)}'
        return None

    ELEM = re.compile(r'\w+\((\w+), ')

    def scan_postfix(self, blk, names):
        """`x++;` / `x--;` written as a statement.

        Measured with the SDK compiler (roundtrip/mytest2/I1.ec) — the three ways of
        adding one are three different code shapes, and only the first has the
        push/pop pair around it:

            x++;         mov eax,[x] ; push eax ; inc eax ; mov [x],eax ; pop eax
            ++x;         mov eax,[x] ;            inc eax ; mov [x],eax
            x = x + 1;   mov eax,[x] ;            add eax,1 ; mov [x],eax

        Read as an assignment the first one came out as `x = x + 1`, which recompiles
        to the third shape — four bytes more and the code segment shifts
        (PNames.ChooseNPCNameNum).

        -> {first address: (text, addresses to skip, is_value)}

        `is_value` marks the third form, `f(…, x++, …)`: there the restored old value
        is pushed straight on as an argument, so the five instructions are an
        expression and not a statement of their own.
        """
        out = {}
        for i in range(len(blk) - 4):
            (m0, a0, _s, i0), (m1, a1, _, i1) = blk[i], blk[i + 1]
            (m2, a2, _, i2), (m3, a3, _, i3) = blk[i + 2], blk[i + 3]
            (m4, a4, _, i4) = blk[i + 4]
            if (m0, m1, m4) != ('mov', 'push', 'pop') or m2 not in ('inc', 'dec'):
                continue
            if None in (i0, i1, i2, i3, i4) or m3 != 'mov':
                continue
            reg, _, mem = i0.op_str.partition(', ')
            if '[' not in mem or i1.op_str != reg or i2.op_str != reg:
                continue
            if i3.op_str != f'{mem}, {reg}' or i4.op_str != reg:
                continue
            nm = self.mem_name(i0, names)
            if nm is None:
                continue
            op = '++' if m2 == 'inc' else '--'
            # `j = i++;` (MissionCommon.ech:1123, `for (j = nNum-1; i < nNum; j = i++)`)
            # is the same five instructions with the restored old value stored
            # afterwards. Reading only the five left the store as `j = eax`, which
            # is not a variable and does not compile (MissionHorseRacing,
            # MissionTeamRustling).
            if i + 5 < len(blk):
                (m5, a5, _s5, i5) = blk[i + 5]
                if m5 == 'mov' and i5 is not None and i5.op_str.endswith(f', {reg}'):
                    tgt = self.mem_name(i5, names)
                    if tgt is not None and tgt != nm:
                        out[a0] = (f'{tgt} = {nm}{op};',
                                   {a0, a1, a2, a3, a4, a5}, False)
                        continue
                # `pMission.AddMarker("…", nDemonMarker++, nX, nY, 0, 0, "")`
                # (TwoWorldsMusic.ec:870): the value the `pop` restored is pushed
                # right back as the call's argument. Taken as a statement the five
                # instructions were cut out of the middle of the argument list and
                # the whole call went with them — AddEnemyMarker came out as four
                # lines, 150 code bytes instead of 192.
                if m5 == 'push' and i5 is not None and i5.op_str == reg:
                    out[a0] = (f'{nm}{op}', {a0, a1, a2, a3, a4, a5}, True)
                    continue
            out[a0] = (f'{nm}{op};', {a0, a1, a2, a3, a4}, False)
        # The same statement on an `int&` parameter. There the variable is reached
        # through the pointer in the frame slot, so load and store are two
        # instructions each and the five-instruction window above never matches:
        #
        #     nQuestsKill--;   mov ecx,[ebp+8] ; mov eax,[ecx] ; push eax ; dec eax
        #                      mov ecx,[ebp+8] ; mov [ecx],eax ; pop eax
        #
        # Read as an assignment it came out `nQuestsKill = nQuestsKill - 1`, which
        # recompiles to `sub eax,1` without the push/pop pair — two bytes per
        # occurrence (PQuestsMulti.ChooseQuestMultiType, PQuestsMulti.ec:748-750).
        for i in range(len(blk) - 6):
            w = blk[i:i + 7]
            ms = [x[0] for x in w]
            ins = [x[3] for x in w]
            if None in ins or w[0][1] in out:
                continue
            if ms[0] != 'mov' or ms[1] != 'mov' or ms[2] != 'push' \
                    or ms[3] not in ('inc', 'dec') or ms[4] != 'mov' \
                    or ms[5] != 'mov' or ms[6] != 'pop':
                continue
            ptr, _, slot = ins[0].op_str.partition(', ')
            if '[' not in slot or not re.fullmatch(r'e[a-d]x|e[sd]i', ptr):
                continue
            reg, _, src = ins[1].op_str.partition(', ')
            if not re.fullmatch(rf'(?:\w+ ptr )?\[{ptr}\]', src):
                continue
            if ins[2].op_str != reg or ins[3].op_str != reg or ins[6].op_str != reg:
                continue
            if ins[4].op_str != ins[0].op_str:
                continue
            st_dst, _, st_src = ins[5].op_str.partition(', ')
            if st_src != reg or not re.fullmatch(rf'(?:\w+ ptr )?\[{ptr}\]', st_dst):
                continue
            nm = self.mem_name(ins[0], names)
            if nm is None:
                continue
            out[w[0][1]] = (f'{nm}{"++" if ms[3] == "inc" else "--"};',
                            {x[1] for x in w}, False)
        # `++x` / `--x`: the same three instructions as `x = x + 1` except that the
        # compiler uses `inc`/`dec` instead of `add`/`sub`, so it is one byte where
        # the assignment form is five. `for (nIndex = 0; nIndex < nCount; ++nIndex)`
        # (Units/Alarm.ech:48) came out as `nIndex = nIndex + 1`, 4 bytes too many.
        # A window inside the postfix shape above cannot match this one — there the
        # `inc` is preceded by a `push`, not by the load.
        for i in range(len(blk) - 2):
            (m0, a0, _s, i0), (m1, a1, _, i1) = blk[i], blk[i + 1]
            (m2, a2, _, i2) = blk[i + 2]
            if m0 != 'mov' or m2 != 'mov' or m1 not in ('inc', 'dec'):
                continue
            if None in (i0, i1, i2) or a0 in out:
                continue
            reg, _, mem = i0.op_str.partition(', ')
            if '[' not in mem or i1.op_str != reg or i2.op_str != f'{mem}, {reg}':
                continue
            nm = self.mem_name(i0, names)
            if nm is None:
                continue
            out[a0] = (f'{"++" if m1 == "inc" else "--"}{nm};',
                       {a0, a1, a2}, False)
        return out

    def is_obj(self, e):
        """expression that is declared with an object type (see simulate)"""
        if e.kind == 'var':
            return e.text in getattr(self, 'objnames', ())
        if e.kind == 'call':
            if getattr(e, 'obj', False):
                return True           # callee's return kind says handle
            # an array element still reads as the raw accessor call here
            # (`native_0057(auBandits, i)`); to_earthc turns it into `auBandits[i]`
            mo = self.ELEM.match(e.text)
            return bool(mo) and mo.group(1) in getattr(self, 'objarrays', ())
        return False

    @staticmethod
    def as_null(v):
        """the integer 0 written as the object literal EarthC wants"""
        return E('const', 'null') if v.kind == 'const' and v.text == '0' else v

    def arg_evidence(self, e):
        """'obj' / 'int' when the argument's own declaration settles its type

        Only used to learn which parameter positions of a native are object
        handles (nativemap.arg_handles). A bare `0` proves nothing and returns
        None; a declared variable or an arithmetic expression does.
        """
        if self.is_obj(e):
            return 'obj'
        if e.kind in ('bin', 'un'):
            return 'int'
        if e.kind == 'var' and e.text in getattr(self, 'intnames', ()):
            return 'int'
        return None

    def no_receiver(self, name, e):
        """can `name(e, …)` NOT be the method overload `e.name(…)`?

        Only says yes when the argument's own declaration settles its type and no
        class that declares this name accepts that type. Everything unknown — a
        literal, a register, a call result, an array element — answers no, so the
        method form stays the default and this can only ever remove a receiver that
        the compiler would have rejected anyway.
        """
        cls = native_classes(name)
        if not cls:
            return False
        if e.kind in ('bin', 'un'):
            want = 'int'                 # arithmetic: never a handle
        elif e.kind == 'var':
            if e.text in getattr(self, 'objnames', ()) or self.is_objelem(e.text):
                return False
            if e.text in getattr(self, 'strnames', ()):
                want = 'str'
            elif e.text in getattr(self, 'numnames', ()):
                want = 'int'
            else:
                return False
        else:
            return False
        # no native class is an int class, so an int receiver is always wrong; a
        # string one is wrong unless the name really is a CECString method
        return True if want == 'int' else not (set(cls) & _STR_CLASS)

    def is_objelem(self, nm):
        """`auUnit[nUnit]` — an element of an array of objects

        The scalar case was already covered; the element case was not, and
        `auUnit[nUnit] = 0;` is an "Invalid type" (Campaigns/Missions/PTown.ec).
        """
        if not nm or '[' not in nm:
            return False
        return nm.split('[', 1)[0] in getattr(self, 'objarrays', ())

    @staticmethod
    def is_bookkeeping(stmt):
        """statement that has no source form: an AddRef/Release/ctor/dtor call

        emit_body owns those index sets; importing it here at module scope would be
        circular (it imports the lifter), so the import is local.
        """
        if stmt[0] != 'expr':
            return False
        import emit_body
        mo = re.match(r'\x02?native_([0-9a-f]{4})\(', str(stmt[1]))
        return bool(mo) and int(mo.group(1), 16) in (emit_body.HANDLE_PASS
                                                     | emit_body.extra_handle_pass()
                                                     | emit_body.LIFECYCLE)

    @staticmethod
    def deref_name(dst, regs):
        """`mov [ecx], eax` -> the name the pointer in ecx was loaded from

        Writing an `int&` parameter compiles to `mov ecx,[ebp+X] ; mov [ecx],eax`,
        and the second instruction has no ebp operand for mem_name to resolve, so
        `nCol = ...` came out as `dword ptr [ecx] = ...` (Common/Levels.ech:26).
        The register still holds the symbolic value loaded in the first
        instruction, which is exactly the name being assigned.
        """
        mo = re.fullmatch(r'(?:\w+ ptr )?\[(e[a-d]x|e[sd]i)\]', dst)
        if not mo:
            return None
        held = regs.get(mo.group(1))
        if held is None:
            return None
        if held.kind == 'var':
            return held.text
        if held.kind == 'ref' and held.text.startswith('&'):
            return held.text[1:]
        return None

    def native_name(self, idx):
        return nativemap.name_of(self.nat, idx)

    def data_string(self, off):
        end = self.data.find(b'\x00', off)
        if end < 0 or not 0 <= off <= len(self.data):
            return None
        s = self.data[off:end]
        # An empty string is a string: `AddMarker(..., "")` (Common/Ghosts.ech:258)
        # put a zero-length entry in the data segment, and rejecting it left the raw
        # `DATA+2427` in the lift. The operand only reaches here when the Code
        # Pointers table marks it as a data reference, so there is no ambiguity with
        # the literal 0.
        if len(s) < 200 and all(9 <= c < 127 for c in s):
            return (s.decode('latin1').replace('\\', '\\\\').replace('\n', '\\n')
                    .replace('\r', '\\r').replace('\t', '\\t').replace('"', '\\"'))
        return None

    # ------------------------------------------------------------ block decode
    def decode(self, r):
        out, pos = [], r['start']
        while pos <= r['end']:
            mo = HOOK.match(self.code, pos, r['end'] + 1)
            if mo:
                # the hook's immediates are not (file, line) in every build, so the
                # comment is taken from the line table instead — see routine_text
                out.append(('hook', pos, mo.end() - pos, None))
                pos = mo.end()
                continue
            mo = DIVGUARD.match(self.code, pos, r['end'] + 1)
            if mo:
                at = mo.start(1)
                div = next(CS.disasm(self.code[at:mo.end()], at), None)
                if div is not None:
                    out.append((div.mnemonic, div.address, div.size, div))
                    pos = mo.end()
                    continue
            ins = next(CS.disasm(self.code[pos:min(r['end'] + 1, pos + 16)], pos), None)
            if ins is None:
                out.append(('bad', pos, 1, None))
                pos += 1
                continue
            out.append((ins.mnemonic, ins.address, ins.size, ins))
            pos = ins.address + ins.size
        return out

    def split_blocks(self, insns, r):
        leaders = {r['start']}
        for m, a, s, ins in insns:
            if m.startswith('j') and ins is not None and ins.op_str.startswith('0x'):
                t = int(ins.op_str, 16)
                if r['start'] <= t <= r['end']:
                    leaders.add(t)
                if a + s <= r['end']:
                    leaders.add(a + s)
        blocks, cur = [], []
        for item in insns:
            if item[1] in leaders and cur:
                blocks.append(cur)
                cur = []
            cur.append(item)
        if cur:
            blocks.append(cur)
        return blocks

    # ------------------------------------------------------------ symbolic run
    @staticmethod
    def block_edges(blocks, order):
        """count incoming edges per block, from the last instruction of each block"""
        preds = collections.Counter()
        addrs = set(order)
        for k, blk in enumerate(blocks):
            m, a, s, ins = blk[-1]
            nxt = order[k + 1] if k + 1 < len(order) else None
            op = ins.op_str if ins is not None else ''
            if m == 'ret':
                continue
            if m == 'jmp':
                if op.startswith('0x') and int(op, 16) in addrs:
                    preds[int(op, 16)] += 1
                continue
            if m.startswith('j') and op.startswith('0x'):
                if int(op, 16) in addrs:
                    preds[int(op, 16)] += 1
            if nxt is not None:
                preds[nxt] += 1
        return preds

    @staticmethod
    def pred_lists(blocks, order):
        """predecessor block indices per block (the edge list block_edges counts)"""
        pos = {a: i for i, a in enumerate(order)}
        out = [[] for _ in blocks]
        for k, blk in enumerate(blocks):
            m, a, s, ins = blk[-1]
            op = ins.op_str if ins is not None else ''
            if m == 'ret':
                continue
            if m == 'jmp':
                if op.startswith('0x') and int(op, 16) in pos:
                    out[pos[int(op, 16)]].append(k)
                continue
            if m.startswith('j') and op.startswith('0x') and int(op, 16) in pos:
                out[pos[int(op, 16)]].append(k)
            if k + 1 < len(blocks):
                out[k + 1].append(k)
        return out

    def run_block(self, blk, names, skip, stack_in=None, regs_in=None, void=False,
                  report=None):
        """Symbolically execute one basic block.

        Registers hold expressions; the argument stack holds pushed expressions.
        A call expression that gets overwritten without being consumed becomes a
        statement of its own (that is how void calls appear in the code).

        stack_in / regs_in carry the state over from the single predecessor block —
        arguments are often pushed before a branch while the call sits in the
        following block, which block-local simulation would report as a missing
        argument.

        returns (statements, exit, stack_out, regs_out)
        """
        st = []
        stack = list(stack_in) if stack_in else []
        regs = (dict(regs_in) if regs_in else
                {r: E('reg', r) for r in ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi')})
        cond = None
        exit_ = ('fall',)
        # which register the last flag-setting instruction wrote; a bare `je`
        # without a compare tests that one, not necessarily eax
        flagreg = 'eax'

        def get(name):
            if name.startswith('0x') or name.isdigit():
                return const(int(name, 0))
            # A variable shift count lives in cl (`shl eax, cl`, 101 times in the
            # first 40 corpus files). Read as its own register it produced the
            # literal `1 << cl` — not an identifier the compiler knows. The value is
            # whatever was moved into ecx.
            return regs.get(SUBREG.get(name, name), E('reg', name))

        def set_reg(name, val):
            old = regs.get(name)
            if old is not None and _pending(old):
                if any(v is old for k, v in regs.items() if k != name):
                    # A copy is still live in another register, so the result is not
                    # dropped at all — the compiler shuffles it out of eax to free
                    # the register (`call GetSize ; mov edx,eax ; mov eax,<spill>`
                    # around an idiv, TwoWorldsMusic.PlayRandom). Emitting it here
                    # printed the call twice: once as a bare statement and once
                    # inside the expression that really consumed it.
                    regs[name] = val
                    return
                st.append(('expr', old, getattr(old, 'addr', 0)))   # dropped -> void call
                old.used = True
            regs[name] = val

        def use(v):
            v.used = True
            return v

        def flush():
            """emit calls whose result was never consumed (void calls)"""
            for rn, v in regs.items():
                if _pending(v):
                    st.append(('expr', v, getattr(v, 'addr', 0)))
                    v.used = True

        # `spill_alias` / `spillcnt` live on the Lifter, not here: the compiler reuses
        # one slot for the spills of successive condition blocks too, and two blocks
        # of the same `&&` / `||` chain storing into `$4` blocked structure2's
        # merge_conditions from folding them (its collision test cannot know which
        # store a reference means). `(arrY[i] <= nY && nY < arrY[j]) || (…)` in
        # Network/MissionCommon.ech:1125 then had the whole arithmetic behind it
        # duplicated into both arms, +184 code bytes in IsPointInPolygon.
        spillcnt = self.spillcnt
        post = self.scan_postfix(blk, names)
        postskip = set().union(*(v[1] for v in post.values())) if post else set()
        vsave = getattr(self, 'vsave', None)
        vsaved = None                 # value parked across the destructor run
        for m, a, s, ins in blk:
            if a in post:
                txt, _sk, is_value = post[a]
                if is_value:
                    # `f(x++)`: the trailing push is part of the pattern, so the
                    # argument goes on the stack here and nothing is flushed —
                    # flushing would turn the call being built into a statement.
                    stack.append(use(E('var', txt)))
                    continue
                flush()
                st.append(('raw2', txt, a))
                continue
            if a in postskip:
                continue
            if m == 'hook':
                flush()                       # statement boundary
                st.append(('hookmark', a, s))   # statement boundary; lines come from addresses
                continue
            if vsave is not None and a == vsave[0]:
                vsaved = regs['eax']          # epilogue save: not an argument push
                continue
            if vsave is not None and a == vsave[1]:
                # …and not an argument pop either. Putting the value back matters
                # for a routine that returns one: the destructor run in between
                # loads the handles it releases into eax, and without the restore
                # the last of them became the return value
                # (TwoWorldsMusic.StateCheck_GetMapType: `return pMission;` in an
                # `int` function, "Invalid return type").
                if vsaved is not None:
                    set_reg('eax', vsaved)
                continue
            if a in skip:
                continue
            o = ins.op_str if ins is not None else ''
            dst, _, src = o.partition(', ')
            if m == 'push':
                nm = self.mem_name(ins, names) if '[' in o else None
                stack.append(use(var(nm) if nm else get(o)))
            elif m == 'pop':
                v = stack.pop() if stack else E('raw', '?')
                set_reg(dst or o, v)
            elif m == 'mov':
                if dst in regs:
                    if '[' in src:
                        # reading an `int&` parameter is the mirror of writing it:
                        # `mov ecx,[ebp+X] ; mov eax,[ecx]` (PTown, RPGCompute)
                        nm = self.mem_name(ins, names)
                        deref = False
                        if not nm:
                            nm = self.deref_name(src, regs)
                            deref = bool(nm)
                        v_ = var(nm) if nm else E('raw', src)
                        if deref:
                            v_.deref = True      # the value behind a reference (typeinfer)
                        set_reg(dst, v_)
                    elif src.startswith('0x') or src.isdigit():
                        v = int(src, 0)
                        if (a + 1) in self.cptr:
                            txt = self.data_string(v)
                            set_reg(dst, E('const', f'"{txt}"' if txt is not None
                                           else f'DATA+{v}'))
                        else:
                            set_reg(dst, const(v))
                    else:
                        set_reg(dst, get(src))
                elif '[' in dst:
                    base = self.mem_name(ins, names, alias=False)
                    if base is not None and base.startswith('$') \
                            and not base.startswith('$vm'):
                        # A fresh version per store, so two spills of the same
                        # statement into the same slot stay apart. Without it
                        # emit_body.inline_temps folded the LAST value into every
                        # reference: `CreateSingleEnemy(…, nX + j%3*128,
                        # nY + j/3*128, …)` (Common/Enemies.ech:1212) came out with
                        # nX in both places.
                        n = spillcnt.get(base, 0) + 1
                        spillcnt[base] = n
                        self.spill_alias[base] = base if n == 1 else f'{base}_{n}'
                    nm = self.mem_name(ins, names)
                    if nm is None:
                        nm = self.deref_name(dst, regs)
                    val = use(get(src))
                    if nm in getattr(self, 'objnames', ()) or self.is_objelem(nm):
                        val = self.as_null(val)     # `m_uDefender = null;`
                    st.append(('assign', nm or dst, val, a))
                    if getattr(self, 'release_mode', False):
                        # the stored value stays in eax; at the routine's end that is
                        # junk a void routine leaves behind, not a return value
                        # (`g24 = 300;` came back as `g24 = 300; return 300;`)
                        val.assigned = True
                    if TYPE_SINK is not None:
                        TYPE_SINK.assign(self, nm, val)
            elif m == 'lea':
                nm = self.mem_name(ins, names)
                set_reg(dst, E('ref', f'&{nm}' if nm else f'&{src}'))
            elif m == 'xor' and dst == src:
                set_reg(dst, const(0))
            elif m == 'and' and dst == src:
                # `and reg,reg` is the compiler's truthiness test; `cmp reg,0` is the
                # explicit comparison. Measured with the SDK compiler on
                # roundtrip/condtest/T1.ec — the test instruction encodes which of the
                # two source families was written, the jcc polarity which member:
                #   if (x)      -> and eax,eax ; je  skip      (2 bytes)
                #   if (!x)     -> and eax,eax ; jne skip
                #   if (x != 0) -> cmp eax,0   ; je  skip      (5 bytes)
                #   if (x == 0) -> cmp eax,0   ; jne skip
                # The third field marks the truthiness family so the jcc below can
                # spell it `x` / `!x` instead of `x != 0` / `x == 0`.
                cond = (use(get(dst)), const(0), True)
            elif m in ('imul', 'mul') and ', ' not in o:
                # one-operand form: edx:eax = eax * r/m32, exactly like idiv above.
                # Reading it as a two-operand `edx *= <empty>` silently threw the
                # product away — Level2LevelNum (Common/Levels.ech:16) lifted to an
                # empty body because every one of its multiplications vanished.
                rhs = self.mem_operand(ins, names) if '[' in o else get(o)
                lhs, rhs = use(get('eax')), use(rhs)
                # Same operand flip as the two-operand form below: a compiler
                # temporary on the right holds the LEFT source operand, because the
                # compiler evaluates `A * B` by putting A in a temporary first.
                # Without it `30 * (a + b)` came back as `(a + b) * 30`, which the
                # compiler renders as `mov edx,30 / imul edx` — no temporary, three
                # bytes more and a frame slot less (TwoWorldsSounds state Nothing).
                regs['eax'] = (binop(rhs, '*', lhs, 3)
                               if rhs.kind == 'var' and rhs.text.startswith('$')
                               else binop(lhs, '*', rhs, 3))
            elif m in ('add', 'sub', 'imul', 'and', 'or', 'xor', 'shl', 'shr'):
                op = {'add': '+', 'sub': '-', 'imul': '*', 'and': '&', 'or': '|',
                      'xor': '^', 'shl': '<<', 'shr': '>>'}[m]
                prio = {'+': 4, '-': 4, '*': 3, '&': 8, '|': 10, '^': 9,
                        '<<': 5, '>>': 5}[op]
                rhs = (self.mem_operand(ins, names, regs, src) if '[' in src
                       else get(src))
                lhs = use(get(dst))
                rhs = use(rhs)
                # For `A op B` the compiler evaluates A into a temporary first, then
                # B into eax, then `op eax, temp`. So when the operand is a compiler
                # temporary it holds the LEFT source operand and the order has to be
                # flipped back — `nPosX + (nPosY << 16)` (Levels.ech:24) otherwise
                # comes out as `(nPosY << 16) + nPosX`, which compiles without the
                # temporary and costs a frame slot.
                flagreg = dst if dst in regs else flagreg
                if op in '+*&|^' and rhs.kind == 'var' and rhs.text.startswith('$'):
                    regs[dst] = binop(rhs, op, lhs, prio)
                else:
                    regs[dst] = binop(lhs, op, rhs, prio)
                    # `A - B` gets the same treatment, but subtraction does not
                    # commute: the compiler still puts A in the temporary, so it
                    # emits `sub eax,temp` (which computes B - A) and repairs the
                    # sign with a following `neg eax`. The pair is one source
                    # subtraction. Remembering the ordered form here lets `neg`
                    # print `nSum - anSoundWeight[i]` instead of
                    # `-(anSoundWeight[i] - nSum)` — the latter recompiles with an
                    # extra temporary slot and 5 more code bytes
                    # (TwoWorldsSounds.CalculateIndex, Sounds.ec:561).
                    if op == '-' and rhs.kind == 'var' and rhs.text.startswith('$'):
                        regs[dst].negswap = binop(rhs, op, lhs, prio)
            elif m == 'idiv':
                # edx:eax / r/m32 -> quotient in eax, remainder in edx. The
                # remainder matters: 120 of the 473 guarded divisions in the corpus
                # are followed by `mov eax, edx`, i.e. they are `%` not `/`.
                rhs = self.mem_operand(ins, names) if '[' in o else get(o)
                num, den = use(get('eax')), use(rhs)
                regs['eax'] = binop(num, '/', den, 3)
                regs['edx'] = binop(num, '%', den, 3)
            elif m == 'neg':
                cur = use(get(dst))
                swap = getattr(cur, 'negswap', None)
                regs[dst] = swap if swap is not None else \
                    E('un', f'-{cur.paren(2)}', 2)
            elif m == 'not':
                regs[dst] = E('un', f'~{use(get(dst)).paren(2)}', 2)
            elif m in ('inc', 'dec'):
                if dst in regs:
                    regs[dst] = binop(use(get(dst)), '+' if m == 'inc' else '-',
                                      const(1), 4)
                else:
                    nm = self.mem_name(ins, names)
                    st.append(('raw2', f'{nm or dst}{"++" if m == "inc" else "--"};', a))
            elif m == 'cmp':
                # `regs`/`part` so that a comparison against an `int&` parameter can
                # be resolved: reading one is `mov ecx,[ebp+X] ; cmp eax,[ecx]`, and
                # the second instruction has no ebp operand for mem_name. Without
                # them `GetAlchemyItemsCount(uHero) >= nTotalMaxCount`
                # (RPGCompute/Alchemy.ech:74) came out as `… >= *`.
                lhs = self.mem_operand(ins, names, regs, dst) \
                    if dst.startswith('[') or 'ptr' in dst else get(dst)
                rhs = (self.mem_operand(ins, names, regs, src) if '[' in src
                       else get(src))
                if self.is_obj(lhs):
                    rhs = self.as_null(rhs)
                elif self.is_obj(rhs):
                    lhs = self.as_null(lhs)
                # `A < B` is compiled as: A into a temporary, B into eax,
                # `cmp eax, temp`. So when the right operand is a compiler temporary
                # it holds the LEFT source operand and the comparison has to be
                # mirrored back — the same flip the arithmetic operators above do.
                # Without it `nA < nB` came back as `nB > nA`, which the compiler
                # renders with the operands (and the temporary) the other way round
                # (PNames.ChooseNPCNameNum).
                swapped = rhs.kind == 'var' and rhs.text.startswith('$')
                cond = (use(lhs), use(rhs), False, swapped)
            elif m == 'sete':
                # `!x` is `mov ecx,eax ; xor eax,eax ; cmp ecx,0 ; sete al` — the
                # value tested sits in ecx, eax has already been cleared for the
                # result. Reading eax gave the useless `0 == 0`
                # (TwoWorldsMusic.UpdateState, `!!(nPrevDanger ^ nDanger)`).
                if cond is not None and cond[1].kind == 'const'                         and cond[1].text == '0':
                    v = use(cond[0])
                    regs['eax'] = E('un', f'!{v.paren(2)}', 2)
                else:
                    regs['eax'] = E('bin', f'{use(get("eax"))} == 0', CMP_PRIO)
                cond = None
            elif m == 'call':
                if ins is not None and self.code[a] == 0xE8:
                    rel = struct.unpack_from('<i', self.code, a + 1)[0]
                    callee = None
                    if rel == 0:
                        idx = self.imports.get(a + 1)
                        # a v1.0 build names each call itself (v10.translate): the name stays right even
                        # where the 1.3 index is only a guess, and the 1.3 compiler picks the overload
                        name = (self.site_names.get(a + 1) or
                                (self.native_name(idx) if idx is not None else 'native_?'))
                        n = self.ari.get(idx)
                        if n is None:
                            n = self.learned.get(idx)
                        if n is None and report is not None:
                            report.append(idx)
                    else:
                        tgt = a + s + rel
                        callee = self.by_start.get(tgt)
                        name = INTERNAL_MARK + (callee['name'] if callee
                                                else f'sub_{tgt}')
                        n = (len(callee['params']) + (0 if callee['kind'] == 7 else 1)
                             if callee else None)
                    # scope enter/leave and string ctor/dtor: bookkeeping, hide them.
                    # inline compiler calls (implicit assignment/conversion) are kept.
                    internal = (rel == 0 and idx not in SOURCE_FORM and
                                self.internal.get(idx) in ('prologue', 'epilogue'))
                    if internal and n is None:
                        v = E('raw', '?')
                        v.used = True
                        regs['eax'] = v
                        self.stats['call-compiler'] += 1
                    elif n is None:
                        self.stats['call-unknown-arity'] += 1
                        unk = E('call', f'{name}(/* arity ? */)')
                        unk.addr = a
                        set_reg('eax', unk)
                    else:
                        take = stack[-n:] if n else []
                        del stack[len(stack) - len(take):]
                        args = list(reversed(take))
                        # a `unit`/`mission` parameter takes `null`, not 0 — EarthC
                        # rejects the integer ("Cannot find suitable function",
                        # Units/Unit.ec FindSlotWithUsableMagicCard(..., null))
                        if callee is not None:
                            pad_ = len(args) - len(callee['params'])
                            for k_, p_ in enumerate(callee['params']):
                                if p_['kind'] == 0 and k_ + pad_ < len(args):
                                    args[k_ + pad_] = self.as_null(args[k_ + pad_])
                        elif idx is not None:
                            # Same rule for natives. They have no parameter list
                            # anywhere in the .eco, so the handle positions are
                            # learned from the debug builds' call sites — see
                            # nativemap.arg_handles. Without it
                            # `CanMakeMagic(null, n, true, true)`
                            # (Units/Magic.ech:768) lifted as `CanMakeMagic(0, …)`
                            # and Hero.ec / Unit.ec did not compile.
                            if ARGKIND_SINK is not None:
                                for k_, a_ in enumerate(args):
                                    ev = self.arg_evidence(a_)
                                    if ev:
                                        ARGKIND_SINK[(idx, k_)][ev] += 1
                            for k_ in self.natargs.get(idx, ()):
                                if k_ < len(args):
                                    args[k_] = self.as_null(args[k_])
                        if TYPE_SINK is not None:
                            TYPE_SINK.call(self, idx if rel == 0 else None,
                                           None if rel == 0 else tgt, args)
                        if len(take) < n:
                            # The table arity is a corpus-wide majority and can be too
                            # large (CreateMission votes 6, every call site in the SDK
                            # sources passes 3 + context = 4). What the code actually
                            # pushed is local, checkable evidence, so it wins. Padding
                            # with `?` produced `CreateMission(a, b, 0, ?, ?)`, which
                            # is not even parseable — a wrong arity at least compiles
                            # and shows up in the byte comparison.
                            self.stats['call-short-stack'] += 1
                        if rel == 0 and idx in DEREF_ARG0 and args \
                                and args[0].kind == 'ref':
                            self.stats['call-deref'] += 1
                            # The node stays a plain variable so every expression
                            # around it is unaffected, but it has to be flushable:
                            # `m_strMissionName;` really is a statement of its own
                            # (Network/MissionCommon.ech:320) and compiles to this
                            # call, 15 code bytes that were dropped because only
                            # unconsumed `call` nodes ever became statements.
                            v = var(args[0].text[1:])
                            v.flushable = True
                            v.addr = a
                            set_reg('eax', v)
                        elif internal:
                            self.stats['call-compiler'] += 1
                            v = E('raw', '?')
                            v.used = True
                            regs['eax'] = v
                        else:
                            # only an object can be a receiver: when argument 0 is a
                            # declared int/string/float or an arithmetic expression,
                            # this call site cannot be the method overload
                            # (a nameless `native_xxxx` is never rewritten into a
                            # method and emit_body matches it anchored, so leave it)
                            mark = (NOMETHOD if rel == 0 and args
                                    and not name.startswith('native_')
                                    and self.no_receiver(name, args[0]) else '')
                            call = E('call',
                                     f'{mark}{name}({", ".join(map(str, args))})')
                            # kind 0 = handle: the result is an object, which decides
                            # whether `!= 0` has to be written `!= null`
                            call.obj = (self.natret.get(idx) == 0 if rel == 0 else
                                        self.ret_kind.get(name.lstrip(INTERNAL_MARK))
                                        == 0)
                            # where the value comes from, for typeinfer
                            call.nidx = idx if rel == 0 else None
                            call.callee = None if rel == 0 else tgt
                            call.argv = args
                            # every call site has a line record with isCall=1 at the
                            # END of the call instruction (22829/22829 verified in
                            # line_anchor.py) — that is the exact source line
                            call.addr = a + s
                            set_reg('eax', call)
                            self.stats['call'] += 1
                else:
                    set_reg('eax', E('call', 'indirect()'))
            elif m.startswith('j'):
                if m == 'jmp' and o.startswith('0x'):
                    exit_ = ('jmp', int(o, 16))
                elif o.startswith('0x'):
                    op = CMP_OP.get(m)
                    if cond is not None and op:
                        if len(cond) > 2 and cond[2] and op in ('==', '!='):
                            # truthiness test (see `and reg,reg` above): jne takes the
                            # branch when the value is true, je when it is false
                            c = (cond[0] if op == '!=' else
                                 E('un', f'!{cond[0].paren(2)}', 2))
                            # `if (pMission)` compiles, `if (pMission && …)` does not
                            # ("Invalid type", measured _spike/p2/probe nl_*.ec and
                            # TestPMMission's CommandDebug): a handle tested on its own
                            # is a nested `if`, never one half of a && / || chain
                            if self.is_obj(cond[0]):
                                c = E(c.kind, c.text, c.prio, c.args)
                                c.handle_truth = True
                        elif len(cond) > 3 and cond[3]:
                            c = binop(cond[1], MIRROR[op], cond[0], CMP_PRIO)
                        else:
                            c = binop(cond[0], op, cond[1], CMP_PRIO)
                    else:
                        # Without an explicit compare the flags come from the last
                        # arithmetic instruction, and that is not always on eax: the
                        # iteration watchdog is `sub edi,1 ; je <stub>`. Reading it
                        # as `eax == 0` consumed whatever call was still in eax, and
                        # since the watchdog block's condition is thrown away
                        # (structure2.guards) the call went with it —
                        # `anTempAngles.RemoveAt(0)` disappeared from
                        # TwoWorldsHeroControl.CheckEnemies (HeroControl.ec:210),
                        # 15 code bytes.
                        c = E('bin', f'{use(get(flagreg))} {op or m} 0', CMP_PRIO)
                    exit_ = ('jcc', c, int(o, 16), blk[-1][1] + blk[-1][2])
                cond = None
            elif m == 'ret':
                v = regs['eax']
                if void or v.kind in ('raw', 'reg') or getattr(v, 'assigned', False) \
                        or getattr(self, 'release_mode', False):
                    exit_ = ('ret', None)     # value in eax is not a return value
                else:
                    exit_ = ('ret', v)
                    v.used = True             # so flush does not print it twice
            elif m == 'nop' and getattr(self, 'mark_nops', False):
                # a debug build puts a `nop` where a loop's `continue` lands: in `for (...; c; i++)`
                # between the body and the increment. emit_ec moves what follows it into the header.
                flush()
                st.append(('raw2', NOP_MARK, a))
            elif m in ('cdq', 'nop'):
                pass
            else:
                self.stats[f'unhandled-{m}'] += 1
        # no final flush here: whether pending void calls belong to this block or
        # travel on to the successor is the caller's call (see routine_text)
        return st, exit_, stack, regs

    @staticmethod
    def flush_regs(st, regs):
        for v in regs.values():
            if _pending(v):
                st.append(('expr', v, getattr(v, 'addr', 0)))
                v.used = True

    def mem_operand(self, ins, names, regs=None, part=None):
        nm = self.mem_name(ins, names)
        if nm is None and regs is not None and part is not None:
            nm = self.deref_name(part, regs)
        return var(nm) if nm else E('raw', '*')

    def mark_value_save(self, insns):
        """the `push eax` … `pop eax` the compiler wraps around the epilogue

        Local destructors run after the last statement, so the compiler saves the
        value in eax across them and pops it back right before the frame teardown.
        Read as an argument push, that save marks the value as consumed and the
        statement it came from is never printed — `arrTooltips.Add(strTooltip);`
        (Levels.ech:58) vanished from the lift for exactly this reason.

        The matching push is found by walking back from the restoring pop and
        letting every epilogue call re-open as many pushes as it takes. Anything
        that is not part of that destructor run aborts the search, so a routine
        whose tail the arity table cannot account for keeps the old behaviour.

        Measured: Network/Towns.ec went from 2873 to a byte-identical 2890, and
        emit_survey dropped from 6700 to 6684 of 7247. The 16 routines are not a
        regression — they are exactly the ones whose recovered last statement is
        a call of still unknown arity (SetConsoleText, PlayDialog,
        PlayVideoCutscene). They used to count as clean because the statement was
        dropped instead of reported; the survey now sees the real gap.
        """
        i = len(insns) - 1
        while i >= 0:
            m, _a, _s, ins = insns[i]
            o = ins.op_str if ins is not None else ''
            if m in ('ret', 'nop') or (m == 'mov' and o == 'esp, ebp') \
                    or (m == 'pop' and o != 'eax'):
                i -= 1
                continue
            break
        if i < 0 or insns[i][0] != 'pop':
            return set()
        pop_at = i
        need = 1
        for k in range(pop_at - 1, -1, -1):
            m, a, s, ins = insns[k]
            if m == 'push':
                need -= 1
                if need == 0:
                    # ordered: run_block has to know which of the two saves and
                    # which restores, so it can put the value back into eax
                    self.vsave = (insns[k][1], insns[pop_at][1])
                    return {insns[k][1], insns[pop_at][1]}
            elif m == 'call':
                if ins is None or self.code[a] != 0xE8 \
                        or struct.unpack_from('<i', self.code, a + 1)[0] != 0:
                    return set()                  # a real call: not the epilogue
                idx = self.imports.get(a + 1)
                # Any compiler-inserted native counts, not only the ones whose
                # majority position is 'epilogue'. That label is a corpus-wide vote
                # (compiler_calls.py) and the handle-release pair 0x057a / 0x03c0
                # is used mid-routine far more often than at the end, so it votes
                # 'inline' and aborted the walk — TwoWorldsMusic.AddEnemyMarker lost
                # its whole `pMission.AddMarker(…)` statement that way. Indices in
                # self.internal are never called from source at all, so accepting
                # them here cannot swallow a user-level call.
                if self.internal.get(idx) is None or self.ari.get(idx) is None:
                    return set()
                need += self.ari[idx]
            elif m not in ('lea', 'mov'):
                return set()
        return set()

    def infer_arity(self, unknown, stack_in, stack_out, entry=False):
        """Recover one missing native arity from the block that just ran.

        The code generator leaves the argument stack empty at the end of a basic
        block, so with a single unknown callee in a block that started empty the
        leftover depth IS that callee's arity: the run treated it as taking nothing,
        and everything else in the block balanced out.

        This is local, first-hand evidence and beats the corpus-wide arity table,
        which is a majority vote over block equations and gets a handful of entries
        wrong (SetHorizonOffset had no value at all, GetWorldWidth voted 2 and
        swallowed the following argument). Hand-checked on
        MainMenuCampaign_1.state Initialize: 16 pushes, six calls of known arity
        consuming 13, leftover 3 = `GetMission(0).SetHorizonOffset(-450, 900)`.

        Returns True when something was learned and the block must be re-run.
        """
        # The block has to start with an empty stack for the leftover to mean
        # anything. A jump target can inherit pushes from a predecessor (`&&` / `||`
        # evaluate across the branch), and counting those as the callee's arguments
        # is exactly the error that makes arity4's corpus vote wrong: over the
        # 107-file corpus the unrestricted rule voted SetHorizonOffset
        # {3:3, 2:4, 6:1, 5:3} — the clean-block rule votes 3.
        if not entry or stack_in or len(set(unknown)) != 1 or len(unknown) != 1:
            return False
        idx = unknown[0]
        n = len(stack_out)
        if idx is None or not 0 < n <= 16 or idx in self.learned:
            return False
        self.learned[idx] = n
        self.stats['arity-learned'] += 1
        return True

    def simulate(self, r):
        """Run every block of a routine and return (order, info, preds, ends).

        `ends` keeps each block's final register state, which is what makes the
        return-value repair below possible.
        """
        import arity4
        names = self.frame(r)
        self.cur_routine = r
        # `uUnit != 0` does not compile — EarthC spells the null object `null` and
        # rejects the integer (Units/Hero.ec, Campaigns/Missions/Mission_E01.ec …).
        # Kind 0 is the object kind (disasm.TYPE), so the declaration says which
        # names need the other spelling.
        decls = list(r['params']) + list(r['locals']) + list(self.globals.values())
        self.objnames = {v['name'] for v in decls if v['kind'] == 0}
        # the counterpart: names that are certainly NOT handles, so a native
        # argument position that ever sees one cannot be a handle position
        self.intnames = {v['name'] for v in decls if v['kind'] in (1, 2, 3)}
        # split finer: which receiver a name can be depends on string vs number
        self.strnames = {v['name'] for v in decls if v['kind'] in (2, 3)}
        self.numnames = {v['name'] for v in decls if v['kind'] == 1}
        # arrays of objects: `auBandits[i] != null` has to keep the spelling too
        self.objarrays = {v['name'] for v in decls if v['kind'] == 6
                          and (isinstance(v.get('type'), str) or v.get('sub') == 0)}
        # A state is `void` in the debug info but its generated code does return a
        # value: the delay in eax (`mov eax,150 ; jmp epilogue`). Treating it as void
        # threw that away, so `$State = 1;` lost the `return 150;` that belongs to it
        # and the transition came out as a bare `state state_1;` — which EarthC
        # rejects inside a state body (TwoWorldsLights, Mission_E01).
        # Only a plain function can be void. `ret_kind` is keyed by NAME, and a
        # command may share its name with the function it forwards to — RPGCompute's
        # `command GetMagicSummonLevels(...)` calls the void function of the same
        # name. Treating the command as void swallowed its `return true;`, emit_ec
        # then fell back to the forwarding stub, and the stub passes the COMMAND's
        # seven parameters to a function that takes eight ("Cannot find suitable
        # function", RPGCompute.ec:336). Commands and events return a bool, states
        # return their delay.
        # `retkind` is the per-callee vote emit_ec resolved from the debug record's
        # address; the name-keyed map cannot tell two overloads apart and made the
        # `int` GetTeamPaddockCenterPoint void, so its body came out with bare
        # `return;` ("Invalid return type", MissionTeamRustling).
        void = (r.get('kind') not in (4, 5, 6)
                and r.get('retkind', self.ret_kind.get(r['name'])) == 4)
        insns = self.decode(r)
        skip = {insns[i][1] for i in arity4.mark_saves(insns)}
        self.vsave = None
        self.spill_alias = {}
        self.spillcnt = {}
        skip |= self.mark_value_save(insns)
        blocks = self.split_blocks(insns, r)
        order = [b[0][1] for b in blocks]
        preds = self.block_edges(blocks, order)
        pred_of = self.pred_lists(blocks, order)
        info, ends = {}, {}
        carry_s = carry_r = None
        clean = [False] * len(blocks)          # starts with an empty argument stack
        empty_out = [False] * len(blocks)
        if blocks:
            clean[0] = True
        for k, blk in enumerate(blocks):
            unk = []
            alias0, cnt0 = dict(self.spill_alias), dict(self.spillcnt)
            st, exit_, out_s, out_r = self.run_block(
                blk, names, skip, carry_s, carry_r,
                void=void, report=unk)
            if self.infer_arity(unk, carry_s, out_s, entry=clean[k]):
                # the retry re-runs the same block, so the spill versions it already
                # handed out have to be rolled back first
                self.spill_alias, self.spillcnt = alias0, cnt0
                st, exit_, out_s, out_r = self.run_block(
                    blk, names, skip, carry_s, carry_r,
                    void=void)
            nxt = order[k + 1] if k + 1 < len(order) else None
            if exit_[0] == 'fall' and nxt is not None and preds[nxt] == 1:
                carry_s, carry_r = out_s, out_r
            else:
                self.flush_regs(st, out_r)
                carry_s = carry_r = None
            info[order[k]] = (st, exit_)
            ends[order[k]] = out_r
            empty_out[k] = not out_s
            # a later block starts empty when every edge into it comes from an
            # already-clean block that ended empty; a back edge (predecessor not yet
            # simulated) makes the answer unknown, so it counts as not clean
            for j in range(k + 1, len(blocks)):
                ps = pred_of[j]
                if ps and all(p < j for p in ps):
                    clean[j] = all(clean[p] and empty_out[p] for p in ps
                                   if p <= k) and all(p <= k for p in ps)
        # `repair_returns` rewrites `jmp epilogue` into `ret <value>`, which removes
        # the edge to the epilogue. Loop recognition reads the block graph, so it has
        # to see the edges the code really has — cfg_exit keeps the originals for it.
        self.cfg_exit = {}
        if not void:                          # void routines return no value
            self.repair_returns(blocks, order, info, ends, preds, self.cfg_exit,
                                jmp_only=getattr(self, 'release_mode', False))
        return order, info, preds, ends

    @staticmethod
    def repair_returns(blocks, order, info, ends, preds, cfg_exit=None, jmp_only=False):
        """Give a shared return block's value back to its predecessors.

        `ABS` compiles to `mov eax,[nVal] / cmp / jge / neg eax / L: ret`. The join
        block L has two predecessors, so it inherits no state and its `eax` is
        unknown — the return value was simply lost (`return;` instead of
        `return -nVal;`). Where such a join carries no statements of its own, letting
        each predecessor return its own value is equivalent and keeps the value.
        """
        succs = collections.defaultdict(list)
        addrs = set(order)
        for k, blk in enumerate(blocks):
            m, a, s, ins = blk[-1]
            here = order[k]
            nxt = order[k + 1] if k + 1 < len(order) else None
            op = ins.op_str if ins is not None else ''
            if m == 'ret':
                continue
            if m.startswith('j') and op.startswith('0x') and int(op, 16) in addrs:
                succs[here].append(int(op, 16))
            if m != 'jmp' and nxt is not None:
                succs[here].append(nxt)
        for join, (st, exit_) in list(info.items()):
            # A single predecessor was excluded before, but the state does not carry
            # over a `jmp` either — only over a fall-through — so `return <expr>;`
            # was lost for every routine that jumps to its epilogue from one place.
            # Level2LevelNum (Common/Levels.ech:16) lifted to an empty body for that
            # reason: with the return gone, inline_temps dropped the assignment that
            # fed it as well.
            if exit_[0] != 'ret' or exit_[1] is not None or preds[join] < 1:
                continue
            # `st` used to have to be empty. A destructor run counts as empty: those
            # statements are the compiler's own AddRef/Release bookkeeping and are
            # dropped again when emitting, so the join still carries no source code.
            # With them counted, every state whose locals are handles kept its
            # `ret None` and lost the delay (TwoWorldsLights state_1).
            if any(not Lifter.is_bookkeeping(s) for s in st):
                continue
            for p, tgts in succs.items():
                if join not in tgts:
                    continue
                v = (ends.get(p) or {}).get('eax')
                if v is not None and v.kind not in ('raw', 'reg') \
                        and not getattr(v, 'assigned', False) \
                        and info[p][1][0] in (('jmp',) if jmp_only else ('fall', 'jmp')):
                    # jmp_only (release build, nothing known): `return <expr>;` always
                    # ends in a `jmp` to the epilogue, even right in front of it
                    # (measured: an int routine ending in `return f();` has the jmp,
                    # the void one ending in `f();` falls through), so a value that
                    # merely falls into the epilogue is what a void routine left in eax
                    if cfg_exit is not None:
                        cfg_exit[p] = info[p][1]
                    # the block already ended, so flush_regs printed this very call as
                    # a void statement; adopting it as the return value would emit it
                    # twice (`GetPlayerHeroUnit(i); return GetPlayerHeroUnit(i);`)
                    st = [s for s in info[p][0]
                          if not (s[0] == 'expr' and s[1] is v)]
                    info[p] = (st, ('ret', v))
                    v.used = True

    def structure_best(self, r, order, info, base=0):
        """Structure a routine's blocks, preferring the dominator-based pass.

        `structure()` below is the original range heuristic. It emits along the code
        layout, which the code generator does not follow for nested conditions —
        it produced `goto` in 41 % of routines and, worse, silently dropped 2957 of
        45366 statements. structure2 walks the graph instead (post-dominators, plus
        short-circuit merging and loops from loops.py) and loses none.
        """
        try:
            import structure2
            return structure2.structure(self, r, order, info, base=base)
        except Exception as ex:
            self.stats[f'structure2-failed-{type(ex).__name__}'] += 1
            out, loops = [], set()
            self.structure(order, info, 0, len(order), base, out, loops)
            return out

    # ------------------------------------------------------------ structuring
    MAXDEPTH = 30

    def structure(self, order, info, lo, hi, depth, out, loops, pos=None,
                  skip_jmp=None):
        """Emit blocks order[lo:hi] as structured code.

        Only jumps that stay inside [lo, hi) are turned into if / if-else; anything
        leaving the range becomes a goto, which keeps the recursion strictly
        shrinking (an earlier version recursed on out-of-range targets and blew up).
        skip_jmp swallows the jump that a then-branch uses to hop over the else part.
        """
        if pos is None:
            pos = {a: i for i, a in enumerate(order)}
        i = lo
        while i < hi:
            addr = order[i]
            st, exit_ = info[addr]
            if addr in loops:
                out.append(('raw', depth, f'L_{addr}:'))
            for s in st:
                out.append(('stmt', depth, s))
            kind = exit_[0]
            if kind == 'jcc':
                _, c, tgt, fall = exit_
                ti = pos.get(tgt)
                inside = ti is not None and i < ti <= hi and depth < self.MAXDEPTH
                if inside:
                    prev_ex = info[order[ti - 1]][1]
                    mi = pos.get(prev_ex[1]) if prev_ex[0] == 'jmp' else None
                    if mi is not None and ti < mi <= hi:          # if / else
                        then_ = []
                        self.structure(order, info, i + 1, ti, depth + 1, then_,
                                       loops, pos,
                                       skip_jmp=order[mi] if mi < len(order) else None)
                        else_ = []
                        self.structure(order, info, ti, mi, depth + 1, else_,
                                       loops, pos)
                        if not any(k == 'stmt' or k == 'raw' for k, _, _ in then_):
                            # empty then-branch: invert and keep only the else body
                            out.append(('open', depth, f'if ({c}) {{'))
                            out += else_
                            out.append(('close', depth, '}'))
                        else:
                            out.append(('open', depth, f'if ({NEG_txt(c)}) {{'))
                            out += then_
                            out.append(('close', depth, '} else {'))
                            out += else_
                            out.append(('close', depth, '}'))
                        i = mi
                        continue
                    out.append(('open', depth, f'if ({NEG_txt(c)}) {{'))   # if only
                    self.structure(order, info, i + 1, ti, depth + 1, out, loops, pos)
                    out.append(('close', depth, '}'))
                    i = ti
                    continue
                if ti is not None and ti <= i:
                    loops.add(tgt)
                    out.append(('raw', depth, f'if ({c}) goto L_{tgt};   // back edge'))
                else:
                    out.append(('raw', depth, f'if ({c}) goto L_{tgt};'))
                i += 1
                continue
            if kind == 'jmp':
                tgt = exit_[1]
                ti = pos.get(tgt)
                if skip_jmp is not None and tgt == skip_jmp:
                    pass                                   # end of a then-branch
                elif ti is not None and ti <= i:
                    loops.add(tgt)
                    out.append(('raw', depth, f'goto L_{tgt};   // back edge'))
                elif ti is not None and i + 1 < ti <= hi:
                    i = ti                                 # forward jump inside range
                    continue
                else:
                    out.append(('raw', depth, f'goto L_{tgt};'))
                i += 1
                continue
            if kind == 'ret':
                if exit_[1] is not None and str(exit_[1]) != '?':
                    out.append(('raw', depth, f'return {exit_[1]};'))
                i += 1
                continue
            i += 1

    # ------------------------------------------------------------ per routine
    def routine_text(self, r):
        order, info, _preds, _ends = self.simulate(r)
        out = self.structure_best(r, order, info, base=1)

        sig = ', '.join(f'{disasm.tname(x)} {x["name"]}' for x in r['params'])
        head = {4: 'event', 5: 'command', 6: 'state', 7: 'function'}.get(r['kind'], '?')
        if r['kind'] == 7 and r['name'] in self.ret_kind:
            head = TYPE.get(self.ret_kind[r['name']], 'function')
        L = [f'{head} {r["name"]}({sig})' + (f'   // index {r["a"]}'
                                            if r['kind'] != 7 else ''), '{']
        for v in r['locals']:
            L.append(f'    {disasm.tname(v)} {v["name"]};')
        if r['locals']:
            L.append('')
        # Source lines come from the statement's own code address, resolved through
        # the line table like a debugger would (largest record offset <= address).
        # Using the hook position instead put every comment one statement too early
        # — caught by comparing against the SDK sources, see validate_src.py.
        offs, at = self.line_map(r)

        def line_of(addr):
            return lookup_line(offs, at, addr)

        cur_line = None
        for kind, depth, payload in out:
            pad = '    ' * depth
            if kind == 'stmt':
                if payload[0] == 'hookmark':
                    continue
                hit = line_of(payload[-1] if isinstance(payload[-1], int) else None)
                if hit is not None and hit != cur_line:
                    fi, line = hit
                    src = self.files[fi] if fi < len(self.files) else f'file{fi}'
                    L.append(f'{pad}// {src}:{line}')
                    cur_line = hit
                if payload[0] == 'assign':
                    L.append(f'{pad}{payload[1]} = {payload[2]};')
                elif payload[0] == 'expr':
                    L.append(f'{pad}{payload[1]};')
                elif payload[0] == 'raw2':
                    L.append(f'{pad}{payload[1]}')
            else:
                L.append(f'{pad}{payload}')
        L.append('}')
        return L

    def text(self):
        nr = self.f['namerec']
        L = [f'// {self.path.name} — lifted from x86 back to EarthC-like source',
             f'// script "{nr.get("name", "?")}" class {nr.get("num", "?")}, '
             + ('debug build (original names and line numbers)'
                if self.b else 'release build (generated names)')]
        if self.b:
            L.append(f'// source {self.b["main"]}')
        L.append('')
        if self.b:
            for g in self.b['globals']:
                L.append(f'{disasm.tname(g)} {g["name"]};   // [esi+{g["addr"]}]')
            L.append('')
        for r in self.routines:
            L += self.routine_text(r)
            L.append('')
        return '\n'.join(L).replace(INTERNAL_MARK, '').replace(NOMETHOD, '')


def NEG_txt(c):
    """negate a comparison expression textually"""
    s = str(c)
    # `!x` negates back to `x` — otherwise the double negation `!(!x)` survives into
    # the source and the compiler emits extra code for it
    if s.startswith('!'):
        inner = s[1:]
        if inner.startswith('(') and inner.endswith(')'):
            inner = inner[1:-1]
        return inner
    for op, inv in NEG.items():
        if f' {op} ' in s:
            return s.replace(f' {op} ', f' {inv} ', 1)
    return f'!({s})'


def run(path, out=None):
    lf = Lifter(path)
    txt = lf.text()
    if out:
        pathlib.Path(out).write_text(txt, encoding='utf8')
    return lf, txt


if __name__ == '__main__':
    if sys.argv[1:2] in (['--all'], ['--debug']):
        only_debug = sys.argv[1] == '--debug'
        outdir = pathlib.Path('../out/dec')
        outdir.mkdir(parents=True, exist_ok=True)
        agg = collections.Counter()
        n = 0
        for p in sorted(pathlib.Path('../eco').rglob('*.eco')):
            rel = p.relative_to('../eco')
            lf = Lifter(p)
            if only_debug and not lf.b:
                continue
            txt = lf.text()
            name = '_'.join([rel.parts[0], rel.parts[-2], p.stem]) + '.ec'
            (outdir / name).write_text(txt, encoding='utf8')
            agg.update(lf.stats)
            n += 1
        tot = agg['call'] + agg['call-unknown-arity'] + agg['call-compiler']
        print(f'{n} files lifted into {outdir}')
        print(f'calls: {agg["call"]} lifted with known arity '
              f'({100 * agg["call"] / max(tot, 1):.1f}%), '
              f'{agg["call-compiler"]} compiler-internal hidden, '
              f'{agg["call-unknown-arity"]} unknown arity, '
              f'{agg["call-short-stack"]} with too few arguments on the stack')
        rest = {k: v for k, v in agg.items() if k.startswith('unhandled')}
        print('unhandled instructions:', rest or 'none')
    else:
        lf, txt = run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
        print(txt if len(sys.argv) <= 2 else
              f'{len(txt.splitlines())} lines written; stats: {dict(lf.stats)}')
