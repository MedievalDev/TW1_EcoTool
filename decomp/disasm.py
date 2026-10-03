"""Annotated disassembler for compiled EarthC scripts (.eco).

The 'Executable Code' segment is 32-bit x86. This tool relocates it symbolically
and annotates it with everything the file knows about itself:

  * routine boundaries + signatures (debug info) or a prologue scan (release files)
  * parameter / local names for [ebp+8+n] / [ebp-4-n]
  * native calls resolved through Function Imports + the recovered name table
  * internal calls resolved to routine names
  * data-segment references (mov eax, imm32 marked in Code Pointers) incl. strings
  * source file / line comments from the line table
  * the per-statement debug line hook folded into one comment line

usage:  py disasm.py <file.eco> [outfile]      py disasm.py --all
"""
import pathlib, sys, struct, re
import capstone
import eco, ecodbg, nativemap

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
KIND = {4: 'event', 5: 'command', 6: 'state', 7: 'func'}
TYPE = {0: 'obj', 1: 'int', 2: 'str', 3: 'str3', 4: 'void', 6: 'array'}
# push eax ; mov eax,imm32 ; push eax ; mov eax,imm32 ; push eax ; call rel32 ; pop eax
DBG_HOOK = re.compile(rb'\x50\xb8(....)\x50\xb8(....)\x50\xe8(....)\x58', re.S)


def tname(rec):
    if rec['kind'] == 0 and isinstance(rec['type'], str):
        return rec['type']
    t = TYPE.get(rec['kind'], f'k{rec["kind"]}')
    if rec['kind'] == 6:
        t += '<' + (rec['type'] if isinstance(rec['type'], str)
                    else TYPE.get(rec['sub'], f'k{rec["sub"]}')) + '>'
    return t


def cstr(data, off, limit=60):
    if not (0 <= off < len(data)):
        return None
    end = data.find(b'\x00', off)
    s = data[off:end if end >= 0 else len(data)]
    if not s or len(s) > 400 or not all(9 <= c < 127 for c in s):
        return None
    return s[:limit].decode('latin1') + ('…' if len(s) > limit else '')


def scan_routines(code):
    """Release files: recover routine boundaries without debug info.

    The code segment is one gap-free run of routines, so a linear sweep is exact:
    disassemble from 0 and treat every instruction boundary that starts
    'push ebp ; mov ebp, esp' as a routine start; each routine ends where the next
    one begins. Verified against the 9 debug builds (see asm_validate.py --scan):
    identical boundaries for all 5307 routines."""
    starts, prev = [], None
    for ins in CS.disasm(code, 0):
        if (prev is not None and prev.mnemonic == 'push' and prev.op_str == 'ebp'
                and ins.mnemonic == 'mov' and ins.op_str == 'ebp, esp'):
            starts.append(prev.address)
        prev = ins
    out = []
    for k, s in enumerate(starts):
        e = (starts[k + 1] - 1) if k + 1 < len(starts) else len(code) - 1
        out.append(dict(name=f'sub_{s}', kind=7, a=0, start=s, end=e,
                        params=[], locals=[], lines=[], refs=[]))
    return out


