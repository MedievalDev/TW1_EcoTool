"""Structure the lifted block graph into EarthC control flow — for real this time.

`Lifter.structure()` folds a jump only when its target happens to sit inside the
block range it is currently looking at, which is a layout heuristic, not an
analysis.  Anything else becomes `goto L_x;`, and EarthC has no goto, so those
routines cannot be recompiled.  Measured over the corpus that hit 2989 of 7247
routines in `eco\\Scripts_wd` — and 77 % of the blocked ones have no back edge at
all, so loops were never the main problem: plain forward jumps were.

This module replaces the heuristic with the three analyses the problem actually
needs.

1. Short-circuit conditions (`&&` / `||`)
   The code generator emits one basic block per `&&` / `||` operand, so a single
   source condition arrives as a chain of blocks.  A chain block carries no
   statement of its own (a call inside the condition is consumed by the compare,
   so it never becomes a statement), it is entered only by falling through from
   its predecessor, and it ends in a conditional jump.  Two shapes occur:

       A: jcc cA -> T        A: jcc cA -> B+1
       B: jcc cB -> T   ==>  B: jcc cB -> T   ==>   (cA || cB) -> T
                                                    (!cA && cB) -> T

   Both fold into one block whose fall-through is still the block behind B, so
   the merged graph keeps the layout invariants the loop recogniser relies on.
   Without this step an `if (a || b)` is not structurable at all: the two arms
   overlap, so no dominator-based split can separate them.

2. Post-dominators over the block graph
   The join of a two-way branch is its immediate post-dominator, computed on the
   reverse graph against a virtual exit node (Cooper/Harvey/Kennedy).  Each arm
   is then the set of blocks reachable from it without passing that join, and
   emission is a walk of the graph, not of the layout — the arms of nested
   conditions interleave in the layout, which is precisely where the range
   heuristic had to give up.  Where both arms return there is no join; the exit
   of the enclosing region is used instead (the ABS / MIN / MAX shape).

3. Loops via `loops.py`
   `find_loops()` supplies head / latch / exit / body / break / continue, which
   map onto `while`, `do while` and their `break` / `continue`.  A `for` is
   emitted as `while (cond) { body; increment; }` — same program, and EarthC has
   no comma operator to rebuild the original header with.  Two details that are
   easy to get wrong and are handled here: the header block often computes the
   bound into a temporary (`for (i = 0; i < a.GetSize(); i++)`), so its
   statements are emitted before the loop *and* at the end of every pass; and a
   `continue` in the `while` form would skip the increment, so the latch (and
   header) statements are copied in front of it.  The loop-counter watchdog the
   compiler emits (`sub edi,1 / je stub`) is bookkeeping and is dropped.

Three smaller rewrites finish the job:

  * a jump to a block that only leads to a return becomes a `return;` right
    there — the compiler funnels every early exit through one epilogue, which
    alone was 514 of the 762 gotos in `PQuests.eco`;
  * node splitting: when a block belongs to both arms (`ASSERT(a && b)` compiles
    into two tests sharing the assert body) it is copied into one of them, which
    is semantically free because only one path ever runs.  Bounded by MAXSPLIT,
    and rolled back if the copy would itself need a goto;
  * `} else { if (…) }` is printed as `} else if (…)`; the long ladders in this
    corpus nest 200 deep and would otherwise need 800 columns of indentation.

What is still emitted as `goto`: loops whose shape `loops.py` refuses (kind
None), and overlapping arms too large for the duplication budget.  91 of 32006
routines across both corpora, down from 11002; every case is counted in `stats`
so the residue stays visible, and blocks that no region reached are appended
with a label rather than dropped (`orphan-block`).

Output is exactly what `Lifter.structure()` produces, so `emit_body.py` and
`Lifter.routine_text()` can consume it unchanged:

    ('stmt',  depth, payload)     statement tuple from run_block
    ('open',  depth, text)        line that opens a block
    ('close', depth, text)        line that closes (or re-opens) a block
    ('raw',   depth, text)        plain line (return / break / continue / goto)

    structure(lf, r, order, info, pos=None, base=0) -> list

  py structure2.py <file.eco> [routine]     structure and print one file/routine
"""
import collections
import loops as _loops

NEGOP = {'==': '!=', '!=': '==', '>=': '<', '<': '>=', '<=': '>', '>': '<='}
MAXDEPTH = 400         # nesting cap; deeper regions fall back to goto
MAXDUP = 4             # statements a tail-duplicated return may carry
MAXSPLIT = 16          # blocks a duplicated (node-split) region may have
MAXSPLITST = 30        # statements in it
MAXSPLITDEPTH = 3      # nested duplications


# ------------------------------------------------------------------ conditions
def _neg_text(s):
    """negate a comparison written as text.

    The expression was built left-associatively, so the *last* comparison
    operator at bracket depth 0 is the top-level one — `a == 0 != 0` negates on
    the `!=`, not on the `==` (which is what a first-match replace would do).
    Shifts are spelled ` << ` / ` >> ` and are skipped by the space test.

    A leaf with no top-level comparison is a truthiness test (`x`, `!x`, `f()`),
    which the compiler tests with `and reg,reg`. Negating it must toggle the `!`
    rather than wrap another one around it: `!(!(x))` is not what the compiler
    emitted and costs extra instructions when recompiled."""
    depth, i, best = 0, 0, None
    while i < len(s):
        ch = s[i]
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        elif depth == 0 and ch in '<>=!' and i and s[i - 1] == ' ':
            for op in ('==', '!=', '>=', '<=', '>', '<'):
                if s.startswith(op, i) and s[i + len(op):i + len(op) + 1] == ' ':
                    best = (i, op)
                    i += len(op) - 1
                    break
        i += 1
    if best is None:
        return _drop_not(s) if s.startswith('!') else f'!({s})'
    return s[:best[0]] + NEGOP[best[1]] + s[best[0] + len(best[1]):]


def _wraps_all(s):
    """True when s is `( ... )` with the opening bracket closing at the very end"""
    if not (s.startswith('(') and s.endswith(')')):
        return False
    d = 0
    for i, ch in enumerate(s):
        d += (ch == '(') - (ch == ')')
        if d == 0:
            return i == len(s) - 1
    return False


def _drop_not(s):
    """`!x` / `!(x)` -> `x`"""
    inner = s[1:]
    return inner[1:-1] if _wraps_all(inner) else inner


class Cond:
    """condition tree: leaf comparison / && / || / !  (negation stays normalised
    at the leaves via De Morgan, so the printed form never nests `!`)"""
    __slots__ = ('op', 'kids', 'text')
    PREC = {'leaf': 0, 'not': 0, 'and': 1, 'or': 2}

    def __init__(self, op, kids=(), text=''):
        self.op, self.kids, self.text = op, tuple(kids), text

    def render(self, maxp=9):
        if self.op == 'leaf':
            return self.text
        if self.op == 'not':
            return f'!({self.kids[0].render(9)})'
        sep = ' && ' if self.op == 'and' else ' || '
        p = self.PREC[self.op]
        s = sep.join(k.render(p) for k in self.kids)
        return f'({s})' if p > maxp else s

    def __str__(self):
        return self.render()


def leaf(x):
    return x if isinstance(x, Cond) else Cond('leaf', (), str(x))


def c_and(a, b):
    return Cond('and', (a, b))


def c_or(a, b):
    return Cond('or', (a, b))


def c_not(c):
    if c.op == 'leaf':
        t = _neg_text(c.text)
        return Cond('not', (c,)) if t.startswith('!(') else Cond('leaf', (), t)
    if c.op == 'not':
        return c.kids[0]
    if c.op == 'and':
        return Cond('or', tuple(c_not(k) for k in c.kids))
    return Cond('and', tuple(c_not(k) for k in c.kids))


# ------------------------------------------------------------------ graph tools
def _stmts(st):
    """the block's real statements — hook markers are only line anchors"""
    return [s for s in st if s[0] != 'hookmark']


def _spills(st):
    """the compiler spill stores of a block, or None when it holds anything else.

    The second half of a `&&` is not always a bare test: when its right-hand side
    is a call, the compiler parks the left-hand side in a temporary slot first
    (`$4 = nCnt ; call GetSize ; cmp`).  That store belongs to the condition, not
    to the loop body, so a chain block holding only such stores can still be
    folded — `while (!strcmp(a, b) && nCnt < arr.GetSize())` (Music.ec:711) came
    out as `while (!strcmp(a, b)) { if (nCnt < arr.GetSize()) { … } else break; }`
    otherwise, and the extra test cost 19 code bytes. `$` names are the compiler's
    own slots (Lifter.mem_name); emit_body.inline_temps folds them back in.
    """
    out = []
    for s in _stmts(st):
        if s[0] != 'assign' or not str(s[1]).startswith('$'):
            return None
        out.append(str(s[1]))
    return out


def _jmp_target(info, order, i):
    """address a statement-less block jumps to, else None"""
    if not 0 <= i < len(order):
        return None
    st, ex = info[order[i]]
    return ex[1] if ex[0] == 'jmp' and not _stmts(st) else None


