"""Emit real EarthC source (not pseudocode) from a compiled .eco.

Syntax learned from the SDK sources:

    mission "Empty Mission"          // class 2 = mission, 3 = campaign, 4 = global
    {
        state Initialize;            // forward declarations first
        state Nothing;

        int nTimer;                  // globals ($State/$Loop are compiler-internal
                                     // and must NOT be declared)

        function void Name(int i) { ... }
        state Initialize { return Nothing; }
        state Nothing   { return Nothing,30; }   // state change with delay
        command Name(string& s, string a[]) { ... return true; }
    }

Routine kinds work without debug info: a routine whose codeStart equals States[i] /
Commands[i][0] / Events[i] is that state / command / event.
"""
import pathlib, sys, struct, re
import eco, ecodbg, disasm

# Script class id (name record bit 4) -> source keyword. Measured by compiling
# `<kw> "x" { state S; state S { return S; } }` for every keyword with the SDK
# compiler (roundtrip/mytest/c_*.ec):
#     unit      -> no num field at all (id 0), 164 commands /  9 events
#     mission   -> num 2,                       28 commands / 34 events
#     campaign  -> num 3,                       29 commands / 35 events
#     global    -> num 4,                       22 commands / 33 events
# `hero` (num 1) and `RPGCompute` (num 25) are the two further classes the corpus
# uses; both appear verbatim as the class keyword in the SDK sources
# (Units/Hero.ec:103 `hero "hero"`, RPGCompute/RPGCompute.ec:1 `RPGCompute "..."`).
# Getting this wrong is not cosmetic: the class decides which natives are in scope,
# so emitting `global` for Units/UnitBase.ec made every unit method
# (IsPreparingToMove, ...) an "Unknown variable, function or constants" error.
CLASS = {0: 'unit', 1: 'hero', 2: 'mission', 3: 'campaign', 4: 'global',
         25: 'RPGCompute'}
# debug type kind -> EarthC type name (stringW confirmed from Towns.ec)
ECTYPE = {0: 'unit', 1: 'int', 2: 'string', 3: 'stringW', 4: 'void', 6: 'int'}


def array_sizes(f, routines, lf=None):
    """Fixed-size array globals are constructed with their size, e.g.

        mov eax, 0x63 ; push eax ; mov eax, 0 ; push eax
        lea eax, [esi + 0xc] ; push eax ; call <ArrConstructorSize>

    -> {global offset: size}. Declaring `unit a[];` instead of `unit a[99];` omits
    that constructor call, which is exactly what the 6 missing code bytes were.
    """
    import lifter, re
    lf = lf or lifter.Lifter(f['path']) if isinstance(f, dict) and 'path' in f else lf
    out = {}
    for r in routines:
        insns = lf.decode(r)
        for i, (m, a, sz, ins) in enumerate(insns):
            if m != 'lea' or ins is None:
                continue
            mo = re.match(r'eax, \[esi \+ (0x[0-9a-f]+|\d+)\]', ins.op_str)
            if not mo:
                continue
            off = int(mo.group(1), 0)
            consts = []
            for j in range(max(0, i - 6), i):
                m2, _a2, _s2, ins2 = insns[j]
                if m2 != 'mov' or ins2 is None \
                        or not ins2.op_str.startswith('eax, '):
                    continue
                # capstone prints small immediates in decimal, so the old
                # `0x`-prefix test missed every size below 10 — `unit auTmpArray[1]`
                # (PInc/PUnitInfo.ech:8) lifted as `auTmpArray[]` and the size
                # argument of its constructor call went missing.
                v = ins2.op_str.split(', ', 1)[1]
                if re.fullmatch(r'0x[0-9a-f]+|\d+', v):
                    consts.append(int(v, 0))
            sizes = [c for c in consts if c > 0]
            if sizes:
                out.setdefault(off, sizes[0])
    return out


# `$ConstructorSize` of the four array classes (int / string / stringW / unit); the
# class bases are 0x17 apart, see the note at the top of emit_body.
ARR_CTOR_SIZE = {0x15, 0x2c, 0x43, 0x5a}


def local_array_sizes(lf, r):
    """the same for a routine's own arrays: {local slot address: size}

    A local `int anAssort[15]` is built in the prologue exactly like a global one,
    only through the frame pointer:

        mov eax, 0xf ; push eax ; mov eax, 1 ; push eax
        lea eax, [ebp - 0x10] ; push eax ; call <ArrConstructorSize>

    Declared `int anAssort[]` instead, the recompile leaves the size argument out
    and the routine is 6 bytes short (PDialogUnits.FillShop, Common/Shops.ech:74).
    Unlike the global scan this checks the call, because `lea eax, [ebp - N]` is
    also how every string and array operation in the body names its operand.
    """
    import re
    insns = lf.decode(r)
    out = {}
    for i, (m, _a, _sz, ins) in enumerate(insns):
        if m != 'lea' or ins is None:
            continue
        mo = re.match(r'eax, \[ebp - (0x[0-9a-f]+|\d+)\]$', ins.op_str)
        if not mo:
            continue
        call = None
        for j in (i + 1, i + 2):
            if j < len(insns) and insns[j][0] == 'call' and insns[j][3] is not None:
                call = lf.imports.get(insns[j][3].address + 1)
                break
        if call not in ARR_CTOR_SIZE:
            continue
        consts = []
        for j in range(max(0, i - 6), i):
            m2, _a2, _s2, ins2 = insns[j]
            if m2 != 'mov' or ins2 is None or not ins2.op_str.startswith('eax, '):
                continue
            v = ins2.op_str.split(', ', 1)[1]
            if re.fullmatch(r'0x[0-9a-f]+|\d+', v):
                consts.append(int(v, 0))
        sizes = [c for c in consts if c > 0]
        if sizes:
            out.setdefault(int(mo.group(1), 0) - 4, sizes[0])
    return out


