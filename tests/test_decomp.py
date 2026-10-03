"""Regression gate for the decompiler (decomp\\): the round trips of the 36 SDK scripts.

Each case decompiles a release build, compiles the result with the SDK 1.3 compiler and compares the body with
the reference byte for byte. The references are the EcoAnalysis round-trip builds (release + debug build of
each SDK source); the tests are skipped where they or SDK 1.3 are missing.

  - debug path: release build + its debug build (names, types) -> 36/36
  - release only, IN-SAMPLE: no debug build of the script, but the type and slot tables were learned from the
    debug builds of these same 36 scripts -> 36/36. Not a generalisation measure.
  - leave-one-out (one fast case): the tables learned WITHOUT the script's own debug build.
Measured 2026-10-03. Run before every change to decomp\\.
"""
import glob
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ecocore as C  # noqa: E402
import decomp  # noqa: E402,F401

REF = os.environ.get('ECOTOOL_RT_REF', r'C:\Users\marco\Desktop\TwStuff\EcoAnalysis_27_07_2026\roundtrip\ref')
ECO = os.path.join(os.path.dirname(os.path.dirname(REF)), 'eco')
SDK13 = next((s for s in C.find_sdks([os.environ.get('ECOTOOL_SDK13', '')]) if s.version == '1.3'), None)


def names():
    return sorted(os.path.basename(p)[:-len('.release.eco')] for p in glob.glob(os.path.join(REF, '*.release.eco')))


def round_trip(name, with_debug):
    import emit_ec
    rel = os.path.join(REF, name + '.release.eco')
    text = emit_ec.emit(rel, os.path.join(REF, name + '.eco') if with_debug else None)
    work = tempfile.mkdtemp(prefix='ecotool_rt_')
    try:
        src = os.path.join(work, name + '.ec')
        with open(src, 'w', encoding='latin-1', errors='replace') as f:
            f.write(text)
        body, msg = C.compile_file(SDK13.tools, src)
        if body is None:
            return 'compile: ' + msg.strip().splitlines()[-1][-120:]
        with open(rel, 'rb') as f:
            ref = C.eco_body(f.read())
        return 'identical' if C.sha(body) == C.sha(ref) else f'differs {len(body) - len(ref):+d}'
    finally:
        shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(os.path.isdir(REF) and SDK13, 'needs the EcoAnalysis round-trip refs and SDK 1.3')
class RoundTrips(unittest.TestCase):
    def test_debug_path_36(self):
        got = {n: round_trip(n, True) for n in names()}
        self.assertEqual(len(got), 36)
        self.assertEqual({n: r for n, r in got.items() if r != 'identical'}, {})

    def test_release_only_in_sample_36(self):
        got = {n: round_trip(n, False) for n in names()}
        self.assertEqual({n: r for n, r in got.items() if r != 'identical'}, {})

    def test_slot_table_from_compiler(self):
        """The command / event slots read out of EarthC.exe agree with every slot a debug build knows."""
        import entries
        import slot_table
        table, _info = slot_table.build(os.path.join(SDK13.tools, 'EarthC.exe'))
        self.assertEqual(slot_table.check(table, entries.load()), [])
        self.assertGreaterEqual(sum(len(v) for k in table.values() for v in k.values()), 600)

    def _left_out(self, out, names_):
        """round trips of `names_` with the type and slot tables learned without the debug builds `out`"""
        import typeinfer
        import entries
        import slot_table
        debug = [p for p in glob.glob(os.path.join(REF, '*.eco')) if not p.endswith('.release.eco')]
        debug += glob.glob(os.path.join(ECO, 'Scripts_wd', '**', '*.eco'), recursive=True)
        keep = (typeinfer.TABLES, typeinfer._tables, entries._cache, slot_table._cache)
        tmp = tempfile.mkdtemp(prefix='ecotool_loo_')
        try:
            typeinfer.TABLES = pathlib.Path(tmp) / 'type_tables.json'
            paths = [p for p in debug if pathlib.Path(p).stem not in out]
            typeinfer._tables = typeinfer.learn(paths)
            entries._cache = entries.harvest(paths)
            # the slot table solves its descriptor types from the debug builds as well
            slot_table._cache = slot_table.build(os.path.join(SDK13.tools, 'EarthC.exe'), known=entries._cache)[0]
            return {n: round_trip(n, False) for n in names_}
        finally:
            typeinfer.TABLES, typeinfer._tables, entries._cache, slot_table._cache = keep
            shutil.rmtree(tmp, ignore_errors=True)

    def test_leave_one_out_towns(self):
        """Towns with the type and slot tables learned without Towns' own debug build."""
        self.assertEqual(self._left_out({'Towns'}, ['Towns']), {'Towns': 'identical'})

    def test_leave_family_out_pquests(self):
        """the three PQuests scripts with no debug build of any of them (string arrays, handle wrappers)"""
        fam = ['PQuests', 'PQuestsMulti', 'PQuestsMulti16']
        self.assertEqual(self._left_out(set(fam), fam), {n: 'identical' for n in fam})

    def test_leave_one_out_rpgcompute(self):
        """RPGCompute, the only script of its class: slots, class tree and lifecycle from the compiler"""
        self.assertEqual(self._left_out({'RPGCompute'}, ['RPGCompute']), {'RPGCompute': 'identical'})


