"""TW1 EcoTool core against the real game and SDKs (skipped where they are missing), plus texts and guide."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ecocore as C  # noqa: E402

SDK13 = os.environ.get('ECOTOOL_SDK13', r'C:\Users\marco\Desktop\Alles vom desktop Ganz rechts\SdkOrginal\TwoWorldsSDK')
SDK12 = os.environ.get('ECOTOOL_SDK12', r'C:\TwoWorldsSDK')
CACHE = os.path.join(tempfile.gettempdir(), 'ecotool_test_cache')


def tree_hash(d):
    h = hashlib.sha256()
    for dp, ds, fs in os.walk(d):
        ds.sort()
        for f in sorted(fs):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, d).lower().encode())
            with open(p, 'rb') as fh:
                h.update(fh.read())
    return h.hexdigest()


GAME = C.find_game_dir()
REBUILT = ['MissionTeamCollecting.eco', 'TestDialogsMission.eco', 'TestPMMission.eco', 'TestPMMission2.eco']


@unittest.skipUnless(GAME and C.Sdk.looks_like(SDK13), 'needs the game and SDK 1.3')
class GameAndSdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scripts, cls.problems = C.read_game(GAME, with_mods=False)
        cls.index = C.build_index(C.find_sdks([SDK13]), CACHE)
        C.classify(cls.scripts, cls.index)

    def test_read(self):
        self.assertEqual(self.problems, [])
        self.assertGreaterEqual(len(self.scripts), 42)

    def test_sources_found(self):
        """Measured 03.10.2026: 36 of the game's .eco paths have a source that gives the same bytes."""
        n = sum(1 for s in self.scripts.values() if s.match)
        self.assertEqual(n, 36)
        names = {s.stem for s in self.scripts.values() if not s.match}
        self.assertEqual(names, {'Cities', 'CityCampaign', 'MissionTeamCollecting', 'MissionTeamHunt',
                                 'TestDialogsMission', 'TestPMMission', 'TestPMMission2'})

    def test_layout_no_clash(self):
        files, mains, clashes = C.layout(self.scripts.values(), self.index)
        self.assertEqual(clashes, [])
        self.assertEqual(len(mains), 36)
        old = [m for m in mains.values() if m.startswith(C.OLD_DIR)]
        self.assertEqual(len(old), 5)              # the five multiplayer missions in the older state

    def test_export_compiles_to_game_bytes(self):
        out = tempfile.mkdtemp(prefix='ecotool_exp_')
        try:
            rep = C.export(self.scripts.values(), self.index, out)
            self.assertEqual([r['name'] for r in rep['source'] if not r['verified']], [])
            self.assertEqual(len(rep['source']), 36)
            self.assertEqual(rep['failed'], [])
            # the four release scripts without any source come back rebuilt, checked by the export
            self.assertEqual(sorted(r['name'] for r in rep['rebuilt'] if r['verified']), sorted(REBUILT))
            self.assertTrue(os.path.isfile(os.path.join(out, 'compile_all.bat')))
            with open(os.path.join(out, 'report.json'), encoding='utf-8') as f:
                self.assertEqual(len(json.load(f)['source']), 36)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def _update_round_trip(self, index):
        """Update a copy of SDK 1.2, check it compiles all 36 with its own (new) compiler, undo it exactly."""
        scripts = dict(self.scripts)
        C.classify(scripts, index)
        tmp = tempfile.mkdtemp(prefix='ecotool_upd_')
        try:
            for d in ('Scripts', 'Tools'):
                shutil.copytree(os.path.join(SDK12, d), os.path.join(tmp, d))
            shutil.copy2(os.path.join(SDK12, '_info_.txt'), tmp)
            target = C.Sdk(tmp)
            before = tree_hash(target.scripts), tree_hash(target.tools)
            plan = C.plan_sdk_update(target, scripts.values(), index)
            self.assertTrue(plan['files'])
            self.assertIsNotNone(plan['compiler'])     # the SDK 1.2 compiler cannot build the 1.7 scripts
            self.assertEqual([k for k, (ok, _m) in plan['checks'].items() if not ok], [])
            backup = C.apply_sdk_update(target, plan, index, os.path.join(tmp, 'bk'))
            res = C.verify_sdk(target, scripts.values(), plan)
            self.assertEqual(len(res), 36)
            self.assertEqual([k for k, (ok, _m) in res.items() if not ok], [])
            self.assertEqual(C.restore_sdk_update(backup), C.plan_count(plan))
            self.assertEqual((tree_hash(target.scripts), tree_hash(target.tools)), before)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            C.classify(self.scripts, self.index)   # the Script objects are shared with the other tests

    @unittest.skipUnless(C.Sdk.looks_like(SDK12), 'needs SDK 1.2')
    def test_sdk_update_on_copy_and_undo(self):
        self._update_round_trip(self.index)

    @unittest.skipUnless(C.Sdk.looks_like(SDK12) and any(s.version == '1.3' for s in C.find_sdks()),
                         'needs SDK 1.2 and an installed SDK 1.3')
    def test_sdk_update_installed_sdks(self):
        """The order the GUI finds them in (C:\\TwoWorldsSDK first): failed before 03.10. with the 1.2 compiler."""
        self._update_round_trip(C.build_index(C.find_sdks(), CACHE))

    def test_rebuild_without_source(self):
        """Measured 03.10.2026: every release script without a source decompiles to EarthC that compiles
        to exactly the game's bytes - no SDK source, no debug build involved."""
        tools = C.tree_tools(self.scripts.values(), self.index)
        got = {}
        for s in self.scripts.values():
            if not s.match and not s.debug:
                got[s.name] = C.reconstruct(s, tools)['status']
        self.assertEqual(got, {n: 'identical' for n in REBUILT})

    def test_rebuild_v10_debug_builds(self):
        """Measured 03.10.2026: the game's three v1.0 debug builds, rebuilt from the WD bytes as the tool does it
        (own debug info, engine functions translated, compiled with SDK 1.3 in debug mode, compared routine by
        routine). Cities is the same program; the other two differ only where SDK 1.3 lacks a function or a
        command of the 2007 interface."""
        tools = C.tree_tools(self.scripts.values(), self.index)
        got = {}
        for s in self.scripts.values():
            if not s.match and s.debug:
                r = C.reconstruct(s, tools)
                got[s.stem] = (r['status'], tuple(r.get('routines') or ()))
        self.assertEqual(got, {'Cities': ('equivalent', (2, 2)), 'CityCampaign': ('differs', (287, 290)),
                               'MissionTeamHunt': ('differs', (343, 344))})

    def test_drop_round_trip(self):
        """Drag & drop: a .ec dropped is compiled next to it (release build), the .eco dropped is decompiled
        next to it - with TestPMMission (no SDK source) the circle closes on the game's own bytes."""
        tools = C.drop_tools(C.find_sdks([SDK13]))
        s = next(s for s in self.scripts.values() if s.stem == 'TestPMMission')
        r = C.reconstruct(s, tools)
        work = tempfile.mkdtemp(prefix='ecotool_drop_')
        try:
            src = os.path.join(work, 'TestPMMission.ec')
            with open(src, 'w', encoding='latin-1') as f:
                f.write(r['text'])
            eco, msg, backup = C.compile_dropped(src, tools)
            self.assertEqual((eco, backup), (os.path.join(work, 'TestPMMission.eco'), None), msg)
            with open(eco, 'rb') as f:
                self.assertEqual(C.sha(C.eco_body(f.read())), s.sha)
            out, kind, _m = C.decompile_dropped(eco, tools, self.index)
            self.assertEqual((os.path.basename(out), kind), ('TestPMMission_decompiled.ec', 'identical'))
            eco2, _m, backup = C.compile_dropped(src, tools)
            self.assertEqual(os.path.basename(backup), 'TestPMMission.eco.bak')
            self.assertTrue(os.path.isfile(eco2))
            # a script with an SDK source gives that source
            g = next(s for s in self.scripts.values() if s.match)
            p = os.path.join(work, g.name)
            with open(p, 'wb') as f:
                f.write(g.body)
            out, kind, _m = C.decompile_dropped(p, tools, self.index)
            self.assertEqual(kind, 'source')
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_decompiler_view(self):
        s = next(s for s in self.scripts.values() if s.stem == 'TestPMMission')
        text = C.decompiled_text(s.body)
        self.assertIn('function', text)