def type_of(rec):
    """EarthC type name of a debug record.

    Kind 0 means "handle to a script type" and the record then carries the type name
    verbatim — `mission`, `global`, `object`, ... Mapping kind 0 to `unit` (what
    ECTYPE does) turned `function void ShowLight(mission pMission, unit uHero)` into
    two `unit` parameters, which changes the natives that resolve on the argument.
    """
    if rec['kind'] == 0 and isinstance(rec.get('type'), str) and rec['type']:
        return rec['type']
    return ECTYPE.get(rec['kind'], 'int')


HANDLE_TYPES_NOT = {'int', 'string', 'stringW', 'void'}

# A call to a routine the debug build did not name. `sub_14941()` is no identifier
# the compiler knows, so an entry point (state / command / event) whose body holds
# one cannot be emitted as it is and has to fall back to its stub. Functions keep
# their body — the name is all that is missing there, and a wrong body would be
# worse. PDialogUnits' `event StartTalkWithDialogUnit` took the whole file down
# without this ("Unknown variable, function", PDialogUnits.ec:191).
UNNAMED_ROUTINE = re.compile(r'\bsub_[0-9a-f]+\(')


def unnamed_calls(lines, known=()):
    """`known` are the sub_<addr> names emit() synthesises a definition for; a call
    to one of those resolves, so it must not degrade the entry point to a stub."""
    out = []
    for l in lines:
        for mo in UNNAMED_ROUTINE.finditer(l):
            if mo.group(0)[:-1] not in known:
                out.append(f'unnamed internal routine: {l.strip()[:60]}')
                break
    return out


def decl_of(g, sizes=None):
    """one global declaration; for arrays the element type carries the name"""
    if g['kind'] == 6:
        elem = (g['type'] if isinstance(g['type'], str)
                else ECTYPE.get(g['sub'], 'int'))
        n = (sizes or {}).get(g['addr'])
        return f'{elem} {g["name"]}[{n if n else ""}];'
    return f'{type_of(g)} {g["name"]};'


# Globals the SDK declares behind `#ifdef _DEBUG`, so the release build has no slot
# for them. `m_bDisabledTrace` is Common/Debug.ech:9; `startLevel` is an enum in
# TwoWorldsCampaign.ec:31 and sits in the MIDDLE of that script's own declarations
# (slot 28 of 13 debug globals against 9 release slots), which is why the count has
# to be repaired by name and not by dropping a leading run.
DEBUG_GLOBALS = ('m_bDisabledTrace', 'startLevel')


