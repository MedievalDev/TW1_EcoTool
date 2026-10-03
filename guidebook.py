"""Guide window of TW1 EcoTool: Help > Guide, F1 (PY_TOOL_DESIGN.md 6.2).

Chapter tree on the left, text on the right, search on top, German and English. The tables come from the
constants the tool uses (eco_tool.STATUS_INFO, the key bindings, the measured script counts), each with a
source line.
"""

import re
import tkinter as tk
from tkinter import ttk

import theme


def _lang():
    import eco_tool as M
    return M._LANG


def _l(de, en):
    return de if _lang() == 'de' else en


def _table(head, rows):
    out = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    for r in rows:
        out.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    return '\n'.join(out)


def _source(text):
    return _l('Quelle: ', 'Source: ') + text + '\n'


# measured 03.10.2026 on Two Worlds Epic Edition v1.7 with SDK 1.2 and 1.3 (STATUS.md)
MEASURED = (
    ('SDK 1.3 Scripts', 30, 'single player: campaign, quests, chests, towns, enemies, weather, hero, RPGCompute, units'),
    ('SDK 1.3 _Scripts_old_1.5_ (= SDK 1.2)', 5, 'HorseRacing, TeamAssault, TeamDeathmatch, TeamMonsterHunt, TeamRustling'),
    ('rebuilt', 4, 'MissionTeamCollecting, TestDialogsMission, TestPMMission, TestPMMission2'),
    ('-', 3, 'Cities, CityCampaign, MissionTeamHunt (v1.0 debug builds)'),
)


def ch_start():
    keys = [('F5', 'F5', _l('Spiel neu einlesen', 'Read the game again')),
            ('Strg+O', 'Ctrl+O', _l('.eco oder .wd oeffnen', 'Open .eco or .wd')),
            ('Strg+E', 'Ctrl+E', _l('Exportieren', 'Export')),
            ('Strg+S', 'Ctrl+S', _l('Angezeigten Text speichern', 'Save the shown text')),
            ('Strg+F', 'Ctrl+F', _l('Suche', 'Search')),
            ('F1', 'F1', _l('Dieser Guide', 'This guide'))]
    rows = [(k[0] if _lang() == 'de' else k[1], k[2]) for k in keys]
    return _l('''# Einstieg

TW1 EcoTool zeigt die kompilierten Skripte von Two Worlds 1 (`.eco`) als
Quelltext. Es liest die Skripte des installierten Spiels und der aktiven
Mods und sucht in deinem Two Worlds SDK die Quelle, die **genau dieselben
Bytes** ergibt. Diese Quelle bekommst du, mit allen Include-Dateien, und sie
kompiliert wieder zum selben Skript. Wo es keine solche Quelle gibt, zeigt
das Tool lesbare Decompiler-Ausgabe.

Das Fenster:

- **Oben:** gefundenes Spiel und gefundene SDKs (anklicken, um einen anderen
  Ordner zu waehlen), dazu Einlesen, Oeffnen, Exportieren, SDK aktualisieren.
- **Links:** alle Skripte mit Status, Quelle und Archiv. Filter und Suche.
- **Rechts:** Quelltext, Include-Dateien, Details des gewaehlten Skripts.
- **Unten:** Statuszeile mit Fortschritt.

Tasten:

''', '''# Getting started

TW1 EcoTool shows the compiled scripts of Two Worlds 1 (`.eco`) as source
code. It reads the scripts of the installed game and of active mods and
looks in your Two Worlds SDK for the source that gives **exactly the same
bytes**. You get that source with all its include files, and it compiles
again to the same script. Where there is no such source the tool shows
readable decompiler output.

The window:

- **Top:** the game and SDKs found (click to choose another folder), plus
  Read game, Open, Export, Update SDK.
- **Left:** every script with status, source and archive. Filter and search.
- **Right:** source code, include files and details of the selected script.
- **Bottom:** status line with progress.

Keys:

''') + _table([_l('Taste', 'Key'), _l('Wirkung', 'Action')], rows) + '\n\n' + _source('eco_tool.py, App._bind_keys')


