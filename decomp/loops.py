"""Loop recognition on the lifted block graph.

`lifter.structure()` rebuilds if / if-else but turns every back edge into
`goto L_x;   // back edge`, which EarthC cannot express. This module is the
missing analysis half: given the same (order, info, pos) that `structure()`
works on, it reports which block ranges are loops and which EarthC loop form
each one is. It only recognises and returns — it changes nothing.

What the compiler emits
-----------------------
The EarthC code generator is a template expander, so every loop form has one
fixed block layout.  Every loop additionally carries a counter watchdog —
`sub edi, 1 / je W`, where edi is the per-routine iteration budget
(`mov edi, [esi+4]`, the `$Loop` VM field) and W is a stub that reports the
runaway loop through native 0x02f5 / 0x02f8, reloads edi and jumps to the
routine epilogue.  Debug and release builds both emit it: across the 107-file
corpus all 3532 detected loops had one, and the block this module picks as the
guard was the block really holding `sub edi,1 / je` in every single case
(`py loops.py --survey ../eco`).  The stub is a block with no statement of its
own ending in an unconditional jump, and its *position* is what separates
`for` from `while`:

    for (init; cond; inc) body        while (cond) body

      init                             head: cond ; jcc  -> exit
    head: cond ; jcc  -> exit          body ...
      sub edi,1 ; je -> W                sub edi,1 ; je -> W
      body ...                         latch: jmp -> head
    latch: inc ; jmp -> head           W:     <watchdog>
    W:    <watchdog>                   exit:
    exit:

    do body while (cond)

    head: body ...
      sub edi,1 ; je -> W
      cond ; jcc -> head               <- back edge is CONDITIONAL
      jmp -> exit
    W:    <watchdog>
    exit:

So: unconditional back edge + a test that leaves the region  => head-controlled
(`for` when the watchdog test sits directly behind the condition and that block
carries no statements of its own, `while` when it sits at the end of the body);
conditional back edge => `do while`; no exiting test at all => infinite loop
(`while (true)`).

A loop condition may be spread over several blocks (`&&` / `||` short-circuit,
and even a plain `a < b` sometimes compiles into a two-block sequence), so the
head test is searched as a chain of blocks starting at the header rather than in
the header block alone.

Recognised
----------
  'for'       head-controlled, watchdog test directly behind the condition
  'while'     head-controlled, watchdog test at the end of the body
  'dowhile'   conditional back edge (bottom-controlled), incl. `&&` conditions
  'infinite'  unconditional back edge, no test that leaves the region
              (`while (true)` / `for (;;)`) — supported but not present in the
              corpus, every `while (true)` there had a reachable exit test
  'head'      head-controlled, but the watchdog stub was not where it should be,
              so for/while stayed undecided.  Does not occur in the corpus;
              emitting `while` is sound unless the body contains a `continue`
              (see Loop.continues) jumping to a latch that still holds the
              increment — then the `for` form is required.

NOT recognised (Loop.kind is None, caller must keep its goto fallback)
----------------------------------------------------------------------
  * irreducible / multi-entry loops — a jump from outside the region into the
    middle of it.  None were found in the corpus, but the check is kept.
  * two back edges to the same header that are not nested (they are merged into
    one loop using the last latch; if the result is not closed, kind is None).
  * `switch`-like block fans — EarthC has no switch, so none exist.
  * loops whose header is not the lowest block of the region (the code generator
    never emits those, so no support was written).

`break` and `continue` are not loop forms of their own; they are reported as
block indices on the Loop (jumps to the exit resp. to the latch/header).

  py loops.py <file.eco> [routine]     dump the loops of a file
  py loops.py --survey <dir>           corpus statistics + invariant check
"""
import collections

Loop = collections.namedtuple(
    'Loop',
    'kind head cond_end latch exit guard watchdog span body cond breaks continues')
Loop.__doc__ = """One recognised loop, all fields are indices into `order`.

  kind      'for' | 'while' | 'dowhile' | 'infinite' | 'head' | None
  head      first block of the loop (the back edge target)
  cond_end  last block of the head test chain (head-controlled only, else None)
  latch     block carrying the back edge
  exit      block where control continues after the loop, None if unknown
  guard     block with the compiler's loop-counter test, None in release builds
  watchdog  the counter-overflow stub, None in release builds
  span      (first, last) inclusive block range the loop occupies in layout
            order — the caller must not emit these as ordinary blocks
  body      (first, last) inclusive block range of the user's loop body
  cond      exit condition expression of the head test if it is a single block
            (loop runs while NOT cond), resp. the back-edge condition of a
            do-while (loop repeats while cond); None when spread over blocks
  breaks    body blocks that jump straight to `exit`      (`break`)
  continues body blocks that jump straight to `latch`/`head` (`continue`)
"""


def _target(exit_):
    """branch target address of a block terminator, None for fall/ret"""
    if exit_[0] == 'jmp':
        return exit_[1]
    if exit_[0] == 'jcc':
        return exit_[2]
    return None