def global_decl_records(f, b, dbg=None):
    """the global records that belong to this build, re-addressed to its slots"""
    n = max(0, (f['mem_reserve'] - 8) // 4)
    src = b or dbg
    if not src:
        return []
    own = [g for g in src['globals'] if not g['name'].startswith('$')]
    if b is None and len(own) != n:
        # The debug build declares one global the release build does not, and it is
        # not always the first one: RPGCompute pulls Debug.ech in the middle of its
        # own declarations, so m_bDisabledTrace sits at slot 18 of 38. Dropping the
        # leading surplus instead shifted every name by one array slot and
        # `m_arrAddSkillsInEquipment[...]` came out as `m_arrAddPointFormat[...]`.
        named = [g for g in own if g['name'] not in DEBUG_GLOBALS]
        if len(named) == n:
            own = named
    if len(own) < n:
        return []
    keep = []
    for k, g in enumerate(own[len(own) - n:]):
        g = dict(g)
        g['addr'] = 8 + 4 * k
        keep.append(g)
    return keep


def _cstring(data, off):
    end = data.find(b'\x00', off)
    return data[off:end if end >= 0 else len(data)].decode('latin1')


def button_enums(f):
    """`command X(...) button <enumvar>` and the `enum` block behind it.

    Measured with the SDK compiler (roundtrip/mytest6 r6.ec with the clause, r7/r8
    without): the second word of a command's header record is the offset of a
    descriptor in the data segment,

        [u32 labels] [u32 global address of the enum variable] [u32 0x80] [0] [0]

    and `labels` is zero unless the command was declared with a `button`. The label
    texts sit immediately in front of that descriptor, one entry each:

        [u32 0xffffffff] [caption\\0] [tooltip\\0]

    `labels` counts only the captions before `multi:`; the rest of the entries
    belong behind it. Declaring the enum alone changes nothing — r8.eco is
    byte-identical to r7.eco — so it is the clause that has to be recovered, and
    without it JSTestCampaign was 63 data bytes short (its code segment already
    matched).

    -> ({command index: global address}, {global address: (captions, multi)})
    """
    data = bytes(f['data_seg'])
    ptrs = sorted(set(f['data_ptrs']))
    bycmd, byglobal = {}, {}
    for i, c in enumerate(f['commands']):
        off = c[1]
        if not off or off + 20 > len(data):
            continue
        n, gaddr = struct.unpack_from('<II', data, off)
        if n == 0 or not gaddr:
            continue
        # walk the entries backwards: two strings per entry, and the four bytes in
        # front of the first one have to be the 0xffffffff marker
        strs = [p for p in ptrs if p < off]
        entries, pos = [], off
        while len(strs) >= 2:
            s1, s2 = strs[-2], strs[-1]
            if s2 + len(_cstring(data, s2)) + 1 != pos or s1 + len(
                    _cstring(data, s1)) + 1 != s2 or s1 < 4 or \
                    data[s1 - 4:s1] != b'\xff\xff\xff\xff':
                break
            entries.append(_cstring(data, s1))
            pos = s1 - 4
            del strs[-2:]
        entries.reverse()
        if len(entries) <= n:
            continue
        bycmd[i] = gaddr
        byglobal[gaddr] = (entries[:n], entries[n:])
    return bycmd, byglobal


CMD_PRIORITY_DEFAULT = 128
CMD_HIDDEN = 2


def command_modifiers(f):
    """`priority <n>` and `hidden` per command index.

    Same descriptor as button_enums reads, five words at `commands[i][1]`:

        [labels] [enum global] [priority] [flags] [first data pointer]

    and behind that three data pointers, of which the FIRST addresses a word holding
    the `item` id (0xffffffff when the command has none).

    Measured with the SDK compiler (roundtrip/mytest6/rc.ec and rd.ec): a plain
    command has priority 128, flags 0 and item -1; `hidden` sets flags to 2,
    `priority 5` puts 5 in the third word, and `item 141` puts 141 where the first
    pointer aims. Over ../roundtrip/ref the only flag value that ever occurs is 2,
    and 46 of 374 commands carry a priority other than 128 — none of which was
    emitted, so `command Stop() hidden` (Units/Move.ech:229) and
    `command MoveOneStepBack(int) item ITEM_MOVEONESTEPBACK hidden` (Move.ech:402)
    came out bare and the data segment differed.

    -> {command index: (item, priority, flags)}
    """
    data = bytes(f['data_seg'])
    out = {}
    for i, c in enumerate(f['commands']):
        off = c[1]
        if not c[0] or not off or off + 20 > len(data):
            continue
        _n, _g, prio, flags, p1 = struct.unpack_from('<5I', data, off)
        item = None
        if p1 + 4 <= len(data):
            v = struct.unpack_from('<I', data, p1)[0]
            if v != 0xFFFFFFFF:
                item = v
        out[i] = (item, prio, flags)
    return out


def enum_decl(g, captions, multi):
    out = [f'enum {g["name"]}', '{']
    out += [f'    "{c}",' for c in captions]
    if multi:
        out.append('multi:')
        out += [f'    "{c}",' for c in multi]
        out[-1] = out[-1].rstrip(',')
    out.append('}')
    return out


def global_decls(f, b, dbg=None, sizes=None):
    """Global declarations.

    Slot count comes from Memory Reservation Size ($State and $Loop take the first
    two slots, so (mem - 8) / 4 are the script's own). Types come from a debug build
    when one is supplied — its debug-only globals sit at the front, because the debug
    includes are processed first, so the trailing n entries are the release set.
    """
    keep = global_decl_records(f, b, dbg)
    if keep:
        enums = button_enums(f)[1]
        out = []
        for g in keep:
            if g['addr'] in enums and g['kind'] == 1:
                out += enum_decl(g, *enums[g['addr']])
            else:
                out.append(decl_of(g, sizes))
        return out
    return [f'int g{i};' for i in range(max(0, (f['mem_reserve'] - 8) // 4))]


def param_decl(p):
    """`string& s`, `string a[]`, `int n` — flags bit 0 marks a reference
    parameter (confirmed: strMapTexture has flags=1 and is `string&` in Towns.ec)."""
    if p['kind'] == 6:
        elem = (p['type'] if isinstance(p['type'], str)
                else ECTYPE.get(p['sub'], 'int'))
        return f'{elem} {p["name"]}[]'
    ref = '&' if p.get('flags', 0) & 1 else ''
    return f'{type_of(p)}{ref} {p["name"]}'


def debug_routine(dbg, kind, idx):
    """the debug build's routine for a given command/event/state index"""
    if not dbg:
        return None
    for r in dbg['routines']:
        if r['kind'] == kind and r['a'] == idx:
            return r
    return None


def classify(f, routines):
    """-> {start: ('state'|'command'|'event', index)} from the header arrays"""
    tag = {}
    starts = {r['start'] for r in routines}
    for i, off in enumerate(f['states']):
        if off and off in starts:
            tag[off] = ('state', i)
    # A zero entry usually means "state declared but never defined". Offset 0 is
    # also a real code address though, and it is not always state 0 that lives
    # there: MainMenuMission declares `state Idle;` first, so Idle is index 0 at
    # offset 163 and `state Initialize` is index 1 at offset 0. Assuming index 0
    # dropped Initialize entirely (163 of 428 code bytes). Only resolvable while
    # exactly one entry is zero; states end in `ret 4`, which keeps a plain
    # function at offset 0 (Unit/UnitBase/Hero) out of it.
    zeros = [i for i, off in enumerate(f['states']) if off == 0]
    if len(zeros) == 1 and 0 in starts and 0 not in tag:
        r0 = next(r for r in routines if r['start'] == 0)
        end = r0['end']
        code = f['code']
        if end >= 2 and code[end - 2] == 0xC2 and \
                struct.unpack_from('<H', bytes(code), end - 1)[0] == 4:
            tag[0] = ('state', zeros[0])
    for i, c in enumerate(f['commands']):
        if c[0]:
            tag.setdefault(c[0], ('command', i))
    for i, off in enumerate(f['events']):
        if off:
            tag.setdefault(off, ('event', i))
    return tag


def locals_for(lf, r, d):
    """the debug record's locals if they describe THIS routine's frame, else None.

    Same frame size means same locals. A debug build can carry one compiler
    temporary more than the release build, because the extra `TRACE(...)` call
    needs a spill slot the release build has no use for: TwoWorldsTeleports'
    CommandDebug has six debug locals (nX, nY, nZ, uHero, pMission, $5) against
    five release slots. Dropping the surplus `$` temporaries — they are never
    declared in the output anyway — makes the two line up; without it the routine
    got no declarations at all and the body referred to undeclared loc1..loc4
    ("Unknown variable", the whole file failed to recompile).
    """
    if not d or not d.get('locals'):
        return None
    n = frame_slots(lf, r)
    loc = list(d['locals'])
    if len(loc) == n:
        return loc
    for i in range(len(loc) - 1, -1, -1):
        if len(loc) <= n:
            break
        if loc[i]['name'].startswith('$'):
            del loc[i]
    return loc if len(loc) == n else None


def frame_slots(lf, r):
    """local slots the routine reserves — `sub esp, N` in the prologue, N/4"""
    for m, a, s, ins in lf.decode(r)[:4]:
        if m == 'sub' and ins is not None and ins.op_str.startswith('esp, '):
            return int(ins.op_str.split(', ')[1], 0) // 4
    return 0


def emit(path, dbg_path=None):
    f, b = ecodbg.parse_file(path)
    dbg = ecodbg.parse_file(dbg_path)[1] if dbg_path else None
    code = bytes(f['code'])
    routines = b['routines'] if b else disasm.scan_routines(code)
    tag = classify(f, routines)
    nr = f['namerec']
    # a missing num field means id 0 (`unit`) — the compiler omits the field when
    # the class id is zero, which is why UnitBase/Unit carry no num
    cls = CLASS.get(nr.get('num', 0), 'global')
    name = nr.get('name', 'Unnamed')

    states, commands, events = [], [], []
    for r in routines:
        kind = (('state', r['a']) if b and r.get('kind') == 6 else
                None if b and r.get('kind') in (4, 5, 7) else
                tag.get(r['start']))
        if not kind:
            continue
        if kind[0] == 'state':
            states.append((kind[1], r))
        elif kind[0] == 'command' and kind[1] > 2:
            # 0..2 are the implicit Initialize/Nothing stubs the compiler emits
            commands.append((kind[1], r))
        elif kind[0] == 'event':
            # `event RemovedUnit(unit uUnit, ...) { … return false; }` — an entry
            # point like a command, and leaving it out is not just a missing
            # routine: EarthC drops functions nothing reaches from the release
            # build, so every helper only an event called went with it.
            # TwoWorldsContainers has four events and recompiled to 9 routines of
            # 984 code bytes against the reference's 44 and 18353.
            events.append((kind[1], r))

    L = [f'{cls} "{name}"', '{']
    for idx, r in sorted(states):
        L.append(f'    state {r["name"] if b else f"state_{idx}"};')
    L.append('')
    import lifter
    lf = lifter.Lifter(path)
    decls = global_decls(f, b, dbg, array_sizes(f, routines, lf))
    for decl in decls:
        L.append(f'    {decl}')
    if decls:
        L.append('')
    byidx = {i: (r['name'] if b else f'state_{i}') for i, r in states}

    # Functions, states and commands are all emitted in ONE pass over the code
    # segment, interleaved exactly as the compiler laid them out — that is the
    # order the source had them in, and it is the only thing that keeps the code
    # segment lined up. Emitting every function first and the states behind them
    # only works while the source #includes all its functions before the first
    # state: TwoWorldsMusic defines AddEnemyMarker and CreateEnemy *after* the
    # three states, and its commands are not in index order either (CommandDebug
    # sits before the two Message commands), so everything behind the first state
    # was shifted (8 routines out of place, 45 in the file).
    matched = {}
    if dbg_path:
        import match_routines, emit_body, struct
        synth = {}
        synth_names = set()
        _f, _rs, matched, _c = match_routines.match(str(path), str(dbg_path))
        sigs = {d['name']: d['params'] for d in matched.values()}
        dbg_lf = lifter.Lifter(dbg_path)
        # Wire the debug symbols into the release lifter. Without this the lift runs
        # blind on the release build: internal calls print as `sub_0()` with no
        # arguments (the arity comes from the callee's parameter list, which is empty
        # without symbols), globals print as `global_116`, and object types collapse
        # to `unit`. The information was already there — it just was not connected.
        for start, d in matched.items():
            if start in lf.by_start:
                lf.by_start[start] = {**lf.by_start[start], 'name': d['name'],
                                      'params': d['params'], 'kind': d['kind']}
        # A release routine the matcher could not name is still real code, and
        # skipping it is expensive: every call to it lifted as the undeclared
        # `sub_<addr>()`, which made emit_ec throw away the WHOLE entry point that
        # contained the call, and EarthC then dropped everything only that entry
        # point reached. TwoWorldsCampaign lost 14 of 43 routines to one such call,
        # PQuests 21 of 384. The signature can be synthesised: `ret n` gives the
        # parameter count, `sub esp, n` the frame, and the name is the one the
        # lifter already prints at the call sites.
        for r in routines:
            if r['start'] in matched or r['start'] in tag:
                continue
            n = match_routines.own_arity(code, r)
            synth[r['start']] = {
                'name': f'sub_{r["start"]}', 'kind': 7, 'a': 0,
                'start': r['start'], 'end': r['end'], 'lines': [], 'refs': [],
                'params': [{'name': f'a{k + 1}', 'kind': 1, 'sub': 0,
                            'tidx': 0xFFFFFFFF, 'type': 0, 'flags': 0,
                            'addr': 4 * k} for k in range(n)],
                'locals': [{'name': f'loc{k + 1}', 'kind': 1, 'sub': 0,
                            'tidx': 0xFFFFFFFF, 'type': 0, 'flags': 0,
                            'addr': 4 * k} for k in range(frame_slots(lf, r))]}
            lf.by_start[r['start']] = {**lf.by_start[r['start']],
                                       'name': synth[r['start']]['name'],
                                       'params': synth[r['start']]['params'],
                                       'kind': 7}
            synth_names.add(synth[r['start']]['name'])
        # States and commands have locals too. Without the debug record the release
        # routine carries none, so the frame slot printed as an undeclared `loc1`
        # and the compiler rejected the file (TwoWorldsTeleports state_0). Same
        # frame-size guard as the functions below: a mismatch means the debug build
        # is a different program here and its names must not be trusted.
        for grp in (states, commands, events):
            for k, (idx, rr_) in enumerate(grp):
                d = matched.get(rr_['start'])
                loc = locals_for(lf, rr_, d)
                if loc:
                    grp[k] = (idx, {**rr_, 'locals': loc})
        keep = global_decl_records(f, b, dbg)
        if keep:
            lf.globals = {8 + 4 * k: g for k, g in enumerate(keep)}
        # return types too: without them every routine looks non-void, and the
        # return-value repair then writes `return a.Add(x);` into a void function
        lf.ret_kind = dict(dbg_lf.ret_kind)

    def function_lines(r, d, ret=None):
        import emit_body
        rr = dict(r)
        rr['params'] = d['params']           # names and types from the debug build
        rr['name'] = d['name']               # ret_kind is keyed by name
        loc = locals_for(lf, r, d)
        if loc is not None:
            rr['locals'] = loc               # same frame size -> same locals
        # Per-callee first, name second: two overloads can differ in return type
        # (MissionTeamRustling.ec:87/88 — `int GetTeamPaddockCenterPoint(int, …)`
        # and `void GetTeamPaddockCenterPoint(int[], …)`), and the name-keyed vote
        # gives whichever won overall.
        rr['retkind'] = getattr(dbg_lf, 'ret_kind_at', {}).get(
            d['start'], dbg_lf.ret_kind.get(d['name']))
        def lift(rec):
            ls, pr = emit_body.body(lf, rec)
            ls = emit_body.strip_prologue_inits(
                ls, rec.get('locals', []), emit_body.prologue_zeroes(lf, rec))
            return emit_body.for_headers(
                emit_body.inline_temps(emit_body.compound_assign(ls))), pr

        lines, problems = lift(rr)
        # The body has to be lifted before the header can be written for a routine
        # the matcher could not name: nothing records its types, so the return type
        # is read off the `return` the body ends with and a parameter is a handle
        # wherever the body calls a method on it. With the types known the body is
        # lifted a second time, so `null` and the handle natives come out right.
        if ret == '?':
            hand = synth_params(lines, rr['params'], sigs)
            if hand:
                rr = dict(rr)
                rr['params'] = hand
                lines, problems = lift(rr)
            ret = synth_ret(lines, dbg_lf)
            if ret == 'void':
                # a void routine may not `return <expr>;` — the call becomes a
                # statement of its own
                lines = [_strip_return(l)
                         if _RET_EXPR.match(l) else l
                         for l in lines]
        ret = ret or (getattr(dbg_lf, 'ret_type_at', {}).get(d['start'])
                      or ECTYPE.get(getattr(dbg_lf, 'ret_kind_at', {}).get(d['start']))
                      or dbg_lf.ret_type.get(d['name'])
                      or ECTYPE.get(dbg_lf.ret_kind.get(d['name']), 'void'))
        sig = ', '.join(param_decl(p) for p in rr['params'])
        proto = f'    function {ret} {d["name"]}({sig});'
        out = [f'    function {ret} {d["name"]}({sig})', '    {']
        lsz = local_array_sizes(lf, r)
        for v in rr.get('locals', []):
            if not v['name'].startswith('$'):    # temps: compiler-generated
                out.append(f'        {decl_of(v, lsz)}')
        lines = null_returns(lines, ret)
        # a plain function may switch state too (`state Moving;`)
        lines, more = rewrite_state_returns(lines, byidx, in_state=False)
        problems = problems + more
        out += [f'        {ln}' for ln in lines]
        if problems:
            out.append(f'        // {len(problems)} unresolved: '
                       f'{problems[0][:70]}')
        return proto, out + ['    }', '']

    def command_lines(idx, r, kw='command', dbgkind=5):
        d = debug_routine(dbg, dbgkind, idx) or (r if b else None)
        if d is None:
            return [f'    // {kw} {idx}: no signature available, skipped']
        sig = ', '.join(param_decl(p) for p in d['params'])
        # `button <enumvar>` is part of the declaration and it is what makes the
        # compiler write the enum's captions into the data segment (button_enums)
        btn = ''
        if kw == 'command':
            item, prio, flags = command_modifiers(f).get(
                idx, (None, CMD_PRIORITY_DEFAULT, 0))
            # source order, as the SDK writes it: item, priority, hidden, button
            if item is not None:
                btn += f' item {item}'
            if prio != CMD_PRIORITY_DEFAULT:
                btn += f' priority {prio}'
            if flags & CMD_HIDDEN:
                btn += ' hidden'
            gaddr = button_enums(f)[0].get(idx)
            if gaddr is not None:
                nm = next((g['name'] for g in global_decl_records(f, b, dbg)
                           if g['addr'] == gaddr), None)
                if nm:
                    btn += f' button {nm}'
        out = [f'    {kw} {d["name"]}({sig}){btn}', '    {']
        out += [f'        {line}' for line in
                command_body(lf, r, d, byidx, synth_names)]
        return out + ['    }', '']

    # A function's code slot is fixed by its DECLARATION, not by its definition:
    # `function void f2(); ... state s0 {...} ... function void f2() {...}` puts f2
    # in front of the state (roundtrip/mytest5/e9.ec vs e10.ec — with the forward
    # declaration f2 is at offset 0, without it behind the state). That is what
    # makes the code order reproducible: walk the code segment once, and at each
    # routine's position emit either the definition (states and commands, which
    # cannot be forward-declared) or the function's prototype. All function bodies
    # then follow at the end, where every name is already known, so no call can be
    # a forward reference. Dropping the prototypes altogether and defining the
    # functions in place was tried and is worse — mutually recursive functions and
    # functions called from a routine that is emitted earlier stop resolving, and
    # 7 more files failed to recompile (19 of 36 down from 26).
    state_at = {r['start']: (idx, r) for idx, r in states}
    cmd_at = {r['start']: (idx, r) for idx, r in commands}
    evt_at = {r['start']: (idx, r) for idx, r in events}
    bodies = []
    for r in routines:
        start = r['start']
        if start in state_at:
            idx, rr = state_at[start]
            L += state_lines(lf, [(idx, rr)], byidx, path, synth_names)
        elif start in cmd_at:
            L += command_lines(*cmd_at[start])
        elif start in evt_at:
            L += command_lines(*evt_at[start], kw='event', dbgkind=4)
        elif dbg_path and matched.get(start, {}).get('kind') == 7:
            proto, body = function_lines(r, matched[start])
            L.append(proto)
            bodies += body
        elif dbg_path and start in synth:
            proto, body = function_lines(r, synth[start], ret='?')
            L.append(proto)
            bodies += body
    if bodies:
        L.append('')
        L += bodies
    L.append('}')
    return '\n'.join(L) + '\n'


_RET_EXPR = re.compile(r'^\s*return (.+);$')
_CALL_HEAD = re.compile(r'^([A-Za-z_]\w*)\(')


def _strip_return(line):
    """`return f(x);` -> `f(x);` — a void routine may not return a value"""
    mo = _RET_EXPR.match(line)
    if not mo:
        return line
    pad = line[:len(line) - len(line.lstrip())]
    return pad + mo.group(1) + ';'


def synth_params(lines, params, sigs):
    """parameter list with the handle parameters re-typed, or None if unchanged.

    A synthesised signature starts out all-`int`, and that does not compile as soon
    as a caller passes a unit: `sub_13109(GetHero(0), i)` was a "Cannot find suitable
    function" in PQuests / PQuestsMulti / PQuestsMulti16. The body says which is
    which — a method call or a `null` comparison on a parameter can only be a handle.
    """
    import emit_body
    out, changed = [], False
    for p in params:
        nm = p['name']
        t = None
        if any(re.search(rf'{nm}\.\w', l)
               or re.search(rf'{nm} [!=]= null', l) for l in lines):
            t = 'unit'
        else:
            t = _passed_as(lines, nm, sigs)
        if t and p['kind'] != 0:
            p = {**p, 'kind': 0, 'type': t}
            changed = True
        out.append(p)
    return out if changed else None


_ANY_CALL = re.compile(r'(?<![.\w])([A-Za-z_]\w*)\(')


def _passed_as(lines, nm, sigs):
    """handle type of the parameter `nm`, taken from a call it is handed to.

    `sub_13109` of PQuests is nothing but `return LockSkill(a1, a2, 0);`, so its own
    body never treats a1 as an object — but LockSkill's signature does, and that is
    a name we already know. Without this the synthesised `int a1` was a "Cannot find
    suitable function" at the call site, which passes a unit.
    """
    import emit_body
    for l in lines:
        for mo in _ANY_CALL.finditer(l):
            sig = sigs.get(mo.group(1))
            if not sig:
                continue
            end = emit_body._match_paren(l, mo.end())
            if end is None:
                continue
            args = emit_body._split_args(l[mo.end():end - 1])
            for k, a in enumerate(args):
                if a.strip() == nm and k < len(sig) and sig[k]['kind'] == 0:
                    return type_of(sig[k])
    return None


def synth_ret(lines, dbg_lf):
    """return type of a routine the matcher could not name.

    Nothing records it, so it is read off the body: no `return <expr>;` at all means
    `void`, `return null;` means a handle, and a returned call takes the type of its
    callee. Guessing `int` throughout does not work — `sub_1096` of
    TwoWorldsCampaign is `return GetHero(0);` and GetHero yields a unit, which
    EarthC rejects as an "Invalid return type".
    """
    exprs = [mo.group(1).strip() for mo in
             (_RET_EXPR.match(l) for l in lines) if mo]
    if not exprs:
        return 'void'
    for e in exprs:
        if e == 'null':
            return 'unit'
        mo = _CALL_HEAD.match(e)
        if mo:
            t = dbg_lf.ret_type.get(mo.group(1))
            if t:
                return t
            k = dbg_lf.ret_kind.get(mo.group(1))
            if k is not None:
                # `return LockSkill(a1, a2, 0);` where LockSkill is void is an
                # "Invalid return type" (PQuests sub_13109); the kind map knows it
                return ECTYPE.get(k, 'int')
    return 'int'


def null_returns(lines, ret):
    """`return 0;` is not valid for a handle-returning function — EarthC spells the
    null handle `null` and rejects the integer ("Invalid return type", e.g.
    Quest.ech's `function unit GetHeroMulti() { ... return null; }`)."""
    if ret in HANDLE_TYPES_NOT:
        return lines
    return [re.sub(r'^(\s*)return 0;$', r'\1return null;', l) for l in lines]


# $State lives in global slot 0 ([esi]). emit() re-addresses the script's own
# globals to start at 8, so slot 0 keeps the anonymous name the lifter gives it.
STATE_VAR = ('global_0', '$State')
_STATE_SET = re.compile(r'^(\s*)(' + '|'.join(re.escape(v) for v in STATE_VAR)
                        + r') = (.+);$')
# The delay is not always a literal: `return Nothing, eLightsCheckDelay;` with
# eLightsCheckDelay = 30*5 compiles to `mov eax,30 ; mov edx,5 ; imul edx` (the
# compiler folds no constants), so the lifted line reads `return 30 * 5;`. Requiring
# a literal here left the transition as a bare `state state_1;`, which EarthC
# rejects inside a state body ("Illegal 'state' set in state body").
_RETURN_NUM = re.compile(r'^\s*return (.+);$')
# reads of $State: `if (state == MovingStep)` (Units/Move.ech:391). The keyword
# reads global slot 0, so the lift had `global_0 == 4` — no such variable exists
# at source level, and Unit/UnitBase/Hero all failed on it.
_STATE_CMP = re.compile(r'(?<![\w$])(' + '|'.join(re.escape(v) for v in STATE_VAR)
                        + r')\s*(==|!=)\s*(\d+)(?![\w.])')


def rewrite_state_reads(lines, byidx):
    """`global_0 == 4` -> `state == <state 4>`; unknown index is reported"""
    problems = []

    def sub(mo):
        nm = byidx.get(int(mo.group(3)))
        if nm is None:
            problems.append(f'unresolved state comparison: {mo.group(0)}')
            return mo.group(0)
        return f'state {mo.group(2)} {nm}'

    return [_STATE_CMP.sub(sub, s) for s in lines], problems


def rewrite_state_returns(lines, byidx, in_state=True):
    """Undo the two source forms that write $State.

      `$State = k; return d;`   is  `return <state>, d;`   (state routines only)
      `$State = k;`             is  `state <state>;`       (anywhere, e.g.
                                    Units/Move.ech MakeCommandMove)

    Only a state routine can fold the following return into the transition. In a
    plain function `state Moving; return true;` produces the same two instructions,
    and reading them as one statement gave `return state_8,1;` in Hero.ec.

    There is no source-level `$State` variable, so leaving the assignment in place
    never compiles. `$State = $State` is the `state` keyword ("stay here"), and a
    delay of eDefaultStateDelay is what `return <state>;` without a delay produces.

    Returns (lines, problems); a target that cannot be resolved is reported instead
    of guessed at.
    """
    lines, problems0 = rewrite_state_reads(lines, byidx)
    out, problems, i = [], list(problems0), 0
    while i < len(lines):
        mo = _STATE_SET.match(lines[i])
        if not mo:
            out.append(lines[i])
            i += 1
            continue
        pad, val = mo.group(1), mo.group(3).strip()
        nxt = (_RETURN_NUM.match(lines[i + 1])
               if in_state and i + 1 < len(lines) else None)
        if val in STATE_VAR:
            target = 'state'
        elif val.isdigit():
            target = byidx.get(int(val))
        else:
            target = None
        if target is None or (nxt is None and target == 'state'):
            problems.append(f'unresolved state assignment: {lines[i].strip()}')
            out.append(lines[i])
            i += 1
            continue
        if nxt is None:
            out.append(f'{pad}state {target};')
            i += 1
            continue
        delay = nxt.group(1).strip()
        tail = '' if delay == str(DEFAULT_STATE_DELAY) else f',{delay}'
        out.append(f'{pad}return {target}{tail};')
        i += 2
    return out, problems


_LOOP_HEAD = re.compile(r'^\s*(while|for|do|switch)\b')


def _tail_lines(lines):
    """indices of statements after which control leaves the routine body

    Bracket-matched, so an arm of an if/else that is itself the last thing in the
    body counts, but a loop body never does (its end jumps back to the header).
    """
    tails = set()

    def mark(i, stop, tail):
        """last statement of the block starting at `i`, recursively"""
        last = None
        while i < stop:
            s = lines[i].strip()
            if s.startswith('}') and not s.endswith('{'):
                break
            if s.endswith('{'):
                head, j = i, i
                spans = []
                while True:
                    j = _match(lines, j, stop)
                    spans.append((head, j))
                    if j < stop and lines[j].strip().startswith('} else'):
                        head = j
                        continue
                    break
                last = ('b', spans, i)
                i = j + 1
            else:
                last = ('s', i)
                i += 1
        if last is None or not tail:
            return
        if last[0] == 's':
            tails.add(last[1])
            return
        if _LOOP_HEAD.match(lines[last[2]]):
            return
        for head, end in last[1]:
            mark(head + 1, end, True)

    mark(0, len(lines), True)
    return tails


def _match(lines, open_at, stop):
    """index of the line closing the block opened on line `open_at`

    `open_at` may be a `} else {` line, which both closes and opens. As the line
    the search STARTS on it only opens — counting its brace as a close made the
    depth run one too low, so the scan never saw its own `}` and returned `stop`.
    The else arm then swallowed everything behind the `if`, and `_tail_lines` no
    longer recognised the last statement of the body as a tail. Every state whose
    body ends in an if/else therefore looked like it fell through outside tail
    position and was replaced by a bare-transition stub: six of UnitBase's nine
    states, and with them the only caller of IsStartingMoving, which EarthC then
    dropped as unreachable (57 routines against 56).
    """
    depth, i = 0, open_at
    while i < stop:
        s = lines[i].strip()
        if s.startswith('}') and i != open_at:
            depth -= 1
            if depth == 0:
                return i
        if s.endswith('{'):
            depth += 1
        i += 1
    return stop


def drop_state_fallthrough(lines):
    """A state that runs off the end of its body compiles to `mov eax,20 ; ret`.

    eDefaultStateDelay in eax with $State untouched is the *only* thing the
    compiler emits there — `return;` inside a state is rejected outright
    ("Invalid return type", verified on ../roundtrip/mytest3/s1.ec), and every
    written `return <state>, d;` sets $State first. So a bare `return 20;` in a
    state body is the implicit end of the body and has no source text at all.

    Dropping it is only sound where control really does leave the body there.
    The structurer copies the shared epilogue into if/else arms (Structurer
    .tail_return), which is where UnitBase's state Nothing got its two, and both
    sit in tail position. Anything else — a hoisted guard clause, say — is
    reported so the caller can fall back to a stub rather than emit code that
    does not compile.
    """
    lit = f'return {DEFAULT_STATE_DELAY};'
    hits = [i for i, s in enumerate(lines) if s.strip() == lit]
    if not hits:
        return lines, []
    tails = _tail_lines(lines)
    bad = [i for i in hits if i not in tails]
    if bad:
        return lines, [f'state falls through outside tail position: line {bad[0]}']
    return [s for i, s in enumerate(lines) if i not in set(hits)], []


def state_lines(lf, states, byidx, path, known=()):
    """Full state bodies, lifted like any other routine.

    Emitting only the trailing `return <state>;` (what this did before) is wrong for
    every state that actually does something: MainMenuCampaign_1's Initialize state
    is 166 bytes of code and used to come out as a 68-byte stub.
    """
    import emit_body
    L = []
    # code order, not index order: the index follows the *declaration* order and the
    # two part company as soon as a state is forward-declared. MainMenuMission
    # declares `state Idle;` (index 0) but defines `state Initialize` (index 1)
    # first, so sorting by index put the routines in the code segment backwards.
    for idx, r in sorted(states, key=lambda t: t[1]['start']):
        nm = byidx[idx]
        L.append(f'    state {nm}')
        L.append('    {')
        locs = [v for v in r.get('locals', []) if not v['name'].startswith('$')]
        lsz = local_array_sizes(lf, r)
        for v in locs:
            L.append(f'        {decl_of(v, lsz)}')
        try:
            lines, problems = emit_body.body(lf, r)
            lines = emit_body.strip_prologue_inits(
                lines, r.get('locals', []), emit_body.prologue_zeroes(lf, r))
            lines = emit_body.for_headers(
            emit_body.inline_temps(emit_body.compound_assign(lines)))
            lines, more = rewrite_state_returns(lines, byidx, nm)
            problems = problems + more
            lines, more = drop_state_fallthrough(lines)
            problems = problems + more + unnamed_calls(lines, known)
        except Exception as ex:
            lines, problems = [], [f'{type(ex).__name__}: {ex}']
        if problems:
            # a body we cannot lift faithfully would not compile; fall back to the
            # bare transition, which at least keeps the file building
            tgt, delay = state_transition(path, r, lf)
            target = byidx.get(tgt, nm)
            lines = [f'return {target}{"," + str(delay) if delay else ""};']
            L.append(f'        // {len(problems)} unresolved: {problems[0][:70]}')
        for ln in lines:
            L.append(f'        {ln}')
        L.append('    }')
        L.append('')
    return L


def command_body(lf, r, d, byidx=None, known=()):
    """The command's real body, lifted like any other routine.

    The old version only knew the Towns.ec shape (forward to a routine of the same
    name) and printed that for every command. PNames' `command Message` actually has
    a body, and the stub `Message(nParam, uUnit);` names a function that does not
    exist — three files failed to recompile on it. The forwarding stub stays as the
    fallback for bodies that cannot be lifted cleanly.
    """
    import emit_body
    rr = dict(r)
    rr['params'], rr['name'] = d['params'], d['name']
    # the kind matters: Lifter.simulate must not read this as a void function just
    # because a function of the same name is void (see the `void` guard there)
    rr['kind'] = d['kind']
    loc = locals_for(lf, r, d)
    if loc is not None:
        rr['locals'] = loc
    try:
        lines, problems = emit_body.body(lf, rr)
        lines = emit_body.strip_prologue_inits(
                lines, rr.get('locals', []), emit_body.prologue_zeroes(lf, rr))
        lines = emit_body.for_headers(
            emit_body.inline_temps(emit_body.compound_assign(lines)))
        if byidx is not None:
            lines, more = rewrite_state_returns(lines, byidx, in_state=False)
            problems = problems + more
        problems = problems + unnamed_calls(lines, known)
        # EarthC wants a literal `return <expr>;` as the last statement. Where the
        # lifted body does not end in one the result was never recovered, and the
        # forwarding stub below (which ends in `return true;`) is the better guess —
        # it is what makes Network/Towns.ec byte-identical.
        if not problems and lines and lines[-1].strip().startswith('return '):
            decls = [decl_of(v, local_array_sizes(lf, r))
                     for v in rr.get('locals', [])
                     if not v['name'].startswith('$')]
            return decls + lines
    except Exception:
        pass
    import struct
    insns = lf.decode(r)
    calls = [i for i in insns if i[0] == 'call']
    forwarded = None
    for m, a, s, ins in calls:
        rel = struct.unpack_from('<i', lf.code, a + 1)[0]
        if rel != 0:                       # internal call
            forwarded = a + s + rel
    out = []
    # The stub only forwards when there really is a same-named *function* behind
    # the call — that is the Towns.ec shape (`command X(...) { X(...); }`).
    # Guessing it from "there was an internal call" writes a call to the entry
    # point itself, and an event cannot be called: PDialogUnits'
    # `event StartTalkWithDialogUnit` came out calling StartTalkWithDialogUnit and
    # took the whole file down ("Unknown variable, function").
    tgt = lf.by_start.get(forwarded) if forwarded is not None else None
    if tgt is not None and tgt.get('kind') == 7 and tgt.get('name') == d['name']:
        args = ', '.join(p['name'] for p in d['params'])
        out.append(f'{d["name"]}({args});')
    out.append('return true;')
    return out


DEFAULT_STATE_DELAY = 20            # eDefaultStateDelay


def state_transition(path, r, lf=None):
    """`return <state>[,delay];` compiles to a fixed template:

        mov eax, <stateIndex> ; mov [esi], eax ; mov eax, <delay>

    so the target is the constant stored into $State ([esi]) and the delay is the
    constant loaded right after it. A delay of eDefaultStateDelay (20) is what
    `return <state>;` without a delay produces.
    """
    import lifter
    lf = lf or lifter.Lifter(path)
    insns = lf.decode(r)
    target = delay = None
    for i, (m, a, sz, ins) in enumerate(insns):
        if m != 'mov' or ins is None or not ins.op_str.startswith('eax, '):
            continue
        val = ins.op_str.split(', ', 1)[1]
        if not val.startswith('0x') and not val.isdigit():
            continue
        nxt = insns[i + 1] if i + 1 < len(insns) else None
        if not (nxt and nxt[0] == 'mov' and nxt[3] is not None
                and nxt[3].op_str.startswith('dword ptr [esi],')):
            continue
        target = int(val, 0)
        after = insns[i + 2] if i + 2 < len(insns) else None
        if (after and after[0] == 'mov' and after[3] is not None
                and after[3].op_str.startswith('eax, ')):
            v2 = after[3].op_str.split(', ', 1)[1]
            if v2.startswith('0x') or v2.isdigit():
                delay = int(v2, 0)
        break
    if delay == DEFAULT_STATE_DELAY:
        delay = None                 # `return <state>;` compiles to the default
    return target, delay


if __name__ == '__main__':
    print(emit(sys.argv[1]))
