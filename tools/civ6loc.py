#!/usr/bin/env python3
"""Инструменты для сборки русской локализации модов Civilization VI.

Команды:
  extract   создать черновик перевода для мода из Steam Workshop
  validate  проверить переводы (теги, плейсхолдеры, падежные формы, дрейф версии)
  build     собрать готовый к установке мод в dist/
  install   собрать и скопировать мод в папку Mods игры
  uninstall удалить установленный мод из папки Mods игры
  status    показать, какие моды из mods.csv уже переведены
  package   собрать zip для GitHub Releases (dist/release/)
  workshop  подготовить папку и .vdf для загрузки в Steam Workshop (dist/workshop/)
  sync-docs обновить список модов в README.md

Зависимостей нет, нужен только Python 3.10+. Подробности: docs/TRANSLATION_GUIDE.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
MOD_CONFIG = ROOT / "mod.json"
MODS_CSV = ROOT / "mods.csv"

WORKSHOP_DIR = Path(os.environ.get(
    "CIV6_WORKSHOP_DIR",
    r"C:\Program Files (x86)\Steam\steamapps\workshop\content\289070"))
GAME_MODS_DIR = Path(os.environ.get(
    "CIV6_MODS_DIR",
    str(Path.home() / "Documents" / "My Games" / "Sid Meier's Civilization VI" / "Mods")))

LANG = "ru_RU"
SOURCE_LANG = "en_US"

# Теги, чьи значения в русской локализации игры записываются шестью падежными
# формами через "|" и сопровождаются Gender/Plurality (так сделано в Vanilla_ru_RU.xml).
DECLINED_TAG = re.compile(
    r"^LOC_(LEADER(?!_TRAIT_)|CIVILIZATION|UNIT|BUILDING|DISTRICT|IMPROVEMENT)_\w+_NAME$"
    r"|^LOC_CIVILIZATION_\w+_ADJECTIVE$")
CASE_FORMS = 6

# [Word] без подчеркивания (например, редакторская вставка "[Lyon]") — не плейсхолдер игры
PLACEHOLDER = re.compile(r"\[(?:NEWLINE|TAB|[A-Za-z0-9]+_[A-Za-z0-9_]+)\]|\{[^{}]+\}|%\d+")
CYRILLIC = re.compile("[\u0400-\u04FF]")


# --------------------------------------------------------------------------- IO

def read_text(path: Path) -> str:
    """Читает файл мода. Некоторые моды сохранены в cp1252 вопреки заголовку XML."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ XML parsing

@dataclass
class Row:
    tag: str
    text: str            # как в XML (экранированный)
    gender: str | None = None
    plurality: str | None = None


ROW_RE = re.compile(r"<(Row|Replace)\s+([^>]*?)(/?)>(.*?)</\1>", re.S)


def parse_rows(xml: str) -> dict[str, dict[str, Row]]:
    """{язык: {тег: Row}} из файла с <Row>/<Replace> внутри <LocalizedText>."""
    out: dict[str, dict[str, Row]] = {}
    xml = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
    for m in ROW_RE.finditer(xml):
        attrs, body = m.group(2), m.group(4)
        tag = re.search(r'Tag="([^"]+)"', attrs)
        lang = re.search(r'Language="([^"]+)"', attrs)
        text = re.search(r"<Text>(.*?)</Text>", body, re.S)
        if not (tag and lang and text):
            continue
        gender = re.search(r"<Gender>(.*?)</Gender>", body, re.S)
        plural = re.search(r"<Plurality>(.*?)</Plurality>", body, re.S)
        out.setdefault(lang.group(1), {})[tag.group(1)] = Row(
            tag.group(1), text.group(1),
            gender.group(1) if gender else None,
            plural.group(1) if plural else None)
    return out


# ------------------------------------------------------------------ SQL parsing

SQL_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+LocalizedText\s*\(([^)]*)\)\s*VALUES", re.I)


