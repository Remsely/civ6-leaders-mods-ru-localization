# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Civilization VI mod that adds Russian (`ru_RU`) text for third-party leader/civilization mods from the Steam Workshop pack (JFD, Sukritact, etc.) that lack a Russian translation. One game mod (`mod.json`) contains many per-target translations; each is enabled only when its target mod is active (`ModInUse` criterion on the target's GUID).

`mods.csv` lists the 28 target mods (no or partial Russian). The target mods' sources are read from the local Steam Workshop folder (`CIV6_WORKSHOP_DIR`, default `C:\Program Files (x86)\Steam\steamapps\workshop\content\289070\<workshop_id>`).

## Commands

All tooling is `tools/civ6loc.py` (Python 3.10+, stdlib only):

```
python tools/civ6loc.py status                      # progress vs mods.csv
python tools/civ6loc.py extract <workshop_id> <Key> # draft ru_RU.xml from the target's en_US text
python tools/civ6loc.py validate [Key ...]          # tags, placeholders, case forms, GUID drift
python tools/civ6loc.py build                       # generate dist/<folder>/ (modinfo + texts)
python tools/civ6loc.py install                     # build + copy to Documents\My Games\...\Mods
python tools/civ6loc.py package                     # dist/release/*.zip for GitHub Releases
python tools/civ6loc.py workshop                    # dist/workshop/ + .vdf for steamcmd (see docs/RELEASING.md)
python tools/civ6loc.py sync-docs                   # refresh the mod list in README.md
```

Local testing steps: `docs/TESTING.md`. Translation rules and workflow: `docs/TRANSLATION_GUIDE.md`.

## Architecture

- `src/<Key>/meta.json` + `ru_RU.xml` — one folder per target mod. `status` is `draft` or `ready`; only `ready` is built.
- The `.modinfo` is **generated** by `build` (criteria, `FrontEndActions`/`InGameActions` `UpdateText`, file list) — never edit it by hand; `dist/` is gitignored.
- Texts use `<Replace Tag Language="ru_RU">`. Name tags (`LOC_{LEADER,UNIT,BUILDING,DISTRICT,IMPROVEMENT,CIVILIZATION}_*_NAME`, `LOC_CIVILIZATION_*_ADJECTIVE`) need six `|`-separated case forms plus `Gender`/`Plurality`, matching the official `Vanilla_ru_RU.xml`.
- `extract`/`validate` read the source mod's English text from `<Row>/<Replace>` in XML and from `INSERT … INTO LocalizedText (…) VALUES (…)` in SQL files listed under `UpdateText` (Bavaria, Leugi's Mexico). `INSERT … SELECT` rows that copy other tags are skipped. Our translations are always XML.

## Notes

- Developed on Windows. Use UTF-8 for all text files; no "ё" (follows the official game text).
- The `[RUS] Localization Pack` workshop mod (id 3661835263) may later cover some of the same targets; check it before starting a new one.
