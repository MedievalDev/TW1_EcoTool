# TW1 EcoTool

The compiled scripts of **Two Worlds 1** (`.eco`) as real, compilable source code.

The tool reads every script of the installed game (and of active mods), compiles the sources of your own
Two Worlds SDK once, and finds the source that gives **exactly the same bytes** as the game's script. You
get that source with every include file it needs, laid out like the SDK, plus a `compile_all.bat`, and
every exported script is compiled once more and compared with the game before you see the result.

Scripts without a source are shown and exported as readable decompiler output.

**Update SDK** brings an old SDK (1.2, 2007) to the state of the game (1.7). It shows every file first,
backs up every replaced file, and can undo the update. SDK 1.2's compiler (`Tools\EarthC.exe`) is too old for
the game's scripts ("Cannot find suitable function"), so the update replaces it with SDK 1.3's, backed up like
every other file.

## What is measured (Two Worlds Epic Edition 1.7, 2026-10-03)

| Source | Scripts that compile to the game's bytes |
|---|---|
| SDK 1.3 `Scripts` | 30 (the whole single player) |
| SDK 1.3 `Scripts\_Scripts_old_1.5_` (= SDK 1.2) | 5 (multiplayer missions, shipped in the older state) |
| no source anywhere | 7 (multiplayer and test scripts: decompiler view only) |

The SDK sources are **not** part of this tool; it reads them from your SDK.

## Not tested yet

- Decompiler output is readable but not yet checked to compile (next step of the tool).
- A changed and recompiled script used as a mod in the game (see Help > Test what is untested).

## Build

`build_exe.bat` (PyInstaller, one file). `selftest_exe.bat` runs the built exe in self-test mode.
Tests: `py -m unittest tests.test_core` (needs the game and SDK 1.3 for the full set).

## Credits

Decompiler: EcoAnalysis (2026). Disassembly: [Capstone](https://www.capstone-engine.org/) (BSD).
Design: PY_TOOL_DESIGN (Alchemy Fox). License: CC0.