def _spill_names(st):
    """every `$` slot the block stores into, whatever else it does.

    The leading run of `= 0` is the compiler zeroing the frame on entry, not a
    spill, and it covers the temporaries too. Counting it made the entry block
    collide with every later spill of the same slot, which blocked the second half
    of the `||` in `(nLocalHeroIndex >= 0 && IsShowLocal()) || (… && IsShowRemote())`
    from being folded in (Network/MissionTeamBase.ech:161) — the loop behind it then
    had to be duplicated into one arm, +149 code bytes in InitTeamMapSigns.
    """
    out, prologue = set(), True
    for s in _stmts(st):
        if s[0] != 'assign':
            prologue = False
            continue
        if prologue and str(s[2]) in ('0', 'null', '""'):
            continue
        prologue = False
        if str(s[1]).startswith('$'):
            out.add(str(s[1]))
    return out


def _emittable(st):
    """statements that reach the source. Handle AddRef/Release and ctor/dtor calls
    do not, so a block holding only those is not code that would be lost — the
    epilogue that `Lifter.repair_returns` orphans is exactly such a block, and
    counting it produced a stray `L_1704:` label in every state with a handle
    local."""
    import lifter
    return [s for s in _stmts(st) if not lifter.Lifter.is_bookkeeping(s)]


def _pred_count(order, info, pos):
    c = collections.Counter()
    for i, a in enumerate(order):
        ex = info[a][1]
        k = ex[0]
        if k == 'ret':
            continue
        if k == 'jmp':
            j = pos.get(ex[1])
            if j is not None:
                c[j] += 1
            continue
        if k == 'jcc':
            j = pos.get(ex[2])
            if j is not None:
                c[j] += 1
        if i + 1 < len(order):
            c[i + 1] += 1
    return c


def merge_conditions(order, info, protect=(), latches=(), heads=()):
    """Fold `&&` / `||` block chains into one block.

    protect = addresses that must stay their own block (the loop-counter guard
    and its watchdog stub have exactly the shape of a condition-chain block).
    latches = the loops' back-edge blocks; see the `converge` case below.
    Returns (order, info) — both fresh objects, the caller's stay untouched."""
    order = list(order)
    info = dict(info)
    protect = set(protect)
    latches = set(latches)
    heads = set(heads)
    merged = 0
    while True:
        pos = {a: i for i, a in enumerate(order)}
        preds = _pred_count(order, info, pos)
        for i in range(len(order) - 1):
            a, b = order[i], order[i + 1]
            if a in protect or b in protect:
                continue
            exa, exb = info[a][1], info[b][1]
            if exa[0] != 'jcc' or exb[0] != 'jcc':
                continue
            spill_b = _spills(info[b][0])
            if spill_b is None or preds[i + 1] != 1:
                continue          # b must be pure condition, entered only from a
            # The compiler reuses ONE slot for all the spills of a chain
            # (`$28 = nX ; … ; $28 = nY ; …`), so folding more than one into a
            # single block leaves several stores to the same name in front of a
            # condition that names it several times, and inline_temps then resolves
            # every reference to the last store: all four comparisons of
            # `nX >= … && nY >= … && nX <= … && nY <= …` came out reading nY
            # (TwoWorldsMusic.GetTownIndexWithHeroInside, Music.ec:560). A chain
            # therefore carries at most one spill in total.
            if len(set(spill_b)) != len(spill_b) or \
                    (set(spill_b) & _spill_names(info[a][0])):
                continue
            fb = order[i + 2] if i + 2 < len(order) else None
            if exa[2] == exb[2]:                       # both jump to the same place
                cond, tgt = c_or(leaf(exa[1]), leaf(exb[1])), exa[2]
            elif fb is not None and exa[2] == fb:      # a skips b's own target
                cond, tgt = c_and(c_not(leaf(exa[1])), leaf(exb[1])), exb[2]
            elif fb is not None and _jmp_target(info, order, i + 2) == exa[2] \
                    and exa[2] not in latches and a not in heads:
                # Same `&&`, except that a's target and b's fall-through only
                # CONVERGE instead of being the same block: the fall-through is a
                # bare `jmp` to it. That is what the tail test of a do-while looks
                # like — `while ((nCount < 10) && (!IsGoodPointForUnit(…)))`
                # (Units/Activity.ech:935) exits on the first half and loops on the
                # second, with a one-instruction block in between. Unmerged, the
                # first half came out as `if (nCount >= 10) break;` inside the body
                # and cost an extra test and two jumps (Unit.MoveToTargetCircle).
                #
                # Not when the shared target is a loop's back edge. There the bare
                # `jmp` is a `continue`, and `continue` is a statement, not the
                # second half of a condition: `if (a[i] == n) { if (f(i) != 1)
                # continue; … }` has exactly this shape, and merged it became
                # `if (a[i] != n || f(i) != 1) { } else …`. Same instruction count,
                # but the first test then jumps to the empty arm instead of straight
                # to the latch, which is one byte per occurrence
                # (PQuestsMulti16.CheckMarkerQuestGivers and its twin in PQuests).
                #
                # And not when `a` is a loop HEADER. The case this rule was built
                # for is the tail test of a do-while, where the condition sits on
                # the latch; on a header the bare `jmp` is the `break` of the first
                # statement in the body, and folding the two swallowed the loop
                # condition: `while (g < n) { if (!RemoveGuard()) break; }` came out
                # as `while (true) { if (g >= n || !RemoveGuard()) break; }`, which
                # is a `mov eax,1 ; and eax,eax ; je` more
                # (PTown.UpdateGuardsNumber, PInc/PGuard.ech:253).
                cond, tgt = c_and(c_not(leaf(exa[1])), leaf(exb[1])), exb[2]
            else:
                continue
            info[a] = (info[a][0] + info[b][0], ('jcc', cond, tgt, fb))
            del info[b]
            order.pop(i + 1)
            merged += 1
            break
        else:
            return order, info, merged


def successors(order, info, pos, skip=()):
    """successor index lists; index len(order) is the virtual exit node.

    Blocks in `skip` (the loop watchdog stubs) are cut out of the graph and the
    guard's escape edge to them with it, so post-dominance sees the control flow
    the source had, not the compiler's runaway-loop bookkeeping."""
    n = len(order)
    out = []
    for i, a in enumerate(order):
        ex = info[a][1]
        k = ex[0]
        if k == 'ret' or i in skip:
            out.append([n] if k == 'ret' else [])
            continue
        if k == 'jmp':
            j = pos.get(ex[1])
            out.append([j] if j is not None and j not in skip else [n])
            continue
        s = []
        if k == 'jcc':
            j = pos.get(ex[2])
            if j is not None and j not in skip:
                s.append(j)
        s.append(i + 1 if i + 1 < n else n)
        out.append(s)
    out.append([])                                     # virtual exit
    return out


def post_dominators(succ, n):
    """ipdom[i] = immediate post-dominator, None when i cannot reach the exit.

    Cooper/Harvey/Kennedy on the reverse graph rooted at the virtual exit."""
    rpreds = [[] for _ in range(n + 1)]
    for i in range(n):
        for s in succ[i]:
            rpreds[s].append(i)
    po, seen = [], [False] * (n + 1)
    stack = [(n, iter(rpreds[n]))]
    seen[n] = True
    while stack:
        node, it = stack[-1]
        nxt = next(it, None)
        if nxt is None:
            po.append(node)
            stack.pop()
        elif not seen[nxt]:
            seen[nxt] = True
            stack.append((nxt, iter(rpreds[nxt])))
    num = {b: k for k, b in enumerate(po)}
    idom = [None] * (n + 1)
    idom[n] = n

    def inter(x, y):
        while x != y:
            while num[x] < num[y]:
                x = idom[x]
            while num[y] < num[x]:
                y = idom[y]
        return x

    rpo = [b for b in reversed(po) if b != n]
    changed = True
    while changed:
        changed = False
        for b in rpo:
            new = None
            for p in succ[b]:                 # reverse-graph predecessors
                if p in num and idom[p] is not None:
                    new = p if new is None else inter(new, p)
            if new is not None and idom[b] != new:
                idom[b] = new
                changed = True
    return idom


def _group_end(out, k):
    """index of the entry that closes the block opened at index k"""
    c = 0
    for i in range(k, len(out)):
        kind, _, txt = out[i]
        if kind == 'open':
            c += 1
        elif kind == 'close' and not str(txt).startswith('} else'):
            c -= 1
            if c == 0:
                return i
    return None


def flatten_else_if(out):
    """`} else { if (c) { … } }` -> `} else if (c) {`.

    A chain of `else if` is a chain of nested else-blocks in the graph, and the
    long ones in this corpus are 200 deep — printed as real nesting they would be
    unreadable and would need 800 columns of indentation.

    The `} else {` must be a bare one. The pass rewrites the line in place and then
    re-examines it, so accepting anything that merely starts with `} else` let it
    fire a second time on the `} else if (c) {` it had just written: the else-group
    of the rewritten line is the body of the inner if, and flattening that dropped
    the condition it had just moved up. `else if (IsPlayer(i)) { if (!a[i]) … }`
    came out as `else if (!a[i]) …` — 18 code bytes and the whole test gone
    (PQuestsMulti/PQuestsMulti16 UpdateHeroLocations)."""
    i = 0
    while i < len(out) - 1:
        kind, d, txt = out[i]
        nk, nd, ntxt = out[i + 1]
        if (kind == 'close' and str(txt) == '} else {'
                and nk == 'open' and nd == d + 1 and str(ntxt).startswith('if (')):
            end = _group_end(out, i + 1)
            if end is not None and end + 1 < len(out) and out[end + 1][:2] == ('close', d) \
                    and str(out[end + 1][2]) == '}':
                out[i] = ('close', d, f'}} else {ntxt}')
                del out[end + 1]
                del out[i + 1]
                for j in range(i + 1, end):
                    k2, d2, t2 = out[j]
                    out[j] = (k2, d2 - 1, t2)
                continue
        i += 1
    return out


