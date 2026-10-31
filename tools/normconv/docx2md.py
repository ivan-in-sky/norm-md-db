"""docx -> canonical Markdown converter for Russian normative documents (СП/ГОСТ/СНиП/РД).

Why not pandoc: normative docs need (a) lossless tables with merged cells,
(b) formulas that survive as machine-readable LaTeX *and* are flagged when the
conversion is uncertain, (c) recomputed ГОСТ auto-numbering, (d) resolved
cross-references, (e) typed примечания/подписи/сноски, (f) a QA report telling a
human exactly what to re-check. Pandoc's docx reader drops or mangles most of it.

Output layout:
    <outdir>/<doc_id>.md              canonical markdown (single source of truth)
    <outdir>/<doc_id>.assets/*        images, formula/OLE fallbacks
    <outdir>/<doc_id>.qa.json         machine-readable QA report
    <outdir>/<doc_id>.qa.md           human review checklist
    <outdir>/<doc_id>.tables.jsonl    normalised tables (grid + spans), 1 JSON/line
    <outdir>/assets-manifest.json     per-document asset index (sha256, size, dims)
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import io
import math
import json
import os
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree
from PIL import Image

from . import __version__
from .meta import extract as extract_meta
from .omml2latex import omml_to_latex
from .ooxml import NS, cell_blocks, child, ln, parse_table, val, w
from .styles import NumberingResolver, StyleMap
from . import obsidian as _ob

MATH_CONFIDENCE_THRESHOLD = 0.999

ANCHOR_KEEP_RE = re.compile(r"[^\w\- ]", re.UNICODE)
NOTE_RE = re.compile(r"^П\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*(?:я|е)\b", re.I)
FIG_CAPTION_RE = re.compile(r"^(Рисунок|Рис\.|Figure)\s*(\d+(?:\.\d+)*[а-яa-z]*(?:\.\d+)*)", re.I)
TBL_CAPTION_RE = re.compile(r"^Таблица\s*(\d+(?:\.\d+)*[а-яa-z]*(?:\.\d+)*)", re.I)
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*|[А-ЯA-Z]\.\d+(?:\.\d+)*)[\s\u00a0\u2002-\u200b]")
LIST_MARK_RE = re.compile(r"^([а-яa-z0-9]{1,3}[\).]|[a-z][\)])[\s\u00a0]")
APPENDIX_RE = re.compile(r"^(ПРИЛОЖЕНИЕ|Приложение)\s+([А-ЯA-Z0-9]+)", re.I)
CONTROL_CHARS = "".join(chr(c) for c in range(0, 32) if c not in (9, 10, 13))
STRIP_RE = re.compile("[" + re.escape(CONTROL_CHARS) + "\u200b\u200c\u200d\ufeff\u00ad]")
XREF_RE = re.compile("\ue000XREF:([^\ue001]*)\ue001")
CLAUSE_START_RE = re.compile(r"^\*\*\d+(?:\.\d+)*\*\*\s")
EXCLUDED_RE = re.compile(r"Исключен\w*\s+с\s+(\d{2}\.\d{2}\.\d{4})", re.I)
# «**10.10** - 10.21 Исключены с 08.01.2024. - Изменение N 5» — исключён диапазон
# Вариант А: в тексте остался весь диапазон («10.10 - 10.21 Исключены …»).
# Вариант Б: номер первого пункта ушёл в маркер пункта, осталось «- 10.21 Исключены …».
EXCLUDED_RANGE_FULL_RE = re.compile(
    r"^(?:\d{1,2}(?:\.\d+)*\s*[-–—]\s*)?(\d{1,2}(?:\.\d+)*)\s*[-–—]\s*"
    r"(\d{1,2}(?:\.\d+)*)\s*Исключен", re.I)
EXCLUDED_RANGE_TAIL_RE = re.compile(
    r"^[-–—]\s*(\d{1,2}(?:\.\d+)*)\s*Исключен", re.I)
AMENDMENT_RE = re.compile(r"Изменение\s*(?:N|№|No)?\s*\.?\s*(\d{1,2})", re.I)
NOTE_WORD_RE = re.compile(r"^(Примечание|Примечания|Сноска|Сноски)\b", re.I)
# определение термина: «3.14 рампа: …», «3.15 фонарь (световой фонарь): …» —
# термин в начале пункта, двоеточие недалеко от начала
TERM_DEF_RE = re.compile(r"^\d+(?:\.\d+)*\s+[а-яёa-z][^:.\n]{0,80}:", re.I)
# «похоже на определение термина»: начинается со строчной буквы, двоеточие недалеко
DEFINITIONISH_RE = re.compile(r"^[*_<]{0,3}[а-яёa-z][^:.\n]{0,80}:")
NUMBER_ONLY_RE = re.compile(r"^(\d+(?:\.\d+)*)[.\s]*$")     # абзац из одного номера
BIBLIO_RE = re.compile(r"^\[(\d{1,3})\]\s+")
PREF_RE = re.compile(r"(?<![\w\[])\b(P\d{2,5})\b")
VENDOR_LINE_RE = re.compile(
    r"^(Документ предоставлен|Информация о документе|Подготовлен с использованием|"
    r"Ссылка на документ предоставлена|\[Документ предоставлен)", re.I)
APPENDIX_WORD_RE = re.compile(r"^(ПРИЛОЖЕНИЕ|Приложение)\s+([А-ЯA-Z0-9])\b\.?")
UNNUMBERED_HEADING_RE = re.compile(
    r"^(Предисловие|ПРЕДИСЛОВИЕ|Введение|ВВЕДЕНИЕ|Содержание|СОДЕРЖАНИЕ|Вступительное слово)\s*$")
TITLE_SERVICE_RE = re.compile(
    r"^(Дата введения|Дата введени|Утвержден|Утверждён|Принят|Зарегистрирован|Введен в действие|"
    r"Введён в действие|ИЗДАНИЕ ОФИЦИАЛЬНОЕ|Издание официальное|Москва|Общероссийский классификатор|"
    r"ОКС\s|ОКП\s|Дата введения\s)", re.I)
BRACKET_NUM_RE = re.compile(r"^\[(\d{2,5})\]$")
CP_FOOTNOTE_RE = re.compile(r"<(\*{1,3})>")          # маркеры сносок КонсультантПлюс: <*>, <**>
PREF_TOKEN_RE = re.compile(r"\bP(\d{2,5})\b")
# теги заменяем пробелом (а не «в ноль»): иначе «Примечание<sup>е</sup> - …»
# после снятия разметки превращается в «Примечаниее - …»
MARKUP_RE = re.compile(r"\*\*|\*|__|_|~~|`|</?(?:u|sup|sub|b|i|em|strong)\s*/?>|<br\s*/?>")

EMU_PER_PX = 9525
FIELD_STACK_LIMIT = 8



# --- Obsidian-compatible block identifiers -------------------------------- #
# Obsidian: «Block identifiers can only consist of Latin letters, numbers, and
# dashes». Мы используем их как стабильные ID пунктов/таблиц/формул — это
# единственный маркер, который одновременно (а) валидный Markdown,
# (б) кликабельная ссылка в Obsidian, (в) машиночитаемый якорь для конвейера.
BLOCK_ID_RE = re.compile(r"\^([A-Za-z0-9][A-Za-z0-9-]*)\s*$")


def translit(s: str) -> str:
    """Кириллица -> латиница (для block id: Приложение А -> prilozhenie-a)."""
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in s.lower():
        if ch in table:
            out.append(table[ch])
        elif ch.isalnum() or ch in "-_":
            out.append(ch if ch.isascii() else "-")
        else:
            out.append("-")
    t = re.sub(r"[_\s]+", "-", "".join(out))
    t = re.sub(r"-+", "-", t).strip("-")
    return t or "x"


def clause_block_id(number: str) -> str:
    """8.1.6 -> c8-1-6 · А.3 -> ca-3 · ПРИЛОЖЕНИЕ Б -> prilozhenie-b · 12.7* -> c12-7-x"""
    n = number.strip().rstrip(".")
    star = n.endswith("*")          # пункт, перенумерованный изменением (ГОСТ)
    n = n.rstrip("*")
    if re.match(r"^[\d.]+$", n):
        base = "c" + n.replace(".", "-")
    else:
        base = translit(n).replace(".", "-")
    return base + ("-x" if star else "")


def table_block_id(seq: int) -> str:
    return "t%d" % seq


def formula_block_id(seq: int) -> str:
    return "e%03d" % seq


def figure_block_id(seq: int) -> str:
    return "f%d" % seq


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def slugify(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE)
    return re.sub(r"-+", "-", s).strip("-")


def anchor_variants(text: str) -> list:
    """Несколько вариантов якоря: закладки Word в экспортах справочных систем
    образуются по-разному («Таблица 6.1» -> «таблица-61» или «таблица-6-1»)."""
    t = (text or "").strip()
    out = [make_anchor(t)]
    out.append(make_anchor(t.replace(".", "-")))
    out.append(make_anchor(t.replace(".", " ")))
    return [a for i, a in enumerate(out) if a and out.index(a) == i]


def make_anchor(text: str) -> str:
    a = (text or "").strip().lower().replace("ё", "е").replace("\u00a0", " ")
    a = ANCHOR_KEEP_RE.sub("", a)
    return re.sub(r"[ ]+", "-", a).strip("-")


def section_seq_ok(num_txt: str, seq: dict) -> bool:
    """Соответствует ли номер ожидаемой последовательности разделов/подразделов."""
    if not num_txt:
        return False
    parts = num_txt.split(".")
    try:
        if len(parts) == 1:
            return parts[0].isdigit() and int(parts[0]) == seq["section"]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            if seq["section"] == 1:
                return False
            parent = seq["section"] - 1
            return int(parts[0]) == parent and int(parts[1]) in (seq["sub"], seq["sub"] + 1)
    except ValueError:
        return False
    return False


def _advance_section_seq(ctx: dict, num: str, level: int) -> None:
    seq = ctx.setdefault("_seq", {"section": 1, "sub": 1})
    parts = (num or "").split(".")
    try:
        if level == 1 and len(parts) == 1 and parts[0].isdigit():
            seq["section"] = int(parts[0]) + 1
            seq["sub"] = 1
        elif level == 2 and len(parts) == 2 and parts[1].isdigit():
            seq["sub"] = int(parts[1]) + 1
    except ValueError:
        pass


def looks_like_title(text: str) -> bool:
    """A clause *heading* vs a clause *paragraph*: headings are short, one sentence,
    no ending period, no ';'. This single heuristic removes the most common
    false-positive headings in ГОСТ/СП documents."""
    t = text.strip()
    body = CLAUSE_RE.sub("", t, count=1).strip()
    if not body:
        return False
    if len(body) > 200:
        return False
    words = len(body.split())
    if words > 32:
        return False
    if body.count(";") > 0 or body.count(":") > 0:
        return False
    if body.count(".") > 1:
        return False
    if body.endswith(".") and words > 12:
        return False
    return True


def clean_text(s: str) -> str:
    s = STRIP_RE.sub("", s or "")
    s = s.replace("\u2007", " ")
    # ГОСТ-style letterspacing used for emphasis: "П р и м е ч а н и е" -> "Примечание"
    s = re.sub(r"(?:(?<=\s)|(?<=\()|^)((?:[А-Яа-яA-Za-z]\s){2,}[А-Яа-яA-Za-z])(?=$|[\s.,;:!?)—-])",
               lambda m: m.group(1).replace(" ", ""), s, flags=re.M)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s



MATH_TOKEN_RE = re.compile(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$")
XREF_TOKEN_RE = re.compile("\ue000XREF:([^\ue001]*)\ue001")


def strip_markup(s: str) -> str:
    """Убрать Markdown-разметку для распознавания паттернов, НЕ трогая формулы.

    В ГОСТ-документах «П р и м е ч а н и е —» часто целиком лежит в жирном
    прогоне, поэтому распознавание надо делать по «голому» тексту. Но LaTeX внутри
    $…$ содержит подчёркивания, которые наивный strip превращает в мусор
    ($A_{s}$ → $A{s}$), поэтому математика и xref-плейсхолдеры защищаются.
    """
    if not s:
        return ""
    # неразрешённые перекрёстные ссылки: подставить кэш Word, иначе текст «потеряет» ссылку
    s = XREF_TOKEN_RE.sub(lambda m: m.group(1), s)
    keep: list[str] = []

    def repl(mm: "re.Match") -> str:
        keep.append(mm.group(0))
        return "\x00%d\x00" % (len(keep) - 1)

    t = MATH_TOKEN_RE.sub(repl, s)
    t = MARKUP_RE.sub(" ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], t)
    return t.strip() if t.strip() == t.strip() else t


def esc_md(s: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~])", r"\\\1", s or "")


SAFE_INLINE_RE = re.compile(r"</?(?:sup|sub|u|br|b|i|em|strong)\s*/?>", re.I)


def esc_html_keep_inline(s: str) -> str:
    """Экранировать текст, сохранив безопасные инлайн-теги (конвертер сам их ставит)."""
    parts = SAFE_INLINE_RE.split(s or "")
    tags = SAFE_INLINE_RE.findall(s or "")
    out = [esc_html(parts[0])]
    for i, tg in enumerate(tags):
        out.append(tg)
        out.append(esc_html(parts[i + 1]) if i + 1 < len(parts) else "")
    return "".join(out)


def esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --------------------------------------------------------------------------- #
# config / report
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    math: str = "latex"          # latex | image | keep
    tables: str = "html"         # html | md | both
    notes: str = "quote"         # quote | plain
    definitions_as_quotes: bool = True   # раздел «Термины и определения»: определения цитатой
    obsidian: bool = True                # имена заметок кириллицей + алиасы + вики-ссылки
    registry_path: str = ""              # реестр актуальных версий (для резолвинга ссылок)
    resolve_xrefs: bool = True
    numbered_as_heading: bool = True     # promote *auto-numbered* short paragraphs to headings
    numbered_headings: bool = False      # also promote *manually* numbered paragraphs
    max_heading_from_numbering: int = 3
    lang: str = "ru"
    license: str = "CC0-1.0"
    source_uri: str = ""
    source_kind: str = "docx"
    max_asset_mb: float = 8.0
    min_confidence: float = 0.9
    strip_vendor_lines: bool = True     # «Документ предоставлен КонсультантПлюс» и т.п.
    clean_vendor_links: bool = True     # ссылки на login.consultant.ru и пр. -> текст + запись в QA
    glyphs_path: str = ""               # словарь символов-картинок (docs/08): правки/символы-<doc>.yaml
    meta_override_path: str = ""        # ручные поля фронтмэттера: правки/<doc_id>.yaml


@dataclass
class Report:
    doc_id: str = ""
    words: int = 0
    paragraphs: int = 0
    empty_paragraphs: int = 0
    headings: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    formulas: list = field(default_factory=list)
    images: list = field(default_factory=list)
    links: list = field(default_factory=list)
    footnotes: int = 0
    endnotes: int = 0
    xrefs_total: int = 0
    xrefs_resolved: int = 0
    notes: int = 0
    list_items: int = 0
    clauses: int = 0
    definitions: int = 0
    sections: list = field(default_factory=list)
    numbering_issues: list = field(default_factory=list)
    unknown_elements: list = field(default_factory=list)
    fallbacks: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    review: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    confidence: float = 1.0

    def add_issue(self, severity, code, msg, loc=""):
        self.issues.append({"severity": severity, "code": code, "msg": msg, "loc": loc})

    def add_review(self, kind, loc, detail):
        self.review.append({"kind": kind, "loc": loc, "detail": detail})

    # Вклад в снижение уверенности. Разделяем «дефекты конвертации» (формулы,
    # таблицы, заголовки — их чинит человек) и «артефакты источника» (сноски СПС,
    # неразрешимые ссылки — их чинит только другой источник). Иначе документ из
    # КонсультантПлюса получает 0.56 при идеальной конвертации, и метрика
    # перестаёт что-либо означать.
    REVIEW_WEIGHTS = {
        "formula": 0.015, "formula_image": 0.010, "table": 0.012, "heading": 0.008,
        "ole": 0.008, "image": 0.005, "image_format": 0.005, "asset_size": 0.002,
        "field": 0.004, "graphic": 0.005, "xref": 0.004, "structure": 0.004,
        "provenance": 0.006,
    }

    @staticmethod
    def _damp(n: int, k: float = 12.0) -> float:
        """Логарифмическое затухание: 1 элемент ~ 1 штраф, 100 элементов ~ 5 штрафов.

        Без этого документ на 740 картинок WMF получает confidence=0.00, и метрика
        перестаёт отличать «всё плохо» от «много однотипной ручной работы».
        """
        if n <= 0:
            return 0.0
        if n <= k:
            return float(n)
        return k + k * math.log(n / k)

    def compute_confidence(self) -> float:
        import math
        n_err = len([i for i in self.issues if i["severity"] == "error"])
        n_warn = len([i for i in self.issues if i["severity"] == "warning"])
        bad = [f for f in self.formulas if f.get("confidence", 1) < MATH_CONFIDENCE_THRESHOLD]
        pen = 0.0
        pen += 0.25 * self._damp(n_err, 3)
        pen += 0.02 * self._damp(n_warn, 10)
        pen += 0.02 * self._damp(len(bad), 10)
        pen += 0.01 * len(set(self.unknown_elements))
        kinds: dict = {}
        for r in self.review:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        review_pen = 0.0
        for kind, n in kinds.items():
            review_pen += self.REVIEW_WEIGHTS.get(kind, 0.006) * self._damp(n, 8)
        pen += review_pen
        self.confidence = round(max(0.0, min(1.0, 1.0 - pen)), 3)
        self.penalty_breakdown = {
            "errors": n_err, "warnings": n_warn, "review_items": len(self.review),
            "review_by_kind": kinds,
            "formulas_low_confidence": len(bad),
            "unknown_element_types": len(set(self.unknown_elements)),
            "penalty": round(pen, 3),
        }
        return self.confidence

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["generated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        d["tool_version"] = __version__
        d["penalty_breakdown"] = getattr(self, "penalty_breakdown", {})
        d["counts"] = {
            "formulas": len(self.formulas),
            "formulas_low_confidence": len(
                [f for f in self.formulas if f.get("confidence", 1) < MATH_CONFIDENCE_THRESHOLD]),
            "tables": len(self.tables),
            "tables_merged": len([t for t in self.tables if t.get("has_merged")]),
            "images": len(self.images),
            "links": len(self.links),
            "unknown_elements": len(set(self.unknown_elements)),
        }
        d["unknown_elements"] = sorted(set(self.unknown_elements))
        return d

    def to_markdown(self) -> str:
        d = self.to_dict()
        L = ["# QA-отчёт: %s" % (d["doc_id"] or "?"), ""]
        L.append("**Уверенность конвертации:** `%.3f`  " % d["confidence"])
        L.append("**Сгенерирован:** %s · normconv %s" % (d["generated_at"], d["tool_version"]))
        if d.get("penalty_breakdown"):
            pb = d["penalty_breakdown"]
            L.append("**Из чего сложился штраф:** errors=%d, warnings=%d, review=%d, "
                     "формул с низкой уверенностью=%d, неизвестных OOXML-элементов=%d → penalty %.3f"
                     % (pb.get("errors", 0), pb.get("warnings", 0), pb.get("review_items", 0),
                        pb.get("formulas_low_confidence", 0), pb.get("unknown_element_types", 0),
                        pb.get("penalty", 0)))
        L.append("")
        L.append("## Сводка")
        L.append("")
        L.append("| Показатель | Значение |")
        L.append("| --- | --- |")
        rows = [
            ("слов", d["words"]), ("абзацев", d["paragraphs"]),
            ("пустых абзацев", d["empty_paragraphs"]),
            ("заголовков", sum(int(v) for v in d["headings"].values())),
            ("разделов/подразделов", len(d["sections"])),
            ("пунктов (clause)", d.get("clauses", 0)),
            ("определений терминов", d.get("definitions", 0)),
            ("таблиц", d["counts"]["tables"]),
            ("таблиц со слияниями", d["counts"]["tables_merged"]),
            ("формул", d["counts"]["formulas"]),
            ("формул с низкой уверенностью", d["counts"]["formulas_low_confidence"]),
            ("изображений", d["counts"]["images"]),
            ("сносок", d["footnotes"]), ("концевых сносок", d["endnotes"]),
            ("перекрёстных ссылок", "%d (разрешено %d)" % (d["xrefs_total"], d["xrefs_resolved"])),
            ("примечаний", d["notes"]),
            ("элементов списка", d["list_items"]),
            ("неизвестных OOXML-элементов", d["counts"]["unknown_elements"]),
        ]
        for k, v in rows:
            L.append("| %s | %s |" % (k, v))
        L.append("")
        meta = d.get("meta") or {}
        if meta.get("vendor"):
            L.append("> ⚠️ **Источник — экспорт из справочной системы «%s».** Такие файлы "
                     "неполны (теряются сноски, ссылки заменяются внутренними идентификаторами) "
                     "и могут быть ограничены лицензией СПС. Для корпуса нужен официальный "
                     "источник: сайт Минстроя/ФАУ «ФЦС» (для СП), ФГИС СТ Росстандарта (для ГОСТ)."
                     % meta["vendor"])
            L.append("")
        if d["sections"]:
            L.append("## Структура документа (%d)" % len(d["sections"]))
            L.append("")
            for sec in d["sections"][:60]:
                indent = "  " * max(0, int(sec["level"]) - 1)
                L.append("%s- `%s` %s" % (indent, sec["number"] or "—", sec["title"][:90]))
            L.append("")
        # --- замечания, сгруппированные по коду (иначе отчёт тонет в дублях) ---
        groups: dict = {}
        for i in d["issues"]:
            groups.setdefault((i["severity"], i["code"]), []).append(i)
        if groups:
            L.append("## Замечания (сгруппировано)")
            L.append("")
            L.append("| severity | код | сколько | примеры |")
            L.append("| --- | --- | --- | --- |")
            order = {"error": 0, "warning": 1, "info": 2}
            for (sev, code), items in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 9), -len(kv[1]))):
                ex = []
                seen = set()
                for it in items:
                    key = (it["msg"], it["loc"])
                    if key in seen:
                        continue
                    seen.add(key)
                    ex.append(("%s @ %s" % (it["msg"], it["loc"])) if it["loc"] else it["msg"])
                    if len(ex) >= 3:
                        break
                L.append("| %s | `%s` | %d | %s |" % (sev, code, len(items),
                                                      " · ".join(e[:110] for e in ex)))
            L.append("")
        if d["review"]:
            L.append("## Требуется ручная проверка (%d)" % len(d["review"]))
            L.append("")
            L.append("| Тип | Место | Что сделать |")
            L.append("| --- | --- | --- |")
            seen_r = set()
            for r in d["review"]:
                key = (r["kind"], r["detail"][:60])
                if key in seen_r:
                    continue
                seen_r.add(key)
                same = [x for x in d["review"] if (x["kind"], x["detail"][:60]) == key]
                loc = r["loc"] or "—"
                if len(same) > 1:
                    loc = "%s (×%d)" % (loc, len(same))
                L.append("| %s | %s | %s |" % (r["kind"], loc,
                                                r["detail"].replace("|", "\\|")[:170]))
            L.append("")
        if d["numbering_issues"]:
            L.append("## Расхождения нумерации")
            L.append("")
            for n in d["numbering_issues"][:20]:
                L.append("- в тексте `%s`, авто-нумерация даёт `%s` — %s" % (n["para"], n["computed"], n["text"]))
            L.append("")
        if d["unknown_elements"]:
            L.append("## Неизвестные элементы OOXML")
            L.append("")
            L.append("```\n" + "\n".join(d["unknown_elements"]) + "\n```")
            L.append("")
        return "\n".join(L)


# --------------------------------------------------------------------------- #
# converter
# --------------------------------------------------------------------------- #


class Docx2Md:
    def __init__(self, path: str, opts: Options | None = None):
        self.path = path
        self.opts = opts or Options()
        self.zf = zipfile.ZipFile(path)
        self.names = set(self.zf.namelist())
        self.doc = etree.fromstring(self.zf.read("word/document.xml"))
        self.styles = StyleMap(
            etree.fromstring(self.zf.read("word/styles.xml")) if "word/styles.xml" in self.names else None,
            etree.fromstring(self.zf.read("word/numbering.xml")) if "word/numbering.xml" in self.names else None,
        )
        self.nums = NumberingResolver(self.styles)
        self.footnotes = self._load_notes("word/footnotes.xml")
        self.endnotes = self._load_notes("word/endnotes.xml")
        self.rels = self._load_rels()
        self.report = Report()
        self.assets: list[dict] = []
        self.asset_by_hash: dict[str, str] = {}
        self.asset_data: dict[str, bytes] = {}
        self.img_seq = 0
        self.tbl_seq = 0
        self.eq_seq = 0
        self.anchors: dict[str, str] = {}
        self.anchor_titles: dict[str, str] = {}
        self.pending_xrefs: list[dict] = []
        self.doc_id = ""
        self.assets_dirname = ""
        self._fn_used: dict[str, int] = {}
        self._fn_seq = 0
        self._en_texts: dict[str, str] = {}

    # ---------------- loading ---------------- #
    def _load_notes(self, part: str) -> dict:
        out = {}
        if part not in self.names:
            return out
        root = etree.fromstring(self.zf.read(part))
        for note in root:
            if ln(note) not in ("footnote", "endnote"):
                continue
            nid = note.get(w("id"))
            out[nid] = {
                "type": note.get(w("type")),
                "text": clean_text(" ".join(self.plain_text(p) for p in note if ln(p) == "p")).strip(),
            }
        return out

    def _load_rels(self) -> dict:
        out = {}
        part = "word/_rels/document.xml.rels"
        if part in self.names:
            for r in etree.fromstring(self.zf.read(part)):
                out[r.get("Id")] = {
                    "type": (r.get("Type") or "").rsplit("/", 1)[-1],
                    "target": r.get("Target") or "",
                    "mode": r.get("TargetMode") or "",
                }
        return out

    def plain_text(self, p) -> str:
        buf = []
        for el in p.iter():
            if not isinstance(el.tag, str):
                continue
            n = ln(el)
            if n == "t":
                buf.append(el.text or "")
            elif n == "tab":
                buf.append(" ")
            elif n in ("br", "cr"):
                buf.append(" ")
        return clean_text("".join(buf))

    # ---------------- assets ---------------- #
    def save_asset(self, data: bytes, ext: str, kind: str, title: str = "") -> dict:
        h = sha256_bytes(data)
        if h in self.asset_by_hash:
            rec = next(a for a in self.assets if a["path"] == self.asset_by_hash[h])
            rec["refs"] = rec.get("refs", 1) + 1
            return rec
        self.img_seq += 1
        name = "%s-%03d.%s" % (self.doc_id or "asset", self.img_seq, ext)
        rel = "%s/%s" % (self.assets_dirname, name)
        rec = {"path": rel, "name": name, "sha256": h, "bytes": len(data),
               "kind": kind, "title": title, "refs": 1}
        try:
            im = Image.open(io.BytesIO(data))
            rec["width"], rec["height"], rec["format"] = im.size[0], im.size[1], im.format
        except Exception:
            pass
        self.assets.append(rec)
        self.asset_by_hash[h] = rel
        self.asset_data[rel] = data
        self.report.images.append({k: v for k, v in rec.items()})
        if len(data) > self.opts.max_asset_mb * 1024 * 1024:
            self.report.add_review("asset_size", rel, "%.1f MB — нужен Git LFS" % (len(data) / 1e6))
        return rec

    def write_assets(self, outdir: str):
        if not self.assets:
            return
        os.makedirs(os.path.join(outdir, self.assets_dirname), exist_ok=True)
        for rec in self.assets:
            with open(os.path.join(outdir, rec["path"]), "wb") as fh:
                fh.write(self.asset_data.get(rec["path"], b""))

    def media(self, rid: str):
        rel = self.rels.get(rid)
        if not rel:
            return None
        t = rel["target"].lstrip("/")
        if not t.startswith("word/"):
            t = "word/" + t
        if t not in self.names:
            alt = rel["target"].lstrip("/")
            if alt in self.names:
                t = alt
            else:
                return None
        data = self.zf.read(t)
        ext = (os.path.splitext(t)[1] or ".bin").lstrip(".").lower()
        if ext == "jpeg":
            ext = "jpg"
        if ext == "emf" or ext == "wmf":
            self.report.add_review("image_format", t,
                                   "векторный EMF/WMF — сконвертировать в SVG (libreoffice/inkscape)")
        return data, ext, t

    # ---------------- inline: runs ---------------- #
    def render_children(self, container) -> str:
        """Render inline children of a run-like container WITHOUT char formatting."""
        rpr = child(container, "rPr")
        buf = []
        for el in container:
            if not isinstance(el.tag, str):
                continue
            n = ln(el)
            if n == "rPr":
                continue
            elif n == "t":
                buf.append(clean_text(el.text or ""))
            elif n == "tab":
                buf.append(" ")
            elif n in ("br", "cr"):
                buf.append("\n")
            elif n == "noBreakHyphen":
                buf.append("-")
            elif n in ("softHyphen", "lastRenderedPageBreak"):
                continue
            elif n == "sym":
                buf.append(self.sym_char(el))
            elif n == "footnoteReference":
                buf.append(self.footnote_md(el.get(w("id"))))
            elif n == "endnoteReference":
                nid = el.get(w("id"))
                self.report.endnotes += 1
                self._en_texts[nid] = self.endnotes.get(nid, {}).get("text", "")
                buf.append("[^en%s]" % nid)
            elif n == "drawing":
                buf.append(self.convert_drawing(el))
            elif n == "pict":
                buf.append(self.convert_pict(el))
            elif n == "object":
                buf.append(self.convert_object(el))
            elif n in ("fldChar", "instrText"):
                continue
            elif n in ("footnoteRef", "separator", "continuationSeparator", "delText"):
                continue
            elif n in ("commentRangeStart", "commentRangeEnd", "commentReference"):
                self.report.add_issue("info", "comment", "в документе есть комментарии Word")
                continue
            else:
                self.report.unknown_elements.append("w:r/" + n)
                for sub in el:
                    if ln(sub) == "t":
                        buf.append(clean_text(sub.text or ""))
        text = "".join(buf)
        return self.apply_format(text, rpr)

    def apply_format(self, text: str, rpr) -> str:
        if rpr is None or not text.strip():
            return text
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        core = text.strip()
        bold = child(rpr, "b") is not None and val(rpr, "b") not in ("0", "false", "none")
        italic = child(rpr, "i") is not None and val(rpr, "i") not in ("0", "false", "none")
        under = child(rpr, "u") is not None and val(rpr, "u") not in ("0", "false", "none")
        vert = val(rpr, "vertAlign")
        strike = child(rpr, "strike") is not None
        fonts = child(rpr, "rFonts")
        mono = False
        if fonts is not None:
            f = (fonts.get(w("ascii")) or "").lower()
            mono = any(k in f for k in ("courier", "consolas", "mono", "menlo"))
        if strike:
            core = "~~%s~~" % core
        elif mono:
            core = "`%s`" % core.replace("`", "")
        elif bold and italic:
            core = "***%s***" % core
        elif bold:
            core = "**%s**" % core
        elif italic:
            core = "*%s*" % core
        if vert == "superscript":
            core = "<sup>%s</sup>" % core
        elif vert == "subscript":
            core = "<sub>%s</sub>" % core
        if under and not mono:
            self.report.add_issue("info", "underline", "подчёркнутый текст (часто артефакт OCR)", core[:40])
            core = "<u>%s</u>" % core
        return lead + core + trail

    def sym_char(self, el) -> str:
        try:
            return chr(int(el.get(w("char")), 16))
        except (TypeError, ValueError):
            return ""

    # ---------------- images / OLE ---------------- #
    def convert_drawing(self, el) -> str:
        out = []
        title = ""
        for docpr in el.iter("{%s}docPr" % NS["wp"]):
            title = docpr.get("descr") or docpr.get("name") or ""
            break
        seen = set()
        for blip in el.iter("{%s}blip" % NS["a"]):
            rid = blip.get("{%s}embed" % NS["r"]) or blip.get("{%s}link" % NS["r"])
            if rid and rid not in seen:
                seen.add(rid)
                out.append(self.image_md(rid, el, title))
        for imagedata in el.iter("{%s}imagedata" % NS["v"]):
            rid = imagedata.get("{%s}id" % NS["r"])
            if rid and rid not in seen:
                seen.add(rid)
                out.append(self.image_md(rid, el, title))
        if not out:
            self.report.add_review("graphic", "", "drawing без извлекаемого изображения")
        return "".join(out)

    def convert_pict(self, el) -> str:
        out = []
        for imagedata in el.iter("{%s}imagedata" % NS["v"]):
            rid = imagedata.get("{%s}id" % NS["r"])
            if rid:
                out.append(self.image_md(rid, el))
        if not out:
            o = self.convert_object(el)
            if o:
                out.append(o)
            else:
                self.report.add_review("graphic", "", "pict (VML) без изображения")
        return "".join(out)

    def convert_object(self, el) -> str:
        for o in el.iter("{urn:schemas-microsoft-com:office:office}OLEObject"):
            rid = o.get("{%s}id" % NS["r"])
            prog = o.get("ProgID") or "OLE"
            if rid:
                rel = self.rels.get(rid)
                if rel:
                    t = rel["target"].lstrip("/")
                    if not t.startswith("word/"):
                        t = "word/" + t
                    if t in self.names:
                        data = self.zf.read(t)
                        ext = (os.path.splitext(t)[1] or ".bin").lstrip(".")
                        rec = self.save_asset(data, ext, "ole:" + prog)
                        self.report.add_review("ole", rec["path"],
                                               "внедрённый объект %s — экспортировать в SVG/PNG" % prog)
                        return "```ole %s\n%s\n```" % (prog, rec["path"])
        for im in el.iter("{%s}imagedata" % NS["v"]):
            rid = im.get("{%s}id" % NS["r"])
            if rid:
                return self.image_md(rid, el)
        return ""

    def image_md(self, rid: str, container=None, title: str = "") -> str:
        info = self.media(rid)
        if not info:
            self.report.add_review("image", rid, "медиа не найдено для r:id=%s" % rid)
            return ""
        data, ext, src = info
        rec = self.save_asset(data, ext, "image", title)
        width = height = None
        if container is not None:
            for e in container.iter("{%s}extent" % NS["wp"]):
                try:
                    width = int(e.get("cx")) / EMU_PER_PX
                    height = int(e.get("cy")) / EMU_PER_PX
                except (TypeError, ValueError):
                    pass
                break
            if width is None:
                for e in container.iter("{%s}extent" % NS["a"]):
                    try:
                        width = int(e.get("cx")) / EMU_PER_PX
                        height = int(e.get("cy")) / EMU_PER_PX
                    except (TypeError, ValueError):
                        pass
                    break
        alt = esc_html(title or rec["name"])
        if width and height:
            return '<img src="%s" alt="%s" width="%d" height="%d" />' % (rec["path"], alt, int(width), int(height))
        return '![%s](%s)' % (alt, rec["path"])

    # ---------------- footnotes ---------------- #
    def footnote_md(self, nid: str) -> str:
        note = self.footnotes.get(nid)
        if not note or note.get("type") in ("separator", "continuationSeparator"):
            return ""
        self._fn_seq += 1
        self._fn_used[nid] = self._fn_seq
        self.report.footnotes += 1
        return "[^fn%s]" % nid

    def footnotes_block(self) -> str:
        if not self._fn_used and not self._en_texts:
            return ""
        lines = ["", "---", ""]
        for nid, n in sorted(self._fn_used.items(), key=lambda kv: kv[1]):
            txt = re.sub(r"^\d+\)?\s*", "", self.footnotes.get(nid, {}).get("text", "")).strip()
            lines.append("[^fn%s]: %s" % (nid, txt or "—"))
        for nid, txt in self._en_texts.items():
            lines.append("[^en%s]: %s" % (nid, re.sub(r"^\d+\)?\s*", "", txt).strip() or "—"))
        return "\n".join(lines) + "\n"

    # ---------------- math ---------------- #
    def convert_math(self, el, display: bool, ctx: dict | None = None) -> str:
        latex, conf, unknown, fallbacks = omml_to_latex(el)
        self.eq_seq += 1
        eq_id = "%s-eq%03d" % (self.doc_id or "doc", self.eq_seq)
        plain = self.math_plain(el)
        rec = {"id": eq_id, "display": display, "confidence": conf, "latex": latex,
               "plain": plain, "unknown_elements": unknown, "fallbacks": fallbacks}
        self.report.formulas.append(rec)
        low = conf < MATH_CONFIDENCE_THRESHOLD or bool(unknown) or bool(fallbacks)
        if low:
            self.report.unknown_elements.extend("m:" + u for u in unknown)
            self.report.fallbacks.extend("%s: %s" % (eq_id, f) for f in fallbacks)
            self.report.add_review("formula", eq_id,
                                   "LaTeX confidence %.2f; unknown=%s; fallbacks=%s; текст: %s"
                                   % (conf, unknown, fallbacks, plain[:60]))
        if self.opts.math == "image":
            self.report.add_review("formula_image", eq_id, "нужен рендер формулы в PNG/SVG")
            return "```formula %s\n%s\n```" % (eq_id, plain)
        if not latex.strip():
            self.report.add_issue("error", "math_empty", "формула не сконвертирована", eq_id)
            return "$$\\\\\text{%s}$$" % plain if plain else "$$\\\\\text{[формула не распознана]}$$"
        bid = ""
        if display and ctx is not None:
            bid = self.block_id(formula_block_id(self.eq_seq), ctx)
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            self.anchor_titles.setdefault(bid, "формула %s" % eq_id)
            rec["block_id"] = bid
        if display:
            out = "\n$$\n%s\n$$\n" % latex
            if bid:
                out += "\n^%s\n" % bid
            return out
        return "$%s$" % latex

    @staticmethod
    def math_plain(el) -> str:
        return clean_text("".join(t.text or "" for t in el.iter("{%s}t" % NS["m"]))).strip()

    # ---------------- fields ---------------- #
    def handle_field(self, instr: str, cached: str) -> str:
        instr = clean_text(instr).strip()
        code = instr.split(" ")[0].upper().strip('"') if instr else ""
        if code in ("REF", "NOTEREF"):
            mm = re.match(r"(?:REF|NOTEREF)\s+([^\s\\]+)", instr, re.I)
            bm = mm.group(1) if mm else ""
            self.report.xrefs_total += 1
            if self.opts.resolve_xrefs and bm:
                self.pending_xrefs.append({"bookmark": bm, "cached": cached, "instr": instr})
                return "\ue000XREF:%s\ue001" % bm
            return cached
        if code == "PAGEREF":
            return cached
        if code == "TOC":
            return ""
        if code in ("EMBED", "LINK", "INCLUDEPICTURE", "INCLUDETEXT", "FORMTEXT", "MERGEFIELD",
                    "IF", "MACROBUTTON", "DOCVARIABLE"):
            self.report.add_review("field", code, "поле не обработано: %r" % instr[:90])
            return cached
        return cached

    # ---------------- inline container walk ---------------- #
    def render_inline(self, container, info: dict | None = None) -> str:
        """Walk inline content of any container (w:p, w:hyperlink, w:tc, w:sdtContent).

        Word fields (REF/PAGEREF/SEQ/TOC...) are reconstructed from the
        begin/separate/end fldChar machinery, including nesting.
        """
        if info is None:
            info = {"bookmarks": [], "math": 0, "fields": [], "links": [], "ctx": None}
        out: list[str] = []
        stack: list[dict] = []  # each frame: {"instr": str, "result": str, "sep": bool}

        def emit(fragment: str):
            if not fragment:
                return
            if stack:
                stack[-1]["result"] += fragment
            else:
                out.append(fragment)

        for el in container:
            if not isinstance(el.tag, str):
                continue
            n = ln(el)
            if n in ("pPr", "tcPr", "trPr", "bookmarkEnd", "proofErr", "permStart",
                     "permEnd", "moveFromRangeStart", "moveToRangeStart",
                     "moveFromRangeEnd", "moveToRangeEnd", "del"):
                continue
            if n == "bookmarkStart":
                bid = el.get(w("name")) or ""
                if bid and not bid.startswith("_GoBack"):
                    info["bookmarks"].append(bid)
                continue
            if n == "r":
                emit(self._render_run(el, stack, info, emit))
                continue
            if n == "hyperlink":
                emit(self.convert_hyperlink(el, info, info.get("ctx")))
                continue
            if n == "fldSimple":
                instr = el.get(w("instr")) or ""
                cached = self.render_inline(el, info)
                info["fields"].append(instr.strip())
                emit(self.handle_field(instr, cached))
                continue
            if n in ("oMath", "oMathPara"):
                info["math"] += 1
                emit(self.convert_math(el, display=(n == "oMathPara"), ctx=info.get("ctx")))
                continue
            if n in ("ins", "smartTag"):
                emit(self.render_inline(el, info))
                continue
            if n == "sdt":
                target = child(el, "sdtContent")
                if target is not None:
                    emit(self.render_inline(target, info))
                continue
            self.report.unknown_elements.append("inline/" + n)
        return clean_text("".join(out))

    def _render_run(self, run, stack, info, emit) -> str:
        """A single w:r; may carry field markup (fldChar/instrText) mixed with content."""
        pieces: list[str] = []

        def sink(frag: str):
            if not frag:
                return
            if stack:
                stack[-1]["result"] += frag
            else:
                pieces.append(frag)

        rpr = child(run, "rPr")
        for sub in run:
            if not isinstance(sub.tag, str):
                continue
            sn = ln(sub)
            if sn == "fldChar":
                ft = sub.get(w("fldCharType"))
                if ft == "begin":
                    if len(stack) >= FIELD_STACK_LIMIT:
                        self.report.add_issue("warning", "field_nesting",
                                              "вложенность полей глубже %d" % FIELD_STACK_LIMIT)
                    else:
                        stack.append({"instr": "", "result": "", "sep": False})
                elif ft == "separate":
                    if stack:
                        stack[-1]["sep"] = True
                elif ft == "end":
                    if stack:
                        f = stack.pop()
                        info["fields"].append(f["instr"].strip())
                        sink(self.handle_field(f["instr"], f["result"]))
                continue
            if sn == "instrText":
                if stack:
                    stack[-1]["instr"] += sub.text or ""
                continue
            if sn == "rPr":
                continue
            if stack and not stack[-1]["sep"]:
                # runs between `begin` and `separate` hold the field code itself
                continue
            tmp = etree.Element(w("r"))
            if rpr is not None:
                tmp.append(copy.deepcopy(rpr))
            tmp.append(copy.deepcopy(sub))
            sink(self.render_children(tmp))
        return "".join(pieces)

    VENDOR_URL_RE = re.compile(
        r"consultant\.ru|garant\.ru|kodeks\.ru|normacs\.info|docs\.cntd\.ru/link|tehexpert", re.I)

    def convert_hyperlink(self, el, info, ctx: dict | None = None) -> str:
        rid = el.get("{%s}id" % NS["r"])
        anchor = el.get(w("anchor"))
        inner = self.render_inline(el, info)
        if rid:
            rel = self.rels.get(rid)
            if rel:
                target = rel["target"]
                if self.opts.clean_vendor_links and self.VENDOR_URL_RE.search(target):
                    # ссылка на коммерческую СПС: вне подписки мертва, а текст цитаты
                    # важнее. Оставляем текст, а цель фиксируем в QA.
                    ctx = ctx if ctx is not None else {}
                    vr = ctx.setdefault("vendor_links", {})
                    vr[target] = vr.get(target, 0) + 1
                    self.report.add_issue("info", "vendor_link_stripped",
                                          "ссылка на СПС заменена текстом", target[:90])
                    return inner
                typ = "external" if (rel.get("mode") == "External" or re.match(r"^https?:", target)) else "file"
                info.setdefault("links", []).append(target)
                self.report.links.append({"type": typ, "target": target, "text": inner.strip()[:80]})
                return "[%s](%s)" % (inner.strip() or target, target)
        if anchor:
            if len(anchor) > 60:
                # закладка-«простыня» поверх всей записи списка литературы
                # (артефакт экспорта из справочной системы) — ссылка бессмысленна
                self.report.add_issue("info", "vendor_anchor_dropped",
                                      "отброшена внутренняя закладка длиной %d символов" % len(anchor))
                return inner
            self.report.xrefs_total += 1
            self.pending_xrefs.append({"bookmark": anchor, "cached": inner, "instr": "hyperlink"})
            return "\ue000XREF:%s\ue001" % anchor
        return inner

    # ---------------- tables ---------------- #
    def convert_table(self, tbl, ctx: dict) -> str:
        grid = parse_table(tbl)
        # --- вырожденные «таблицы»: вёрстка, а не данные ---
        # 1x1 = бокс (термин/определение, врезка), 1xN = строка-раскладка.
        # Ни то, ни другое таблицей не является: разворачиваем в обычный текст.
        if grid["n_rows"] == 1 and grid["n_cols"] == 1:
            inner = []
            numbered = 0
            for c in grid["cells"]:
                for b in cell_blocks(c["tc"]):
                    if ln(b) == "tbl":
                        inner.append(self.convert_block(b, ctx))
                        continue
                    txt = clean_text(self.render_inline(b)).strip()
                    if not txt:
                        continue
                    if CLAUSE_RE.match(strip_markup(txt)):
                        numbered += 1
                        inner.append(self.convert_paragraph(b, ctx))   # полноценный пункт
                    else:
                        inner.append("\n%s\n" % txt)
            text = "\n".join(x for x in inner if x.strip()).strip()
            if not text:
                return ""
            merged_num = ctx.pop("merge_clause_into_box", "")
            if merged_num:
                bid = self.block_id(clause_block_id(merged_num), ctx)
                self.anchors.setdefault(bid, ("#^" + bid, "block"))
                self.report.clauses += 1
                ctx.setdefault("clauses", []).append(merged_num)
                first, _, rest = text.partition("\n")
                first = first.lstrip("> ").strip()
                head = ("**%s** %s" % (merged_num, first)).strip()
                text = (head + " ^" + bid + ("\n\n" + rest.strip() if rest.strip() else ""))
                self.report.add_issue("info", "layout_table_unwrapped",
                                      "бокс 1x1 развёрнут, номер пункта %s присоединён" % merged_num)
                return "\n%s\n" % text
            if numbered:
                self.report.add_issue("info", "layout_table_unwrapped",
                                      "бокс 1x1 с %d нумерованными пунктами развёрнут в обычный текст" % numbered)
                return "\n%s\n" % text
            self.report.add_issue("info", "layout_table_unwrapped",
                                  "таблица 1x1 развёрнута в текст (это бокс вёрстки, не данные)")
            return "\n> %s\n" % text.replace("\n", "\n> ")
        if grid["n_rows"] == 1 and not grid["has_merged"] and not ctx.get("pending_table_caption"):
            cells = []
            for c in grid["cells"]:
                t = " ".join(self.render_inline(b).strip() for b in cell_blocks(c["tc"]) if ln(b) == "p")
                if t.strip():
                    cells.append(t.strip())
            if cells:
                self.report.add_issue("info", "layout_row_unwrapped",
                                      "однострочная таблица-раскладка (%d ячеек) развёрнута в текст" % len(cells))
                return "\n%s\n" % " · ".join(cells)
        self.tbl_seq += 1
        tid = "%s-tbl%03d" % (self.doc_id or "doc", self.tbl_seq)
        renders = {}
        nested = 0
        for c in grid["cells"]:
            parts = []
            for b in cell_blocks(c["tc"]):
                if ln(b) == "p":
                    t = self.render_inline(b, {"bookmarks": [], "math": 0, "fields": [],
                                               "links": [], "ctx": ctx}).strip()
                    parts.append(t)
                else:
                    nested += 1
                    parts.append(self.convert_table(b, ctx))
            renders[(c["r"], c["c"])] = " ".join(p for p in parts if p).strip()

        def owner_at(r, cc):
            idx = grid["owner"].get((r, cc))
            if idx is not None:
                return grid["cells"][idx], False
            if (r, cc) in grid["covered"]:
                return None, True
            return None, False

        rows_out = []
        for r in range(grid["n_rows"]):
            row = []
            for cc in range(grid["n_cols"]):
                own, covered = owner_at(r, cc)
                row.append("" if own is None else renders.get((own["r"], own["c"]), ""))
            rows_out.append(row)

        caption = ctx.pop("pending_table_caption", "")
        rec = {
            "id": tid, "caption": caption, "n_rows": grid["n_rows"], "n_cols": grid["n_cols"],
            "has_merged": grid["has_merged"], "nested": nested,
            "repeat_header": any(c["repeat_header"] for c in grid["cells"]),
            "spans": [{"r": c["r"], "c": c["c"], "rowspan": c["rowspan"], "colspan": c["colspan"],
                       "text": renders.get((c["r"], c["c"]), "")} for c in grid["cells"]],
            "grid": rows_out,
        }
        self.report.tables.append({k: rec[k] for k in ("id", "caption", "n_rows", "n_cols", "has_merged", "nested")})
        if grid["has_merged"]:
            self.report.add_review("table", tid,
                                   "таблица %dx%d со слияниями ячеек — проверить разметку и заголовки"
                                   % (grid["n_rows"], grid["n_cols"]))
        if nested:
            self.report.add_issue("warning", "nested_table", "вложенные таблицы", tid)
        if not caption:
            self.report.add_issue("info", "no_table_caption", "нет подписи «Таблица N —»", tid)
        if grid["n_rows"] and not any(rows_out[0]):
            self.report.add_issue("warning", "empty_header_row", "первая строка таблицы пуста", tid)
        ctx.setdefault("tables_jsonl", []).append(rec)

        hdr_rows = 1 if (rec["repeat_header"] or self._looks_like_header(rows_out)) else 0
        ctx.setdefault("tables_index", []).append({
            "id": tid,
            "block_id": ctx.get("last_table_block_id", ""),
            "caption": caption,
            "rows": grid["n_rows"],
            "cols": grid["n_cols"],
            "merged": bool(grid["has_merged"]),
            "nested": nested,
            "clause": ctx.get("current_clause", ""),
        })
        parts = []
        if not caption:
            # без подписи таблица ненаходима ни в одном представлении — это дефект
            parts.append("<!-- %s: нет подписи -->" % tid)
        parts.append("<table>")
        for r in range(grid["n_rows"]):
            tag = "th" if r < hdr_rows else "td"
            cells_html = ["<tr>"]
            for cc in range(grid["n_cols"]):
                own, covered = owner_at(r, cc)
                if covered:
                    continue
                if own is None:
                    cells_html.append("<%s></%s>" % (tag, tag))
                    continue
                attrs = ""
                if own["colspan"] > 1:
                    attrs += ' colspan="%d"' % own["colspan"]
                if own["rowspan"] > 1:
                    attrs += ' rowspan="%d"' % own["rowspan"]
                content = renders.get((own["r"], own["c"]), "").strip().replace("\n", "<br />")
                cells_html.append("<%s%s>%s</%s>" % (tag, attrs, esc_html_keep_inline(content), tag))
            cells_html.append("</tr>")
            parts.append("".join(cells_html))
        parts.append("</table>")
        if self.opts.tables in ("md", "both") and not grid["has_merged"]:
            parts.append(self.grid_to_md_table(rows_out, hdr_rows))
        return "\n" + "\n".join(parts) + "\n"

    @staticmethod
    def _looks_like_header(rows) -> bool:
        if not rows:
            return False
        first = [c.strip() for c in rows[0]]
        filled = [c for c in first if c]
        return len(filled) > 1 and all(len(c) < 80 for c in filled)

    @staticmethod
    def grid_to_md_table(rows, hdr_rows=1) -> str:
        if not rows:
            return ""
        n_cols = max(len(r) for r in rows)
        rows = [list(r) + [""] * (n_cols - len(r)) for r in rows]
        esc = lambda c: c.replace("\n", "<br />").replace("|", "\\|").strip()
        out = []
        if hdr_rows:
            out.append("| " + " | ".join(esc(c) for c in rows[0]) + " |")
            out.append("|" + "|".join([" --- "] * n_cols) + "|")
            body = rows[1:]
        else:
            out.append("| " + " | ".join([" "] * n_cols) + " |")
            out.append("|" + "|".join([" --- "] * n_cols) + "|")
            body = rows
        for r in body:
            out.append("| " + " | ".join(esc(c) for c in r) + " |")
        return "\n".join(out)

    # ---------------- blocks ---------------- #
    def ppr_info(self, p) -> dict:
        ppr = child(p, "pPr")
        style_id = val(ppr, "pStyle") if ppr is not None else None
        num_id = ilvl = None
        numpr = child(ppr, "numPr") if ppr is not None else None
        if numpr is not None:
            num_id = val(numpr, "numId")
            ilvl = val(numpr, "ilvl") or "0"
        kind, level = self.styles.kind_of(style_id)
        return {"style_id": style_id, "num_id": num_id, "ilvl": ilvl, "style_kind": kind,
                "style_level": level,
                "jc": val(ppr, "jc") if ppr is not None else None,
                "ind_left": val(child(ppr, "ind"), "left") if ppr is not None else None}

    def block_id(self, base: str, ctx: dict) -> str:
        """Уникальный Obsidian-совместимый block id (латиница/цифры/дефис)."""
        used = ctx.setdefault("block_ids", {})
        bid = base
        n = 2
        while bid in used:
            bid = "%s-%d" % (base, n)
            n += 1
            if n > 50:
                break
        used[bid] = used.get(base, 0) + 1
        return bid

    def lookahead_text(self, ctx: dict) -> str:
        """Текст следующего абзаца (нужен для «висячих» номеров пунктов)."""
        if not ctx.get("_paras"):
            body = child(self.doc, "body")
            ctx["_paras"] = [c for c in body if ln(c) == "p"]
            ctx["_pindex"] = {}
            for i, el in enumerate(ctx["_paras"]):
                ctx["_pindex"][id(el)] = i
        return ctx

    def next_paragraph_text(self, p, ctx: dict) -> str:
        """Текст следующего значимого блока (абзац ИЛИ ячейка таблицы-бокса)."""
        if not ctx.get("_body_children"):
            body = child(self.doc, "body")
            ctx["_body_children"] = list(body)
        kids = ctx["_body_children"]
        try:
            i = next(k for k, el in enumerate(kids) if el is p)
        except StopIteration:
            return ""
        for j in range(i + 1, min(i + 4, len(kids))):
            el = kids[j]
            if ln(el) == "p":
                t = clean_text(self.plain_text(el)).strip()
                if t:
                    return t
            elif ln(el) == "tbl":
                grid = parse_table(el)
                texts = []
                for c in grid["cells"]:
                    for b in cell_blocks(c["tc"]):
                        if ln(b) == "p":
                            t = clean_text(self.plain_text(b)).strip()
                            if t:
                                texts.append(t)
                if texts:
                    return " ".join(texts)
        return ""

    def convert_block(self, el, ctx: dict) -> str:
        n = ln(el)
        if n == "p":
            return self.convert_paragraph(el, ctx)
        if n == "tbl":
            return self.convert_table(el, ctx)
        if n == "sdt":
            content = child(el, "sdtContent")
            return "".join(self.convert_block(c, ctx) for c in content) if content is not None else ""
        if n in ("sectPr", "bookmarkStart", "bookmarkEnd", "proofErr"):
            return ""
        self.report.unknown_elements.append("body/" + n)
        return ""

    def convert_paragraph(self, p, ctx: dict) -> str:  # noqa: C901
        info = {"bookmarks": [], "math": 0, "fields": [], "links": [], "ctx": ctx}
        text = self.render_inline(p, info)
        pinfo = self.ppr_info(p)
        plain = text.strip()
        # для распознавания паттернов (примечание/подпись/пункт) разметку игнорируем:
        # в ГОСТ-документах «П р и м е ч а н и е —» часто целиком в жирном прогоне
        bare = strip_markup(plain).strip()
        self.report.paragraphs += 1
        if not plain:
            self.report.empty_paragraphs += 1
        for b in info["bookmarks"]:
            if plain:
                # закладка на обычном абзаце: якоря у такого блока нет ни в Obsidian,
                # ни на GitHub, поэтому помечаем как «bookmark» и при разрешении
                # оставляем текст ссылки без href
                self.anchors.setdefault(b, ("#" + make_anchor(plain), "bookmark"))

        # display math paragraph
        if info["math"] and re.search(r"\$\$", text):
            return text if text.startswith("$$") else "\n%s\n" % text.strip()

        if not plain:
            return ""

        # ---------- служебные строки титульного листа ----------
        if TITLE_SERVICE_RE.match(bare) and not ctx.get("body_started"):
            return "\n%s\n" % bare

        # ---------- «Предисловие» / «Введение»: раздел без номера ----------
        if UNNUMBERED_HEADING_RE.match(bare):
            # «Предисловие»/«Введение» — разделы без номера, но основную часть
            # документа они НЕ начинают: после них ещё идёт перечень сведений
            self.report.headings["1"] = self.report.headings.get("1", 0) + 1
            ctx["sections"].append({"level": 1, "number": "", "title": bare, "full": bare})
            anchor = make_anchor(bare)
            self.anchors.setdefault(bare, ("#" + anchor, "heading"))
            self.anchor_titles.setdefault(bare, bare)
            return "\n# %s\n" % bare

        # ---------- headings ----------
        heading_level = None
        auto_num = self.nums.number_for(pinfo["num_id"], pinfo["ilvl"]) if pinfo["num_id"] else None
        mm = CLAUSE_RE.match(bare)
        titlelike = looks_like_title(bare)

        # «1 Область применения» — достоверное начало основной части документа:
        # до него идут Предисловие/Введение, где нумерованные строки являются
        # перечислением («1 ИСПОЛНИТЕЛЬ …»), а не разделами
        if mm and re.match(r"^1\s+(Область применения|Назначение|Нормы и требования)", bare, re.I):
            ctx["body_started"] = True

        # ---------- «Примечание» внутри пункта и его нумерованные строки ----------
        # Важно: «1 Рампа может располагаться…» после термина — это примечание к термину,
        # но «3.14 рампа: …» — уже следующее определение. Различаем по форме номера
        # (одиночная цифра = перечисление примечаний) и по сбросу in_notes на определении.
        in_notes = bool(ctx.get("in_notes"))
        is_note_word = bool(NOTE_WORD_RE.match(bare))
        # «1 Рампа может…» после «Примечания» — пункт примечания; но «2 Нормативные
        # ссылки» после списка ГОСТов — раздел. Различаем строго по предыдущему блоку.
        # перечисления внутри «Примечания» нумеруются 1, 2, 3…; разделы документа —
        # тоже («3 Термины и определения»), поэтому требуем непосредственную
        # близость к метке примечания, а не просто флаг in_notes
        # Разделы нормативного документа образуют последовательность 1, 2, 3…,
        # подразделы — N.1, N.2… Это надёжный структурный признак, который отличает
        # «3 Термины и определения» (раздел) от «2 Высота рампы определяется…»
        # (перечисление внутри примечания) без хрупкой логики «по соседнему блоку».
        seq = ctx.setdefault("_seq", {"section": 1, "sub": 1})
        num_txt = (mm.group(1).rstrip(".") if mm else "")
        seq_ok = section_seq_ok(num_txt, seq)
        note_item = bool(re.match(r"^\d\s+[А-ЯЁA-Z(]", bare)
                         and not re.match(r"^\d+\.\d", bare)
                         and len(bare) < 200
                         and not seq_ok
                         and ctx.get("prev_kind") in ("note", "note_label", "note_item"))
        if is_note_word or note_item:
            self.report.notes += 1
            if is_note_word:
                body_txt = bare
                # «Примечание<sup>е</sup> - Классы…» (маркер сноски СПС) -> «Примечание — Классы…»
                for pat in (r"^Примечания?\s*[а-яa-z]?\s*[—–-]\s*",
                            r"^Примечания?\s*[а-яa-z]?\s*",
                            r"^Сноски?\s*[а-яa-z]?\s*[—–-]?\s*"):
                    new_body = re.sub(pat, "", body_txt, flags=re.I)
                    if new_body != body_txt:
                        body_txt = new_body
                        break
                body_txt = body_txt.strip()
                label = "**Примечания**" if bare.lower().startswith("примечания") else "**Примечание**"
                ctx["in_notes"] = True
                ctx["prev_kind"] = "note"
                if not body_txt:
                    ctx["prev_kind"] = "note_label"
                    return "\n> %s\n" % label
                return "\n> %s — %s\n" % (label, body_txt)
            ctx["prev_kind"] = "note_item"
            return "\n> %s\n" % bare

        uppercase_title = bool(mm) and (mm.end() < len(bare)) and \
            sum(1 for c in bare[mm.end():] if c.isupper()) >= max(
                3, int(0.6 * max(1, len([c for c in bare[mm.end():] if c.isalpha()]))))
        if pinfo["style_kind"] == "heading" and not (ctx.get("manual_mode") and uppercase_title):
            heading_level = pinfo["style_level"] or 1
            if not titlelike:
                self.report.add_review("heading", plain[:20],
                                       "стиль «заголовок», но текст выглядит как пункт — проверить уровень")
        elif mm and self.opts.numbered_headings and auto_num is None and titlelike:
            # manually typed "5.2 Заголовок" -> heading (rare; default is off,
            # because in ГОСТ/СП a manually numbered paragraph is a пункт, not a heading)
            depth = len(mm.group(1).split("."))
            if depth <= self.opts.max_heading_from_numbering:
                heading_level = depth
        elif auto_num and self.opts.numbered_as_heading and titlelike:
            depth = len(str(auto_num).split("."))
            if depth <= self.opts.max_heading_from_numbering:
                heading_level = depth
        # приложение («Приложение А», «ПРИЛОЖЕНИЕ Б (обязательное)») — всегда раздел
        appendix_m = APPENDIX_WORD_RE.match(bare) or APPENDIX_RE.match(bare)
        if appendix_m and heading_level is None:
            heading_level = 1
        elif appendix_m:
            heading_level = 1
        # ручной режим нумерации: 1–2 уровня номера = раздел/подраздел, 3+ = пункт
        prev_kind = ctx.get("prev_kind")
        value_line = bool(re.match(r"^\d+(?:\.\d+)*\s+[-–—]\s", bare))   # «400 - для помещений…»
        if heading_level is None and mm and ctx.get("manual_mode") and titlelike and not value_line:
            if ctx.get("body_started"):
                if seq_ok:
                    heading_level = min(len(mm.group(1).split(".")), 2)
                else:
                    self.report.add_issue("info", "section_sequence_skip",
                                          "абзац «%s» не похож на следующий раздел (ожидали %d) — оставлен текстом"
                                          % (bare[:50], seq["section"]))
            elif uppercase_title:
                pass                      # «1 ИСПОЛНИТЕЛЬ - …» в Предисловии — не раздел


        if heading_level:
            num, rest = "", bare
            if mm:
                num = mm.group(1).rstrip(".")
                rest = bare[mm.end():].strip()
            else:
                am = APPENDIX_WORD_RE.match(bare) or APPENDIX_RE.match(bare)
                if am:
                    num = "%s %s" % (am.group(1), am.group(2))
                    rest = bare[am.end():].strip().lstrip(".—–- ")
            if auto_num:
                if num and auto_num != num:
                    self.report.numbering_issues.append({"para": num, "computed": auto_num, "text": rest[:70]})
                    self.report.add_issue("warning", "numbering_mismatch",
                                          "авто-нумерация %s ≠ текст %s" % (auto_num, num), rest[:70])
                elif not num:
                    num = auto_num
                    self.report.add_issue("info", "numbering_restored",
                                          "номер восстановлен из numbering.xml", num)
            elif not num:
                self.report.add_issue("info", "heading_without_number", "заголовок без номера", rest[:70])
            full = ("%s %s" % (num, rest)).strip()
            full = re.sub(r"[\s]*[—–-]\s*$", "", full).strip()
            anchor = make_anchor(full)
            self.anchor_titles[anchor] = full
            self.anchors.setdefault(anchor, ("#" + anchor, "heading"))
            ctx["prev_kind"] = "heading"
            ctx["prev_text"] = rest
            ctx["in_notes"] = False
            _advance_section_seq(ctx, num, heading_level)
            ctx["sections"].append({"level": heading_level, "number": num, "title": rest, "full": full})
            key = str(heading_level)
            self.report.headings[key] = self.report.headings.get(key, 0) + 1
            return "\n%s %s\n" % ("#" * min(heading_level, 6), full)

        # ---------- запись списка литературы («[12] Приказ …») ----------
        bib = BIBLIO_RE.match(bare)
        if bib and (ctx.get("in_bibliography") or len(ctx.get("biblio") or {}) > 3):
            ctx["in_bibliography"] = True
            key = bib.group(1).lstrip("0") or "0"
            bid = self.block_id("biblio-" + key, ctx)
            self.anchors.setdefault(key, ("#^" + bid, "block"))
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            self.anchor_titles.setdefault(bid, "[%s]" % key)
            # закладка Word в экспорте СПС вешается на весь текст записи
            for a in anchor_variants(plain):
                self.anchors.setdefault(a, ("#^" + bid, "block"))
            for a in anchor_variants(BIBLIO_RE.sub("", plain)):
                self.anchors.setdefault(a, ("#^" + bid, "block"))
            if not ctx.get("biblio_heading_emitted"):
                ctx["biblio_heading_emitted"] = True
                self.report.headings["2"] = self.report.headings.get("2", 0) + 1
                return "\n## Список использованных источников\n\n%s ^%s\n" % (plain, bid)
            return "\n%s ^%s\n" % (plain, bid)

        # ---------- «висячий» номер пункта: номер отдельно, текст отдельно ----------
        only_num = NUMBER_ONLY_RE.match(bare)
        if only_num and not plain.startswith(("<table", "$$", ">")):
            nxt = self.next_paragraph_text(p, ctx)
            dangling_num = only_num.group(1).rstrip(".")
            mm = mm or only_num
            nxt_starts_same = bool(nxt) and bool(
                re.match(r"^%s\b" % re.escape(dangling_num.replace(".", r"\.")), nxt))
            if nxt_starts_same:
                # номер уже есть в следующем блоке (бокс/таблица) — свой выбрасываем
                self.report.add_issue("info", "dangling_clause_number",
                                      "пустой абзац с номером %s удалён: текст идёт следующим блоком" % dangling_num)
                return ""
            if nxt and DEFINITIONISH_RE.match(nxt):
                # номер пункта стоит отдельным абзацем, а определение — в следующем блоке
                # (у экспортов справочных систем это часто «бокс» из таблицы 1x1)
                ctx["merge_clause_into_box"] = dangling_num
                self.report.add_issue("info", "dangling_clause_number",
                                      "номер пункта %s перенесён на следующий блок" % dangling_num)
                return ""
            if nxt and DEFINITIONISH_RE.match(nxt) and len(nxt) > 12:
                clause_num = dangling_num
                clause_body = nxt
                ctx.setdefault("clauses", []).append(clause_num)
                ctx["current_clause"] = clause_num
                ctx["prev_kind"] = "def"
                ctx["in_notes"] = False
                self.report.clauses += 1
                self.report.add_issue("info", "dangling_clause_number",
                                      "номер пункта %s стоял отдельным абзацем — склеен с текстом" % clause_num)
                bid = self.block_id(clause_block_id(clause_num), ctx)
                self.anchors.setdefault(bid, ("#^" + bid, "block"))
                ctx.setdefault("skip_next", []).append(nxt)
                return '\n**%s** %s ^%s\n' % (clause_num, clause_body, bid)

        # ---------- clause paragraph: numbered body text (a "пункт") ----------
        clause_num = None
        clause_body = plain
        if mm:
            clause_num = mm.group(1).rstrip(".")
            clause_body = bare[mm.end():].strip()
        elif auto_num:
            clause_num = auto_num
            clause_body = bare
        m_range = None
        range_to = ""
        if clause_num:
            m_range = EXCLUDED_RANGE_FULL_RE.match(clause_body)
            if m_range:
                range_to = m_range.group(2)
            else:
                m_tail = EXCLUDED_RANGE_TAIL_RE.match(clause_body)
                if m_tail:
                    m_range, range_to = m_tail, m_tail.group(1)
        if clause_num and m_range:
            m_ex = EXCLUDED_RE.search(clause_body)
            m_am = AMENDMENT_RE.search(clause_body)
            ctx.setdefault("excluded_clauses", []).append({
                "clause": clause_num, "range_to": range_to,
                "since": m_ex.group(1) if m_ex else "",
                "amendment": m_am.group(1) if m_am else ""})
            ctx["excluded_range"] = (clause_num, range_to)
            self.report.add_issue("warning", "clauses_excluded_range",
                                  "Пункты %s–%s исключены с %s%s" % (
                                      clause_num, range_to,
                                      m_ex.group(1) if m_ex else "?",
                                      (" (изменение N %s)" % m_am.group(1)) if m_am else ""))
            bid = self.block_id(clause_block_id(clause_num), ctx)
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            return ('\n> [!WARNING] Пункты %s–%s исключены с %s%s\n>\n> %s ^%s\n'
                    % (clause_num, range_to, m_ex.group(1) if m_ex else "?",
                       (" (изменение N %s)" % m_am.group(1)) if m_am else "", clause_body, bid))

        if clause_num and EXCLUDED_RE.search(clause_body):
            # «10.10 - 10.21 Исключены с 08.01.2024. - Изменение N 5» — пункт изъят
            # изменением. Помечаем явно: RAG не должен выдавать его как требование.
            m_ex = EXCLUDED_RE.search(clause_body)
            m_am = re.search(r"Изменение\s*(?:N|№)?\s*(\d{1,2})", clause_body, re.I)
            ctx.setdefault("excluded_clauses", []).append({
                "clause": clause_num, "since": m_ex.group(1),
                "amendment": m_am.group(1) if m_am else ""})
            self.report.add_issue("warning", "clause_excluded",
                                  "пункт %s исключён с %s%s" % (
                                      clause_num, m_ex.group(1),
                                      (" (изменение N %s)" % m_am.group(1)) if m_am else ""))
            bid = self.block_id(clause_block_id(clause_num), ctx)
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            return ('\n> [!WARNING] Пункт %s исключён с %s%s\n>\n> %s ^%s\n'
                    % (clause_num, m_ex.group(1),
                       (" (изменение N %s)" % m_am.group(1)) if m_am else "",
                       clause_body, bid))

        if clause_num:
            ctx.setdefault("clauses", []).append(clause_num)
            ctx["current_clause"] = clause_num
            is_def = bool(TERM_DEF_RE.match(bare))
            ctx["prev_kind"] = "def" if is_def else "clause"
            ctx["prev_text"] = bare
            ctx["in_notes"] = False
            self.report.clauses += 1
            # пункты служебной зоны (Предисловие) не должны занимать идентификаторы разделов
            # Пространства имён идентификаторов:
            #   c…   — пункты («8.1.6», «3.14»);
            #   i…   — перечисления внутри пункта («1 Следует задать…», «2 Оконное…»);
            #   fm-… — служебная зона титула/Предисловия.
            # Признак перечисления структурный: одиночный номер без точки.
            if not ctx.get("body_started"):
                prefix = "fm-"
            elif "." not in clause_num:
                prefix = "i"
            else:
                prefix = ""
            bid = self.block_id(prefix + clause_block_id(clause_num), ctx)
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            self.anchor_titles.setdefault(bid, "%s %s" % (clause_num, clause_body[:60]))
            for a in anchor_variants(bare):
                self.anchors.setdefault(a, ("#^" + bid, "block"))
            # вводка перечисления («Насыпные грунты состоят из … или смесей:»)
            # выглядит как определение термина, но за ней идёт список — не цитируем
            nxt_after = self.next_paragraph_text(p, ctx)
            is_list_intro = bool(re.search(r":\s*$", bare)) and bool(
                re.match(r"^(?:[а-яa-z]\)|\d{1,2}\s+[А-ЯЁ]|[-*•])", nxt_after or ""))
            if is_list_intro:
                is_def = False
                self.report.add_issue("info", "list_intro_not_definition",
                                      "абзац %s — вводка перечисления, а не определение термина" % clause_num)
            if is_def and self.opts.definitions_as_quotes:
                # раздел «Термины и определения»: определение визуально отделяется
                # от требования цитатой, термин выделяется жирным
                defm = TERM_DEF_RE.match(bare)
                term_end = defm.end()
                head = strip_markup(bare[len(mm.group(0)):term_end]).rstrip(": ").strip() if mm else ""
                tail = bare[term_end:].strip()
                if head and len(head) <= 60:
                    m_em = re.match(r"^([*_]{1,3})(.+?)\1$", head.strip())
                    if m_em:                      # термин уже выделен в источнике
                        head = m_em.group(2).strip()
                    clause_body = "**%s**: %s" % (head, tail)
                self.report.definitions += 1
                return '\n> **%s** %s ^%s\n' % (clause_num, clause_body, bid)
            return '\n**%s** %s ^%s\n' % (clause_num, clause_body, bid)

        # ---------- figure captions ----------
        fm = FIG_CAPTION_RE.match(bare)
        if fm:
            label = bare[: fm.end()].strip()
            rest = bare[fm.end():].strip().lstrip("—–- .:")
            ctx["last_figure"] = label
            figs = ctx.setdefault("figures", [])
            figs.append({"label": label, "caption": rest})
            bid = self.block_id(figure_block_id(len(figs)), ctx)
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            self.anchor_titles.setdefault(bid, "%s — %s" % (label, rest))
            for av in anchor_variants("%s — %s" % (label, rest)):
                self.anchors.setdefault(av, ("#^" + bid, "block"))
            caption_md = "**%s — %s**\n\n^%s" % (label, rest, bid)
            if ctx.get("pending_figure"):
                # картинка уже напечатана абзацем выше — подпись идёт сразу за ней
                ctx["pending_figure"] = False
                return "\n%s\n" % caption_md
            ctx["deferred_figure_caption"] = caption_md
            return "\n<!-- figure-caption-deferred -->\n"
        tm = TBL_CAPTION_RE.match(bare)
        if tm:
            # подпись «путешествует» вместе с таблицей: она же служит её якорем
            ctx["pending_table_caption"] = bare
            caps = ctx.setdefault("table_captions", [])
            caps.append(bare)
            bid = self.block_id(table_block_id(len(caps)), ctx)
            ctx["last_table_block_id"] = bid
            self.anchors.setdefault(bid, ("#^" + bid, "block"))
            self.anchor_titles.setdefault(bid, bare)
            for a in anchor_variants(bare):
                self.anchors.setdefault(a, ("#^" + bid, "block"))
            # Obsidian: для структурных блоков (таблиц) идентификатор — на отдельной
            # строке, с пустой строкой до и после
            return "\n%s\n\n^%s\n" % (bare, bid)

        # ---------- notes ----------
        if NOTE_RE.match(bare):
            self.report.notes += 1
            body = NOTE_RE.sub("", bare, count=1).strip().lstrip("—–-: .")
            if self.opts.notes == "quote":
                return "\n> **Примечание** — %s\n" % body
            return "\n**Примечание** — %s\n" % body

        # ---------- numbered / bulleted list items ----------
        if pinfo["num_id"]:
            auto = self.nums.number_for(pinfo["num_id"], pinfo["ilvl"])
            self.report.list_items += 1
            indent = "  " * min(int(pinfo["ilvl"] or 0), 4)
            body = bare
            if auto and body.startswith(auto):
                body = body[len(auto):].lstrip(".\) \u00a0").strip()
            marker = auto if auto else "-"
            if marker != "-":
                marker = marker if (marker.endswith((")", ".")) or re.match(r"^\d+(\.\d+)*$", marker)) else marker + ")"
            return "%s%s %s" % (indent, marker, body)
        if LIST_MARK_RE.match(bare):
            self.report.list_items += 1
            return plain

        # ---------- centered uppercase pseudo-titles ----------
        if pinfo["jc"] == "center" and len(plain) < 120 and plain.isupper() and len(plain) > 3:
            ctx["prev_kind"] = "title"
            ctx["in_notes"] = False
            return "\n**%s**\n" % plain

        # обычный абзац сбрасывает режим «примечаний»: иначе следующий за списком
        # раздел («3 Термины и определения») распознаётся как пункт примечания
        ctx["prev_kind"] = "text"
        ctx["prev_text"] = bare
        ctx["in_notes"] = False
        return "\n%s\n" % plain

    # ---------------- top level ---------------- #
    def first_lines(self, n=24) -> list:
        """Строки титульного листа. Останавливаемся на первом заголовке или пункте,
        иначе в метаданные уезжает тело документа (включая плоский текст формул)."""
        body = child(self.doc, "body")
        out = []
        for el in body:
            if ln(el) == "tbl":
                break
            if ln(el) != "p":
                continue
            if self.styles.kind_of(self.ppr_info(el)["style_id"])[0] == "heading":
                break
            t = self.plain_text(el).strip()
            if not t:
                continue
            if CLAUSE_RE.match(t) or APPENDIX_RE.match(t):
                break
            out.append(t)
            if len(out) >= n:
                break
        return out

    # ---------------- pre-scan ---------------- #
    def prescan(self, body) -> dict:
        """Один проход по абзацам до конвертации.

        Определяет:
          * есть ли в документе стилевые заголовки (у экспортов из КонсультантПлюс/
            Гарант их нет вообще — тогда разделы приходится восстанавливать по номеру);
          * библиографический список («[12] …») — чтобы разрешить ссылки вида P1553,
            которые такие экспорты оставляют «как есть»;
          * вендора экспорта.
        """
        info = {"styled_headings": 0, "numbered_paras": 0, "clauses": 0,
                "biblio": {}, "vendor": "", "vendor_lines": 0}
        for el in body:
            if ln(el) != "p":
                continue
            pinfo = self.ppr_info(el)
            if pinfo["style_kind"] == "heading":
                info["styled_headings"] += 1
            text = self.plain_text(el).strip()
            if not text:
                continue
            if VENDOR_LINE_RE.match(text) or "КонсультантПлюс" in text[:40] or "consultant.ru" in text:
                info["vendor_lines"] += 1
                if not info["vendor"] and "КонсультантПлюс" in text:
                    info["vendor"] = "КонсультантПлюс"
                elif not info["vendor"] and "Гарант" in text:
                    info["vendor"] = "Гарант"
            if pinfo["num_id"]:
                info["numbered_paras"] += 1
            m = CLAUSE_RE.match(text)
            if m:
                info["clauses"] += 1
            b = BIBLIO_RE.match(text)
            if b:
                info["biblio"][b.group(1).lstrip("0") or "0"] = BIBLIO_RE.sub("", text).strip()
            else:
                # экспорты СПС иногда делают из «[12]» гиперссылку — номер остаётся в тексте
                mb = re.search(r"\[(\d{2,5})\]", text)
                if mb and len(text) > 60:
                    info["biblio"].setdefault(mb.group(1).lstrip("0") or "0", text)
        return info

    def convert(self, outdir: str, stem: str = "") -> tuple:
        stem = stem or os.path.splitext(os.path.basename(self.path))[0]
        meta = extract_meta(self.first_lines(), stem)
        self.doc_id = meta.get("doc_id") or slugify(stem)
        # режим «папка на документ»: outdir — сама папка документа → картинки в assets/
        _out_base = os.path.basename(os.path.normpath(outdir))
        _note_name = _ob.note_name((meta.get("designation") or "").strip())
        self.assets_dirname = ("assets"
                               if _out_base and _out_base == _note_name
                               else "%s.assets" % self.doc_id)
        self.report.doc_id = self.doc_id
        self.report.meta = meta

        body = child(self.doc, "body")
        prescan = self.prescan(body)
        # «ручной» режим: стилевых заголовков нет, но пункты пронумерованы в тексте.
        # Типично для экспортов из справочных систем — иначе документ получается
        # абсолютно плоским (0 заголовков на 58 страниц).
        manual_mode = (prescan["styled_headings"] == 0 and prescan["clauses"] >= 5)
        if manual_mode:
            self.report.add_issue("info", "manual_numbering_mode",
                                  "в документе нет стилевых заголовков (%d нумерованных абзацев) — "
                                  "разделы восстановлены по номеру" % prescan["clauses"])
            self.report.add_review("structure", "",
                                   "структура разделов восстановлена эвристикой (в источнике нет стилей заголовков)")
        if prescan["vendor"]:
            self.report.add_issue("warning", "vendor_export",
                                  "источник — экспорт из справочной системы «%s» (%d служебных строк)"
                                  % (prescan["vendor"], prescan["vendor_lines"]))
            self.report.add_review("provenance", "",
                                   "источник — экспорт из «%s»: проверьте полноту, ссылки на документы "
                                   "и легальность использования для вашего сценария" % prescan["vendor"])
        if prescan["biblio"]:
            self.report.add_issue("info", "bibliography",
                                  "найден список источников: %d записей" % len(prescan["biblio"]))
        ctx = {"sections": [], "tables_jsonl": [], "manual_mode": manual_mode,
               "biblio": prescan["biblio"], "vendor": prescan["vendor"]}
        parts = [self.convert_block(el, ctx) for el in body]
        text = "".join(parts)

        # resolve cross references
        for xr in self.pending_xrefs:
            bm = xr["bookmark"]
            entry = self.anchors.get(bm)
            if entry:
                anchor, atype = entry if isinstance(entry, tuple) else (entry, "heading")
                frag = anchor[1:] if anchor.startswith("#") else anchor
                title = self.anchor_titles.get(frag) or xr["cached"].strip()
                if frag.startswith("^"):
                    # цель — блок (пункт/таблица/формула/запись списка литературы).
                    # Ссылка `#^id` работает в Obsidian и на веб-зеркале, но на GitHub
                    # якоря не существует. Если текст ссылки уже называет цель
                    # («таблице 6.1», «[30]») — оставляем чистый текст без битой ссылки.
                    cached = xr["cached"].strip()
                    named = bool(re.search(r"(таблиц|рисунк|приложени|пункт|\[\d+\])", cached, re.I))
                    repl = cached if named else "[%s](%s)" % (title or cached, anchor)
                    if named:
                        self.report.add_issue("info", "block_ref_unwrapped",
                                              "ссылка на блок оставлена текстом (на GitHub якорей блоков нет)")
                elif atype == "heading":
                    repl = "[%s](%s)" % (title, anchor)
                else:
                    # закладка на абзаце без идентификатора: оставляем текст как есть
                    repl = xr["cached"].strip() or title
                    self.report.add_issue("info", "bookmark_no_anchor",
                                          "закладка ведёт на абзац без якоря — ссылка заменена текстом")
                self.report.xrefs_resolved += 1
            else:
                cached = xr["cached"].strip()
                mb = BRACKET_NUM_RE.match(cached)
                bib = ctx.get("biblio") or {}
                if mb and (mb.group(1).lstrip("0") or "0") in bib:
                    key = mb.group(1).lstrip("0") or "0"
                    repl = "[%s](#^biblio-%s)" % (key, key)
                    self.report.xrefs_resolved += 1
                else:
                    repl = cached
                    self.report.add_review("xref", bm,
                                           "закладка не найдена — оставлен кэш поля Word (%r)" % cached[:40])
            text = text.replace("\ue000XREF:%s\ue001" % bm, repl, 1)
        text = XREF_RE.sub("", text)

        text = self.strip_vendor_lines(text)
        biblio = ctx.get("biblio") or {}
        text = self.resolve_pref_links(text, biblio)
        text = self.resolve_pref_tokens(text, biblio)
        text = self.convert_cp_footnotes(text)
        text = self.fix_bracket_refs(text, biblio)
        text = self.drop_vendor_anchor_links(text)
        md_body = self.mark_bibliography(self.postprocess(text))
        # правки/: словарь символов-картинок (docs/08) — до подсчёта слов,
        # чтобы статистика отражала итоговый текст
        if self.opts.glyphs_path:
            md_body = _ob.apply_glyph_rules(md_body, self.opts.glyphs_path, self.report)
        self.report.sections = ctx["sections"]
        self.report.words = len(re.findall(r"[^\s]+", md_body))
        for mm in re.finditer(r"\b(в|во|по|из|с|к|на|со|от|до|см\.)\s*\.", md_body):
            self.report.add_issue("warning", "dangling_reference",
                                  "похоже на потерянную ссылку: «%s»" % mm.group(0),
                                  md_body[max(0, mm.start() - 60):mm.end() + 10].replace("\n", " "))
        if self.report.empty_paragraphs > self.report.paragraphs * 0.5 and self.report.paragraphs:
            self.report.add_issue("warning", "many_empty_paragraphs",
                                  "%d%% пустых абзацев — похоже на артефакт вёрстки/OCR"
                                  % round(100 * self.report.empty_paragraphs / self.report.paragraphs))

        # изменения/исключения видны только в теле документа — досчитываем по всему тексту
        body_amend = sorted({m.group(1) for m in AMENDMENT_RE.finditer(text)}, key=lambda x: int(x))
        if body_amend:
            meta["amendments"] = sorted(set(list(meta.get("amendments") or []) + body_amend),
                                        key=lambda x: int(x) if x.isdigit() else 99)
        body_excl = sorted({m.group(1) for m in
                            re.finditer(r"Исключен\w*\s+с\s+(\d{2}\.\d{2}\.\d{4})", text, re.I)})
        if body_excl:
            meta["exclusions"] = body_excl
        excl_list = ctx.get("excluded_clauses") or []
        if excl_list:
            meta["excluded_clauses"] = excl_list
            self.report.add_review("currency", ", ".join(
                sorted({e["clause"] for e in excl_list}))[:60],
                "в документе есть пункты, исключённые изменениями — рабочая редакция новее "
                "исходного издания; зафиксировать номера изменений в overrides")

        # правки/<doc_id>.yaml — ручные поля фронтмэттера; применяются ПОСЛЕ
        # автоматического распознавания, чтобы ручные значения имели приоритет
        if self.opts.meta_override_path:
            import yaml as _yaml
            with open(self.opts.meta_override_path, encoding="utf-8") as fh:
                extra = _yaml.safe_load(fh) or {}
            meta.update(extra)
            self.report.add_issue("info", "meta_override",
                                  "поля фронтмэттера из правки: %s" % ", ".join(sorted(extra)))

        os.makedirs(outdir, exist_ok=True)
        conf = self.report.compute_confidence()
        self._tables_index = ctx.get("tables_index", [])
        self._figures = ctx.get("figures", [])

        # имя заметки: designation кириллицей (для Obsidian), иначе doc_id
        designation = (meta.get("designation") or "").strip()
        self.note_base = (_ob.note_name(designation)
                          if (self.opts.obsidian and designation) else self.doc_id)
        meta["note_name"] = self.note_base
        meta["aliases"] = _ob.build_aliases(designation, meta.get("supersedes_designations") or [])

        fm = self.build_frontmatter(meta, conf, len(ctx.get("block_ids", {})),
                                    ctx.get("vendor", "") or meta.get("vendor", ""),
                                    ctx.get("biblio") or {}, ctx.get("vendor_links") or {})
        full_md = fm + md_body.rstrip() + "\n" + self.footnotes_block()

        # Obsidian-слой: чистка вендора, маркеры изменений, вики-ссылки с резолвингом
        if self.opts.obsidian:
            registry = _ob.load_registry(self.opts.registry_path)
            if designation:
                registry.add(designation, designation)   # собственная версия
            for old_d in (meta.get("supersedes_designations") or []):
                registry.map[_ob.versionless(old_d)] = {
                    "note": self.note_base, "alias": _ob.versionless(designation)}
            full_md = _ob.postprocess_obsidian(full_md, meta, registry, self.report)

        md_path = os.path.join(outdir, "%s.md" % self.note_base)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(full_md)
        self.write_assets(outdir)
        if ctx["tables_jsonl"]:
            with open(os.path.join(outdir, "%s.tables.jsonl" % self.note_base), "w", encoding="utf-8") as fh:
                for rec in ctx["tables_jsonl"]:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(os.path.join(outdir, "%s.assets.json" % self.note_base), "w", encoding="utf-8") as fh:
            json.dump({"doc_id": self.doc_id, "note_name": self.note_base, "assets": self.assets},
                      fh, ensure_ascii=False, indent=2)

        rep = self.report.to_dict()
        rep["confidence"] = conf
        rep["note_name"] = self.note_base
        with open(os.path.join(outdir, "%s.qa.json" % self.note_base), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(outdir, "%s.qa.md" % self.note_base), "w", encoding="utf-8") as fh:
            fh.write(self.report.to_markdown())
        return md_path, rep

    def strip_vendor_lines(self, text: str) -> str:
        """Убрать служебные строки справочной системы из титульной зоны."""
        if not self.opts.strip_vendor_lines:
            return text
        m = re.search(r"^#{1,6}\s", text, re.M)
        head, rest = (text[: m.start()], text[m.start():]) if m else ("", text)
        kept = [l for l in head.split("\n")
                if not (l.strip() and VENDOR_LINE_RE.match(l.strip()))]
        removed = len(head.split("\n")) - len(kept)
        if removed:
            self.report.add_issue("info", "vendor_lines_stripped",
                                  "удалено служебных строк справочной системы: %d" % removed)
        return "\n".join(kept) + rest

    def resolve_pref_links(self, text: str, biblio: dict) -> str:
        """P1553 (внутренняя ссылка экспорта КонсультантПлюс) -> «[12]» со ссылкой на список литературы."""
        if not biblio:
            return text
        stats = {"resolved": 0, "unresolved": 0}

        def sub(m):
            key = m.group(1)[1:]
            key = key.lstrip("0") or "0"
            if key in biblio:
                stats["resolved"] += 1
                return "[%s](#biblio-%s)" % (key, key)
            stats["unresolved"] += 1
            return "[%s?]" % key

        out = PREF_RE.sub(sub, text)
        if stats["resolved"]:
            self.report.add_issue("info", "pref_resolved",
                                  "ссылок вида P<N> разрешено в список литературы: %d" % stats["resolved"])
        if stats["unresolved"]:
            self.report.add_review("xref", "P…",
                                   "%d ссылок вида P<N> не найдены в списке литературы — проверить текст"
                                   % stats["unresolved"])
        return out

    def resolve_pref_tokens(self, text: str, biblio: dict) -> str:
        """Второй проход: P1553, оставшиеся после разрешения полей Word.

        Экспорт КонсультантПлюс кодирует так два разных объекта:
          * P15xx… — ссылка на запись списка литературы (кэш поля = «[33]»);
          * P111x  — ссылка на сноску (кэш поля = «<*>», «<**>»).
        Первые превращаем в ссылку на `^biblio-N`, вторые — в надстрочный маркер.
        """
        if not biblio:
            return text
        stats = {"resolved": 0, "missing": []}

        def sub(m):
            key = m.group(1).lstrip("0") or "0"
            if key in biblio:
                stats["resolved"] += 1
                return "[%s](#^biblio-%s)" % (key, key)
            stats["missing"].append("P" + m.group(1))
            return m.group(0)

        out = PREF_TOKEN_RE.sub(sub, text)
        if stats["resolved"]:
            self.report.add_issue("info", "pref_resolved_pass2",
                                  "ссылок на список литературы разрешено: %d" % stats["resolved"])
        if stats["missing"]:
            uniq = sorted(set(stats["missing"]))
            self.report.add_issue("warning", "pref_unresolved",
                                  "%d ссылок вида P<N> не соответствуют ни одной записи списка литературы"
                                  % len(stats["missing"]))
            self.report.add_review("xref", ", ".join(uniq[:8]),
                                   "ссылки P<N> без цели — вероятно, сноски или удалённые источники; "
                                   "проверить по официальному тексту")
        return out

    def fix_bracket_refs(self, text: str, biblio: dict) -> str:
        """«[1530?]» -> «[30](#^biblio-30)»: номер источника, «склеенный» экспортом СПС."""
        if not biblio:
            return text
        stats = {"ok": 0, "miss": []}

        def sub(m):
            raw = m.group(1)
            # экспорт СПС «склеивает» номер ссылки с номером источника: [1530?] = «[30]»
            cands = [raw.lstrip("0") or "0"] + [raw[-n:].lstrip("0") or "0" for n in (1, 2, 3)]
            for key in cands:
                if key and key in biblio:
                    stats["ok"] += 1
                    return "[%s](#^biblio-%s)" % (key, key)
            stats["miss"].append(m.group(0))
            return m.group(0)

        out = re.sub(r"\[(\d{2,5})\?\](?![\]:])", sub, text)
        if stats["ok"]:
            self.report.add_issue("info", "bracket_refs_fixed",
                                  "номеров источников восстановлено: %d" % stats["ok"])
        if stats["miss"]:
            self.report.add_review("xref", ", ".join(sorted(set(stats["miss"]))[:6]),
                                   "%d «склеенных» ссылок на источники не найдено в списке литературы"
                                   % len(stats["miss"]))
        return out

    @staticmethod
    def drop_vendor_anchor_links(text: str) -> str:
        """`[<sup>*</sup>](#какая-то-простыня)` -> `<sup>*</sup>`.

        Экспорт СПС вешает закладки на произвольные абзацы, поэтому «сноска»
        ссылается на не имеющий к ней отношения текст. Маркер важнее ссылки.
        """
        return re.sub(r"\[(<sup>\*{1,3}</sup>|<\*{1,3}>)\]\(#[^)]{20,}\)", r"\1", text)

    def convert_cp_footnotes(self, text: str) -> str:
        """`<*>`, `<**>`, `<***>` — надстрочные маркеры сносок справочной системы."""
        found = CP_FOOTNOTE_RE.findall(text)
        if not found:
            return text
        self.report.add_issue("warning", "vendor_footnote_markers",
                              "в тексте %d маркеров сносок справочной системы (<*>, <**>) — "
                              "самих сносок в файле нет" % len(found))
        self.report.add_review("provenance", "<*>",
                               "маркеры сносок «<*>» без текста сносок: источник неполон, "
                               "сверить с официальной публикацией")
        return CP_FOOTNOTE_RE.sub(lambda m: "<sup>%s</sup>" % m.group(1), text)

    @staticmethod
    def mark_bibliography(text: str) -> str:
        """Пометить записи «[N] …» как библиографию (для чанкера: отдельный тип чанка)."""
        lines = text.split("\n")
        out, in_bib, started = [], False, 0
        for i, l in enumerate(lines):
            if BIBLIO_RE.match(l.strip()) and not in_bib:
                in_bib, started = True, i
                out.append("<!-- bibliography -->")
            elif in_bib and not (BIBLIO_RE.match(l.strip()) or not l.strip() or l.startswith("<!--")):
                in_bib = False
                out.append("<!-- /bibliography -->")
            out.append(l)
        if in_bib:
            out.append("<!-- /bibliography -->")
        return "\n".join(out)

    def postprocess(self, text: str) -> str:
        text = re.sub(r"[ \t]+$", "", text, flags=re.M)   # хвостовые пробелы
        text = re.sub(r"\n{3,}", "\n\n", text)
        # блочная формула: ровно `$$\n<содержимое>\n$$` (пустые строки внутри ломают KaTeX)
        text = re.sub(r"\$\$\n+((?:[^$]|\$(?!\$))*?)\n+\$\$", r"$$\n\1\n$$", text)
        text = self._merge_where_blocks(text)
        text = self._insert_block_separators(text)
        # пунктуация и пробелы
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"\b(в|во|по|из|с|со|к|на|от|до|см\.)\s+([,.;:!?])", r"\1 \2", text)
        text = re.sub(r"\(\s+", "(", text)
        text = re.sub(r"\s+\)", ")", text)
        text = re.sub(r"[ \t]{2,}(\^[A-Za-z0-9][A-Za-z0-9-]*)$", r" \1", text, flags=re.M)
        text = re.sub(r"[ \t]+$", "", text, flags=re.M)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"

    # -- постобработка: отдельные шаги ------------------------------------ #
    @staticmethod
    def _merge_where_blocks(text: str) -> str:
        """«где»-блок после формулы: склеить короткие строки-расшифровки в один блок.

        Важно: строки-расшифровки идут ПОДРЯД, без пустых строк. Пустая строка —
        граница блока, иначе мы «проглотим» следующую формулу (реальный баг:
        $A$ — …; / $B$ — … попадали в один блок с $$ … $$ между ними).
        """
        DEFLINE = re.compile(r"^\$[^$\n]+\$\s+[—–-]\s+\S")
        INTRO = ("где", "где:", "здесь", "здесь:", "где —", "где:")
        lines = text.split("\n")
        out, i = [], 0
        while i < len(lines):
            cur = lines[i].strip()
            if cur.lower() in INTRO:
                collected, j = [], i + 1
                while j < len(lines):
                    t = lines[j].strip()
                    if not t:
                        # пустая строка допустима, если за ней снова строка-расшифровка
                        n2 = j + 1
                        while n2 < len(lines) and not lines[n2].strip():
                            n2 += 1
                        if n2 < len(lines) and DEFLINE.match(lines[n2].strip()):
                            j = n2
                            continue
                        break
                    if not DEFLINE.match(t):
                        break
                    collected.append(t)
                    j += 1
                if collected:
                    if out and out[-1].strip():
                        out.append("")            # отбить «где» от предыдущего блока
                    out.append(lines[i])
                    out.append("")
                    # одна строка на обозначение: внутри абзаца это soft-wrap,
                    # то есть в рендере — единый компактный блок без «разрыва» на абзацы
                    body = list(collected)
                    body[-1] = body[-1][:-1] + "." if body[-1].endswith(";") else body[-1]
                    out.extend(body)
                    i = j
                    continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)

    @staticmethod
    def _insert_block_separators(text: str) -> str:
        """Расставить пустые строки так, чтобы Markdown-блоки не слипались.

        Явная машина состояний: отдельно обрабатываются блочная математика ($$…$$),
        fenced-блоки (```), HTML-таблицы, строки с Obsidian block id (^id),
        заголовки и пункты (**8.1.6** …). Внутри $$…$$ и ``` пустые строки
        удаляются — KaTeX на них ломается.
        """
        BID = re.compile(r"^\^[A-Za-z0-9][A-Za-z0-9-]*$")
        LISTY = re.compile(r"^\s{0,8}(\d+(?:\.\d+)*[.)]|[-*+]|[a-zа-я][.)])\s+\S")
        HEAD = re.compile(r"^#{1,6}\s+\S")
        DEF = re.compile(r"^\$[^$\n]+\$\s+[—–-]\s+\S")   # строка «где»-блока

        def kind_of(raw: str) -> str:
            t = raw.strip()
            if not t:
                return "blank"
            if t.startswith("```"):
                return "fence"
            if t == "$$":
                return "math"
            if t.startswith("<table"):
                return "table_open"
            if t.startswith("</table"):
                return "table_close"
            if HEAD.match(t):
                return "heading"
            if t.startswith("<img") or t.startswith("!["):
                return "image"
            if BID.match(t):
                return "bid"
            if CLAUSE_START_RE.match(t):
                return "clause"
            if LISTY.match(raw):
                return "list"
            if DEF.match(t):
                return "def"
            return "text"

        out: list = []
        math_open = code_open = table_open = False
        prev_kind = ""
        prev_list = False

        for raw in text.split("\n"):
            k = kind_of(raw)
            was_inside = math_open or code_open or table_open

            # --- переключение состояний ---
            if k == "fence":
                code_open = not code_open
            elif k == "math":
                math_open = not math_open
            elif k == "table_open":
                table_open = True
            elif k == "table_close":
                table_open = False
            # разделитель ($$ / ``` / </table>) сам является частью блока,
            # поэтому «внутри» и до, и после него
            inside = was_inside or (math_open or code_open or table_open)

            # --- пустые строки ---
            if k == "blank":
                if was_inside or math_open or code_open:
                    continue                    # внутри/вокруг $$…$$ и ```…``` — выбрасываем
                if table_open:
                    if out and out[-1].strip():
                        out.append("")          # внутри <table> сохраняем как есть
                    continue
                if out and out[-1] != "":
                    out.append("")
                prev_list = False
                continue

            # --- разделители перед значимой строкой ---
            need_blank = False
            if not inside:
                if out and out[-1] != "":
                    if prev_kind == "def" and k in ("clause", "heading", "text", "list", "bid", "image"):
                        need_blank = True
                    elif k in ("heading", "clause", "image", "bid"):
                        need_blank = True
                    elif k == "text" and prev_kind in ("clause", "bid", "math", "table_close", "image"):
                        need_blank = True
                    elif k == "def" and prev_kind == "def":
                        need_blank = False      # «где»-блок: строки идут подряд (soft-wrap)
                    elif k == "list" and not prev_list:
                        need_blank = True
                    elif k != "list" and prev_list:
                        need_blank = True
            if need_blank:
                out.append("")
            out.append(raw)

            if not inside:
                prev_list = (k == "list")
                prev_kind = k
            elif k in ("math", "table_close", "fence"):
                prev_kind = {"math": "math", "table_close": "table_close", "fence": "code"}[k]

        text = "\n".join(out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # страховка: <table> всегда отделён пустой строкой
        text = re.sub(r"([^\n])\n?(<table>)", r"\1\n\n\2", text)
        text = re.sub(r"(</table>)\n?([^\n])", r"\1\n\n\2", text)
        return re.sub(r"\n{3,}", "\n\n", text)

    def _source_dict(self, meta: dict, src_sha: str) -> dict:
        """Блок `source:` фронтмэттера + необязательные поля из правок (overrides)."""
        src = {"kind": self.opts.source_kind, "file": os.path.basename(self.path),
               "sha256": src_sha, "uri": self.opts.source_uri}
        for k in ("origin_url", "origin_date", "publisher", "text_layer"):
            if meta.get(k):
                src[k] = meta[k]
        return src

    def build_frontmatter(self, meta: dict, confidence: float, ctx_ids: int = 0,
                          ctx_vendor: str = "", ctx_biblio: dict | None = None,
                          ctx_vendor_links: dict | None = None) -> str:
        try:
            with open(self.path, "rb") as fh:
                src_sha = sha256_bytes(fh.read())
        except OSError:
            src_sha = ""
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
            "title_lines": meta.get("title_lines") or [],
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
            "okp_okpd": meta.get("okp_okpd") or "",
            "ics": meta.get("ics") or [],
            "topics": meta.get("topics") or [],
            "language": self.opts.lang,
            "license": self.opts.license,
            "references": meta.get("referenced_docs") or [],
            "vendor": ctx_vendor or meta.get("vendor") or "",
            "bibliography_entries": len(ctx_biblio or {}),
            "vendor_links_stripped": len(ctx_vendor_links or {}),
            "source": self._source_dict(meta, src_sha),
            "conversion": {"tool": "normconv", "version": __version__,
                           "converted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                           "confidence": confidence, "needs_review": bool(self.report.review),
                           "review_items": len(self.report.review),
                           "verified": bool(meta.get("verified") or False),
                           "verified_by": meta.get("verified_by") or "",
                           "verified_at": meta.get("verified_at") or ""},
            "blocks": {
                "clauses": self.report.clauses,
                "ids": ctx_ids,
                "tables": [
                    {"id": t["id"], "block_id": t["block_id"], "caption": t["caption"],
                     "rows": t["rows"], "cols": t["cols"], "merged": t["merged"]}
                    for t in self._tables_index
                ],
                "figures": [{"label": f["label"], "caption": f["caption"]} for f in self._figures],
                "formulas": len(self.report.formulas),
                "formulas_to_review": [
                    {"id": f["id"], "block_id": f.get("block_id", ""), "confidence": f["confidence"],
                     "plain": f.get("plain", "")[:80]}
                    for f in self.report.formulas
                    if f.get("confidence", 1) < MATH_CONFIDENCE_THRESHOLD or f.get("fallbacks")
                ],
            },
            "stats": {"words": self.report.words,
                      "headings": self.report.headings,
                      "tables": len(self.report.tables),
                      "formulas": len(self.report.formulas),
                      "images": len(self.report.images),
                      "footnotes": self.report.footnotes,
                      "xrefs": "%d/%d" % (self.report.xrefs_resolved, self.report.xrefs_total)},
        }
        return dump_yaml(fm)


