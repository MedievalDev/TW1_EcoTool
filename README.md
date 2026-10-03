# TW1 EcoTool

The compiled scripts of **Two Worlds 1** (`.eco`) as real, compilable source code.

The tool reads every script of the installed game (and of active mods), compiles the sources of your own
Two Worlds SDK once, and finds the source that gives **exactly the same bytes** as the game's script. You
get that source with every include file it needs, laid out like the SDK, plus a `compile_all.bat`, and
every exported script is compiled once more and compared with the game before you see the result.

Scripts without a source are **rebuilt**: the decompiler turns the compiled script back into EarthC (types
worked out from the code itself), the tool compiles it and compares every byte with the game. Measured: the
game's four release scripts without any source, and the mod versions of PQuests (QuestLimit600),
RPGCompute (EnemyLevels) and TwoWorldsCampaign (TW1_Probe), come back byte for byte. What cannot be rebuilt exactly is shown as readable
decompiler output.

How general that is, measured on the 36 SDK scripts (their release builds, no source, no own debug build):

| tables learned from | byte for byte |
|---|---|
| all 36 debug builds (in-sample, not a generalisation measure) | 36 / 36 |
| all but the script's own debug build (leave-one-out) | 36 / 36 |
| all but the script's whole family (leave-family-out) | 36 / 36 |

Where the types come from:

- **Command and event slots:** read out of the SDK compiler (EarthC.exe, all 614 slots). A class or array
  parameter type no debug build shows is taken from the compiler's own type object; the plain types are voted
  from the debug builds.
- **Engine functions:** the SDK documentation's signatures, each compiled once to find its index (2149
  functions), plus the class tree and every class's lifecycle functions, also asked of the compiler.
- **What the debug builds of the SDK scripts show** on top of that.

In each leave-out run the type tables, the slot lists and the votes behind the slot table's types are learned
again without the held-out scripts; the slot table comes out the same as with all scripts (72 of 72 runs).
Not re-learned are the engine function tables taken over from
EcoAnalysis (names, argument counts, return kinds). Of the 804 engine functions only a held-out family calls,
796 are covered by the measured signatures anyway; the other 8 are compiler-internal array helpers and two
undocumented hero functions. See the guide, chapter Rebuilding.

**Update SDK** brings an old SDK (1.2, 2007) to the state of the game (1.7). It shows every file first,
backs up every replaced file, and can undo the update. SDK 1.2's compiler (`Tools\EarthC.exe`) is too old for
the game's scripts ("Cannot find suitable function"), so the update replaces it with SDK 1.3's, backed up like
every other file.

**Drop a file** on the window or on the exe: a `.eco` gets `Name.ec` next to it (the SDK source that gives these
bytes, else the checked rebuild, else readable decompiler output; the first line says which, and an existing
`Name.ec` is never overwritten). A `.ec` gets `Name.eco` next to it, compiled with SDK 1.3's compiler as a release
build; an existing `Name.eco` is kept as `Name.eco.bak` first.

## What is measured (Two Worlds Epic Edition 1.7, 2026-10-03)

| Source | Scripts that compile to the game's bytes |
|---|---|
| SDK 1.3 `Scripts` | 30 (the whole single player) |
| SDK 1.3 `Scripts\_Scripts_old_1.5_` (= SDK 1.2) | 5 (multiplayer missions, shipped in the older state) |
| no source anywhere, rebuilt by the decompiler | 4 (MissionTeamCollecting, TestDialogsMission, TestPMMission, TestPMMission2) |
| no source, v1.0 debug build, rebuilt to the same program | 1 (Cities: 2 of 2 routines) |
| no source, v1.0 debug builds, rebuilt but not complete | 2 (CityCampaign 287 of 290, MissionTeamHunt 343 of 344 routines) |

The v1.0 debug builds come from the 2007 compiler, which numbers the engine functions differently. The tool
reads them with their own debug info and translates the numbers to SDK 1.3. Cities and CityCampaign share one
old numbering, MissionTeamHunt has another. Of the 200 / 212 numbers they use, 148 / 161 are measured on library
routines that are the same program in an SDK 1.3 debug build, 30 / 39 come from the round trip (the index the
1.3 compiler picks for the same call), the rest from the name or the neighbours; one is a guess
(SetPlayerHeroUnit, which SDK 1.3 lacks anyway). The rebuilt source is compiled with SDK 1.3 as a debug build
and compared routine by routine: code, engine calls, data and the declared states, commands and events, with
source paths and line numbers aside. Three deliberately changed Cities sources (a number, a string, two lines
swapped) are each reported as different.
What stays different is the 2007 interface SDK 1.3 no longer has: `SetPlayerHeroUnit` and the commands
`FillNetworkMissionsList` (four parameters then, eight now) and `IsNetworkMissionAvailable` in CityCampaign,
`GetObject` in MissionTeamHunt. Those places are kept as `not in SDK 1.3` comments so the source compiles.

The SDK sources are **not** part of this tool; it reads them from your SDK.

## Not tested yet

- A rebuilt script, changed and recompiled, used as a mod in the game.
- A rebuilt v1.0 script (Cities) recompiled with SDK 1.3 and used in multiplayer.
- A changed and recompiled script used as a mod in the game (see Help > Test what is untested).

## Build

`build_exe.bat` (PyInstaller, one file). `selftest_exe.bat` runs the built exe in self-test mode.
Tests: `py -m unittest tests.test_core` (needs the game and SDK 1.3 for the full set).

## Credits

Decompiler: EcoAnalysis (2026), extended for release builds without debug info (type inference, rebuild
check). Disassembly: [Capstone](https://www.capstone-engine.org/) (BSD).
Design: PY_TOOL_DESIGN (Alchemy Fox). License: CC0.