# ------------------------------------------------------------------ structurer


class Structurer:
    """Rebuilds the control flow of one routine.

    Emission is a walk of the block graph, not of the block layout: a two-way
    branch is split at its immediate post-dominator, and each arm is the set of
    blocks reachable from it without passing that join.  Those sets are not
    contiguous in layout order — the code generator interleaves the arms of
    nested conditions — which is exactly why the range heuristic had to give up
    and print a goto.
    """

    @staticmethod
    def _cfg_view(lf, info):
        """`info` with the return repair undone.

        `Lifter.repair_returns` turns `jmp epilogue` into `ret <value>` to keep a
        return value that a shared epilogue would swallow. That is right for the
        emitted text but wrong for the graph: the loop watchdog stub ends in exactly
        such a jump, so after the repair it no longer reached the epilogue and loop
        recognition fell back from `for` to the shapeless `head` — the guard block
        was then emitted as user code (`if (eax == 0) return 0;` inside every loop of
        TwoWorldsLights / TwoWorldsWeather / TwoWorldsCampaign).
        """
        pre = getattr(lf, 'cfg_exit', None)
        if not pre:
            return info
        return {a: (st, pre.get(a, ex)) for a, (st, ex) in info.items()}

    def __init__(self, lf, order, info, pos=None):
        self.lf = lf
        order0 = list(order)
        info0 = dict(info)
        cfg0 = self._cfg_view(lf, info0)
        pos0 = pos or {a: i for i, a in enumerate(order0)}
        # the guard / watchdog pair looks exactly like a condition-chain block,
        # so it has to be known before the chains are folded
        keep = set()
        latches = set()
        heads = set()
        for L in _loops.find_loops(order0, cfg0, pos0):
            for b in (L.guard, L.watchdog):
                if b is not None and 0 <= b < len(order0):
                    keep.add(order0[b])
            for b in (L.latch, L.head):
                if b is not None and 0 <= b < len(order0):
                    latches.add(order0[b])
            if L.head is not None and 0 <= L.head < len(order0):
                heads.add(order0[L.head])
        self.order, self.info, self.merged = merge_conditions(
            order0, info0, keep, latches, heads)
        self.pos = {a: i for i, a in enumerate(self.order)}
        self.n = len(self.order)

        # merge_conditions only ever folds jcc-into-jcc blocks, which the repair never
        # touches, so the pre-repair exits still line up with the merged block list
        self.loops = _loops.find_loops(
            self.order, self._cfg_view(lf, self.info), self.pos)
        self.guards, self.stubs, self.loop_at = set(), set(), {}
        for L in self.loops:
            if L.kind is None:
                continue                       # shape not understood: keep goto
            self.loop_at.setdefault(L.span[0], L)
            if L.guard is not None:
                self.guards.add(L.guard)
            if L.watchdog is not None:
                self.stubs.add(L.watchdog)
        self.succ = successors(self.order, self.info, self.pos, self.stubs)
        # blocks the entry can actually reach; the compiler's dead "skip the else"
        # jumps are not among them, and a chain of them must not make each other
        # look alive (see _else_end)
        self.live, _stk = set(), [0]
        while _stk:
            _b = _stk.pop()
            if _b in self.live or _b >= self.n:
                continue
            self.live.add(_b)
            _stk.extend(self.succ[_b])
        self.ipdom = post_dominators(self.succ, self.n)
        self.stack = []            # enclosing loops, innermost last
        self.open = set()          # loops currently being emitted
        self.dupdepth = 0
        self.labels = set()
        self.need = set()
        self.elsejmp = set()   # skip-else jumps already claimed, see _empty_else
        self.done = set()
        # `_falls_into` may treat a loop exit / latch / head as a way out; a routine
        # that needs a goto with that reading is re-structured without it (see run)
        self.allow_loop_leave = True
        self.stats = collections.Counter()

    # -------------------------------------------------------------- analysis
    def reach(self, entry, stop, scope):
        """blocks reachable from `entry` without passing `stop`, inside `scope`.

        That is the branch arm: everything the arm owns, however the blocks are
        spread through the layout."""
        out, stack = set(), [entry]
        while stack:
            b = stack.pop()
            if b in out or b == stop or b >= self.n or b not in scope:
                continue
            out.add(b)
            stack.extend(self.succ[b])
        return out

    def _falls_into(self, fi, ti, scope):
        """does the `fi` arm only ever end in a return or in `ti` itself?

        Then the branch has no else part: the `ti` arm is simply what follows the
        `if`.  Emitted as two independent arms the transfer became
        `if (A) { …; goto L; } else { L: … }` — five files failed to recompile on
        exactly that one line (PDialogUnits, PQuests, PQuestsMulti16, PTown,
        TwoWorldsContainers; the shape is the tail of a `switch`-like ladder such
        as PDialogUnits' GetCreateString, where the special-cased town falls back
        into the generic list).
        """
        if not (0 <= fi < self.n and 0 <= ti < self.n) or fi == ti:
            return False
        if fi in self.done or ti in self.done or ti not in scope:
            return False
        region = self.reach(fi, ti, scope)     # reach() never includes `stop`
        if not region:
            return False
        # An enclosing loop's exit / latch / head is a `break` or a `continue`, so an
        # arm that jumps there has left just as much as one that returns. Counting it
        # as "leaves elsewhere" rejected the shape in Units/Hero.ech's FindBestTarget,
        # where the arm ends in `continue`: the shared tail behind the `if` then went
        # into the else arm and had to be duplicated into the other one, +341 code
        # bytes in Hero and Unit alike.
        leave = set()
        if self.allow_loop_leave:
            for L in self.stack:
                leave |= {L['exit'], L['latch'], L['head']}
        entered = False
        for b in region:
            for s in self.succ[b]:
                if s == ti:
                    entered = True
                elif s >= self.n or s in leave:
                    continue                   # off the end / break / continue
                elif s not in region and self.tail_return(s) is not None:
                    # An early `return` inside the arm. It reaches the shared
                    # epilogue BLOCK, which is outside the region whenever the
                    # region ends before it, so the test above read it as a second
                    # join and rejected the shape. `tail_return` is the predicate
                    # the emitter already uses for "this block only ever returns",
                    # so no new notion of the epilogue is introduced here.
                    # PTown.StartChatDialog (PInc/PActivity.ech:343) is the case:
                    # `if (!ValidDialog(…)) { …; if (…) { …; return; } … }` followed
                    # by the shared tail. Rejected, the tail went into the else arm
                    # and had to be duplicated into the other one — 237 code bytes.
                    continue
                elif s not in region:
                    return False               # leaves elsewhere: real join
        return entered

    def tail_return(self, ti):
        """`return` statement if block ti only ever leads to a return, else None.

        The compiler funnels every early exit through one epilogue block, so a
        jump to it is a `return`, not a goto.  Copying a straight-line tail of at
        most MAXDUP statements is an equivalence transformation."""
        seen, cost, chain = set(), 0, []
        while ti is not None and 0 <= ti < self.n and ti not in seen:
            seen.add(ti)
            chain.append(ti)
            st, ex = self.info[self.order[ti]]
            body = _stmts(st)
            cost += len(body)
            if cost > MAXDUP:
                return None
            if ex[0] == 'ret':
                txt = (f'return {ex[1]};' if ex[1] is not None and str(ex[1]) != '?'
                       else 'return;')
                return chain, body, txt
            if ex[0] == 'jmp':
                ti = self.pos.get(ex[1])
            elif ex[0] == 'fall':
                ti += 1
            else:
                return None
        return None

    @staticmethod
    def _has_code(out):
        return any(k != 'stmt' or p[0] != 'hookmark' for k, _, p in out)

    @staticmethod
    def _is_exit_arm(out):
        """arm that only leaves — worth pulling in front as a guard clause.

        A nested `if` disqualifies it however short it is: the last arm of an
        `else if` ladder whose branches all return fits in four lines
        (`if (c) { return 0; } return 2;`), and pulling that in front reverses the
        whole ladder. TwoWorldsMusic.StateCheck_GetMapType (Music.ec:501) came out
        with its three `return`s in the order 0, 2, 1 instead of 1, 2, 0."""
        code = [e for e in out if e[0] != 'stmt' or e[2][0] != 'hookmark']
        if not code or len(code) > 4 or any(k == 'open' for k, _, _ in code):
            return False
        k, _, p = code[-1]
        return k == 'raw' and (p in ('break;', 'continue;', 'return;')
                               or p.startswith('return '))

    # -------------------------------------------------------------- emission
    def go(self, nxt, depth, out, follow, scope):
        """continue at block `nxt`.

        -> the index to carry on with, or None when the transfer was emitted
        (or was the natural end of the region) and this sequence is finished."""
        if nxt is None:
            out.append(('raw', depth, 'goto ???;'))
            self.stats['goto-unknown'] += 1
            return None
        if nxt == follow:
            return None                                # region ends here
        for L in reversed(self.stack):
            if nxt == L['exit']:
                out.append(('raw', depth, 'break;'))
                self.stats['break'] += 1
                return None
            if nxt in (L['latch'], L['head']):
                if nxt == L['latch'] and L['inc']:
                    # emitted as `while`, so the increment sits at the end of the
                    # body and a bare continue would jump over it — copy it in
                    for s in L['inc']:
                        out.append(('stmt', depth, s))
                    self.stats['continue-inc'] += 1
                out.append(('raw', depth, 'continue;'))
                self.stats['continue'] += 1
                return None
        if nxt < self.n and nxt in scope and nxt not in self.done:
            return nxt
        if (nxt == self.n - 1 and follow == self.n and not self.stack
                and self._exit_text(nxt) == 'return;'):
            # The routine's own trailing `return;` is implicit: the epilogue is the
            # last block and control falls into it. `tail_return` below would copy
            # it in as a statement, which costs a dead `jmp <epilogue>` — the same
            # reasoning emit_seq applies when it reaches the block directly, and
            # the epilogue is now regularly marked done inside an arm first
            # (TwoWorldsLights.ShowLight: 3516 with the copy, 3511 without).
            return None
        dup = self.tail_return(nxt)
        if dup is not None:
            chain, body, txt = dup
            for s in body:
                out.append(('stmt', depth, s))
            out.append(('raw', depth, txt))
            self.done.update(chain)
            self.stats['tail-return'] += 1
            return None
        if self.duplicate(nxt, depth, out, follow):
            return None
        out.append(('raw', depth, f'goto L_{self.order[nxt]};'))
        self.need.add(nxt)
        self.stats['goto'] += 1
        return None

    def duplicate(self, nxt, depth, out, follow):
        """Node splitting: emit a *copy* of the region [nxt … follow).

        Two arms of a branch can share blocks — `if (a || b) assert()` compiles
        into a chain whose second test falls into the same assert block the first
        one jumps to — and a shared block cannot be nested under both arms.
        Copying it along one of the paths is the standard cure and is
        semantically free: only one path ever runs.  Bounded by MAXSPLIT so the
        output cannot blow up; if the copy would itself need a goto, it is
        rolled back and the goto is printed instead."""
        if self.dupdepth >= MAXSPLITDEPTH:
            return False
        region = self.reach(nxt, follow, set(range(self.n)))
        if not region or len(region) > MAXSPLIT:
            return False
        if sum(len(_stmts(self.info[self.order[b]][0])) for b in region) > MAXSPLITST:
            return False
        keep_done, keep_need = set(self.done), set(self.need)
        keep_stats = collections.Counter(self.stats)
        self.done -= region
        self.dupdepth += 1
        tmp = []
        self.emit_seq(nxt, follow, depth, tmp, region)
        self.dupdepth -= 1
        if any(k == 'raw' and 'goto' in str(p) for k, _, p in tmp):
            self.done, self.need, self.stats = keep_done, keep_need, keep_stats
            return False
        self.done |= keep_done | region
        out += tmp
        self.stats['duplicate'] += 1
        return True

    def emit_seq(self, entry, follow, depth, out, scope):
        """emit the region entered at `entry` and left at `follow`"""
        cur = entry
        while cur is not None:
            if depth > MAXDEPTH:
                self.stats['too-deep'] += 1
                out.append(('raw', depth, f'goto L_{self.order[cur]};'))
                self.need.add(cur)
                return
            L = self.loop_at.get(cur)
            if L is not None and L.span[1] < self.n and cur not in self.open:
                self.emit_loop(L, depth, out, scope)
                nxt = L.exit if L.exit is not None else L.span[1] + 1
                cur = self.go(nxt, depth, out, follow, scope)
                continue
            if cur in self.stubs:                      # watchdog, not user code
                return
            self.done.add(cur)
            addr = self.order[cur]
            st, ex = self.info[addr]
            if cur in self.labels:
                out.append(('raw', depth, f'L_{addr}:'))
            for s in st:
                out.append(('stmt', depth, s))
            if cur in self.guards:                     # loop-counter bookkeeping
                cur = self.go(cur + 1, depth, out, follow, scope)
                continue
            k = ex[0]
            if k == 'ret':
                if ex[1] is not None and str(ex[1]) != '?':
                    out.append(('raw', depth, f'return {ex[1]};'))
                elif cur != self.n - 1 or self.stack or follow != self.n:
                    # The routine's own trailing `return;` is implicit — the last
                    # block IS the epilogue and control falls into it. Inside a
                    # region that ends somewhere else (`follow != self.n`) that
                    # reasoning does not hold: reaching the epilogue from there is
                    # a real `return;` in the source, and dropping it silently
                    # emptied the arm (TwoWorldsLights.ShowLight, Lights.ec:72,
                    # `if (…) { } else return;` came out as `if (!…) { rest }`).
                    out.append(('raw', depth, 'return;'))
                return
            if k == 'jmp':
                tgt = self.pos.get(ex[1])
                if (follow == self.n and not self.stack and not _stmts(st)
                        and tgt == cur + 1 == self.n - 1
                        and self._exit_text(tgt) == 'return;'):
                    # A statement-less block that jumps to the epilogue sitting
                    # right behind it is the source's own trailing `return;` (or the
                    # `jmp` over an empty else at the very end). go() would hand the
                    # epilogue back, and emit_seq suppresses the implicit return
                    # there, so the five bytes went missing —
                    # MissionCommon.ech's ShowTeamResultsText after its three
                    # `if (…) { …; return; }` guards.
                    out.append(('raw', depth, 'return;'))
                    self.stats['trailing-return'] += 1
                    return
                cur = self.go(tgt, depth, out, follow, scope)
            elif k == 'jcc':
                cur = self.emit_if(cur, ex, depth, out, follow, scope)
            else:                                      # plain fall-through
                cur = self.go(cur + 1, depth, out, follow, scope)

    def emit_arm(self, entry, stop, depth, out, scope):
        """one branch arm: its own region, or just the transfer when the arm is
        nothing but a jump out (`break` / `continue` / `return`)"""
        if entry is None or entry == stop:
            return
        if entry >= self.n or entry not in scope or entry in self.done:
            self.go(entry, depth, out, stop, scope)
            return
        self.emit_seq(entry, stop, depth, out, self.reach(entry, stop, scope))

    def _else_end(self, k, ti):
        """where the `else` arm ends, or None when the source wrote no `else`

        `if (c) { … return X; } else { … }` makes the compiler append a `jmp` past
        the else part after the arm; since the arm always returns, that jump is
        never executed and forms an unreachable one-instruction block right in front
        of the else arm. Its presence is the only thing that distinguishes the
        if/else source from the guard-clause source, which produces the same code
        minus those five bytes (roundtrip/mytest2/G1.ec vs G2.ec).

        The jump has to land BEYOND the else arm to belong to this `if` — the last
        member of an `else if` ladder is followed by the very same dead jump, but
        that one belongs to an enclosing level and lands on the else arm itself
        (Mission_E01's Initialize has both readings six statements apart).

        "Unreachable" is measured against the blocks the entry really reaches: a
        nested ladder whose arms all return leaves a whole run of these dead jumps,
        and they all target the outermost one, so counting any predecessor at all
        made every level but the innermost look alive
        (TwoWorldsMusic.StateCheck_GetMapType, Music.ec:499 — the outer
        `if (nLevelType == eSingleplayerMap) {…} else if (…)` lost its else).
        """
        if not 0 <= k < self.n:
            return None
        st, ex = self.info[self.order[k]]
        if _stmts(st) or ex[0] != 'jmp':
            return None
        if any(k in self.succ[b] for b in self.live if b != k):
            return None               # reachable: a real jump, not the dead one
        tgt = self.pos.get(ex[1])
        return tgt if tgt is not None and tgt > ti else None

    def _exit_text(self, j):
        """the `return …;` block `j` performs, when it does nothing but return.

        Used to put back the terminator an arm loses when the region it is emitted
        in happens to end on the routine's epilogue — see emit_if."""
        if j is None or not 0 <= j < self.n:
            return None
        st, ex = self.info[self.order[j]]
        # `_emittable`, not `_stmts`: the epilogue regularly carries the handle
        # Release calls for the routine's object locals, which never reach the
        # source. Counting them made the block look like real code, so `go()` fell
        # through to `tail_return` and copied a `return;` in that the source does
        # not have (TwoWorldsEnemies.RemoveEnemyMarker, 5 bytes).
        if _emittable(st) or ex[0] != 'ret':
            return None
        if ex[1] is not None and str(ex[1]) != '?':
            return f'return {ex[1]};'
        return 'return;'

    @staticmethod
    def _ends_with_exit(out):
        """arm whose last emitted statement already leaves the routine/loop"""
        code = [e for e in out if e[0] != 'stmt' or e[2][0] != 'hookmark']
        if not code:
            return False
        k, _d, p = code[-1]
        return k == 'raw' and (str(p) in ('break;', 'continue;', 'return;')
                               or str(p).startswith('return '))

    def _shared_end(self, fi, ti, j, scope):
        """the block both arms merge on when the post-dominator lies past it.

        The then-arm's last block ends in the compiler's `jmp <endif>`.  When that
        target is reachable from the else arm too but is *not* the post-dominator
        — because a third path skips it, typically an early `return` in the last
        `else` — then it, not the post-dominator, is where the `if` ends.  Taking
        the post-dominator makes whichever arm is emitted first swallow the
        convergence block, and the other arm then has to duplicate it."""
        k = ti - 1
        if not (0 <= fi <= k < self.n):
            return None
        ex = self.info[self.order[k]][1]
        if ex[0] != 'jmp':
            return None
        t = self.pos.get(ex[1])
        if t is None or not (ti < t < self.n) or t == j or t not in scope:
            return None
        if t in self.done:
            return None
        if t not in self.reach(ti, j, scope) or t not in self.reach(fi, j, scope):
            return None
        return t

    def _skip_else(self, fi, ti, j, scope):
        """the still-live `jmp` the then-arm ends with, which names the end of the `if`

        `_else_end` only accepts the jump when it is unreachable (the arm returned);
        `_shared_end` only when both arms reach the target. Neither covers
        `if (c) { … } else { return 0; } return 0;` (Music.ec:411): the arm falls
        into a real skip-else jump, but the else arm leaves instead of merging, so
        post-dominance puts the join at the epilogue and the tail after the `if` was
        swallowed into the then-arm — the skip-else jump then has nothing to skip
        and disappears (5 code bytes, StateCheck_IsHeroInDanger).
        """
        k = ti - 1
        if not (0 <= fi <= k < self.n):
            return None
        # The block may well carry the arm's own statements — the compiler appends
        # the skip-else jump to them instead of opening a block for it. Requiring a
        # statement-less block (as `_else_end` does, where the jump really is dead)
        # missed `if (arrStartX.GetSize() > 0) { … } else { continue; }`
        # (Network/MissionCommon.ech, MoveHeroesToStartMarkersAndCreateHorses): the
        # four statements of the arm sit in front of the jump, so the `else` was
        # dropped and the rest of the loop body pulled into the arm, 10 code bytes.
        ex = self.info[self.order[k]][1]
        if ex[0] != 'jmp':
            return None
        t = self.pos.get(ex[1])
        if t is None or not (ti < t < self.n) or t == j or t not in scope \
                or t in self.done or self._leaves_at(t, only_epilogue=True):
            return None
        return t

    def _empty_else(self, fi, ti, j, scope):
        """did the source write an `else { }` the compiler still jumped over?

        An empty else part is not free: the compiler puts its `jmp <endif>` after
        the then-arm anyway, and with nothing in the else part that jump lands on
        the very next instruction — five bytes that a plain `if (c) { … }` does not
        have. The shape is a statement-less block at the end of the then-arm whose
        jump target is `ti` itself.

        It occurs wherever a debug-only macro is the whole else part:
        `if (…) { … } else { __ASSERT_FALSE(); }` (Common/Lock.ech:139) expands to
        an empty block in the release build — TwoWorldsContainers.TryOpenLock and
        RegisterContainers, 5 bytes each.

        Measured with the SDK compiler, roundtrip/mytest6/r2.ec: f1 (no else) is
        45 code bytes, f2 (`else { }`) is 50, and the difference is exactly that
        `jmp` to the following instruction. The block may well carry the arm's own
        statements — only the exit matters.
        """
        k = ti - 1 if ti is not None else None
        if k is None or not (0 <= fi <= k < self.n) or ti != j or k in self.elsejmp:
            return False
        # NOT DONE: skipping the case where `ti` is the epilogue — where the jump in
        # front of it could just as well be the arm's own `return;` — is a net loss.
        # The sources really do end a void routine with `else { }` often enough
        # (TwoWorldsContainers, TwoWorldsEnemies and Enemies16 each lost 5 bytes,
        # TwoWorldsHeroControl 26874 -> 26836), so the empty else is kept.
        ex = self.info[self.order[k]][1]
        if not (ex[0] == 'jmp' and self.pos.get(ex[1]) == ti
                and k in self.reach(fi, ti, scope)):
            return False
        # One `jmp <ti>` belongs to exactly one `if`. Nested empty else parts share
        # the block that carries it — `if (a) { if (b) { … } else { } }` has the
        # inner arm end on the same instruction — and arms are emitted innermost
        # first, so the first claim is the right one. Without this the outer `if`
        # grew an else part of its own (TwoWorldsContainers.RegisterContainers,
        # +5 bytes).
        self.elsejmp.add(k)
        return True

    def reach_set(self, entry, stops, scope):
        """blocks reachable from `entry` without passing any of `stops`"""
        out, stack = set(), [entry]
        while stack:
            b = stack.pop()
            if b in out or b in stops or b >= self.n or b not in scope:
                continue
            out.add(b)
            stack.extend(self.succ[b])
        return out

    def _returns_to_epilogue(self, fi, ti, j, scope, depth=0):
        """fall-through arm that ends on the routine's epilogue: a `return;` guard.

        In a void routine `return;` stays a `jmp <epilogue>` (repair_returns only
        runs for routines that return a value), so the arm lands on a real block and
        `_never_joins` reads that as a merge. The caller has already established that
        `_else_end` found no dead skip-else jump, which is what proves the source
        wrote no `else`; on top of that this asks for the narrowest possible shape:

          * `j` is the value-less epilogue,
          * the arm's last block jumps straight to it — an explicit `return;`,
          * every other edge out of the arm goes there or off the end, and
          * the two arms share no block.
        """
        if j is None or not 0 <= j < self.n or self._exit_text(j) != 'return;':
            return False
        last = self.info[self.order[ti - 1]][1] if 0 <= ti - 1 < self.n else None
        if not (last and last[0] == 'jmp' and self.pos.get(last[1]) == j):
            return False
        stops = {j}
        for L in self.stack:
            stops |= {L['exit'], L['latch'], L['head']}
        region = self.reach_set(fi, stops, scope)
        if not region or ti in region:
            return False
        if region & self.reach_set(ti, stops, scope):
            return False
        # An arm with no code of its own is `if (c) return;`, and that compiles to
        # the same two instructions as `if (c) { } else { rest }` (jne + jmp), so
        # rewriting it wins nothing — while it does move the whole rest of the
        # routine from an else arm up to the top level, which changes every scope
        # below it. In TwoWorldsHeroControl.AddSP that reshuffle made `_shared_end`
        # miss the block behind the ladder and `tail_return` copy the epilogue into
        # each branch instead, losing `SetConsoleText` and `AddSkillPoints`
        # (26874 -> 26390, and EarthC dropped the now unreachable AddSkillPoints).
        if depth <= 0 and not any(
                _emittable(self.info[self.order[b]][0]) for b in region):
            return False
        return all(s == j or s >= self.n or s in region
                   for b in region for s in self.succ[b])

    def _never_joins(self, entry, other, stop, scope):
        """arm at `entry` that leaves on every path instead of merging back.

        Leaving through the virtual exit (index self.n, i.e. `return`) is the
        normal case and does not count as a merge; reaching the join block or
        falling into the other arm does.

        `break` and `continue` leave just as much as `return` does, so an enclosing
        loop's exit, latch and head end the walk too. Without that, an arm ending in
        `continue` reached the loop head, and from there the whole body including
        the other arm — so the guard clause was rejected and the two separate `if`s
        of TwoWorldsEnemies16.CheckEnemyTrap (Enemies16.ec:248/254) came out as one
        if/else, which pays for the skip-else jump on top of the continue.
        """
        stops = {stop}
        for L in self.stack:
            stops |= {L['exit'], L['latch'], L['head']}
        region = self.reach_set(entry, stops, scope)
        if not region or other in region:
            return False
        # NOT DONE: also rejecting an arm whose successor leaves the region for a
        # block that is neither the join nor a loop edge. `reach_set` stops at the
        # `scope` boundary, so such an edge can hide the merge — but demanding it
        # cost TwoWorldsLights its `if (IsDay()) return;` guard (3511 -> 3516), and
        # the shape it was meant to reject is caught by the emittable-code test in
        # `_returns_to_epilogue` instead.
        # NOT DONE, on purpose: a void routine keeps its `jmp <epilogue>`
        # (repair_returns only runs for routines that return a value), so every
        # `return;` lands on a real block and the test below calls that a merge.
        # Treating the epilogue block like the virtual exit here — with or without
        # an added disjointness check on the two arms — turns every tail if/else of
        # a void routine into a guard clause and reverses the arms. It was tried and
        # cost nine of the fourteen byte-identical files at once (TwoWorldsWeather
        # 11838 -> 11658, TwoWorldsContainers 28571 -> 27936, TwoWorldsLights
        # 3511 -> 3501, and Sounds/Music/Enemies/Enemies16/Achievements/Mission_E01
        # with them). The guard clauses of TwoWorldsHeroControl.CheckRessurect
        # (HeroControl.ec:484) stay unrecovered instead; they cost nothing in size,
        # only the jump target differs (end of the ladder instead of the epilogue).
        return not any(s == stop and s != self.n
                       for b in region for s in self.succ[b])

    def _empty_then(self, fi):
        """end of the `if` when the then-part was written out empty, else None.

        An empty then-part still gets the compiler's `jmp` over the else part, so
        it survives as a block with no statement of its own ending in an
        unconditional jump — and that jump names the end of the `if`.  The caller
        must check the target against the join it already has: `if (c) break;`,
        `if (c) continue;` and `if (c) return;` leave exactly the same block shape
        behind and would otherwise be misread (see emit_if)."""
        if not 0 <= fi < self.n:
            return None
        st, ex = self.info[self.order[fi]]
        if _stmts(st) or ex[0] != 'jmp':
            return None
        return self.pos.get(ex[1])

    def _behind(self, j, follow):
        """does `j` lie BEHIND the end of the region being emitted?

        The region stops at `follow`, so a join further along cannot be expressed:
        the arms would have to run past their own end to reach it. Post-dominance
        does not know about the region, and `reach()` pulls the epilogue into every
        scope (a `return` leads there), so `j not in scope` does not catch it —
        RPGCompute's GetHitFightAction has an `if` whose region ends at block 37
        while its post-dominator is the epilogue at 40. Its arms were then emitted
        with stop=40, the transfer to 37 fell outside their scope, and what came out
        was `duplicate` seven times over and twice a `goto`.
        """
        if follow is None or j is None or follow >= self.n or j >= self.n:
            return False
        seen, stack = set(), [follow]
        while stack:
            b = stack.pop()
            if b == j:
                return True
            if b in seen or b >= self.n:
                continue
            seen.add(b)
            stack.extend(self.succ[b])
        return False

    def _leave_word(self, t):
        """the statement that transfers to `t`, when `t` is a loop edge.

        Only `break` and `continue` qualify: a jump to the epilogue is a `return`,
        and that shape is already covered by the guard clause further up (and the
        epilogue is not always a leave — see _leaves_at).

        -> (statements to emit first, word) or None. The increment copy is the same
        one go() makes: written as a `while`, the increment sits at the end of the
        body and a bare `continue` would skip it. emit_body.for_headers hoists the
        copies into the `for` header afterwards, which is what the reference has.
        """
        for L in reversed(self.stack):
            if t == L['exit']:
                return [], 'break;'
            if t in (L['latch'], L['head']):
                return (list(L['inc']) if t == L['latch'] else []), 'continue;'
        return None

    def _leaves_at(self, t, only_epilogue=False):
        """`t` is where a break / continue / return lands, not ordinary code.

        An empty then-part and a `break` / `continue` / `return` then-part leave the
        same block shape behind (no statements, one unconditional jump); the target
        is what tells them apart.

        `only_epilogue` narrows the return case to the routine's value-less
        epilogue. A `ret <value>` block is a `return <expr>;` the source wrote and
        is perfectly ordinary code to end an `if` on — see _skip_else, where the
        skip-else jump of `if (c) { … } else { return 0; } return 0;` lands exactly
        on such a block."""
        if t is None or not 0 <= t < self.n:
            return True
        for L in self.stack:
            if t in (L['exit'], L['latch'], L['head']):
                return True
        st, ex = self.info[self.order[t]]
        if _stmts(st) or ex[0] != 'ret':
            return False
        return not only_epilogue or ex[1] is None or str(ex[1]) == '?'

    def _snapshot(self):
        return set(self.done), set(self.need), collections.Counter(self.stats)

    def _restore(self, snap):
        self.done, self.need, self.stats = snap[0], snap[1], snap[2]

    def emit_if(self, cur, ex, depth, out, follow, scope):
        cond = leaf(ex[1])
        ti = self.pos.get(ex[2])
        fi = cur + 1
        j = self.ipdom[cur]
        if j is None or j >= self.n or (j != follow and j not in scope) \
                or self._behind(j, follow):
            j = follow                       # both arms leave: nothing to join
        if j != follow and any(j in (L['exit'], L['latch'], L['head'])
                               for L in self.stack):
            # An enclosing loop's head / latch / exit is a back edge or a way out,
            # not a join. Taken as the end of the `if`, the arm that reaches it
            # falls silent instead of writing `continue;` / `break;`, and the
            # compiler's skip-else jump then goes to the end of the body rather
            # than to the loop head — same five bytes, different target
            # (TwoWorldsEnemies16.CheckEnemyTrap, Enemies16.ec:252, whose
            # `i++; continue;` arm lost its continue).
            j = follow
            self.stats['if-loop-join-rejected'] += 1
        if ti == j and fi == j:
            # Both arms are the join, yet the compiler still emitted the test and
            # a `je` over nothing — that is an `if` with an empty body, and it
            # costs 8 code bytes that have to be reproduced.  The release build is
            # full of them because `TRACE(...)` expands to `void` outside _DEBUG
            # (Common/Debug.ech:18), so `if (bTrace) { TRACE(…); }` becomes
            # `if (bTrace) { }` (TwoWorldsSounds.ec:577).  Same bytes in
            # roundtrip/mytest5/e7.ec: `and eax,eax / je +6`.
            out.append(('open', depth, f'if ({c_not(cond).render()}) {{'))
            out.append(('close', depth, '}'))
            self.stats['if-empty'] += 1
            return self.go(j, depth, out, follow, scope)
        # The dead jump also says exactly where the else arm ends. Post-dominance
        # cannot: when every arm returns there is no join at all, and the tail after
        # the ladder was swallowed into the last `else` (Mission_E01's Initialize —
        # `return state_2,300;` belongs after the chain, not inside it).
        endif = self._else_end(ti - 1, ti)
        # only the DEAD jump proves the then-arm ended in a terminator; the live
        # skip-else jump found further down proves the opposite (see below)
        dead_jump = endif is not None
        if endif is not None and (endif == follow or endif in scope):
            j = endif
        elif j is not None:
            # `if (a) {…} else if (b) {…} else return false;  return true;` — the
            # two `return true` paths jump to one shared block, the `return false`
            # path skips it, so post-dominance puts the join at the epilogue and
            # the shared block lands inside whichever arm is emitted first. The
            # other arm then repeats it (`return 1;` in the branch instead of
            # falling out of the `if`), which is 5 code bytes per occurrence —
            # TwoWorldsSounds.CommandDebug, Sounds.ec:706. Worth 5 more routines
            # over the round-trip corpus (270 -> 275 of 1630 byte-identical) and
            # the last two routines of TwoWorldsSounds, which is now IDENTICAL.
            shared = self._shared_end(fi, ti, j, scope)
            if shared is not None:
                j = shared
                self.stats['if-shared-end'] += 1
        # An explicitly empty then-part leaves a statement-less block behind whose
        # only exit is the compiler's `jmp` over the else part, and that jump
        # target — not the post-dominator — is where the `if` ends.  Post-dominance
        # cannot find it when the else arm returns: `if (c) { } else return; rest`
        # post-dominates at the epilogue, so `rest` was swallowed into the then-part
        # and came back out as `if (!c) { rest }` (TwoWorldsLights.ShowLight,
        # Lights.ec:72 — 5 bytes short, two jumps folded into one).
        #
        # Taking the target unconditionally is a regression, and that is what an
        # earlier attempt did: `if (c) break;`, `if (c) continue;` and
        # `if (c) return;` leave exactly the same block shape behind, so the rewrite
        # fired on those too (TwoWorldsSounds fell out of IDENTICAL, 18974 -> 18954,
        # and TwoWorldsLights overshot its own reference, 3506 -> 3516 of 3511).
        # They are told apart by where the jump lands: on a loop exit, on a loop
        # head/latch, or on a pure `return` block.  Only a jump into ordinary code
        # is the empty-then shape.
        if endif is None:
            t = self._empty_then(fi)
            if (t is not None and t != j and ti is not None and ti < t < self.n
                    and t in scope and t not in self.done
                    and not self._behind(t, follow)
                    and not self._leaves_at(t)):
                # `not _behind`: the same clamp the post-dominator gets at the top of
                # this method. A target that lies past the end of this region is not
                # the end of the `if` — taking it makes the else arm swallow
                # everything between `follow` and the target, and the code after the
                # region has to be duplicated into the other branch. In
                # RPGCompute.GetDirectFightAction (Unit.ech:1037) that is the whole
                # `if (bStrike) …` tail behind `if (!bInRange) return;`, 155 bytes.
                j = endif = t
                self.stats['if-empty-then-end'] += 1
        if endif is None and ti is not None:
            t = self._skip_else(fi, ti, j, scope)
            # The skip-else jump may aim past the end of this region, and then it is
            # not the end of the `if`: the else arm swallows everything up to it and
            # whatever follows the region has to be duplicated into the other branch
            # (RPGCompute.GetDirectFightAction, Unit.ech:1037 — the whole
            # `if (bStrike) …` tail behind `if (!bInRange) return;`, 155 bytes).
            # Only for a `ret` block, though. `_skip_else` was built for
            # `if (c) { … } else { return 0; } return 0;` (Music.ec:411), where the
            # target is ordinary code that happens to lie behind `follow`; clamping
            # that too cost PQuests, both PQuestsMulti and MissionHorseRacing.
            if t is not None and not (self._behind(t, follow)
                                      and self.info[self.order[t]][1][0] == 'ret'):
                j = endif = t
                self.stats['if-skip-else-end'] += 1
        # no else part at all: the fall-through arm drops into the jump target
        if ti != j and self._falls_into(fi, ti, scope):
            j = ti
            endif = dead_jump = None
            self.stats['if-fallinto'] += 1
        # A fall-through arm that does nothing but leave is the `if (c) return x;`
        # guard the sources are written with, and the difference is not cosmetic:
        # written as `if (c) { return x; } else { rest }` the compiler appends a
        # `jmp` past the else part after the return, five dead bytes per branch
        # that the reference build does not have (measured on
        # roundtrip/mytest2/G1.ec vs G2.ec: 65 vs 85 code bytes for the same
        # three-branch ladder). EarthC also refuses a function whose last
        # statement is not a literal `return <expr>;`, so the ladder form did not
        # even recompile ("Expected return expression", 6 files).
        # The arm has to be emitted to be judged, so the emission state is
        # snapshotted and rolled back when the shape turns out to be something else.
        epi_guard = (endif is None and ti != j and fi != j and fi < self.n
                     and fi not in self.done
                     and self._returns_to_epilogue(fi, ti, j, scope, depth))
        if ti != j and fi != j and fi not in self.done and fi < self.n \
                and endif is None \
                and (self._never_joins(fi, ti, j, scope) or epi_guard):
            snap = self._snapshot()
            probe = []
            self.emit_arm(fi, j, depth + 1, probe, scope)
            if epi_guard and not self._ends_with_exit(probe):
                # The arm reaches the epilogue, which is this region's `follow`, so
                # go() stops without writing the `return;` the source has there.
                # Leaving it out is not only 5 bytes short — the guards of
                # TwoWorldsHeroControl.CheckRessurect assign bRessurect and would
                # then fall into the next test.
                txt = self._exit_text(j)
                if txt is not None:
                    probe.append(('raw', depth + 1, txt))
                    self.stats['guard-return-restored'] += 1
            if self._has_code(probe) and not any(
                    k == 'raw' and 'goto' in str(p) for k, _, p in probe):
                out.append(('open', depth, f'if ({c_not(cond).render()}) {{'))
                out += probe
                out.append(('close', depth, '}'))
                self.stats['guard'] += 1
                return self.go(ti, depth, out, follow, scope)
            self._restore(snap)
        # NOT DONE: reading `_empty_then(fi) == j` with `j` a loop edge as the guard
        # `if (c) continue;` instead of `if (!c) { } else { rest }`. It is the right
        # reading for PQuests.CheckMarkerQuestGivers — there the reference's jcc goes
        # STRAIGHT to the latch while the else form puts the empty arm's jump in
        # between, so a merged condition targets that instead (same size, different
        # jump targets). But the else form is what the sources write elsewhere, and
        # emitting the guard whenever the shape allows it costs more than it wins:
        # TwoWorldsSounds fell out of byte-identical (18974 -> 19001) and PQuests
        # itself went from ±0 to +28. Telling the two apart needs more than the shape
        # of the fall-through arm.
        else_ = []
        self.emit_arm(ti, j, depth + 1, else_, scope)
        if j == follow >= self.n and fi != j and endif is None \
                and self._is_exit_arm(else_):
            # Guard clause: `if (c) return;` instead of wrapping the remainder of
            # the region in the opposite branch. It has to stay restricted to the
            # tail of the routine (follow == the virtual exit), and that is not
            # cosmetic in either direction:
            #   * at the tail it is required — EarthC rejects a function whose last
            #     statement is not a literal `return <expr>;`, and the if/else form
            #     ends the routine with a closing brace ("Expected return
            #     expression", TwoWorldsMusic);
            #   * anywhere else it is wrong, because it turns the layout around.
            #     The compiler puts the fall-through arm first and the jump target
            #     second; the guard form emits the jump target's code first, so the
            #     two arms come back in the opposite order. Restricting it here is
            #     worth 10 code bytes in TwoWorldsMusic (19572 -> 19582 of 19606)
            #     and nothing anywhere else in the corpus.
            # `endif is None` on top of that: when `_else_end` did find the dead jump
            # the source demonstrably wrote an `else`, and the guard form reverses
            # the two arms. It is kept even when the target lies outside the region
            # and cannot be used as the join — the mere existence of the jump is the
            # evidence (TwoWorldsMusic.StateCheck_GetMapType, Music.ec:505:
            # `else if (…) return eMusicDeadLand; else return eMusicForest;` came
            # out as `if (!…) return 0; return 2;`, 10 code bytes short).
            out.append(('open', depth, f'if ({cond.render()}) {{'))
            out += else_
            out.append(('close', depth, '}'))
            self.stats['guard'] += 1
            return self.go(fi, depth, out, follow, scope)
        then_ = []
        self.emit_arm(fi, j, depth + 1, then_, scope)
        if dead_jump and then_ and not self._ends_with_exit(then_) \
                and any(s == j for b in self.reach(fi, j, scope)
                        for s in self.succ[b]):
            # `_else_end` found its dead jump, so the then-arm provably ended in a
            # `return` in the source — that is the only reason the jump over the
            # else part is unreachable.  The arm loses it whenever the end of the
            # `if` happens to be the epilogue block itself: `go()` sees the arm
            # reach its own `follow` and stops, and the `return;` never gets
            # written (TwoWorldsWeather.ChangeWeather, Weather.ec:110 —
            # `if (nWeather == eWeatherForest) { …; return; } else if (…)`, whose
            # else ladder runs to the end of the function).  The reference then
            # has two `jmp <epilogue>` in a row, the recompile only one.
            #
            # The arm has to be able to fall out at all: a dead jump also follows an
            # arm that ends in a nested ladder whose every branch returns, and there
            # the source wrote no `return` of its own — appending one cost 10 bytes
            # in TwoWorldsMusic.StateCheck_GetMapType. `succ` says which: no edge to
            # `j` anywhere in the arm means every path already leaves.
            txt = self._exit_text(j)
            if txt is not None:
                then_.append(('raw', depth + 1, txt))
                self.stats['arm-return-restored'] += 1
        has_then, has_else = self._has_code(then_), self._has_code(else_)
        if has_then and has_else:
            out.append(('open', depth, f'if ({c_not(cond).render()}) {{'))
            out += then_
            out.append(('close', depth, '} else {'))
            out += else_
            out.append(('close', depth, '}'))
            self.stats['if-else'] += 1
        elif has_then:
            out.append(('open', depth, f'if ({c_not(cond).render()}) {{'))
            out += then_
            if not self._ends_with_exit(then_) and self._empty_else(fi, ti, j, scope):
                out.append(('close', depth, '} else {'))
                out.append(('close', depth, '}'))
                self.stats['if-empty-else'] += 1
            else:
                out.append(('close', depth, '}'))
            self.stats['if'] += 1
        elif has_else and self._empty_then(fi) == j:
            # An explicitly empty then-part is not the same program text as the
            # inverted `if (!c)`: `if (c) { } else { X }` compiles to
            # `je X / jmp end`, `if (!c) { X }` to a single `jne end`
            # (roundtrip/mytest5/e1.ec 45 code bytes vs e2.ec 40).  The empty arm
            # survives in the binary as a statement-less block whose only exit is
            # the jump past the else part, and that is what is tested here.
            # The sources really are written that way — TwoWorldsSounds.ec:64
            # `if (nMultiplayer) { /* commented out */ } else { … }`.
            out.append(('open', depth, f'if ({c_not(cond).render()}) {{'))
            out.append(('close', depth, '} else {'))
            out += else_
            out.append(('close', depth, '}'))
            self.stats['if-empty-then'] += 1
        elif has_else:
            out.append(('open', depth, f'if ({cond.render()}) {{'))
            out += else_
            out.append(('close', depth, '}'))
            self.stats['if'] += 1
        else:
            # Both arms came out empty, but the test itself is real code. Outside
            # _DEBUG `TRACE(...)` expands to nothing, so
            # `if (nFlag == 0) { TRACE(a); } else { TRACE(b); }`
            # (Network/MissionCommon.ech:1207) leaves the compare, the `jne` and the
            # skip-else `jmp` behind — 19 code bytes. Dropping the whole `if` made
            # __PrintTraceHeader an empty function in all three MissionTeam* files
            # and MissionHorseRacing.
            # Emitting a bare `if (c) { }` whenever both arms come out empty is far
            # too much (MissionTeamDeathmatch -381 -> +195): most of those tests are
            # already accounted for elsewhere. Only the exact shape counts — the
            # else target IS the join and the fall-through arm is nothing but the
            # skip-else jump to it.
            if ti == j and self._empty_then(fi) == j and fi not in self.elsejmp:
                out.append(('open', depth, f'if ({c_not(cond).render()}) {{'))
                out.append(('close', depth, '} else {'))
                out.append(('close', depth, '}'))
                # claim the jump, exactly as _empty_else does: the enclosing `if`
                # ends on the same instruction, and reading it a second time gave
                # the outer one an `else { }` of its own — one `jmp` too many at the
                # tail of PQuests' DoQuestAction ladder (`else if (nType == 213) {}`
                # is the innermost claimant).
                self.elsejmp.add(fi)
                self.stats['if-empty-both'] += 1
            else:
                self.stats['if-empty'] += 1
        return self.go(j, depth, out, follow, scope)

    def _drop_tail_continue(self, body, inc):
        """strip `continue;` from the end of a loop body — it is a dead jump.

        Control reaches the latch from there anyway, and the compiler's own back
        edge is the only jump the reference has. The statement turns up where the
        tail of the body sits inside a branch arm whose `follow` is not the latch,
        so go() cannot see the fall-through: TwoWorldsEnemies16.CheckEnemyTrap
        (Enemies16.ec:246) came out with two of them, 10 code bytes.

        `go()` copies the loop increment in front of a `continue` (a `while`-shaped
        emission puts the increment at the end of the body, so a bare jump would
        skip it). When the `continue` goes, that copy has to go with it, otherwise
        the increment is emitted twice.
        """
        # Only a copy made of compiler spill temporaries may be removed with the
        # `continue`. A real increment cannot be told apart from the same statement
        # emitted as the body's own last line, and deleting that loses code — so
        # such a `continue` is left alone rather than risk it.
        n = len(inc)
        droppable = bool(n) and all(
            s[0] == 'assign' and str(s[1]).startswith('$') for s in inc)
        while True:
            k = len(body) - 1
            while k >= 0 and body[k][0] == 'close' and str(body[k][2]) == '}':
                k -= 1
            if k < 0 or body[k][0] != 'raw' or str(body[k][2]) != 'continue;':
                return
            has_copy = n and k - n >= 0 and all(
                body[k - n + i][0] == 'stmt' and body[k - n + i][2] is inc[i]
                for i in range(n))
            if has_copy and not droppable:
                return
            body.pop(k)
            self.stats['continue-dropped'] += 1
            if has_copy:
                del body[k - n:k]

    def emit_loop(self, L, depth, out, scope):
        span = {b for b in range(L.span[0], L.span[1] + 1) if b in scope}
        b0, b1 = L.body
        latch_st = _stmts(self.info[self.order[b1]][0]) if 0 <= b1 < self.n else []
        head_ok = (L.kind in ('for', 'while', 'head') and L.cond is not None
                   and L.cond_end == L.head)
        # The header block does not only test — `for (i = 0; i < a.GetSize(); i++)`
        # compiles the bound into a temporary that is recomputed before every
        # test.  Hoisting only the test would leave that assignment out and the
        # condition would read a stale value, so the header statements are
        # emitted once in front of the loop and once at the end of each pass.
        head_st = _stmts(self.info[self.order[L.head]][0]) if head_ok else []
        # `for` and `while` are not interchangeable in the binary: the compiler's
        # iteration watchdog (`sub edi,1 / je stub`) sits directly behind the
        # condition in a `for` and at the end of the body in a `while`.  Measured
        # on roundtrip/mytest5/e3.ec (for) vs e4.ec (while): same 108 code bytes,
        # watchdog at offset 46 resp. 61.  loops.py already tells the two apart
        # from that position, so a `for` has to be written as a `for`.
        # Nothing has to be moved into the header for that, though: init and
        # increment are emitted where the compiler put them and only the keyword
        # changes.  `for (i = 0; c; i++) {b}`, `for (; c; i++) {b}` (init hoisted)
        # and `for (; c;) {b; i++;}` (increment left in the body) all produce the
        # same bytes — verified in roundtrip/mytest5: e3.eco == e5.eco == e6.eco,
        # 1095 body bytes each.  So the `continue` fix-up below stays valid too,
        # because in the emitted form the increment really is at the end of the
        # body, exactly as in the `while` form.
        as_for = L.kind == 'for' and head_ok
        ctx = {'exit': L.exit, 'latch': b1, 'head': L.head,
               'inc': (latch_st + head_st
                       if L.kind in ('for', 'while', 'head') else [])}
        if L.kind == 'dowhile':
            out.append(('open', depth, 'do {'))
        elif head_ok:
            # the head test is the loop condition; the loop runs while it fails
            for s in self.info[self.order[L.head]][0]:
                out.append(('stmt', depth, s))
            cond_txt = c_not(leaf(L.cond)).render()
            out.append(('open', depth, f'for (; {cond_txt};) {{'
                        if as_for else f'while ({cond_txt}) {{'))
            self.done.add(L.head)
            if head_st:
                self.stats['loop-head-stmts'] += 1
        else:
            # condition spread over blocks that carry statements: keep them in
            # the body, where the exiting test turns into `if (…) break;`
            out.append(('open', depth, 'while (true) {'))
            if L.kind != 'infinite':
                self.stats['loop-cond-unfolded'] += 1
                b0 = L.head
        self.stats[f'loop-{L.kind}'] += 1
        self.stack.append(ctx)
        self.open.add(L.span[0])          # a do-while body starts at the header
        body = []
        if 0 <= b0 < self.n and b0 != b1:
            self.emit_seq(b0, b1, depth + 1, body, self.reach(b0, b1, span))
        # before the latch and header statements go in: at this point the end of
        # `body` really is the end of the loop body
        self._drop_tail_continue(body, ctx['inc'])
        if 0 <= b1 < self.n and b1 not in self.done:
            self.done.add(b1)                          # latch: increment / test
            for s in self.info[self.order[b1]][0]:
                body.append(('stmt', depth + 1, s))
        for s in (self.info[self.order[L.head]][0] if head_st else []):
            body.append(('stmt', depth + 1, s))        # recompute the test inputs
        self.stack.pop()
        self.open.discard(L.span[0])
        out += body
        if L.kind == 'dowhile':
            out.append(('close', depth, f'}} while ({leaf(L.cond).render()});'))
        else:
            out.append(('close', depth, '}'))
        self.done.update(span)

    # -------------------------------------------------------------- driver
    def run(self, base=0):
        out = self._pass(base)
        if any(k == 'raw' and 'goto' in str(p) for k, _d, p in out)                 and self.allow_loop_leave:
            # The wider `_falls_into` reading (a `break` / `continue` counts as
            # leaving the arm) is what lets the shared tail behind an `if` stay
            # outside it — Units/Hero.ech FindBestTarget, +341 code bytes. In three
            # of the 7247 corpus routines it instead produces an unstructurable
            # region, so those are simply structured the old way rather than losing
            # them from emit_survey (6925 -> 6922 without this).
            self.allow_loop_leave = False
            out = self._pass(base)
            self.stats['fallinto-narrowed'] += 1
        if self.need:
            # second pass, now that the goto targets needing a label are known
            self.labels = set(self.need)
            out = self._pass(base)
        missed = [i for i in range(self.n)
                  if i not in self.done and i not in self.stubs
                  and _emittable(self.info[self.order[i]][0])]
        if missed:
            # never drop code: blocks no region reached keep a label of their own
            self.stats['orphan-block'] += len(missed)
            for i in missed:
                out.append(('raw', base, f'L_{self.order[i]}:'))
                for s in self.info[self.order[i]][0]:
                    out.append(('stmt', base, s))
        flatten_else_if(out)
        self.stats['blocks'] = self.n
        self.stats['depth'] = max((d for _, d, _ in out), default=0)
        self.stats['merged-conditions'] = self.merged
        return out

    def _pass(self, base):
        self.need, self.done, self.stack, self.open = set(), set(), [], set()
        self.elsejmp = set()
        self.dupdepth = 0
        self.stats = collections.Counter()
        out = []
        self.emit_seq(0, self.n, base, out, set(range(self.n)))
        return out