# --------------------------------------------------------------------------- #
# minimal YAML emitter (no pyyaml dependency)
# --------------------------------------------------------------------------- #
def dump_yaml(d) -> str:
    import json as _json
    lines = ["---"]

    def scalar(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        if s == "":
            return '""'
        if re.search(r"^[\s]|[\s]$|[:#\[\]{}&*!|>'\"%@`,]", s) or "\n" in s:
            return _json.dumps(s, ensure_ascii=False)
        return s

    def emit(obj, indent=0):
        pad = "  " * indent
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, dict):
                    if not v:
                        lines.append("%s%s: {}" % (pad, k))
                    else:
                        lines.append("%s%s:" % (pad, k))
                        emit(v, indent + 1)
                elif isinstance(v, list):
                    if not v:
                        lines.append("%s%s: []" % (pad, k))
                    elif all(isinstance(x, (str, int, float, bool)) for x in v):
                        lines.append("%s%s:" % (pad, k))
                        for x in v:
                            lines.append("%s  - %s" % (pad, scalar(x)))
                    else:
                        lines.append("%s%s:" % (pad, k))
                        for x in v:
                            lines.append("%s  -" % pad)
                            emit(x, indent + 2)
                else:
                    lines.append("%s%s: %s" % (pad, k, scalar(v)))

    emit(d)
    lines.append("---")
    return "\n".join(lines) + "\n\n"
