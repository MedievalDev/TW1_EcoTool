"""Byte-exact parser for the .eco debug section (EarthC / Two Worlds 1).

  BLOB = STR mainSource
         u32 nFiles, u32 0
         nFiles x STR                    includes + the .ec itself, compile order
         u32 0,0,0,0
         u32 nRoutines, u32 0
         nRoutines x ROUTINE
         u32 nConsts
         nConsts x (STR name, u32 value)     ends exactly at EOF

  ROUTINE = STR name, u32 kind(5|6|7), u32 a, u32 codeStart, u32 codeEnd,
            u32 nParams, u32 b
            nParams x RECORD                 parameters
            u32 nLocals, u32 c ; nLocals x RECORD
            u32 nLines,  u32 d ; nLines  x LINE
            u32 nRefs,   u32 e ; nRefs   x RECORD   (addr = call site code offset)

  RECORD  = STR name, u32 kind, u32 sub, u32 dim,
            (kind==0 ? STR typeName : u32 typeRef),
            u32 flags, u32 addr
  LINE    = u32 fileIndex, u32 sourceLine, u32 codeOffset, u32 x, u32 flag
"""
import struct, sys, pathlib
import eco

ROUTINE_KINDS = (5, 6, 7)


class Err(Exception):
    pass


class P:
    def __init__(self, b):
        self.b, self.o = b, 0

    def u32(self):
        if self.o + 4 > len(self.b):
            raise Err(f'eof at 0x{self.o:x}')
        v = struct.unpack_from('<I', self.b, self.o)[0]
        self.o += 4
        return v

    def s(self):
        o0 = self.o
        n = self.u32()
        if n > 250 or self.o + n > len(self.b):
            raise Err(f'bad str len {n} at 0x{o0:x}')
        v = self.b[self.o:self.o + n]
        self.o += n
        if not all(32 <= c < 127 for c in v):
            raise Err(f'non-ascii str at 0x{o0:x}: {v[:20]!r}')
        return v.decode('latin1')

    @property
    def left(self):
        return len(self.b) - self.o


def record(p):
    """name, kind, sub, typeIdx, (user type ? STR typeName : u32), flags, addr

    A type name follows whenever the type is user-defined: kind 0 directly, or kind 6
    (array) whose element kind is 0. Deciding this by `tidx != -1` instead happened to
    work on the 107 original files — every kind-0 record there had a type index — but
    breaks on a debug build of RPGCompute, where `pChangedParams` is kind 0 with
    tidx = -1 and is still followed by the name "object".
    """
    o0 = p.o
    r = dict(o=o0, name=p.s(), kind=p.u32(), sub=p.u32(), tidx=p.u32())
    user_type = r['kind'] == 0 or (r['kind'] == 6 and r['sub'] == 0)
    r['type'] = p.s() if user_type else p.u32()
    r['flags'], r['addr'] = p.u32(), p.u32()
    return r


def counted(p, fn, what, limit=200000):
    n, extra = p.u32(), p.u32()
    if n > limit:
        raise Err(f'{what}: count {n} at 0x{p.o - 8:x}')
    return [fn(p) for _ in range(n)], extra


def line(p):
    return tuple(p.u32() for _ in range(5))


def is_const_table(p):
    """True if 'u32 n + n x (STR,u32)' from here consumes exactly to EOF."""
    save = p.o
    try:
        n, _extra = p.u32(), p.u32()
        if not (0 < n < 200000):
            return False
        for _ in range(n + 1):          # + the trailing script-class record
            p.s()
            p.u32()
        return p.left == 0
    except Err:
        return False
    finally:
        p.o = save


def routine(p, csize, expect=None):
    o0 = p.o
    name, kind = p.s(), p.u32()
    a, start, end, npar, b = (p.u32() for _ in range(5))
    if not (start <= end <= csize) or npar > 256:
        raise Err(f'{name}: head ({kind},{a},{start},{end},{npar},{b}) at 0x{o0:x}')
    if expect is not None and start != expect:
        raise Err(f'{name}: codeStart {start} != expected {expect} at 0x{o0:x}')
    r = dict(o=o0, name=name, kind=kind, a=a, b=b, start=start, end=end,
             params=[record(p) for _ in range(npar)])
    r['locals'], r['c'] = counted(p, record, f'{name}.locals', 4096)
    r['lines'], r['d'] = counted(p, line, f'{name}.lines')
    r['refs'], r['e'] = counted(p, record, f'{name}.refs', 65536)
    return r


def parse_blob(tail, csize):
    p = P(tail)
    out = dict(main=p.s())
    nf, out['z_files'] = p.u32(), p.u32()
    out['files'] = [p.s() for _ in range(nf)]
    out['pad'] = [p.u32() for _ in range(4)]
    nent, out['z_sym'] = p.u32(), p.u32()
    out['nEntries'] = nent
    out['routines'] = []
    expect = 0
    for _ in range(nent):
        r = routine(p, csize, expect)
        out['routines'].append(r)
        expect = r['end'] + 1
    out['globals'], out['z_glob'] = counted(p, record, 'globals', 65536)
    out['triples'], out['z_trip'] = counted(
        p, lambda q: tuple(q.u32() for _ in range(3)), 'triples')
    out['extra'] = []                        # anything still unaccounted for
    while not is_const_table(p):
        lst, ex = counted(p, record, 'trailing', 65536)
        out['extra'].append((lst, ex))
    n, out['z_const'] = p.u32(), p.u32()
    out['consts'] = [(p.s(), p.u32()) for _ in range(n)]
    out['script_class'] = (p.s(), p.u32())
    out['used'], out['left'] = p.o, p.left
    return out


def parse_file(path):
    f = eco.parse(path)
    if not f['has_debug']:
        return f, None
    return f, parse_blob(f['tail'], len(f['code']))


if __name__ == '__main__':
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '../eco/Scripts_wd')
    for p in ([root] if root.is_file() else sorted(root.rglob('*.eco'))):
        f = eco.parse(p)
        if not f['has_debug']:
            continue
        try:
            b = parse_blob(f['tail'], len(f['code']))
        except Err as ex:
            print(f'{p.name:<22} FAILED: {ex}')
            continue
        R = b['routines']
        holes = sum(1 for i in range(1, len(R))
                    if R[i]['start'] != R[i - 1]['end'] + 1)
        print(f'{p.name:<22} tail={len(f["tail"]):>7} left={b["left"]:>3} '
              f'ent={b["nEntries"]:>4} rout={len(R):>4} holes={holes:>2} '
              f'glob={len(b["globals"]):>3} trip={len(b["triples"]):>5} '
              f'xtra={[len(l) for l, _ in b["extra"]]} '
              f'par={sum(len(r["params"]) for r in R):>5} '
              f'loc={sum(len(r["locals"]) for r in R):>6} '
              f'lines={sum(len(r["lines"]) for r in R):>6} '
              f'refs={sum(len(r["refs"]) for r in R):>6} '
              f'const={len(b["consts"]):>5} class={b["script_class"]}')