class Basics(unittest.TestCase):
    def test_eco_body_plain(self):
        body = b'ECO' + bytes(1) + bytes(40)
        self.assertEqual(C.eco_body(body), body)

    def test_translations(self):
        import eco_tool
        missing = eco_tool._check_translations()
        self.assertEqual(missing, [])
        import ast
        with open(os.path.join(ROOT, 'eco_tool.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        keys = [n.args[0].value for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'tr' and n.args
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)]
        for k in keys:
            self.assertIn(k, eco_tool.DE, k)
        for _k, (label, text) in eco_tool.STATUS_INFO.items():
            self.assertIn(label, eco_tool.DE)
            self.assertIn(text, eco_tool.DE)

    def test_guide_tables_have_sources(self):
        import guidebook
        guidebook.check_sources()
        import eco_tool
        for lang in ('de', 'en'):
            eco_tool._LANG = lang
            for _cid, _t, fn in guidebook.CHAPTERS:
                self.assertTrue(fn().startswith('# '))
        eco_tool._LANG = 'en'

    def test_untested_json(self):
        with open(os.path.join(ROOT, 'untested.json'), encoding='utf-8') as f:
            d = json.load(f)
        for t in d['tests']:
            for k in ('title', 'why', 'steps', 'expect'):
                self.assertIn('de', t[k])
                self.assertIn('en', t[k])


if __name__ == '__main__':
    unittest.main()