def back_edges(order, info, pos=None):
    """[(latch index, head index, 'jmp'|'jcc')] — terminators aiming backwards"""
    if pos is None:
        pos = {a: i for i, a in enumerate(order)}
    out = []
    for i, a in enumerate(order):
        ex = info[a][1]
        t = _target(ex)
        if t is None:
            continue
        j = pos.get(t)
        if j is not None and j <= i:
            out.append((i, j, ex[0]))
    return out


def _empty(info, order, i):
    """block without user statements (hook markers do not count)"""
    if not 0 <= i < len(order):
        return False
    return not [s for s in info[order[i]][0] if s[0] != 'hookmark']


def _is_stub(order, info, i):
    """watchdog shape: a single unconditional jump out, carrying at most the
    call that reports the runaway loop (hidden as compiler-internal in most
    builds, lifted as `native_02f8(<line>)` in a few)"""
    if not 0 <= i < len(order) or info[order[i]][1][0] != 'jmp':
        return False
    st = [s for s in info[order[i]][0] if s[0] != 'hookmark']
    return len(st) <= 1 and all(s[0] == 'expr' for s in st)


def _multi_entry(order, info, pos, head, last):
    """True if some block outside [head, last] jumps INTO the region body.

    Fall-through cannot enter the middle of a contiguous range from outside, so
    only explicit branch targets are checked."""
    for i, a in enumerate(order):
        if head <= i <= last:
            continue
        t = _target(info[a][1])
        j = pos.get(t) if t is not None else None
        if j is not None and head < j <= last:
            return True
    return False


def find_loops(order, info, pos=None):
    """Recognise the loops of one routine's block graph.

    order / info / pos are exactly what `lifter.structure()` receives:
    order = block start addresses in layout order, info[addr] = (statements,
    exit) with exit in ('fall',) | ('jmp', t) | ('jcc', cond, t, fall) |
    ('ret', v), pos = address -> index.

    Returns the loops sorted by span, outermost first, so a caller can nest
    them directly.  A loop whose shape was not understood is returned with
    kind None instead of being dropped, so the caller can fall back to goto.
    """
    if pos is None:
        pos = {a: i for i, a in enumerate(order)}
    n = len(order)

    # one natural loop per header; a second back edge to the same header (a
    # `continue` compiled as a direct jump home) only extends the latch
    per_head = {}
    for latch, head, kind in back_edges(order, info, pos):
        cur = per_head.get(head)
        if cur is None or latch > cur[0]:
            per_head[head] = (latch, kind)

    found = [_build(order, info, pos, head, latch, edge, n)
             for head, (latch, edge) in sorted(per_head.items())]
    # a nested loop's watchdog stub also jumps forward and can land on the outer
    # loop's exit — that is compiler bookkeeping, not a `break`
    stubs = {L.watchdog for L in found if L.watchdog is not None}
    found = [L._replace(breaks=[b for b in L.breaks if b not in stubs],
                        continues=[c for c in L.continues if c not in stubs])
             for L in found]
    found.sort(key=lambda L: (L.span[0], -L.span[1]))
    return found


def _build(order, info, pos, head, latch, edge, n):
    guard = watchdog = exit_ = cond_end = None
    cond = None
    kind = None

    if edge == 'jcc':
        # ---- bottom-controlled: latch jumps back when the condition holds
        cond = info[order[latch]][1][1]
        # layout after the latch: [jmp exit] [watchdog] exit
        if latch + 1 < n and info[order[latch + 1]][1][0] == 'jmp':
            exit_ = pos.get(info[order[latch + 1]][1][1])
        # exit three blocks on means the watchdog stub sits in between
        if exit_ == latch + 3 and _is_stub(order, info, latch + 2):
            watchdog = latch + 2
        last = watchdog if watchdog is not None else latch + 1
        if exit_ is None:
            exit_ = last + 1 if last + 1 < n else None
        kind = 'dowhile'
        body = (head, latch)
    else:
        # ---- unconditional back edge: head-controlled or infinite.
        # walk the head test chain: blocks that only branch inside the region
        # until one of them branches past the latch
        for j in range(head, latch + 1):
            ex = info[order[j]][1]
            t = _target(ex)
            k = pos.get(t) if t is not None else None
            if k is not None and k > latch:
                cond_end, exit_ = j, k
                if ex[0] == 'jcc':
                    cond = ex[1]
                break
            if ex[0] == 'ret' or (k is not None and not head <= k <= latch):
                break                     # not a plain condition chain any more
        if cond_end is None:
            kind, body = 'infinite', (head, latch)
            last = latch
            exit_ = None
        else:
            kind, body = 'head', (cond_end + 1, latch)
            last = latch
        # the head test already told us where the loop ends; the watchdog stub
        # is exactly the block that sits between the latch and that exit
        if kind == 'head':
            if exit_ == latch + 2 and _is_stub(order, info, latch + 1):
                watchdog = last = latch + 1
        elif _is_stub(order, info, latch + 1):
            watchdog = last = latch + 1
            exit_ = pos.get(info[order[latch + 1]][1][1])

    # the guard is the block inside the region whose conditional jump goes to
    # the watchdog stub
    if watchdog is not None:
        wa = order[watchdog]
        for j in range(head, latch + 1):
            ex = info[order[j]][1]
            if ex[0] == 'jcc' and ex[2] == wa:
                guard = j
                break

    if kind == 'head':
        if guard is None:
            pass                                   # release build: undecidable
        elif guard == cond_end + 1 and _empty(info, order, guard):
            kind = 'for'
            body = (guard + 1, latch)
        else:
            kind = 'while'
            body = (cond_end + 1, latch)

    span = (head, last)
    if _multi_entry(order, info, pos, head, last):
        kind = None

    ea = order[exit_] if exit_ is not None and exit_ < n else None
    breaks, continues = [], []
    for j in range(body[0], body[1] + 1):
        if not 0 <= j < n or j == latch:
            continue
        ex = info[order[j]][1]
        if ex[0] != 'jmp':
            continue
        if ea is not None and ex[1] == ea:
            breaks.append(j)
        elif ex[1] in (order[latch], order[head]):
            continues.append(j)
    return Loop(kind, head, cond_end, latch, exit_, guard, watchdog,
                span, body, cond, breaks, continues)