class Disasm:
    def __init__(self, path):
        self.f, self.b = ecodbg.parse_file(path)
        self.path = pathlib.Path(path)
        self.code = bytes(self.f['code'])
        self.data = bytes(self.f['data_seg'])
        self.nat = nativemap.load()
        self.imports = dict(self.f['imports'])
        self.cptr = set(self.f['code_ptrs'])
        self.routines = self.b['routines'] if self.b else scan_routines(self.code)
        self.by_start = {r['start']: r for r in self.routines}
        self.files = self.b['files'] if self.b else []
        self.globals = self.global_names()

    # ---- symbolisation helpers
    def frame_names(self, r):
        """Arguments live at [ebp + 8 + addr] for every routine kind.
        For kind 4/5/6 the implicit script instance occupies addr 0 — the debug info
        simply leaves that slot out and starts the declared parameters at addr 4,
        which is why their ret imm16 is 4*nparams + 4."""
        m = {} if r['kind'] == 7 else {8: ('this', 'script')}
        for p in r['params']:
            m[8 + p['addr']] = (p['name'], tname(p))
        for l in r['locals']:
            m[-4 - l['addr']] = (l['name'], tname(l))
        return m

    def global_names(self):
        if not self.b:
            return {}
        return {g['addr']: (g['name'], tname(g)) for g in self.b['globals']}

    def sym_operand(self, op, names):
        hit_any = [False]

        def rep_ebp(mo):
            sign, num = mo.group(1), int(mo.group(2), 0)
            hit = names.get(num if sign == '+' else -num)
            if hit:
                hit_any[0] = True
                return hit[0]
            return mo.group(0)

        def rep_esi(mo):
            hit = self.globals.get(int(mo.group(1), 0) if mo.group(1) else 0)
            if hit:
                hit_any[0] = True
                return hit[0]
            return mo.group(0)

        op = re.sub(r'ebp ([+-]) (0x[0-9a-f]+|\d+)', rep_ebp, op)
        op = re.sub(r'esi(?: \+ (0x[0-9a-f]+|\d+))?(?=\])', rep_esi, op)
        return op.replace('dword ptr ', '') if hit_any[0] else op

    def call_target(self, addr, size):
        """addr = offset of the E8 opcode."""
        rel = struct.unpack_from('<i', self.code, addr + 1)[0]
        if rel == 0:
            idx = self.imports.get(addr + 1)
            if idx is None:
                return 'call ???'
            return f'call {nativemap.name_of(self.nat, idx)}   ; native 0x{idx:04x}'
        tgt = addr + size + rel
        r = self.by_start.get(tgt)
        return f'call {r["name"] if r else f"loc_{tgt}"}'

    def data_ref(self, ins):
        """mov eax, imm32 where the imm32 slot is a data relocation."""
        if ins.mnemonic != 'mov' or not ins.op_str.startswith('eax, '):
            return None
        if (ins.address + 1) not in self.cptr:
            return None
        off = struct.unpack_from('<I', self.code, ins.address + 1)[0]
        s = cstr(self.data, off)
        return f'DATA+{off}' + (f'  "{s}"' if s else '')

    # ---- output
    def routine_text(self, r, strip_dbg=True):
        L = []
        sig = ', '.join(f'{tname(x)} {x["name"]}' for x in r['params'])
        head = KIND.get(r['kind'], str(r['kind']))
        if r['kind'] != 7:
            head += f'[{r["a"]}]'
        L.append(f'{head} {r["name"]}({sig})   ; code {r["start"]}..{r["end"]}')
        for v in r['locals']:
            L.append(f'    local {tname(v):<12} {v["name"]:<24} [ebp-{4 + v["addr"]}]')
        names = self.frame_names(r)
        lines = {}
        for ln in r['lines']:
            lines.setdefault(ln[2], ln)
        refs = {x['addr']: x for x in r['refs']}
        body = self.code[r['start']:r['end'] + 1]

        # jump targets inside the routine
        targets = set()
        for ins in CS.disasm(body, r['start']):
            if ins.mnemonic.startswith('j') and ins.op_str.startswith('0x'):
                targets.add(int(ins.op_str, 16))

        pos = r['start']
        while pos <= r['end']:
            if strip_dbg:
                mo = DBG_HOOK.match(self.code, pos, r['end'] + 1)
                if mo:
                    ln = struct.unpack('<I', mo.group(1))[0]
                    fi = struct.unpack('<I', mo.group(2))[0]
                    src = self.files[fi] if fi < len(self.files) else f'file{fi}'
                    L.append(f'  {pos:>8}:   ; --- {src}:{ln}')
                    pos = mo.end()
                    continue
            gen = CS.disasm(self.code[pos:min(r['end'] + 1, pos + 16)], pos)
            ins = next(gen, None)
            if ins is None:
                L.append(f'  {pos:>8}: db {self.code[pos]:#04x}')
                pos += 1
                continue
            if ins.address in targets:
                L.append(f'  L_{ins.address}:')
            if ins.address in lines:
                ln = lines[ins.address]
                src = self.files[ln[0]] if ln[0] < len(self.files) else f'file{ln[0]}'
                L.append(f'      ; {src}:{ln[1]}' + ('  [call]' if ln[4] else ''))
            if ins.mnemonic == 'call' and self.code[ins.address] == 0xE8:
                txt = self.call_target(ins.address, ins.size)
            else:
                txt = f'{ins.mnemonic} {self.sym_operand(ins.op_str, names)}'.strip()
                d = self.data_ref(ins)
                if d:
                    txt = f'mov eax, {d}'
                elif ins.mnemonic.startswith('j') and ins.op_str.startswith('0x'):
                    t = int(ins.op_str, 16)
                    txt = f'{ins.mnemonic} L_{t}' if t in targets else txt
            ref = refs.get(ins.address + ins.size)
            L.append(f'  {ins.address:>8}: {ins.bytes.hex(" "):<21} {txt}'
                     + (f'   ; -> {ref["name"]} : {tname(ref)}' if ref else ''))
            pos += ins.size
        return L

    def text(self, strip_dbg=True):
        nr = self.f['namerec']
        L = [f'; {self.path.name} — annotated x86 disassembly of the EarthC code segment',
             f'; script "{nr.get("name", "?")}" class id {nr.get("num", "?")}'
             f'   guid {nr.get("guid", "-")}',
             f'; code {len(self.code)} bytes, data {len(self.data)} bytes, '
             f'{len(self.routines)} routines, '
             + ('debug build' if self.b else 'release build (names generated)'),
             '; esi = script instance, kept across calls; script globals are [esi+addr].',
             '; ebx = [esi-12], edi = [esi+4] (loaded in every state/command/event prologue).']
        if self.b:
            L.append(f'; source {self.b["main"]}  class {self.b["script_class"][0]}')
            L.append('; source files: ' + ', '.join(
                f'[{i}] {s}' for i, s in enumerate(self.files)))
            L.append('; script globals: ' + ', '.join(
                f'{tname(g)} {g["name"]}@{g["addr"]}' for g in self.b['globals']))
        L.append('')
        for r in self.routines:
            L += self.routine_text(r, strip_dbg)
            L.append('')
        return '\n'.join(L)


def run(path, out=None, strip_dbg=True):
    d = Disasm(path)
    txt = d.text(strip_dbg)
    if out:
        pathlib.Path(out).write_text(txt, encoding='utf8')
    return d, txt


if __name__ == '__main__':
    if sys.argv[1:2] == ['--all']:
        outdir = pathlib.Path('../out/asm')
        outdir.mkdir(parents=True, exist_ok=True)
        for p in sorted(pathlib.Path('../eco').rglob('*.eco')):
            tag = p.relative_to('../eco').parts[0]
            d, txt = run(p, outdir / f'{tag}_{p.stem}.asm')
            print(f'{p.relative_to("../eco")}: {len(d.routines)} routines, '
                  f'{len(txt.splitlines())} lines')
    else:
        d, txt = run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
        print(txt if len(sys.argv) <= 2 else f'{len(txt.splitlines())} lines written')