def ch_first():
    return _l('''# Erstes Ergebnis in 10 Minuten

1. Das Tool starten. Es findet das Spiel (Steam) und das SDK
   (`C:\\TwoWorldsSDK`) meist selbst. Sonst oben auf die rote Zeile klicken
   und den Ordner waehlen.
2. Beim ersten Start kompiliert es jede SDK-Quelle einmal (etwa eine halbe
   Minute, Fortschritt unten). Danach geht es aus dem Zwischenspeicher.
3. Links `TwoWorldsContainers.eco` waehlen (Suche: Containers). Rechts steht
   der Quelltext, gruen markiert: diese Quelle ergibt genau das Skript des
   Spiels. Unter "Include-Dateien" siehst du `Quest.ech`, `Lock.ech` usw.
4. Auf **Exportieren** klicken, Ordner bestaetigen. Das Tool schreibt die
   Quellen, kompiliert jede noch einmal und vergleicht sie mit dem Spiel.
5. Im Exportordner `compile_all.bat` starten: Jede `.eco` entsteht neben
   ihrer Quelle, byte-gleich mit der des Spiels.

Jetzt kannst du eine Quelle aendern (zum Beispiel die Kistenfuellung in
`TwoWorldsContainers.ec`), mit `compile_all.bat` kompilieren und die `.eco`
in eine Mod packen.
''', '''# First result in 10 minutes

1. Start the tool. It usually finds the game (Steam) and the SDK
   (`C:\\TwoWorldsSDK`) by itself. Otherwise click the red line at the top
   and choose the folder.
2. On the first start it compiles every SDK source once (about half a
   minute, progress at the bottom). Afterwards it comes from the cache.
3. Select `TwoWorldsContainers.eco` on the left (search: Containers). On
   the right you see the source, marked green: this source gives exactly the
   game's script. "Include files" lists `Quest.ech`, `Lock.ech` and so on.
4. Click **Export** and confirm the folder. The tool writes the sources,
   compiles each once more and compares it with the game.
5. Run `compile_all.bat` in the export folder: every `.eco` appears next to
   its source, byte for byte the game's.

Now you can change a source (for example how chests are filled in
`TwoWorldsContainers.ec`), compile it with `compile_all.bat` and pack the
`.eco` into a mod.
''')


def ch_read():
    return _l('''# Spiel, Mods und SDKs

**Spiel:** Das Tool sucht den Two-Worlds-Ordner (mit `WDFiles`) in den
Einstellungen, in der Registry und in den Steam-Bibliotheken. Es legt die
Archive in derselben Reihenfolge uebereinander wie das Spiel: Basisarchive,
GraphicsUpdate, Update11-15, Update16. Die zuletzt geladene Kopie eines
Skripts gewinnt; die verdeckten aelteren Kopien stehen unter "Details".

**Mods:** Mit der Einstellung "aktive Mods mitlesen" kommen die Mods dazu,
die in der Registry eingeschaltet sind (`Mods\\*.wd`), und `.wd` neben der
Exe. Ein Skript aus einer Mod steht in der Spalte Archiv mit dem Mod-Namen.
Ueber **Oeffnen** liest du einzelne `.eco` oder ganze `.wd` (zum Beispiel
eine fremde Mod) dazu; der Filter "nur Mods und geoeffnete Dateien" zeigt
sie allein.

**SDK:** Ein SDK-Ordner hat `Scripts` (Quellen) und `Tools` (`cpp` und
`EarthC.exe`). Das Tool findet `C:\\TwoWorldsSDK` selbst, weitere fuegst du
unter Werkzeuge > SDK-Ordner hinzufuegen dazu. Es kompiliert jede `.ec`
jedes SDK einmal in einer Arbeitskopie (nie im SDK selbst) und merkt sich
das Ergebnis. Das SDK 1.3 enthaelt in `Scripts\\_Scripts_old_1.5_` auch den
aelteren Stand; der wird mitgelesen.
''', '''# Game, mods and SDKs

**Game:** The tool looks for the Two Worlds folder (with `WDFiles`) in the
settings, the registry and the Steam libraries. It stacks the archives in
the game's order: base archives, GraphicsUpdate, Update11-15, Update16. The
copy loaded last wins; the older copies it hides are listed under
"Details".

**Mods:** With the setting "also read active mods" the mods switched on in
the registry (`Mods\\*.wd`) and `.wd` next to the exe come on top. A script
from a mod shows the mod's name in the Archive column. **Open** adds single
`.eco` or whole `.wd` (for example someone else's mod); the filter "only
mods and opened files" shows them alone.

**SDK:** An SDK folder has `Scripts` (sources) and `Tools` (`cpp` and
`EarthC.exe`). The tool finds `C:\\TwoWorldsSDK` by itself; add others with
Tools > Add SDK folder. It compiles every `.ec` of every SDK once in a work
copy (never inside the SDK) and remembers the result. SDK 1.3 also keeps the
older set in `Scripts\\_Scripts_old_1.5_`; that is read too.
''')