V10 = os.path.join(ECO, 'Scripts_wd', 'Scripts', 'Network')
RT_SRC = os.path.join(os.path.dirname(REF), 'src')


def v10_round_trip(name):
    """the game's v1.0 debug build: emit, compile with SDK 1.3 in debug mode, compare routine by routine"""
    import emit_ec
    import dbgcompare
    import v10
    orig = os.path.join(V10, name + '.eco')
    work = tempfile.mkdtemp(prefix='ecotool_v10_')
    try:
        src = os.path.join(work, name + '.ec')
        with open(src, 'w', encoding='latin-1', errors='replace') as f:
            f.write(emit_ec.emit(orig))
        body, msg = C.compile_file(SDK13.tools, src, debug=True)
        if body is None:
            return 'compile: ' + msg.strip().splitlines()[-1][-120:]
        return dbgcompare.compare(os.path.join(work, name + '.eco'), orig, native_map=v10.load(name)[0],
                                  mask_lines=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(os.path.isdir(REF) and os.path.isdir(RT_SRC) and SDK13,
                     'needs the EcoAnalysis round-trip sources and refs and SDK 1.3')
class DebugCompare(unittest.TestCase):
    def test_sdk_debug_build_same(self):
        """the yardstick of the v1.0 path: an SDK source compiled in debug mode in another folder is the same
        as the round-trip debug ref - every routine, engine call and data item (the 36 measured on 2026-10-03)"""
        import dbgcompare
        work = tempfile.mkdtemp(prefix='ecotool_dbg_')
        try:
            shutil.copytree(RT_SRC, os.path.join(work, 'src'))
            src = os.path.join(work, 'src', 'Campaigns', 'Missions', 'TwoWorldsWeather.ec')
            body, msg = C.compile_file(SDK13.tools, src, debug=True)
            self.assertIsNotNone(body, msg)
            r = dbgcompare.compare(src[:-3] + '.eco', os.path.join(REF, 'TwoWorldsWeather.eco'))
            self.assertEqual((r['same'], r['differ'], r['imports'], r['data']), (r['routines'], [], [], None))
        finally:
            shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(os.path.isfile(os.path.join(V10, 'Cities.eco')) and SDK13, 'needs the game\'s v1.0 builds and SDK 1.3')
class V10Builds(unittest.TestCase):
    """measured 2026-10-03: Cities the same program; CityCampaign and MissionTeamHunt the same except the
    routines that call functions or declare commands SDK 1.3 no longer has"""

    def test_cities_equivalent(self):
        r = v10_round_trip('Cities')
        self.assertEqual((r['same'], r['routines'], r['imports'], r['data']), (2, 2, [], None))

    def test_citycampaign(self):
        r = v10_round_trip('CityCampaign')
        self.assertEqual((r['same'], r['routines']), (287, 290))
        self.assertEqual(sorted(n for n, _w in r['differ']),
                         ['FillNetworkMissionsList', 'Initialize', 'IsNetworkMissionAvailable'])

    def test_missionteamhunt(self):
        r = v10_round_trip('MissionTeamHunt')
        self.assertEqual((r['same'], r['routines'], r['data']), (343, 344, None))
        self.assertEqual([n for n, _w in r['differ']], ['RespawnItems'])


if __name__ == '__main__':
    unittest.main()