def structure(lf, r, order, info, pos=None, base=0):
    """Emit the routine's block graph as structured code.

    Drop-in for `Lifter.structure()`: same statement tuples, same nesting
    convention, `base` is the indentation the caller starts at (1 for
    `routine_text`, 0 for `emit_body`)."""
    return Structurer(lf, order, info, pos).run(base)


def structure_ex(lf, r, order, info, pos=None, base=0):
    """structure() plus the analysis counters, for measurement"""
    s = Structurer(lf, order, info, pos)
    return s.run(base), s.stats


# ------------------------------------------------------------------ standalone
def _render(out):
    L = []
    for kind, depth, payload in out:
        pad = '    ' * depth
        if kind == 'stmt':
            if payload[0] == 'hookmark':
                continue
            if payload[0] == 'assign':
                L.append(f'{pad}{payload[1]} = {payload[2]};')
            elif payload[0] == 'expr':
                L.append(f'{pad}{payload[1]};')
            elif payload[0] == 'raw2':
                L.append(f'{pad}{payload[1]}')
        else:
            L.append(f'{pad}{payload}')
    return L


def dump(path, only=None):
    import lifter
    lf = lifter.Lifter(path)
    for r in lf.routines:
        if only and r['name'] != only:
            continue
        order, info, pos, _blocks = _loops._graph(lf, r)
        out, stats = structure_ex(lf, r, order, info, pos, base=1)
        print(f'{r["name"]}()   {dict(stats)}')
        print('{')
        for line in _render(out):
            print(line)
        print('}\n')


if __name__ == '__main__':
    import sys
    dump(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
