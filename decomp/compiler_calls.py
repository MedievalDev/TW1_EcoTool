"""Which native calls does the compiler insert on its own?

Every call that came from source has a ref record in the debug info. So calls
without a ref record are compiler-generated: the debug line hook, scope
enter/leave, implicit conversions/copies. Position tells them apart.
"""
import pathlib, collections, struct, json
import capstone
import eco, ecodbg, disasm

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
CACHE = (pathlib.Path(__file__).resolve().parent / 'data' / 'compiler_natives.json')


def scan():
    where = collections.defaultdict(collections.Counter)
    total = collections.Counter()
    # The SDK builds too, not only the shipped scripts: the object copy/release
    # pair of the RPGCompute script class (0x074c / 0x074d) occurs in no shipped
    # script, so it was never classified and the lifter wrote it out as
    # `native_074c(pUnit.GetEquipmentValuesOnIndex(nIndex));` (RPGCompute.ec:829).
    files = [p for p in sorted(pathlib.Path('../eco/Scripts_wd').rglob('*.eco'))]
    files += [p for p in sorted(pathlib.Path('../roundtrip/ref').rglob('*.eco'))
              if not p.name.endswith('.release.eco')]
    for p in files:
        try:
            f, b = ecodbg.parse_file(p)
        except Exception:
            continue
        if not b:
            continue
        code = bytes(f['code'])
        loc2fn = dict(f['imports'])
        for r in b['routines']:
            refs = {x['addr'] for x in r['refs']}
            insns = [i for i in disasm.CS.disasm(code[r['start']:r['end'] + 1], r['start'])]
            calls = [i for i in insns if i.mnemonic == 'call' and code[i.address] == 0xE8]
            for k, ins in enumerate(calls):
                idx = loc2fn.get(ins.address + 1)
                if idx is None:
                    continue
                total[idx] += 1
                if ins.address + ins.size in refs:
                    where[idx]['source'] += 1
                elif k < 2:
                    where[idx]['prologue'] += 1
                elif k >= len(calls) - 2:
                    where[idx]['epilogue'] += 1
                else:
                    where[idx]['inline'] += 1
    return where, total


if __name__ == '__main__':
    where, total = scan()
    internal = {}
    print(f'{"idx":>7} {"total":>7}  source  prologue  epilogue  inline')
    for idx in sorted(where, key=lambda i: -total[i])[:14]:
        c = where[idx]
        print(f'  0x{idx:04x} {total[idx]:>7}  {c["source"]:>6}  {c["prologue"]:>8}  '
              f'{c["epilogue"]:>8}  {c["inline"]:>6}')
    for idx, c in where.items():
        if c['source'] == 0:
            kind = max(('prologue', 'epilogue', 'inline'), key=lambda k: c[k])
            internal[idx] = kind
    print(f'\n{len(internal)} indices never called from source: '
          + ', '.join(f'0x{i:04x}({k})' for i, k in sorted(internal.items())[:12]))
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({str(k): v for k, v in sorted(internal.items())}, indent=1))
    print('written', CACHE)