def regions(order, info, pos=None):
    """{(first block index, last block index): kind} — the plain answer to
    "which range is which loop form"."""
    return {L.span: L.kind for L in find_loops(order, info, pos)}


# --------------------------------------------------------------------- report
def _graph(lf, r):
    """the same block graph lifter.routine_text builds, without the emission"""
    import arity4
    names = lf.frame(r)
    insns = lf.decode(r)
    skip = {insns[i][1] for i in arity4.mark_saves(insns)}
    blocks = lf.split_blocks(insns, r)
    order = [b[0][1] for b in blocks]
    preds = lf.block_edges(blocks, order)
    info, cs, cr = {}, None, None
    for k, blk in enumerate(blocks):
        st, exit_, os_, or_ = lf.run_block(blk, names, skip, cs, cr)
        nxt = order[k + 1] if k + 1 < len(order) else None
        if exit_[0] == 'fall' and nxt is not None and preds[nxt] == 1:
            cs, cr = os_, or_
        else:
            lf.flush_regs(st, or_)
            cs = cr = None
        info[order[k]] = (st, exit_)
    return order, info, {a: i for i, a in enumerate(order)}, blocks


def _guard_truth(blocks):
    """indices of blocks ending in the real `sub edi,1 / je` — ground truth for
    the structural watchdog detection above"""
    out = set()
    for k, blk in enumerate(blocks):
        for j in range(len(blk) - 1):
            m, a, s, ins = blk[j]
            if (m == 'sub' and ins is not None and ins.op_str == 'edi, 1'
                    and blk[j + 1][0] == 'je'):
                out.add(k)
    return out


def dump(path, only=None):
    import lifter
    lf = lifter.Lifter(path)
    for r in lf.routines:
        if only and r['name'] != only:
            continue
        order, info, pos, blocks = _graph(lf, r)
        L = find_loops(order, info, pos)
        if not L:
            continue
        print(f'=== {r["name"]}  {len(order)} blocks')
        for l in L:
            print(f'   {str(l.kind):<9} head #{l.head} cond_end {l.cond_end} '
                  f'latch #{l.latch} exit #{l.exit} guard {l.guard} '
                  f'watchdog {l.watchdog} span {l.span} body {l.body} '
                  f'break {l.breaks} continue {l.continues}')
            if l.cond is not None:
                print(f'             cond: {l.cond}')


def survey(root):
    import pathlib, lifter
    kinds = collections.Counter()
    bad = collections.Counter()
    rout_total = rout_loop = rout_all_ok = 0
    for p in sorted(pathlib.Path(root).rglob('*.eco')):
        try:
            lf = lifter.Lifter(p)
        except Exception as ex:
            print(f'   (skipped {p.name}: {ex})')
            continue
        for r in lf.routines:
            rout_total += 1
            order, info, pos, blocks = _graph(lf, r)
            L = find_loops(order, info, pos)
            if not L:
                continue
            rout_loop += 1
            if all(l.kind is not None for l in L):
                rout_all_ok += 1
            truth = _guard_truth(blocks)
            for l in L:
                kinds[l.kind] += 1
                # invariant checks against the instruction level
                g = [j for j in truth if l.head <= j <= l.latch]
                if l.guard is not None and l.guard not in truth:
                    bad['watchdog found but no sub edi,1 in that block'] += 1
                if l.guard is None and g and l.watchdog is not None:
                    bad['guard missed'] += 1
                if l.exit is not None and not l.span[1] < l.exit:
                    bad['exit not behind span'] += 1
    print(f'{root}: {rout_total} routines, {rout_loop} with loops, '
          f'{rout_all_ok} where every loop was recognised')
    for k, v in kinds.most_common():
        print(f'   {str(k):<10} {v}')
    print('   invariant violations:', dict(bad) or 'none')


if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['--survey']:
        survey(sys.argv[2])
    else:
        dump(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