def _strip_sql_comments(sql: str) -> str:
    """Убирает `-- ...` и `/* ... */` вне строковых литералов."""
    out: list[str] = []
    i, n = 0, len(sql)
    quote = ""
    while i < n:
        c = sql[i]
        if quote:
            out.append(c)
            if c == quote:
                if i + 1 < n and sql[i + 1] == quote:   # удвоенная кавычка внутри строки
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = ""
        elif c in "'\"":
            quote = c
            out.append(c)
        elif sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _parse_sql_values(sql: str, pos: int) -> list[list[str]]:
    """Разбирает `(...), (...), ...` начиная с pos и возвращает список кортежей значений."""
    rows: list[list[str]] = []
    n = len(sql)
    ws = " \t\r\n"
    while pos < n:
        while pos < n and (sql[pos] in ws or sql[pos] == ","):
            pos += 1
        if pos >= n or sql[pos] != "(":
            break
        pos += 1
        values: list[str] = []
        while pos < n:
            while pos < n and sql[pos] in ws:
                pos += 1
            if pos < n and sql[pos] in "'\"":
                q = sql[pos]
                pos += 1
                buf: list[str] = []
                while pos < n:
                    if sql[pos] == q:
                        if pos + 1 < n and sql[pos + 1] == q:
                            buf.append(q)
                            pos += 2
                            continue
                        pos += 1
                        break
                    buf.append(sql[pos])
                    pos += 1
                values.append("".join(buf))
            else:
                start = pos
                while pos < n and sql[pos] not in ",)":
                    pos += 1
                values.append(sql[start:pos].strip())
            while pos < n and sql[pos] in ws:
                pos += 1
            if pos < n and sql[pos] == ",":
                pos += 1
                continue
            if pos < n and sql[pos] == ")":
                pos += 1
                break
        rows.append(values)
    return rows


def parse_sql_rows(sql: str) -> dict[str, dict[str, Row]]:
    """{язык: {тег: Row}} из `INSERT ... INTO LocalizedText (...) VALUES (...)`.

    Конструкции `INSERT ... SELECT ... FROM LocalizedText` (копирование чужих тегов)
    пропускаются: у них нет собственного текста.
    """
    out: dict[str, dict[str, Row]] = {}
    sql = _strip_sql_comments(sql)
    for m in SQL_INSERT_RE.finditer(sql):
        cols = [c.strip().lower() for c in m.group(1).split(",")]
        if not {"language", "tag", "text"} <= set(cols):
            continue
        for values in _parse_sql_values(sql, m.end()):
            if len(values) != len(cols):
                continue
            rec = dict(zip(cols, values))
            text = rec["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            out.setdefault(rec["language"], {})[rec["tag"]] = Row(rec["tag"], text)
    return out


# --------------------------------------------------------------- source mod info

@dataclass
class SourceMod:
    workshop_id: str
    guid: str
    name: str
    text_files: list[Path] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return WORKSHOP_DIR / self.workshop_id

    def rows(self, lang: str = SOURCE_LANG) -> dict[str, Row]:
        merged: dict[str, Row] = {}
        for f in self.text_files:
            text = read_text(f)
            parsed = parse_sql_rows(text) if f.suffix.lower() == ".sql" else parse_rows(text)
            merged.update(parsed.get(lang, {}))
        return merged


def load_source_mod(workshop_id: str) -> SourceMod:
    base = WORKSHOP_DIR / workshop_id
    if not base.is_dir():
        sys.exit(f"Мод {workshop_id} не найден в {WORKSHOP_DIR} "
                 f"(можно задать путь через CIV6_WORKSHOP_DIR)")
    infos = sorted(base.glob("*.modinfo"))
    if not infos:
        sys.exit(f"В {base} нет .modinfo")
    raw = read_text(infos[0])
    root = ET.fromstring(raw.lstrip("\ufeff").encode("utf-8"))
    guid = root.get("id") or ""
    name = re.sub(r"\[[A-Za-z0-9_:]+\]", "", root.findtext("Properties/Name", "")).strip()
    files: list[Path] = []
    for upd in root.iter("UpdateText"):
        for f in upd.findall("File"):
            p = base / (f.text or "").strip()
            if p.suffix.lower() in (".xml", ".sql") and p.exists() and p not in files:
                files.append(p)
    return SourceMod(workshop_id, guid, name, files)


# ------------------------------------------------------------------- our targets

@dataclass
class Target:
    key: str
    dir: Path
    meta: dict

    @property
    def xml_files(self) -> list[Path]:
        return sorted(self.dir.glob("*.xml"))

    @property
    def ready(self) -> bool:
        return self.meta.get("status") == "ready"

    def rows(self) -> dict[str, Row]:
        merged: dict[str, Row] = {}
        for f in self.xml_files:
            merged.update(parse_rows(read_text(f)).get(LANG, {}))
        return merged


def load_targets(keys: list[str] | None = None) -> list[Target]:
    targets = []
    for meta_path in sorted(SRC.glob("*/meta.json")):
        key = meta_path.parent.name
        if keys and key not in keys:
            continue
        targets.append(Target(key, meta_path.parent, json.loads(meta_path.read_text("utf-8"))))
    if keys:
        missing = set(keys) - {t.key for t in targets}
        if missing:
            sys.exit(f"Не найдены цели: {', '.join(sorted(missing))}")
    return targets


def load_config() -> dict:
    return json.loads(MOD_CONFIG.read_text("utf-8"))


# ---------------------------------------------------------------------- extract

def render_rows_xml(rows: list[Row], header: str) -> str:
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<!-- {header} -->",
             "<GameData>", "\t<LocalizedText>"]
    for r in rows:
        lines.append(f'\t\t<Replace Tag="{r.tag}" Language="{LANG}">')
        lines.append(f"\t\t\t<Text>{r.text}</Text>")
        if r.gender:
            lines.append(f"\t\t\t<Gender>{r.gender}</Gender>")
        if r.plurality:
            lines.append(f"\t\t\t<Plurality>{r.plurality}</Plurality>")
        lines.append("\t\t</Replace>")
    lines += ["\t</LocalizedText>", "</GameData>", ""]
    return "\n".join(lines)