def ch_status():
    import eco_tool as M
    rows = [(M.tr(label), M.tr(text)) for _k, (label, text) in M.STATUS_INFO.items()]
    return _l('''# Status der Skripte

Jedes Skript hat einen von drei Zustaenden (Farbe in der Liste: gruen,
orange, rot):

''', '''# Status of the scripts

Every script has one of three states (colour in the list: green, orange,
red):

''') + _table(['Status', _l('Bedeutung', 'Meaning')], rows) + '\n\n' + _source(
        'eco_tool.py STATUS_INFO, ecocore.status_of') + _l('''
"Gleiche Bytes" heisst: Die Quelle wird mit dem SDK-Compiler kompiliert und
der Inhalt der entstehenden `.eco` mit dem des Spiels verglichen (SHA-256).
Nur der Kopf mit dem Skriptnamen wird nicht verglichen.
''', '''
"Same bytes" means: the source is compiled with the SDK compiler and the
content of the resulting `.eco` is compared with the game's (SHA-256). Only
the header with the script name is not compared.
''')


def ch_export():
    return _l('''# Exportieren

**Exportieren** (oben oder Strg+E, im Kontextmenue nur die gewaehlten)
schreibt in einen Ordner:

- `Scripts\\...` - die Quellen im Aufbau des SDK, mit jeder Include-Datei,
  die sie brauchen. Braucht ein Skript eine aeltere Fassung einer
  Include-Datei (die Mehrspieler-Missionen), liegt es mit seinen Includes
  in `Scripts\\_Scripts_old_1.5_\\` wie im SDK 1.3.
- `compile_all.bat` - kompiliert jede exportierte Quelle mit dem
  SDK-Compiler; die `.eco` entsteht neben der Quelle.
- `Decompiled\\...` - Skripte ohne Quelle als lesbare Decompiler-Ausgabe.
- `report.json` - was wohin ging und das Pruefergebnis.

Mit dem Haken "noch einmal kompilieren und vergleichen" (Standard) wird
jede exportierte Quelle in einer Kopie des Exports kompiliert und mit dem
Spiel verglichen, bevor du das Ergebnis siehst.
''', '''# Export

**Export** (top or Ctrl+E, from the context menu only the selected ones)
writes into a folder:

- `Scripts\\...` - the sources in the SDK layout, with every include file
  they need. A script that needs an older version of an include file (the
  multiplayer missions) lies with its includes in
  `Scripts\\_Scripts_old_1.5_\\` as in SDK 1.3.
- `compile_all.bat` - compiles every exported source with the SDK compiler;
  each `.eco` appears next to its source.
- `Decompiled\\...` - scripts without a source as readable decompiler
  output.
- `report.json` - what went where and the check result.

With "compile again and compare" (default) every exported source is
compiled in a copy of the export and compared with the game before you see
the result.
''')


