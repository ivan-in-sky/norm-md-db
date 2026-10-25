"""PDF (официальная публикация) -> канонический Markdown.

Это «путь C» из docs/02-conversion.md: источник без OOXML-разметки, зато
официальный. Layout извлекается pymupdf4llm, дальше работает нормализатор,
который приводит «сырой» Markdown к тому же каноническому формату, что и
docx2md: те же block id (^c8-1-6), те же таблицы, тот же фронтмэттер.

Дополнительно к Markdown строится pages.json — привязка каждого пункта к
странице издания (это то, ради чего официальный PDF ценен: цитирование
«п. 6.2.19, с. 44»).

    python -m normconv.pdf2md sources/faufcc-sp56.pdf --out md/
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys

from . import __version__
from .docx2md import (MATH_CONFIDENCE_THRESHOLD, Report, Options, anchor_variants,
                      clause_block_id, clean_text, dump_yaml, make_anchor, sha256_bytes)
from .meta import extract as extract_meta
from . import obsidian as _ob

RUNNING_HEAD_RE = re.compile(
    r"^\s*(СП|ГОСТ(?:\s*Р)?|СНиП|РД|МДС|ВСН|ТСН)\s*\d[\d.]*"
    r"\s*(?:[—–\-]\s*\d{4}\s*[гГ]?\.?)?\s*\*?\s*$")
PAGE_NUM_RE = re.compile(r"^\s*(?:[IVXLC]+|\d{1,4})\s*$")
CLAUSE_LINE_RE = re.compile(r"^(\d{1,2}(?:\.\d+){1,3}\*?)\s+(.*)$", re.S)
SECTION_RE = re.compile(r"^(\d{1,2})\s+([А-ЯЁA-Z][^\n]{2,90})$")
SUBSECTION_RE = re.compile(r"^(\d{1,2}\.\d{1,2})\s+([А-ЯЁA-Z][^\n]{2,90})$")
APPENDIX_RE = re.compile(r"^(Приложение|ПРИЛОЖЕНИЕ)\s+([А-ЯA-Z])\b\s*\(?(обязательное|рекомендуемое|справочное)?\)?", re.I)
NOTE_RE = re.compile(r"^П\s?р\s?и\s?м\s?е\s?ч\s?а\s?н\s?и\s?(?:я|е)\b", re.I)
BIBLIO_RE = re.compile(r"^\[(\d{1,3})\]\s+")
TOC_LEADER_RE = re.compile(r"[…\.]{4,}")
HYPHEN_BREAK_RE = re.compile(r"([а-яёa-z])- (?=[а-яёa-z])")
MARK_RE = re.compile(r"</?mark>")


def load_pdf(path: str) -> list[dict]:
    """Страницы с текстом в Markdown-разметке (pymupdf4llm)."""
    import pymupdf4llm

    chunks = pymupdf4llm.to_markdown(path, page_chunks=True, write_images=False,
                                     show_progress=False)
    out = []
    for c in chunks:
        meta = c.get("metadata") or {}
        out.append({
            "page": meta.get("page") or (len(out) + 1),
            "label": str(meta.get("page_label") or meta.get("page") or (len(out) + 1)),
            "text": c.get("text") or "",
        })
    return out


PAGE_LABEL_LINE_RE = re.compile(r"^\s*Страница\s+\d+\s*$")


def detect_running_lines(pages: list[dict], share: float = 0.6) -> set:
    """Строки-«колонтитулы», повторяющиеся почти на всех страницах.

    Экспорты справочных систем (и wkhtmltopdf-печать их страниц) вкладывают в PDF
    скрытый служебный заголовок вида «ГОСТ X … Применяется с … взамен ГОСТ Y …»:
    он повторяется дословно на каждой странице и сбивает extract_meta с толку
    (designation читается из «взамен …»). Такие строки убираем, но факты из них
    (взамен / применяется с) вынимаем заранее — см. convert().
    """
    from collections import Counter

    cnt: Counter = Counter()
    for p in pages:
        seen = {l.strip() for l in p["text"].split("\n") if len(l.strip()) > 40}
        cnt.update(seen)
    need = max(3, int(share * len(pages)))
    return {l for l, c in cnt.items() if c >= need}


def _furniture_key(s: str) -> str:
    """Строка без markdown-обвязки: pymupdf4llm отдаёт колонтитулы вида
    `**ГОСТ 31937—2024**` или `### **ГОСТ 31937—2024**`, а ABBYY ещё и
    разрывает тире пробелом («31937— 2024»)."""
    t = re.sub(r"^[\s#>*_]+|[\s#>*_]+$", "", s)
    t = re.sub(r"\s*([—–\-])\s*", r"\1", t)
    return t.strip()


def strip_page_furniture(text: str) -> str:
    """Убрать колонтитулы и номера страниц: в издании они на каждой странице."""
    keep = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            keep.append("")
            continue
        key = _furniture_key(s)
        if RUNNING_HEAD_RE.match(key):
            continue
        if PAGE_NUM_RE.match(key):
            continue
        if PAGE_LABEL_LINE_RE.match(key):
            continue
        keep.append(line)
    return "\n".join(keep)


def normalize_text(s: str) -> str:
    s = MARK_RE.sub("", s)
    s = HYPHEN_BREAK_RE.sub(r"\1", s)          # «жилищному-\nкоммунального» -> «жилищнокоммунального»
    s = re.sub(r"\s*\n\s*", " ", s)            # внутри абзаца переносы склеиваем
    s = re.sub(r"[ \t]{2,}", " ", s)
    return clean_text(s).strip()


SPACED_NUM_RE = re.compile(r"^\d+(?:\s*\.\s*\d+){1,4}(?:\s\d+)*\*?")


def collapse_spaced_number(flat: str) -> str:
    """«2 . 1 . 1 6 термин…» -> «2.1.16 термин…» (разрядка номера в вёрстке)."""
    m = SPACED_NUM_RE.match(flat)
    if not m:
        return flat
    head = re.sub(r"\s", "", m.group(0))
    return head + flat[m.end():]


class Pdf2Md:
    def __init__(self, path: str, opts: Options | None = None):
        self.path = path
        self.opts = opts or Options()
        self.report = Report()
        self.doc_id = ""
        self.assets_dirname = ""
        self.anchors: dict = {}
        self.sections: list = []
        self.clauses: list = []
        self.pages: dict = {}
        self.tables: list = []
        self.biblio: dict = {}
        self.rights_note = ""
        self._predisl = False

    # ------------------------------------------------------------------ #
    def convert(self, outdir: str, stem: str = "") -> tuple:
        stem = stem or os.path.splitext(os.path.basename(self.path))[0]
        pages = load_pdf(self.path)
        # скрытые служебные заголовки экспортов СПС: повторяются на каждой странице
        running = detect_running_lines(pages)
        self.running_facts: dict = {}
        if running:
            run_txt = re.sub(r"[_*]", "", " ".join(sorted(running)))
            m = re.search(r"взамен\s+((?:СП|СНиП|ГОСТ(?:\s*Р)?)[\s\d.-]{3,40}\d)", run_txt)
            if m:
                self.running_facts["supersedes"] = re.sub(r"\s+", " ", m.group(1)).strip()
            m = re.search(r"[Пп]рименяется с[^\d]{0,10}(\d{1,2}\.\d{1,2}\.\d{4})", run_txt)
            if m:
                self.running_facts["in_force_since"] = m.group(1)
            for p in pages:
                p["text"] = "\n".join(l for l in p["text"].split("\n")
                                      if l.strip() not in running)
            self.report.add_issue("info", "running_lines_stripped",
                                  "повторяющихся служебных заголовков снято: %d стр. на %d стр."
                                  % (len(running), len(pages)))
        # вендорные строки снимаем ДО extract_meta: «Текст сравнения X с Y см. по
        # ссылке» иначе подменяет designation (в Y читается «взамен …»)
        for p in pages:
            p["text"] = _ob.strip_vendor_lines(p["text"])
        full_plain = "\n".join(p["text"] for p in pages)
        first_lines = [normalize_text(l) for l in full_plain.split("\n")[:60]]
        meta = extract_meta([l for l in first_lines if l], stem)

        # правовая оговорка официального издания — важна для NOTICE.md
        m = re.search(r"(©[^©]{0,200}?\d{4})", full_plain)
        if m:
            meta["copyright_notice"] = normalize_text(m.group(1))
        m = re.search(r"(Настоящий нормативный документ не может быть[^\n]{0,400})", full_plain)
        if m:
            self.rights_note = normalize_text(m.group(1))
            meta["rights_note"] = self.rights_note
            self.report.add_issue("warning", "rights_notice",
                                  "в издании есть ограничительная оговорка: %s" % self.rights_note[:120])
        # «Пересмотр СП X» / «Взамен СНиП Y»
        for pat, field in ((r"Пересмотр\s+(СП\s[\d.]+)", "supersedes"),
                           (r"[Вв]замен\s+(СП\s[\d.]+|СНиП\s[\d.-]+|ГОСТ[\sР]*[\d.-]*)", "supersedes")):
            for mm in re.finditer(pat, full_plain):
                meta.setdefault(field, [])
                if mm.group(1) not in meta[field]:
                    meta[field].append(mm.group(1))
        if self.running_facts.get("supersedes"):
            meta.setdefault("supersedes", [])
            if self.running_facts["supersedes"] not in meta["supersedes"]:
                meta["supersedes"].append(self.running_facts["supersedes"])
        if self.running_facts.get("in_force_since") and not meta.get("in_force_since"):
            meta["in_force_since"] = self.running_facts["in_force_since"]

        self.doc_id = meta.get("doc_id") or stem
        self.report.doc_id = self.doc_id
        self.report.meta = meta

        body = []
        toc_lines = []
        toc_done = False
        # абзацы, разорванные переводом страницы, склеиваем: иначе «половина»
        # пункта выглядит как короткий заголовок подраздела и повышается до ##
        merged: list = []
        for pg in pages:
            text = strip_page_furniture(pg["text"])
            for raw_block in re.split(r"\n{2,}", text):
                b = raw_block.strip()
                if not b:
                    continue
                if merged and re.search(r"[a-zа-яё]$", merged[-1][0]) and \
                        re.match(r"^[a-zа-яё*]", b):
                    merged[-1][0] += " " + b
                else:
                    merged.append([b, pg["page"], pg["label"]])
        for block, pg_page, pg_label in merged:
                flat = normalize_text(block)
                # --- оглавление: не переносим в корпус, но сохраняем в метаданные ---
                if re.match(r"^(СОДЕРЖАНИЕ|Содержание)\s*$", flat):
                    toc_lines.append(flat)
                    continue
                if not toc_done:
                    if TOC_LEADER_RE.search(block) or (
                            len(TOC_LEADER_RE.findall(block)) == 0
                            and re.match(r"^\d{1,2}(\.\d+)*\s+[А-ЯЁ]", flat)
                            and toc_lines and "……" in "".join(toc_lines[-3:])):
                        toc_lines.append(flat)
                        continue
                    if re.match(r"^(Введение|1\s+Область применения)\b", flat) and toc_lines:
                        toc_done = True
                rendered = self.render_block(block, pg_page, pg_label, meta)
                if rendered is None:
                    continue
                body.append(rendered)
        meta["toc"] = [l for l in toc_lines if l]

        md_body = self.postprocess("\n\n".join(b for b in body if b.strip()))
        if getattr(self.opts, "glyphs_path", ""):
            md_body = apply_glyph_rules(md_body, self.opts.glyphs_path, self.report)
        if getattr(self.opts, "meta_override_path", ""):
            import yaml as _yaml
            with open(self.opts.meta_override_path, encoding="utf-8") as fh:
                extra = _yaml.safe_load(fh) or {}
            meta.update(extra)
            self.report.add_issue("info", "meta_override",
                                  "поля фронтмэттера из правки: %s" % ", ".join(sorted(extra)))
        self.report.words = len(re.findall(r"[^\s]+", md_body))
        self.report.sections = self.sections
        self.report.headings = {}
        for s in self.sections:
            k = str(s["level"])
            self.report.headings[k] = self.report.headings.get(k, 0) + 1

        os.makedirs(outdir, exist_ok=True)
        conf = self.report.compute_confidence()
        designation = (meta.get("designation") or "").strip()
        self.note_base = (_ob.note_name(designation)
                          if (self.opts.obsidian and designation) else self.doc_id)
        meta["note_name"] = self.note_base
        meta["aliases"] = _ob.build_aliases(designation, meta.get("supersedes_designations") or [])
        fm = self.build_frontmatter(meta, conf, pages)
        full_md = fm + md_body.rstrip() + "\n"
        if self.opts.obsidian:
            registry = _ob.load_registry(self.opts.registry_path)
            if designation:
                registry.add(designation, designation)
            full_md = _ob.postprocess_obsidian(full_md, meta, registry, self.report)
        md_path = os.path.join(outdir, "%s.md" % self.note_base)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(full_md)
        with open(os.path.join(outdir, "%s.pages.json" % self.note_base), "w", encoding="utf-8") as fh:
            json.dump({"doc_id": self.doc_id,
                       "source_sha256": sha256_file(self.path),
                       "pages_total": len(pages),
                       "clauses": self.pages}, fh, ensure_ascii=False, indent=1)
        if self.tables:
            with open(os.path.join(outdir, "%s.tables.jsonl" % self.note_base), "w", encoding="utf-8") as fh:
                for t in self.tables:
                    fh.write(json.dumps(t, ensure_ascii=False) + "\n")
        rep = self.report.to_dict()
        rep["confidence"] = conf
        with open(os.path.join(outdir, "%s.qa.json" % self.note_base), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(outdir, "%s.qa.md" % self.note_base), "w", encoding="utf-8") as fh:
            fh.write(self.report.to_markdown())
        return md_path, rep

    # ------------------------------------------------------------------ #
    def render_block(self, block: str, page: int, page_label: str, meta: dict):
        flat = normalize_text(block)
        if not flat:
            return None
        flat = collapse_spaced_number(flat)
        flat = re.sub(r"^[-–•]\s+(?=\d{1,2}(?:\.\d+)+)", "", flat)   # «- 5.1.3 …» -> «5.1.3 …»
        # остаток титульного листа: строка с самим обозначением до «Предисловия»
        if not self._predisl and meta.get("designation") and \
                flat == normalize_text(str(meta["designation"])):
            return None
        if not self._predisl and (SECTION_RE.match(flat) or CLAUSE_LINE_RE.match(flat)):
            self._predisl = True
        # пункты предисловия («6 ВВЕДЕН ВПЕРВЫЕ», «2 ВНЕСЕН ТК…») — обычные абзацы,
        # куда бы их ни попытался повысить pymupdf или SECTION_RE
        if re.match(r"^\d{1,2}\s+(ВВЕДЕН|ПРИНЯТ|РАЗРАБОТАН|ПОДГОТОВЛЕН|ВНЕСЕН|ВНЕСЕНО|"
                    r"ИЗДАНИЕ|ПЕРЕИЗДАНИЕ|УТВЕРЖДЕН)", flat):
            return flat

        # --- заголовки, которые дал pymupdf ---
        hm = re.match(r"^#{1,6}\s+(.*)$", block.strip(), re.S)
        if hm:
            title = normalize_text(hm.group(1)).strip("*_ ")
            title = re.sub(r"^(\d{1,2}(?:\.\d+){0,3})\.\s+", r"\1 ", title)  # «13. Оценка» -> «13 Оценка»
            # титульный блок издания (до «Предисловия») в теле не дублируем:
            # он уже во фронтмэттере
            if not self._predisl:
                if re.match(r"^Предисловие\s*$", title, re.I):
                    self._predisl = True
                else:
                    return None
            # пункты предисловия («6 ВВЕДЕН ВПЕРВЫЕ»), повышенные pymupdf до H1,
            # возвращаем в обычные абзацы
            if re.match(r"^\d{1,2}\s+(ВВЕДЕН|ПРИНЯТ|РАЗРАБОТАН|ПОДГОТОВЛЕН|ВНЕСЕН|ВНЕСЕНО|"
                        r"ИЗДАНИЕ|ПЕРЕИЗДАНИЕ|УТВЕРЖДЕН)", title):
                return title
            # длинное «заголовок»-предложение — это пункт, а не заголовок
            cm0 = CLAUSE_LINE_RE.match(title)
            if cm0 and len(title) > 60:
                flat, block = title, title
            else:
                return self.emit_heading(title, page, page_label)

        # --- таблица (pipe-синтаксис) ---
        if block.lstrip().startswith("|") or re.search(r"^\|.+\|\s*$", block, re.M):
            return self.emit_table(block, page)

        # --- подписи таблиц ---
        if re.match(r"^Таблица\s+[\dА-Я.]+", flat):
            self._pending_caption = flat
            return "**%s**" % flat

        # --- запись списка литературы ---
        bm = BIBLIO_RE.match(flat)
        if bm:
            key = bm.group(1).lstrip("0") or "0"
            self.biblio[key] = BIBLIO_RE.sub("", flat)
            bid = "biblio-" + key
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            if key == "1":
                self.sections.append({"level": 2, "number": "", "title": "Список использованных источников",
                                      "full": "Список использованных источников"})
                return "## Список использованных источников\n\n%s ^%s" % (flat, bid)
            return "%s ^%s" % (flat, bid)

        # --- примечание ---
        if NOTE_RE.match(flat):
            self.report.notes += 1
            body_txt = re.sub(r"^Примечания?\s*[—–-]?\s*", "", flat, flags=re.I).strip()
            label = "**Примечания**" if flat.lower().startswith("примечания") else "**Примечание**"
            return ("> %s" % label) if not body_txt else ("> %s — %s" % (label, body_txt))

        # --- приложение ---
        am = APPENDIX_RE.match(flat)
        if am:
            return self.emit_heading(flat, page, page_label)

        # --- раздел / подраздел ---
        sm = SECTION_RE.match(flat) or SUBSECTION_RE.match(flat)
        if sm and len(flat) < 110:
            ssm = SUBSECTION_RE.match(flat)
            # «подраздел»-предложение («13.1 Оценку … в следующих случаях:») — пункт
            if ssm and CLAUSE_LINE_RE.match(flat) and \
                    (len(flat) > 60 or flat.rstrip().endswith((':', ';'))):
                pass
            else:
                return self.emit_heading(flat, page, page_label)

        # --- пункт ---
        cm = CLAUSE_LINE_RE.match(flat)
        if cm:
            num, rest = cm.group(1), cm.group(2).strip()
            bid = clause_block_id(num)
            self.clauses.append(num)
            self.pages[num] = {"page": page, "label": page_label}
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            self.report.clauses += 1
            dm = re.match(r"^([а-яёa-z][\w\s()\-,.'’]{0,80}:)\s*(.*)$", rest)
            if dm and len(dm.group(1)) <= 82:
                rest = "**%s** %s" % (dm.group(1).rstrip(":"), dm.group(2))
                self.report.definitions = getattr(self.report, "definitions", 0) + 1
                return "> **%s** %s ^%s" % (num, rest, bid)
            return "**%s** %s ^%s" % (num, rest, bid)

        # --- перечисление внутри пункта ---
        if re.match(r"^[а-яa-z]\)\s", flat) or re.match(r"^\d{1,2}\s+[А-ЯЁ]", flat):
            self.report.list_items += 1
            return flat

        return flat

    _pending_caption = ""

    def emit_heading(self, title: str, page: int, page_label: str) -> str:
        title = title.strip("*_ ").strip()
        am = APPENDIX_RE.match(title)
        sm = SECTION_RE.match(title)
        ssm = SUBSECTION_RE.match(title)
        if am:
            level, num, rest = 1, "%s %s" % (am.group(1), am.group(2)), title[am.end():].strip()
        elif sm and not ssm:
            level, num, rest = 1, sm.group(1), sm.group(2)
        elif ssm:
            level, num, rest = 2, ssm.group(1), ssm.group(2)
        else:
            level, num, rest = 1, "", title
        full = ("%s %s" % (num, rest)).strip()
        self.sections.append({"level": level, "number": num, "title": rest, "full": full})
        anchor = make_anchor(full)
        self.anchors.setdefault(anchor, ("#" + anchor, "heading"))
        if num:
            self.pages[num] = {"page": page, "label": page_label}
        return "%s %s" % ("#" * min(level, 6), full)

    def emit_table(self, block: str, page: int) -> str:
        rows = [r for r in (l.strip() for l in block.split("\n")) if r.startswith("|")]
        if len(rows) < 2:
            return block
        grid = []
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if all(re.match(r"^:?-{2,}:?$", c or "---") for c in cells):
                continue
            grid.append([normalize_text(c) for c in cells])
        if not grid:
            return block
        self.tables.append({
            "id": "%s-tbl%03d" % (self.doc_id, len(self.tables) + 1),
            "page": page,
            "caption": self._pending_caption,
            "n_rows": len(grid),
            "n_cols": max(len(r) for r in grid),
            "grid": grid,
            "source": "pdf-pymupdf4llm",
            "merged_cells_preserved": False,
        })
        self._pending_caption = ""
        self.report.tables.append({"id": self.tables[-1]["id"], "caption": self.tables[-1]["caption"],
                                   "n_rows": len(grid), "n_cols": self.tables[-1]["n_cols"],
                                   "has_merged": False, "nested": 0})
        self.report.add_review("table", self.tables[-1]["id"],
                               "таблица из PDF: слияния ячеек не восстановлены (pipe-синтаксис), "
                               "сверить с изданием стр. %d" % page)
        return block.strip()

    # ------------------------------------------------------------------ #
    def postprocess(self, text: str) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = text.split("\n")
        out = []
        for l in lines:
            s = l.strip()
            if not s:
                if out and out[-1] != "":
                    out.append("")
                continue
            need_blank = bool(re.match(r"^(#{1,6}\s|<table|\||\*\*\d|\*\*Таблица|> |```)", s))
            if need_blank and out and out[-1] != "":
                out.append("")
            out.append(s)
        return "\n".join(out).strip() + "\n"

    def build_frontmatter(self, meta: dict, confidence: float, pages: list) -> str:
        fm = {
            "doc_id": self.doc_id,
            "designation": meta.get("designation") or "",
            "kind": meta.get("kind") or "",
            "number": meta.get("number") or "",
            "title": meta.get("title") or "",
            "title_short": meta.get("title_short") or "",
            "aliases": meta.get("aliases") or [],
            "note_name": meta.get("note_name") or "",
            "year": meta.get("year") or "",
            "issued": meta.get("issued") or "",
            "in_force_since": meta.get("in_force_since") or "",
            "issuing_body": meta.get("issuing_body") or "",
            "status": meta.get("status") or "unknown",
            "status_checked": meta.get("status_checked") or "",
            "supersedes": meta.get("supersedes") or [],
            "other_designations": meta.get("other_designations") or [],
            "superseded_by": meta.get("superseded_by") or "",
            "amendments": meta.get("amendments") or [],
            "exclusions_since": meta.get("exclusions") or [],
            "excluded_clauses": meta.get("excluded_clauses") or [],
            "topics": meta.get("topics") or [],
            "ics": meta.get("ics") or [],
            "language": "ru",
            "license": self.opts.license,
            "rights_note": self.rights_note or "see NOTICE.md",
            "copyright_notice": meta.get("copyright_notice") or "",
            "references": meta.get("referenced_docs") or [],
            "source": {
                "kind": "pdf",
                "file": os.path.basename(self.path),
                "sha256": sha256_file(self.path),
                "uri": self.opts.source_uri or "",
                "origin_url": self.opts.source_uri or "",
                "publisher": meta.get("publisher") or "ФАУ «ФЦС» / Минстрой России",
                "pages": len(pages),
                "text_layer": meta.get("text_layer") or "ABBYY FineReader (в издании)",
            },
            "conversion": {
                "tool": "normconv.pdf2md",
                "version": __version__,
                "converted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "confidence": confidence,
                "needs_review": bool(self.report.review),
                "review_items": len(self.report.review),
                "verified": bool(meta.get("verified") or False),
                "verified_by": meta.get("verified_by") or "",
                "verified_at": meta.get("verified_at") or "",
            },
            "blocks": {
                "clauses": len(self.clauses),
                "tables": [{"id": t["id"], "caption": t["caption"], "rows": t["n_rows"],
                            "cols": t["n_cols"], "page": t["page"]} for t in self.tables],
                "bibliography": len(self.biblio),
            },
            "stats": {
                "words": self.report.words,
                "headings": self.report.headings,
                "clauses": len(self.clauses),
                "tables": len(self.tables),
                "pages": len(pages),
            },
        }
        return dump_yaml(fm)


# Словарь символов-картинок (docs/08) переехал в obsidian.py — он общий для
# pdf2md и docx2md; имя здесь сохранено для обратной совместимости.
apply_glyph_rules = _ob.apply_glyph_rules


def sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="normconv.pdf2md")
    p.add_argument("pdf")
    p.add_argument("--out", default="md")
    p.add_argument("--source-uri", default="")
    p.add_argument("--license", default="CC0-1.0")
    p.add_argument("--registry", default="")
    p.add_argument("--glyphs", default="", help="словарь символов-картинок (правки/символы-<doc>.yaml)")
    p.add_argument("--meta-override", default="", help="ручные поля фронтмэттера (правки/<doc_id>.yaml)")
    p.add_argument("--no-obsidian", action="store_true")
    a = p.parse_args(argv)
    opts = Options(source_kind="pdf", source_uri=a.source_uri, license=a.license,
                   obsidian=not a.no_obsidian, registry_path=a.registry,
                   glyphs_path=a.glyphs, meta_override_path=a.meta_override)
    md, rep = Pdf2Md(a.pdf, opts).convert(a.out)
    print("-> %s  confidence=%.3f  слов=%d  пунктов=%d  таблиц=%d"
          % (md, rep["confidence"], rep["words"], rep["clauses"], len(rep["tables"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