def cmd_extract(args: argparse.Namespace) -> int:
    mod = load_source_mod(args.workshop_id)
    rows = mod.rows()
    if not rows:
        sys.exit(f"В {mod.name} не найдены тексты {SOURCE_LANG} (ни в XML, ни в SQL-файлах UpdateText)")
    target_dir = SRC / args.key
    if target_dir.exists() and not args.force:
        sys.exit(f"{target_dir} уже существует (используйте --force, чтобы перезаписать)")
    meta = {
        "name": mod.name,
        "workshop_id": mod.workshop_id,
        "mod_guid": mod.guid,
        "status": "draft",
    }
    write_text(target_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    write_text(target_dir / f"{LANG}.xml", render_rows_xml(
        list(rows.values()), f"Черновик: английский текст {mod.name}. Переведите и поставьте status=ready"))
    print(f"Создано {target_dir.relative_to(ROOT)} ({len(rows)} строк, status=draft)")
    return 0


# --------------------------------------------------------------------- validate

@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def placeholders(text: str) -> Counter:
    return Counter(PLACEHOLDER.findall(text))


def validate_target(t: Target) -> Report:
    rep = Report()
    if not t.xml_files:
        rep.errors.append("нет XML-файлов с переводом")
        return rep
    for f in t.xml_files:
        try:
            ET.fromstring(read_text(f).lstrip("\ufeff").encode("utf-8"))
        except ET.ParseError as e:
            rep.errors.append(f"{f.name}: некорректный XML: {e}")
            return rep
        wrong = [l for l in parse_rows(read_text(f)) if l != LANG]
        if wrong:
            rep.errors.append(f"{f.name}: строки с языком {wrong}, ожидается только {LANG}")

    ru = t.rows()
    mod = load_source_mod(t.meta["workshop_id"])
    en = mod.rows()

    if mod.guid.lower() != t.meta["mod_guid"].lower():
        rep.errors.append(f"GUID исходного мода изменился: {mod.guid} (в meta.json {t.meta['mod_guid']})")
    for tag in sorted(set(ru) - set(en)):
        rep.errors.append(f"{tag}: тега нет в исходном моде (опечатка или тег удалён)")
    for tag in sorted(set(en) - set(ru)):
        rep.errors.append(f"{tag}: нет перевода")

    for tag, row in ru.items():
        src = en.get(tag)
        if src is None:
            continue
        if row.text == src.text and (re.search(r"[A-Za-z]", PLACEHOLDER.sub("", row.text))
                                     and not CYRILLIC.search(row.text)):
            rep.warnings.append(f"{tag}: текст совпадает с английским (не переведён?)")
        if placeholders(row.text) != placeholders(src.text):
            rep.errors.append(
                f"{tag}: плейсхолдеры отличаются: было {dict(placeholders(src.text))}, "
                f"стало {dict(placeholders(row.text))}")
        if DECLINED_TAG.match(tag):
            forms = row.text.split("|")
            if len(forms) != CASE_FORMS:
                rep.warnings.append(
                    f"{tag}: ожидается падежных форм: {CASE_FORMS} (через '|'), найдено {len(forms)}")
            if not row.gender or not row.plurality:
                rep.warnings.append(f"{tag}: нет <Gender>/<Plurality>")
    return rep


def cmd_validate(args: argparse.Namespace) -> int:
    targets = load_targets(args.keys or None)
    failed = False
    for t in targets:
        rep = validate_target(t)
        status = t.meta.get("status", "?")
        bad = bool(rep.errors) or (args.strict or t.ready) and bool(rep.warnings)
        if status == "draft" and not args.keys:
            # черновики проверяем, но не валим из-за них общий результат
            bad = False
        mark = "FAIL" if bad else "ok"
        print(f"[{mark}] {t.key} ({status}): {len(rep.errors)} ошибок, {len(rep.warnings)} предупреждений")
        for e in rep.errors[:args.limit]:
            print(f"    ERROR   {e}")
        for w in rep.warnings[:args.limit]:
            print(f"    WARNING {w}")
        failed |= bad
    return 1 if failed else 0


# ------------------------------------------------------------------------ build

def modinfo_xml(cfg: dict, targets: list[Target]) -> str:
    q = lambda s: escape(s, {'"': "&quot;"})
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           f'<Mod id="{cfg["guid"]}" version="{cfg["version"]}">',
           "  <Properties>",
           f'    <Name>{escape(cfg["name"])}</Name>',
           f'    <Description>{escape(cfg["description"])}</Description>',
           f'    <Teaser>{escape(cfg["teaser"])}</Teaser>',
           f'    <Authors>{escape(cfg["authors"])}</Authors>',
           "    <AffectsSavedGames>0</AffectsSavedGames>",
           "    <CompatibleVersions>1.2,2.0</CompatibleVersions>",
           "  </Properties>",
           "  <ActionCriteria>"]
    for t in targets:
        out.append(f'    <!-- {escape(t.meta["name"])} -->')
        out.append(f'    <Criteria id="{t.key}_Active">')
        out.append(f'      <ModInUse>{t.meta["mod_guid"]}</ModInUse>')
        out.append("    </Criteria>")
    out.append("  </ActionCriteria>")
    files = [f"{t.key}/{f.name}" for t in targets for f in t.xml_files]
    for section, suffix in (("FrontEndActions", "FE"), ("InGameActions", "ING")):
        out.append(f"  <{section}>")
        for t in targets:
            out.append(f'    <UpdateText id="{t.key}_{suffix}">')
            out.append("      <Properties>")
            out.append(f'        <LoadOrder>{cfg.get("load_order", 100)}</LoadOrder>')
            out.append("      </Properties>")
            out.append(f"      <Criteria>{t.key}_Active</Criteria>")
            for f in t.xml_files:
                out.append(f"      <File>{t.key}/{f.name}</File>")
            out.append("    </UpdateText>")
        out.append(f"  </{section}>")
    out.append("  <Files>")
    out += [f"    <File>{f}</File>" for f in files]
    out += ["  </Files>", "</Mod>", ""]
    return "\n".join(out)