def ch_update():
    return _l('''# SDK aktualisieren

Ein SDK 1.2 (`C:\\TwoWorldsSDK`, 14.09.2007) hat den Skriptstand von 2007.
**SDK aktualisieren** bringt seinen Ordner `Scripts` auf den Stand des
Spiels, aus den Quellen, die das Tool kennt (zum Beispiel einem zweiten,
neueren SDK 1.3):

1. Schritt 1: das SDK waehlen, das aktualisiert werden soll.
2. Schritt 2: **Zeigen, was sich aendert** - das Tool baut den kuenftigen
   Ordner in einer Arbeitskopie, kompiliert dort jedes Skript und vergleicht
   es mit dem Spiel. Die Liste zeigt jede Datei: neu oder ersetzt.
3. Schritt 3: **Sichern und schreiben** - erst jetzt. Jede ersetzte Datei
   kommt vorher in den Sicherungsordner.

Der Compiler von SDK 1.2 ist zu alt fuer die Skripte des Spiels. Dann ersetzt
das Tool auch `Tools\\EarthC.exe` durch den Compiler aus SDK 1.3 (eigene
Zeile in der Liste, vorher gesichert). Sonst nichts aus `Tools`.

Nichts wird geschrieben, solange auch nur ein Skript nicht genau die Bytes
des Spiels ergibt. Nach dem Schreiben kompiliert das Tool jedes Skript noch
einmal im SDK selbst, mit dessen eigenem Compiler. Werkzeuge >
SDK-Aktualisierung rueckgaengig stellt den alten Stand wieder her: ersetzte
Dateien und den alten Compiler zurueck, neue Dateien entfernt.

Das SDK bekommt immer die Skripte des Spiels selbst, nie die einer aktiven
Mod.

Skripte, fuer die es nirgends eine Quelle gibt, bleiben, wie sie sind; das
Tool nennt sie.
''', '''# Update an SDK

An SDK 1.2 (`C:\\TwoWorldsSDK`, 2007-09-14) has the scripts of 2007.
**Update SDK** brings its `Scripts` folder to the state of the game, from
the sources the tool knows (for example a second, newer SDK 1.3):

1. Step 1: choose the SDK to update.
2. Step 2: **Show what changes** - the tool builds the future folder in a
   work copy, compiles every script there and compares it with the game.
   The list shows every file: new or replaced.
3. Step 3: **Back up and write** - only now. Every replaced file goes to the
   backup folder first.

SDK 1.2's compiler is too old for the game's scripts. Then the tool also
replaces `Tools\\EarthC.exe` with SDK 1.3's compiler (its own row in the list,
backed up first). Nothing else from `Tools`.

Nothing is written while even one script would not give exactly the game's
bytes. After writing, the tool compiles every script once more in the SDK
itself, with its own compiler. Tools > Undo an SDK update puts the old state
back: replaced files and the old compiler back, new files removed.

The SDK always gets the game's own scripts, never those of an active mod.

Scripts without a source anywhere stay as they are; the tool names them.
''')


def ch_decompiler():
    return _l('''# Nachbauen und Decompiler-Ansicht

Fuer ein Skript ohne passende Quelle baut das Tool beim Einlesen eine
Quelle nach: Die `.eco` enthaelt x86-Maschinencode, der Decompiler
uebersetzt ihn zurueck in EarthC und leitet die Typen aus dem Code selbst ab
(welche Engine-Funktion einen Wert bekommt oder liefert). Dann kompiliert
das Tool diese Quelle und vergleicht sie mit dem Spiel. Nur wenn **jedes
Byte** stimmt, heisst das Skript **Nachgebaut**, und der Export schreibt die
Quelle nach `Scripts\\_TW1_Rebuilt` (mit `compile_all.bat` und erneuter
Pruefung).

Namen gibt es in einer kompilierten Datei nicht: Funktionen heissen
`sub_<Adresse>`, globale Variablen `g0`, `g1`, lokale `loc1`, Parameter
`a1`. Befehle und Events behalten ihre echten Namen (die sind Teil der
Skriptklasse).

Gemessen am 03.10.2026: Die vier Release-Skripte des Spiels ohne Quelle und
die Mod-Fassungen von PQuests (QuestLimit600), RPGCompute (EnemyLevels) und
TwoWorldsCampaign (TW1_Probe) kommen byte-gleich heraus.

Wie allgemein das ist, zeigt die Gegenprobe an den 36 SDK-Skripten (nur die
`.eco`, ohne Quelle): Mit Typ- und Platztabellen aus allen 36 Debug-Builds
36/36 (das zaehlt nicht als Beweis, die Tabellen kennen die Skripte schon).
Ohne den eigenen Debug-Build 36/36, ohne die ganze Skriptfamilie 36/36.

Wo der Nachbau nicht exakt gelingt, zeigt das Tool lesbare
Decompiler-Ausgabe. Das betrifft die drei v1.0-Debug-Builds (Cities,
CityCampaign, MissionTeamHunt): Sie stammen von einem aelteren Compiler
mit anderer Nummerierung der Engine-Funktionen.

Woher das Tool die Typen kennt: die Befehls- und Event-Plaetze aus dem
SDK-Compiler selbst (EarthC.exe, 614 Plaetze; Klassen- und Array-Typen aus
seinen eigenen Typ-Objekten), die Engine-Funktionen aus der
SDK-Dokumentation (jede Signatur einmal kompiliert, 2149 Funktionen) samt
Klassenbaum und Lebenszyklus-Funktionen jeder Klasse (ebenfalls beim
Compiler erfragt), dazu was die Debug-Builds der SDK-Skripte zeigen. Bei der
Gegenprobe werden Typtabellen, Platzlisten und die Typ-Stimmen der
Platztabelle ohne die zurueckgehaltenen Skripte neu gelernt (die
Platztabelle kommt dabei in allen 72 Laeufen gleich heraus); die aus
EcoAnalysis uebernommenen Tabellen der Engine-Funktionen nicht - von den 804
Engine-Funktionen, die nur eine zurueckgehaltene Familie aufruft, decken die
gemessenen Signaturen 796 ab.
''', '''# Rebuilding and the decompiler view

For a script without a matching source the tool rebuilds a source while
reading: the `.eco` holds x86 machine code, the decompiler translates it
back into EarthC and works the types out from the code itself (which engine
function a value is handed to or comes from). Then the tool compiles that
source and compares it with the game. Only when **every byte** matches is
the script called **Rebuilt**, and Export writes the source to
`Scripts\\_TW1_Rebuilt` (with `compile_all.bat` and checked once more).

A compiled file holds no names: functions are called `sub_<address>`,
globals `g0`, `g1`, locals `loc1`, parameters `a1`. Commands and events keep
their real names (those belong to the script class).

Measured on 2026-10-03: the game's four release scripts without a source and
the mod versions of PQuests (QuestLimit600), RPGCompute (EnemyLevels) and
TwoWorldsCampaign (TW1_Probe) come out byte for byte.

How general that is, from the cross-check on the 36 SDK scripts (the `.eco`
alone, no source): with type and slot tables from all 36 debug builds 36/36
(no proof, the tables already know those scripts); without the script's own
debug build 36/36; without its whole family 36/36.

Where an exact rebuild does not work the tool shows readable decompiler
output. That applies to the three v1.0 debug builds (Cities, CityCampaign,
MissionTeamHunt): they come from an older compiler that numbers the engine
functions differently.

Where the tool gets the types from: the command and event slots from the SDK
compiler itself (EarthC.exe, 614 slots; class and array types from its own
type objects), the engine functions from the SDK
documentation (each signature compiled once, 2149 functions) with the class
tree and every class's lifecycle functions (asked of the compiler as well),
plus what the debug builds of the SDK scripts show. In the cross-check the
type tables, slot lists and the slot table's type votes are learned again
without the held-out scripts (the slot table comes out the same in all 72
runs); the engine function tables taken over from
EcoAnalysis are not - of the 804 engine functions only a held-out family
calls, the measured signatures cover 796.
''')


