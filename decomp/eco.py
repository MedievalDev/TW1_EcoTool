"""Minimal EarthC .eco reader (Two Worlds 1, version 0x60000).

Read-only. Works on copies in ../eco.
Layout per File Format Documentation/eco.html plus the outer container:
  outer:  [zlib stream 1 = name record][zlib stream 2 = eco body]
"""
import struct, zlib, pathlib


class R:
    def __init__(self, buf):
        self.b = buf
        self.o = 0

    def u32(self):
        v = struct.unpack_from('<I', self.b, self.o)[0]
        self.o += 4
        return v

    def raw(self, n):
        v = self.b[self.o:self.o + n]
        self.o += n
        return v

    def arr32(self, n):
        v = list(struct.unpack_from('<%dI' % n, self.b, self.o))
        self.o += 4 * n
        return v

    @property
    def left(self):
        return len(self.b) - self.o


def unwrap(path):
    """returns (name_record_bytes, eco_body_bytes)"""
    raw = pathlib.Path(path).read_bytes()
    if raw[:4] == b'ECO\0':
        return None, raw
    d = zlib.decompressobj()
    head = d.decompress(raw)
    rest = d.unused_data
    if rest:
        body = zlib.decompress(rest)
    else:                      # single stream containing everything
        head, body = None, head
    return head, body


def parse_namerec(buf):
    """3 magic bytes + flags byte; bit3 = pascal name, bit4 = uint32 script
    class id, bit5 = GUID (same layout common.js uses for every RP header)."""
    if not buf:
        return {}
    r = dict(magic=buf[:3].hex(), flags=buf[3])
    o = 4
    if r['flags'] & (1 << 3):
        n = buf[o]
        r['name'] = buf[o + 1:o + 1 + n].decode('latin1')
        o += 1 + n
    if r['flags'] & (1 << 4):
        r['num'] = struct.unpack_from('<I', buf, o)[0]
        o += 4
    if r['flags'] & (1 << 5):
        r['guid'] = buf[o:o + 16].hex()
        o += 16
    r['rest'] = buf[o:].hex()
    return r


def parse(path):
    head, body = unwrap(path)
    r = R(body)
    f = {'file': str(path), 'namerec': parse_namerec(head),
         'body_size': len(body), 'spans': []}

    def mark(tag, start):
        f['spans'].append((tag, start, r.o - start))

    s = r.o
    f['magic'] = r.raw(4)
    f['version'] = r.u32()
    f['mem_reserve'] = r.u32()
    mark('header', s)

    s = r.o
    n = r.u32()
    f['data_seg'] = r.raw(n)
    mark('data_seg', s)

    for key in ('data_ptrs', 'code_ptrs', ):
        s = r.o
        n = r.u32()
        f[key] = r.arr32(n)
        mark(key, s)

    s = r.o
    n = r.u32()
    f['imports'] = [(r.u32(), r.u32()) for _ in range(n)]
    mark('imports', s)

    s = r.o
    n = r.u32()
    f['states'] = r.arr32(n)
    mark('states', s)

    s = r.o
    n = r.u32()
    f['commands'] = [r.arr32(4) for _ in range(n)]
    mark('commands', s)

    s = r.o
    n = r.u32()
    f['events'] = r.arr32(n)
    mark('events', s)

    s = r.o
    n = r.u32()
    f['code'] = r.raw(n)
    mark('code', s)

    s = r.o
    f['has_debug'] = r.u32()
    mark('has_debug', s)

    f['tail_off'] = r.o
    f['tail'] = body[r.o:]
    return f


def summary(f):
    known = sum(sz for _, _, sz in f['spans'])
    return dict(name=f['namerec'].get('name'), size=f['body_size'],
                version=hex(f['version']), mem=f['mem_reserve'],
                data=len(f['data_seg']), dptr=len(f['data_ptrs']),
                cptr=len(f['code_ptrs']), imp=len(f['imports']),
                st=len(f['states']), cmd=len(f['commands']),
                ev=len(f['events']), code=len(f['code']),
                dbg=f['has_debug'], tail=len(f['tail']),
                cov=round(100 * known / f['body_size'], 2))