def build(include_drafts: bool = False) -> Path:
    cfg = load_config()
    targets = [t for t in load_targets() if t.ready or include_drafts]
    if not targets:
        sys.exit("Нет готовых целей (status=ready). Нечего собирать.")
    bad = False
    for t in targets:
        rep = validate_target(t)
        if rep.errors or rep.warnings:
            bad = True
            print(f"[FAIL] {t.key}: {len(rep.errors)} ошибок, {len(rep.warnings)} предупреждений "
                  f"(запустите: python tools/civ6loc.py validate {t.key})")
    if bad:
        sys.exit("Сборка остановлена: исправьте переводы.")
    out = DIST / cfg["folder"]
    if out.exists():
        shutil.rmtree(out)
    for t in targets:
        for f in t.xml_files:
            dest = out / t.key / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
    write_text(out / f'{cfg["folder"]}.modinfo', modinfo_xml(cfg, targets))
    print(f"Собрано: {out} ({len(targets)} модов)")
    return out


def cmd_build(args: argparse.Namespace) -> int:
    build(include_drafts=args.drafts)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    cfg = load_config()
    out = build(include_drafts=args.drafts)
    dest = GAME_MODS_DIR / cfg["folder"]
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(out, dest)
    print(f"Установлено: {dest}")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    cfg = load_config()
    dest = GAME_MODS_DIR / cfg["folder"]
    if not dest.exists():
        print("Не установлен.")
        return 0
    if not (dest / f'{cfg["folder"]}.modinfo').exists():
        sys.exit(f"{dest} не похож на наш мод, удалите вручную")
    shutil.rmtree(dest)
    print(f"Удалено: {dest}")
    return 0