def ch_reference():
    rows = [(src, n, _l(what.replace('single player', 'Einzelspieler').replace('campaign', 'Kampagne')
                        .replace('quests', 'Quests').replace('chests', 'Kisten').replace('towns', 'Staedte')
                        .replace('enemies', 'Gegner').replace('weather', 'Wetter').replace('hero', 'Held')
                        .replace('units', 'Einheiten'), what) if src != '-' else what)
            for src, n, what in MEASURED]
    rows[-2] = (_l('vom Decompiler nachgebaut', 'rebuilt by the decompiler'), rows[-2][1], rows[-2][2])
    rows[-1] = (_l('keine Quelle', 'no source'), rows[-1][1], rows[-1][2])
    return _l('''# Referenz: das Spiel 1.7 und die SDKs

Two Worlds Epic Edition (TwoWorlds.exe 1.7.0.0) hat 42 verschiedene
Skripte; die neueste Schicht ist `Update16.wd`. So viele davon ergeben sich
byte-gleich aus welchem SDK:

''', '''# Reference: the game 1.7 and the SDKs

Two Worlds Epic Edition (TwoWorlds.exe 1.7.0.0) has 42 different scripts;
the newest layer is `Update16.wd`. How many of them come out byte for byte
from which SDK:

''') + _table([_l('Quelle', 'Source'), _l('Skripte', 'Scripts'), _l('welche', 'which')], rows) + '\n\n' + _source(
        _l('Messung 03.10.2026 mit SDK 1.2 (C:\\TwoWorldsSDK) und SDK 1.3, STATUS.md',
           'measured 2026-10-03 with SDK 1.2 (C:\\TwoWorldsSDK) and SDK 1.3, STATUS.md')) + _l('''
Die Mehrspieler-Missionen liefert das Spiel im aelteren Stand aus; mit den
neueren Quellen aus SDK 1.3 ergeben sie andere Bytes.

Die Compiler sind nicht gleich: `EarthC.exe` aus SDK 1.2 (19.07.2007) kennt
Funktionen nicht, die die Skripte des Spiels aufrufen (zum Beispiel
`CommandMessageGet` mit Text-Rueckgabe in `Common\\Levels.ech`), und bricht
mit "Cannot find suitable function" ab. Der Compiler aus SDK 1.3 kompiliert
alle 36 Skripte mit Quelle byte-gleich, auch die alten Mehrspieler-Missionen.
Das Tool prueft deshalb jeden Export mit dem Compiler, den auch
`compile_all.bat` benutzt.
''', '''
The game ships the multiplayer missions in the older state; with SDK 1.3's
newer sources they give other bytes.

The compilers are not the same: SDK 1.2's `EarthC.exe` (2007-07-19) does not
know functions the game's scripts call (for example `CommandMessageGet` with
a text result in `Common\\Levels.ech`) and stops with "Cannot find suitable
function". SDK 1.3's compiler builds all 36 scripts with a source byte for
byte, the old multiplayer missions too. So the tool checks every export with
the compiler `compile_all.bat` uses.
''')


