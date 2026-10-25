"""Сверка двух версий одного документа: рабочая (docx) ↔ эталон (официальный PDF).

Зачем: официальная публикация СП/ГОСТ существует в виде PDF (часто скан с
текстовым слоем ABBYY). Извлекать из неё канонический текст целиком — плохая идея:
layout-модели теряют пункты в многоколоночной вёрстке и плодят ложные заголовки.
Зато официальный PDF идеален как **эталон для сверки** и как источник номеров
страниц. Этот модуль делает и то, и другое:

  * compare  — покрытие пунктов, разделы, таблицы, числовые значения;
  * pages    — перенос номеров страниц издания в рабочую версию (pages.json).

    python -m normconv.compare compare md/sp-56….md md-official/sp-56….md
    python -m normconv.compare pages   md/sp-56….md --from md-official/sp-56….pages.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

CLAUSE_ID_RE = re.compile(r"\^c(\d+(?:-\d+)+)")
CLAUSE_TEXT_RE = re.compile(r"(?:^|\n)>?\s*\*\*(\d{1,2}\.\d+(?:\.\d+)*)\*\*")
EXCLUDED_RE = re.compile(r"Исключен\w*\s+с\s+\d{2}\.\d{2}\.\d{4}", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
SECTION_RE = re.compile(r"^(\d{1,2})\s+[А-ЯЁ]")
VALUE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:м²|м2|м\^2|м3|м³|кПа|МПа|кгс?|кН|т\b|мм|см|м\b|ч\b|мин|лк|дБ|Вт|°C|град)")


CLAUSE_HEADING_RE = re.compile(r"^#{1,6}\s+(\d{1,2}(?:\.\d+)+)\s", re.M)


def clause_set(text: str) -> set:
    out = set()
    for m in CLAUSE_ID_RE.finditer(text):
        out.add(m.group(1).replace("-", "."))
    for m in CLAUSE_TEXT_RE.finditer(text):
        out.add(m.group(1))
    # нумерованные заголовки («## 5.8 Особенности…») — те же пункты: в docx-пути
    # они становятся разделами без block ID, в pdf-пути — пунктами ^c5-8;
    # без этого compare даёт ложные «потери» (кейс СП 22.13330.2016)
    for m in CLAUSE_HEADING_RE.finditer(text):
        out.add(m.group(1))
    return out


def sections(text: str) -> list:
    out = []
    for m in HEADING_RE.finditer(text):
        t = m.group(2).strip()
        if SECTION_RE.match(t) and len(t) < 90:
            out.append(t)
    return out


def tables(md_path: str) -> list:
    p = os.path.splitext(md_path)[0] + ".tables.jsonl"
    if not os.path.exists(p):
        return []
    rows = []
    for line in open(p, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def values(text: str) -> Counter:
    """Числовые значения с единицами, нормализованные (переносы строк склеены)."""
    flat = re.sub(r"\s+", " ", text)
    out = Counter()
    for m in VALUE_RE.finditer(flat):
        v = m.group(0).replace(",", ".").strip()
        v = re.sub(r"\s+", " ", v)
        out[v] += 1
    return out


def cmd_compare(a) -> int:
    work = open(a.work, encoding="utf-8").read()
    ref = open(a.reference, encoding="utf-8").read()
    cw, cr = clause_set(work), clause_set(ref)
    sw, sr = sections(work), sections(ref)
    tw, tr = tables(a.work), tables(a.reference)
    vw, vr = values(work), values(ref)

    # объяснённые расхождения (правки/сверка-<doc_id>.yaml): перенумерации
    # изменением, артефакты извлечения эталона — не считаются потерями
    explained: list = []
    explained_map: dict = {}
    if getattr(a, "explained", ""):
        import yaml as _yaml
        with open(a.explained, encoding="utf-8") as fh:
            for rec in (_yaml.safe_load(fh) or []):
                e = str(rec.get("эталон", "")).strip()
                if e:
                    explained_map[e] = rec

    missing_all = sorted(cr - cw)
    # пункт может отсутствовать законно: он исключён более поздним изменением,
    # которого нет в эталонном издании (эталон — исходная редакция)
    missing, excluded_by_amendment = [], []
    def num_tuple(x):
        try:
            return tuple(int(p) for p in x.split("."))
        except ValueError:
            return (10 ** 6,)

    excluded_set = set()
    for m in re.finditer(r"\*\*(\d{1,2}(?:\.\d+)+)\*\*[^\n]{0,10}?(\d{1,2}(?:\.\d+)+)\s*Исключен",
                         work, re.I):
        lo, hi = num_tuple(m.group(1)), num_tuple(m.group(2))
        for cl in missing_all:
            if lo <= num_tuple(cl) <= hi:
                excluded_set.add(cl)
    for m in re.finditer(r"\*\*(\d{1,2}(?:\.\d+)+)\*\*[^\n]{0,40}Исключен", work, re.I):
        excluded_set.add(m.group(1))
    for m in re.finditer(r"(?:Пункты|пункты)\s+(\d[\d.]*)\s*[–—-]\s*(\d[\d.]*)\s+исключены", work, re.I):
        lo, hi = num_tuple(m.group(1)), num_tuple(m.group(2))
        for cl in missing_all:
            if lo <= num_tuple(cl) <= hi:
                excluded_set.add(cl)
    for c in missing_all:
        if c in excluded_set:
            excluded_by_amendment.append(c)
        elif c in explained_map:
            rec = explained_map[c]
            w = str(rec.get("рабочая", "")).rstrip("*").strip()
            if not w or w in cw:
                explained.append((c, rec))
            else:
                missing.append(c)   # объяснение есть, но рабочего пункта реально нет
        else:
            missing.append(c)
    extra = sorted(cw - cr)          # есть у нас, нет в эталоне — чаще артефакт извлечения эталона
    vdiff = [(k, vr[k], vw[k]) for k in sorted(set(vr) | set(vw)) if vr[k] != vw[k]]

    cov = (len(cr & cw) / len(cr) * 100) if cr else 100.0
    L = ["# Сверка: рабочая версия ↔ эталон", "",
         "| | рабочая | эталон |",
         "| --- | --- | --- |",
         "| файл | `%s` | `%s` |" % (a.work, a.reference),
         "| слов | %d | %d |" % (len(work.split()), len(ref.split())),
         "| пунктов | %d | %d |" % (len(cw), len(cr)),
         "| разделов | %d | %d |" % (len(sw), len(sr)),
         "| таблиц | %d | %d |" % (len(tw), len(tr)),
         "| строк в таблицах | %d | %d |" % (sum(t.get("n_rows", 0) for t in tw),
                                              sum(t.get("n_rows", 0) for t in tr)),
         "| числовых значений с единицами | %d | %d |" % (sum(vw.values()), sum(vr.values())),
         "",
         "**Покрытие пунктов эталона: %.1f%%** (%d из %d)" % (cov, len(cr & cw), len(cr)), ""]

    verdict = "OK"
    if excluded_by_amendment:
        L += ["## ℹ️ Пункты, исключённые изменением (%d)" % len(excluded_by_amendment), "",
              "В рабочей версии они помечены «Исключены с ДАТА. — Изменение N …», а эталон —",
              "исходное издание без этого изменения. Это не потеря текста, а **разница редакций**:",
              "рабочая версия новее. Требуется только убедиться, что номер изменения и дата верны.", ""]
        L += ["- `%s`" % c for c in excluded_by_amendment[:40]]
        L.append("")
    if explained:
        L += ["## ℹ️ Объяснённые расхождения (%d)" % len(explained), "",
              "Зафиксированы в `%s`: перенумерации изменениями и артефакты"
              % os.path.basename(a.explained),
              "извлечения эталона. Потерями не считаются.", "",
              "| эталон | рабочая версия | причина |", "| --- | --- | --- |"]
        for c, rec in explained:
            L.append("| `%s` | `%s` | %s |" % (c, rec.get("рабочая", "—"),
                                               rec.get("причина", "")))
        L.append("")
    if missing:
        verdict = "ЕСТЬ ПОТЕРИ"
        L += ["## ❌ Пункты эталона, отсутствующие в рабочей версии (%d)" % len(missing), "",
              "Это потери текста — исправлять обязательно.", ""]
        L += ["- `%s`" % m for m in missing[:60]]
        L.append("")
    else:
        L += ["## ✅ Потерь пунктов нет", "",
              "Все пункты, найденные в эталоне, присутствуют в рабочей версии.", ""]
    if extra:
        L += ["## Пункты рабочей версии, не найденные в эталоне (%d)" % len(extra), "",
              "Обычно это не ошибка рабочей версии, а то, что извлечение из PDF",
              "не распознали как пункт (многоколоночная вёрстка, `<u>` в номере,",
              "определения внутри блоков). Проверить выборочно.", ""]
        L += ["- `%s`" % e for e in extra[:60]]
        L.append("")
    if sw != sr:
        L += ["## Разделы", "", "| рабочая | эталон |", "| --- | --- |"]
        for i in range(max(len(sw), len(sr))):
            L.append("| %s | %s |" % (sw[i] if i < len(sw) else "—", sr[i] if i < len(sr) else "—"))
        L.append("")
    else:
        L += ["## ✅ Разделы совпадают (%d)" % len(sw), ""]
    if vdiff:
        L += ["## Расхождения числовых значений (%d)" % len(vdiff), "",
              "| значение | эталон | рабочая |", "| --- | --- | --- |"]
        for k, r_, w_ in vdiff[:60]:
            L.append("| `%s` | %d | %d |" % (k, r_, w_))
        L += ["", "Часть расхождений — артефакты извлечения PDF (переносы строк,",
              "склейки единиц измерения). Требуют выборочной ручной сверки.", ""]
    else:
        L += ["## ✅ Числовые значения совпадают", ""]

    L += ["## Как читать этот отчёт", "",
          "* **Потери пунктов** — единственная по-настоящему критичная секция. Если её нет,",
          "  рабочая версия содержит весь текст эталона.",
          "* **«Пункты, не найденные в эталоне»** — почти всегда дефект извлечения из PDF",
          "  (многоколоночная вёрстка, номер в `<u>`, определения внутри блоков), а не лишнее",
          "  в рабочей версии. Проверяется выборочно.",
          "* **Таблиц и разделов в эталоне может быть больше** — layout-модели дробят одну",
          "  таблицу на несколько pipe-блоков и объявляют заголовками случайные короткие строки.",
          "  Сравнивать имеет смысл строки таблиц и состав разделов, а не их количество.",
          "* **Расхождения числовых значений** требуют ручной сверки: часть из них — артефакты",
          "  переносов строк и склейки единиц измерения в PDF.",
          "",
          "## Вердикт", "",
          "**%s**. Покрытие пунктов %.1f%% (%d из %d), исключено изменением: %d, "
          "объяснено: %d, расхождений числовых значений: %d." % (
              verdict, cov, len(cr & cw), len(cr), len(excluded_by_amendment),
              len(explained), len(vdiff)), "",
          "Автоматическая сверка не заменяет выборочную ручную проверку таблиц",
          "(в эталоне слияния ячеек, в pipe-таблицах PDF они не сохраняются).", ""]

    out = a.out or os.path.join(os.path.dirname(a.work) or ".", "COMPARISON.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print("вердикт: %s | покрытие пунктов %.1f%% (%d/%d) | объяснено %d | расхождений значений %d"
          % (verdict, cov, len(cr & cw), len(cr), len(explained), len(vdiff)))
    print("-> %s" % out)
    return 1 if missing else 0


def cmd_pages(a) -> int:
    """Перенести номера страниц издания в рабочую версию (pages.json + frontmatter)."""
    src = json.load(open(a.from_pages, encoding="utf-8"))
    ref = src.get("clauses") or {}
    md_path = a.md
    text = open(md_path, encoding="utf-8").read()
    sidecar = os.path.splitext(md_path)[0] + ".pages.json"
    out = {"doc_id": src.get("doc_id", ""),
           "reference_source": src.get("source_sha256", ""),
           "reference_url": src.get("reference_url", ""),
           "pages_total": src.get("pages_total"),
           "clauses": {}}
    found = 0
    for clause in sorted(clause_set(text)):
        if clause in ref:
            out["clauses"][clause] = ref[clause]
            found += 1
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    # пометка во фронтмэттере
    if "pages_source:" not in text:
        text = text.replace("\nstats:", "\npages_source:\n  edition: \"%s\"\n  matched: %d\n  file: %s\n\nstats:" % (
            os.path.basename(a.from_pages), found, os.path.basename(sidecar)), 1)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    print("страниц проставлено: %d из %d пунктов -> %s" % (found, len(clause_set(text)), sidecar))
    return 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="normconv.compare", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare", help="сверить рабочую версию с эталоном")
    c.add_argument("work")
    c.add_argument("reference")
    c.add_argument("--out", default="")
    c.add_argument("--explained", default="",
                   help="yaml объяснённых расхождений (правки/сверка-<doc_id>.yaml): "
                        "список {эталон, рабочая, причина} — перенумерации изменением и "
                        "артефакты эталона, которые не считаются потерями")
    c.set_defaults(func=cmd_compare)
    g = sub.add_parser("pages", help="перенести номера страниц издания в рабочую версию")
    g.add_argument("md")
    g.add_argument("--from", dest="from_pages", required=True)
    g.set_defaults(func=cmd_pages)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