# ----------------------------------------------------------------------- status

def cmd_status(_: argparse.Namespace) -> int:
    by_id = {t.meta["workshop_id"]: t for t in load_targets()}
    with MODS_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    counts = Counter()
    for r in rows:
        t = by_id.get(r["id"])
        state = t.meta.get("status", "?") if t else "todo"
        counts[state] += 1
        print(f"{state:6} {r['id']:>11}  {r['name']}")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return 0


# ---------------------------------------------------------------------- release

WORKSHOP_CFG = ROOT / "workshop" / "workshop.json"
STEAM_APP_ID = "289070"
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id="
MODS_START, MODS_END = "<!-- MODS:START -->", "<!-- MODS:END -->"


def ready_targets() -> list[Target]:
    return [t for t in load_targets() if t.ready]


def mods_markdown() -> str:
    lines = ["| Мод | Лидер/цивилизация |", "|---|---|"]
    for t in ready_targets():
        lines.append(f"| [{t.meta['name']}]({WORKSHOP_URL}{t.meta['workshop_id']}) | `{t.key}` |")
    return "\n".join(lines)


def mods_bbcode() -> str:
    return "[list]\n" + "\n".join(
        f"[*] [url={WORKSHOP_URL}{t.meta['workshop_id']}]{t.meta['name']}[/url]"
        for t in ready_targets()) + "\n[/list]"


def cmd_sync_docs(_: argparse.Namespace) -> int:
    """Обновляет таблицу модов в README.md между маркерами MODS:START/MODS:END."""
    readme = ROOT / "README.md"
    text = read_text(readme)
    pat = re.compile(re.escape(MODS_START) + r".*?" + re.escape(MODS_END), re.S)
    if not pat.search(text):
        sys.exit(f"В README.md нет маркеров {MODS_START} ... {MODS_END}")
    new = pat.sub(lambda _m: f"{MODS_START}\n{mods_markdown()}\n{MODS_END}", text)
    if new != text:
        write_text(readme, new)
        print("README.md обновлён.")
    else:
        print("README.md уже актуален.")
    return 0


def cmd_package(_: argparse.Namespace) -> int:
    """zip для GitHub Releases: <folder>/… внутри архива, чтобы его можно было просто распаковать в Mods."""
    import hashlib
    import zipfile
    cfg = load_config()
    built = build()
    out_dir = DIST / "release"
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f'{cfg["folder"]}-v{cfg["version"]}.zip'
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(built.rglob("*")):
            if f.is_file():
                z.write(f, Path(cfg["folder"]) / f.relative_to(built))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    write_text(archive.with_suffix(".zip.sha256"), f"{digest}  {archive.name}\n")
    print(f"Архив: {archive} ({archive.stat().st_size // 1024} КБ)\nSHA-256: {digest}")
    return 0