def ch_trouble():
    return _l('''# Fehlersuche

## "Spiel: nicht gefunden"

Oben auf die rote Zeile klicken und den Two-Worlds-Ordner waehlen (der mit
`WDFiles` darin), oder Datei > Einstellungen.

## "SDK: keines gefunden"

Ohne SDK gibt es nur die Decompiler-Ansicht. Den SDK-Ordner (mit `Scripts`
und `Tools\\EarthC.exe`) ueber Werkzeuge > SDK-Ordner hinzufuegen waehlen.

## Der erste Start dauert

Beim ersten Mal kompiliert das Tool jede Quelle jedes SDK (gemessen: 25 s
fuer SDK 1.3 mit beiden Skriptstaenden). Danach kommt das Ergebnis aus dem
Zwischenspeicher, bis sich eine Quelle aendert. Werkzeuge > SDK-Index neu
aufbauen erzwingt es.

## Export: "NICHT OK"

Eine exportierte Quelle ergibt nicht mehr die Bytes des Spiels. Meist wurde
das SDK geaendert, nachdem der Index gebaut war: Werkzeuge > SDK-Index neu
aufbauen, dann noch einmal exportieren.

## Ein Mod-Skript ist orange

Die Mod hat das Skript veraendert. Die SDK-Quelle passt nicht mehr; du
siehst die Decompiler-Ausgabe der Mod-Fassung.
''', '''# Troubleshooting

## "Game: not found"

Click the red line at the top and choose the Two Worlds folder (the one
with `WDFiles` in it), or File > Settings.

## "SDK: none found"

Without an SDK there is only the decompiler view. Choose the SDK folder
(with `Scripts` and `Tools\\EarthC.exe`) with Tools > Add SDK folder.

## The first start takes a while

The first time the tool compiles every source of every SDK (measured: 25 s
for SDK 1.3 with both script sets). Afterwards the result comes from the
cache until a source changes. Tools > Rebuild SDK index forces it.

## Export: "NOT OK"

An exported source no longer gives the game's bytes. Usually the SDK was
changed after the index was built: Tools > Rebuild SDK index, then export
again.

## A mod script is orange

The mod changed the script. The SDK source no longer matches; you see the
decompiler output of the mod's version.
''')


CHAPTERS = (
    ('start', ('Einstieg', 'Getting started'), ch_start),
    ('first', ('Erstes Ergebnis in 10 Minuten', 'First result in 10 minutes'), ch_first),
    ('read', ('Spiel, Mods und SDKs', 'Game, mods and SDKs'), ch_read),
    ('status', ('Status der Skripte', 'Status of the scripts'), ch_status),
    ('export', ('Exportieren', 'Export'), ch_export),
    ('update', ('SDK aktualisieren', 'Update an SDK'), ch_update),
    ('decompiler', ('Decompiler-Ansicht', 'Decompiler view'), ch_decompiler),
    ('reference', ('Referenz', 'Reference'), ch_reference),
    ('trouble', ('Fehlersuche', 'Troubleshooting'), ch_trouble),
)


_SEPARATOR = re.compile(r'^\|[\s|:-]+\|?$')
_LIST_ITEM = re.compile(r'^(- |\d+\. )')


def _prepare(text):
    """Join wrapped prose lines into paragraphs and turn markdown tables into
    aligned columns, so the text widget shows them readably."""
    out, para, table, in_code = [], [], [], False

    def flush_para():
        if para:
            out.append(' '.join(x.strip() for x in para))
            para.clear()

    def flush_table():
        if not table:
            return
        rows = [[c.strip().replace('`', '') for c in r.strip().strip('|').split('|')]
                for r in table if not _SEPARATOR.match(r.strip())]
        ncol = max(len(r) for r in rows)
        widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(ncol)]
        out.append('```')
        for n, r in enumerate(rows):
            cells = [(r[i] if i < len(r) else '').ljust(widths[i]) for i in range(ncol)]
            out.append('  '.join(cells).rstrip())
            if n == 0:
                out.append('  '.join('-' * w for w in widths))
        out.append('```')
        table.clear()

    for ln in text.split('\n'):
        if ln.startswith('```'):
            flush_para()
            flush_table()
            in_code = not in_code
            out.append(ln)
            continue
        if in_code:
            out.append(ln)
            continue
        if ln.startswith('|'):
            flush_para()
            table.append(ln)
            continue
        flush_table()
        stripped = ln.strip()
        if not stripped or ln.startswith('#'):
            flush_para()
            out.append(ln)
        elif _LIST_ITEM.match(stripped):
            flush_para()
            para.append(ln)
        else:
            para.append(ln)
    flush_para()
    flush_table()
    return '\n'.join(out)


def render_markdown(txt, text):
    in_code = False
    for line in text.split('\n'):
        if line.startswith('```'):
            in_code = not in_code
            continue
        if in_code or line.startswith('|'):
            txt.insert('end', line + '\n', 'code')
            continue
        m = re.match(r'(#{1,3}) (.*)', line)
        if m:
            txt.insert('end', m.group(2) + '\n', 'h%d' % len(m.group(1)))
            continue
        tag = None
        if re.match(r'\s*[-*] ', line):
            line = '• ' + re.sub(r'^\s*[-*] ', '', line)
            tag = 'li'
        elif re.match(r'\s*\d+\. ', line):
            tag = 'li'
        for part in re.split(r'(`[^`]+`|\*\*[^*]+\*\*)', line):
            if part.startswith('`') and part.endswith('`') and len(part) > 1:
                txt.insert('end', part[1:-1], ('inline',) + ((tag,) if tag else ()))
            elif part.startswith('**') and part.endswith('**'):
                txt.insert('end', part[2:-2], ('bold',) + ((tag,) if tag else ()))
            else:
                part = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', part)
                txt.insert('end', part, tag)
        txt.insert('end', '\n', tag)


def chapter_text(cid):
    for key, _title, fn in CHAPTERS:
        if key == cid:
            return fn()
    return ''


def check_sources():
    """Every table in every chapter (both languages) carries a source line."""
    import eco_tool as M
    saved = M._LANG
    try:
        for lang in ('de', 'en'):
            M._LANG = lang
            for cid, _t, fn in CHAPTERS:
                lines = fn().split('\n')
                for i, ln in enumerate(lines):
                    if ln.startswith('|---'):
                        j = i + 1
                        while j < len(lines) and lines[j].startswith('|'):
                            j += 1
                        tail = '\n'.join(lines[j:j + 3])
                        assert 'Quelle:' in tail or 'Source:' in tail, f'{cid}/{lang}: table without source'
    finally:
        M._LANG = saved