def _vdf(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")


def cmd_workshop(args: argparse.Namespace) -> int:
    """Готовит папку и .vdf для загрузки в Steam Workshop через steamcmd."""
    if not WORKSHOP_CFG.exists():
        sys.exit(f"Нет {WORKSHOP_CFG}")
    wcfg = json.loads(WORKSHOP_CFG.read_text("utf-8"))
    cfg = load_config()
    preview = WORKSHOP_CFG.parent / wcfg["preview"]
    if not preview.exists():
        sys.exit(f"Нет картинки для Workshop: {preview} (PNG/JPG, лучше 512x512, до 1 МБ)")
    description = read_text(WORKSHOP_CFG.parent / wcfg["description_file"]).replace("{{MODS}}", mods_bbcode())
    built = build()
    root = DIST / "workshop"
    if root.exists():
        shutil.rmtree(root)
    content = root / "content" / cfg["folder"]
    shutil.copytree(built, content)
    shutil.copyfile(preview, root / preview.name)
    note = args.note or f'Версия {cfg["version"]}'
    fields = [("appid", STEAM_APP_ID),
              ("publishedfileid", str(wcfg.get("publishedfileid", "0"))),
              ("contentfolder", str((root / "content" / cfg["folder"]).resolve())),
              ("previewfile", str((root / preview.name).resolve())),
              ("visibility", str(wcfg.get("visibility", 0))),
              ("title", wcfg["title"]),
              ("description", description),
              ("changenote", note)]
    vdf = '"workshopitem"\n{\n' + "".join(f'\t"{k}"\t\t"{_vdf(v)}"\n' for k, v in fields) + "}\n"
    vdf_path = root / "workshop_item.vdf"
    write_text(vdf_path, vdf)
    print(f"Готово: {vdf_path}\n")
    print("Загрузка (нужен steamcmd и аккаунт Steam с Civilization VI):")
    print(f'  steamcmd +login <логин> +workshop_build_item "{vdf_path.resolve()}" +quit')
    if str(wcfg.get("publishedfileid", "0")) == "0":
        print("\nЭто первая публикация. steamcmd выведет PublishFileID: впишите его в "
              "workshop/workshop.json (publishedfileid), чтобы дальше обновлять тот же мод.")
    return 0


# ------------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("extract", help="создать черновик перевода")
    s.add_argument("workshop_id")
    s.add_argument("key", help="имя папки в src/, например JFD_Russia_IvanIV")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_extract)

    s = sub.add_parser("validate", help="проверить переводы")
    s.add_argument("keys", nargs="*")
    s.add_argument("--strict", action="store_true", help="предупреждения тоже считать ошибками")
    s.add_argument("--limit", type=int, default=30, help="сколько проблем показывать на мод")
    s.set_defaults(fn=cmd_validate)

    for name, fn, h in (("build", cmd_build, "собрать dist/"),
                        ("install", cmd_install, "собрать и установить в папку Mods игры")):
        s = sub.add_parser(name, help=h)
        s.add_argument("--drafts", action="store_true", help="включить и черновики (только для отладки)")
        s.set_defaults(fn=fn)

    sub.add_parser("uninstall", help="удалить из папки Mods игры").set_defaults(fn=cmd_uninstall)
    sub.add_parser("status", help="прогресс по mods.csv").set_defaults(fn=cmd_status)
    sub.add_parser("package", help="собрать zip для GitHub Releases").set_defaults(fn=cmd_package)
    s = sub.add_parser("workshop", help="подготовить папку и .vdf для загрузки в Steam Workshop")
    s.add_argument("--note", help="описание изменений (changenote)")
    s.set_defaults(fn=cmd_workshop)
    sub.add_parser("sync-docs", help="обновить список модов в README.md").set_defaults(fn=cmd_sync_docs)

    args = p.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