class GuideWindow:
    _open = None

    @classmethod
    def show(cls, app, chapter='start'):
        win = cls._open
        if win is not None:
            try:
                win.front()
                win.select(chapter)
                return win
            except tk.TclError:
                cls._open = None
        cls._open = cls(app, chapter)
        return cls._open

    def __init__(self, app, chapter='start'):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title('EcoTool Guide')
        self.win.geometry('1000x700')
        self.win.minsize(820, 520)
        theme.dark_titlebar(self.win)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self.win.bind('<Escape>', lambda e: self.close())
        top = ttk.Frame(self.win, padding=(10, 8))
        top.pack(fill='x')
        ttk.Label(top, text=_l('Suche', 'Search')).pack(side='left')
        self.q = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.q, width=32)
        ent.pack(side='left', padx=6)
        ent.bind('<KeyRelease>', lambda e: self._search())
        self.hits = ttk.Label(top, style='Muted.TLabel')
        self.hits.pack(side='left', padx=8)
        body = ttk.PanedWindow(self.win, orient='horizontal')
        body.pack(fill='both', expand=True)
        left = ttk.Frame(body)
        self.tree = ttk.Treeview(left, show='tree', selectmode='browse')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._show_selected())
        right = ttk.Frame(body)
        sb = ttk.Scrollbar(right, orient='vertical')
        self.txt = tk.Text(right, wrap='word', bd=0, padx=26, pady=20, cursor='arrow',
                           spacing1=2, spacing3=4, yscrollcommand=sb.set, font=('Segoe UI', 10))
        sb.configure(command=self.txt.yview)
        sb.pack(side='right', fill='y')
        self.txt.pack(fill='both', expand=True)
        for tag, kw in (('h1', dict(font=theme.FONT_H1, foreground=theme.GOLD, spacing1=18)),
                        ('h2', dict(font=theme.FONT_H2, foreground=theme.GOLD_HI, spacing1=14)),
                        ('h3', dict(font=('Segoe UI Semibold', 10), foreground=theme.GOLD_HI, spacing1=8)),
                        ('li', dict(lmargin1=20, lmargin2=34)),
                        ('code', dict(font=theme.FONT_MONO, background=theme.FIELD, lmargin1=16, lmargin2=16)),
                        ('inline', dict(font=theme.FONT_MONO, foreground=theme.GOLD_HI)),
                        ('bold', dict(font=('Segoe UI Semibold', 10))),
                        ('hit', dict(background=theme.SEL, foreground=theme.GOLD_HI))):
            self.txt.tag_configure(tag, **kw)
        body.add(left, weight=0)
        body.add(right, weight=1)
        self.win.update_idletasks()
        try:
            body.sashpos(0, 250)
        except tk.TclError:
            pass
        self._fill_tree()
        self.select(chapter)
        self.front()

    def front(self):
        """Das Fenster nach vorn holen - sonst geht es hinter dem Hauptfenster auf."""
        try:
            self.win.lift()
            self.win.focus_force()
            self.win.attributes('-topmost', True)          # einmal nach vorn,
            self.win.after(120, lambda: self.win.attributes('-topmost', False))
        except tk.TclError:                                # und gleich wieder normal
            pass

    def close(self):
        GuideWindow._open = None
        self.win.destroy()

    def _fill_tree(self, only=None):
        self.tree.delete(*self.tree.get_children())
        lang = 0 if _lang() == 'de' else 1
        for i, (cid, titles, _fn) in enumerate(CHAPTERS, start=1):
            if only is not None and cid not in only:
                continue
            self.tree.insert('', 'end', iid=cid, text=f'{i}. {titles[lang]}')

    def select(self, cid):
        if cid not in {c for c, _t, _f in CHAPTERS}:
            cid = 'start'
        if not self.tree.exists(cid):
            self._fill_tree()
        self.tree.selection_set(cid)
        self.tree.see(cid)
        self._show(cid)

    def _show_selected(self):
        sel = self.tree.selection()
        if sel:
            self._show(sel[0])

    def _show(self, cid):
        self.current = cid
        self.txt.configure(state='normal')
        self.txt.delete('1.0', 'end')
        render_markdown(self.txt, _prepare(chapter_text(cid)))
        self._mark_hits()
        self.txt.configure(state='disabled')

    def _search(self):
        needle = self.q.get().strip().lower()
        if not needle:
            self._fill_tree()
            self.hits.configure(text='')
            self.select(getattr(self, 'current', 'start'))
            return
        found = [cid for cid, _t, fn in CHAPTERS if needle in fn().lower()]
        self._fill_tree(set(found))
        self.hits.configure(text=_l('{n} Kapitel', '{n} chapters').format(n=len(found)))
        if found:
            self.select(found[0])

    def _mark_hits(self):
        needle = self.q.get().strip() if hasattr(self, 'q') else ''
        self.txt.tag_remove('hit', '1.0', 'end')
        if not needle:
            return
        first = None
        pos = '1.0'
        while True:
            pos = self.txt.search(needle, pos, nocase=True, stopindex='end')
            if not pos:
                break
            end = f'{pos}+{len(needle)}c'
            self.txt.tag_add('hit', pos, end)
            first = first or pos
            pos = end
        if first:
            self.txt.see(first)
