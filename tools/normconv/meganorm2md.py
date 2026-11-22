# -*- coding: utf-8 -*-
"""meganorm2md — Меганорм HTML (Word-HTML экспорт) → канонический Markdown.

Концепция источников с 2026-11-04: основной текст действующей редакции —
консолидированный vendor HTML спецсистем (изменения включены в текст).
Контракт вывода — docs/format.md (§3 скелет, §4 пункты, §5 формулы, §6 таблицы,
§7 рисунки, §8 примечания/пометы, §9 ID).

Вход:  .sources/meganorm-<doc>.html — страница meganorm.ru с полной
       консолидированной редакцией (Word-HTML: плоская последовательность div).
Выход: <out>/<ОБЗ>/<ОБЗ>.md + assets/ (PNG, дедупликация по sha256)

Модель элементов (проверено на СП 64.13330.2017):
- тело = плоская последовательность div-братьев под contener_doc (+ <table>);
- заголовки — div с классом s2B* и якорем <a id="ParN">; уровни берутся из
  оглавления (margin-left 25/50/75pt → H2/H3/H4), приложения — по паттерну;
- пункты/абзацы/примечания/перечисления — обычные div (s0 aJ bG);
- таблица: подпись «Таблица N» (div с якорем) [+ помета] + <table>;
- рисунок: img-only div [+ легенда] + подпись «Рисунок N - …»;
- формула: центрированный текстовый div с хвостом «(N)» ИЛИ картинка-формула
  + tag-div «(N)»; инлайн — <i>/<sub>/<sup> (в т.ч. вложенные);
- пометы изменений — вендорная аннотация «(в ред. Изменения N X, утв. …)» →
  нейтральный маркер *(в ред. Изменения № X)* (format.md §8.1: third_party_editorial).

Правила консервативности (format.md §2, §8): неразрешённое — в QA-очередь
(.qa.meganorm2md.json рядом с выходом), а не угадывание в текст.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import re
import urllib.request
from pathlib import Path

from lxml import html as lhtml

# ---------------------------------------------------------------- символы/утилиты

GREEK = {
    "α": "\\alpha", "β": "\\beta", "γ": "\\gamma", "δ": "\\delta", "ε": "\\varepsilon",
    "ζ": "\\zeta", "η": "\\eta", "θ": "\\theta", "ι": "\\iota", "κ": "\\kappa",
    "λ": "\\lambda", "μ": "\\mu", "ν": "\\nu", "ξ": "\\xi", "ο": "\\omicron",
    "π": "\\pi", "ρ": "\\rho", "σ": "\\sigma", "τ": "\\tau", "υ": "\\upsilon",
    "φ": "\\varphi", "χ": "\\chi", "ψ": "\\psi", "ω": "\\omega",
    "Π": "\\Pi",
    "Γ": "\\Gamma", "Δ": "\\Delta", "Θ": "\\Theta", "Λ": "\\Lambda",
    "Ξ": "\\Xi", "Π": "\\Pi", "Σ": "\\Sigma", "Φ": "\\Phi", "Ψ": "\\Psi", "Ω": "\\Omega",
}

LATIN_VARS = set("abcdefghijklmnopqrstuvwxyz")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*[а-яё]?)[\. ]+\s*(.*)$", re.S)
NOTE_RE = re.compile(r"^(Примечание|Пример|Примечания|Примеры)\b\s*[:\-—]?\s*(.*)$", re.S | re.I)

# вендорная помета изменений: «(в ред. Изменения N 4, утв. Приказом …)»
AMEND_INLINE_RE = re.compile(r"\(\s*(?:в ред\.\s*)?[^()]*Изменени[яй]\s+N\s+(\d+)(?:[^()]*)\)")


def clause_block_id(number: str) -> str:
    # 9.4а → c9-4a (кириллический суффикс → латиница, для ASCII-совместимых ID)
    m = re.fullmatch(r"(\d+(?:\.\d+)*)([а-яё]?)", number)
    if not m:
        return "c" + number.replace(".", "-")
    base, letter = m.group(1), m.group(2)
    _LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
            "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
            "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
            "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
            "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
            "ь": "", "э": "e", "ю": "yu", "я": "ya"}
    lat = _LAT.get(letter, letter) if letter else ""
    return f"c{base.replace('.', '-')}{lat}"


def _norm_amend_note(note: str) -> str:
    note = re.sub(r"(Изм\.\s*)N\b", r"\g<1>№", note)
    return re.sub(r"\s+", " ", note).strip()


# ---------------------------------------------------------------- инлайн-математика

def _math_frag(el):
    """Элемент i/sub/sup (с возможными вложенными детьми) → (latex, is_math)."""
    tag = el.tag
    txt = re.sub(r"\s+", " ", (el.text or "").replace("\u00a0", "")).strip()
    greek_in_txt = any(c in GREEK for c in txt)
    core = "".join(GREEK.get(c, c) if c in GREEK else c for c in txt)
    kids = [c for c in el if isinstance(c.tag, str)]
    frag = core
    # собственный sup/sub оборачиваем (вложенные дети обрабатываются ниже отдельно)
    own_sub = any(k.tag == "sub" for k in kids)
    own_sup = any(k.tag == "sup" for k in kids)
    if tag == "sub" and not own_sub:
        frag = "_{" + frag + "}" if frag else ""
    elif tag == "sup" and not own_sup:
        frag = "^{" + frag + "}" if frag else ""
    # вложенные i (вендорный артефакт <i><sub>i</sub></i> — индекс без базы)
    for k in kids:
        if k.tag == "i":
            f2, _ = _math_frag(k)
            frag += f2
    subs = [k for k in kids if k.tag == "sub"]
    sups = [k for k in kids if k.tag == "sup"]
    is_math = greek_in_txt or bool(subs) or bool(sups)
    if subs:
        s = "".join((k.text or "").strip() for k in subs).replace(" ", "")
        frag += "_{" + s + "}"
    if sups:
        s = "".join((k.text or "").strip() for k in sups).replace(" ", "")
        frag += "^{" + s + "}"
    return frag, is_math


def _run_is_math(run) -> bool:
    """Прогон i/sub/sup — математика? (греческая буква ИЛИ индекс у переменной)."""
    for el in run:
        txt = (el.text or "")
        if any(c in GREEK for c in txt):
            return True
    has_base_var = False
    prev_i_text = ""
    for el in run:
        if el.tag == "i":
            t = (el.text or "").strip()
            if any(c.isalpha() and c.lower() in LATIN_VARS for c in t):
                prev_i_text += t
        elif el.tag in ("sub", "sup") and re.search(r"[A-Za-zА-Яа-яЁё]", prev_i_text):
            has_base_var = True
    return has_base_var


def inline_mn(el) -> str:
    """Текст элемента с инлайн-разметкой → Markdown (математические прогоны → $..$).

    <a> → текст; img → ⟦IMG:src|WxH⟧; i/sub/sup прогон → $latex$ если math-run.
    """
    out: list[str] = []

    def _tail(node):
        t = node.tail or ""
        if t:
            out.append(t.replace("\u00a0", " "))

    def walk(node):
        pre = node.text or ""
        if pre:
            out.append(pre.replace("\u00a0", " "))
        consumed_ids: set[int] = set()
        for child in list(node):
            if id(child) in consumed_ids:
                continue
            tag = child.tag if isinstance(child.tag, str) else None
            if tag is None:
                _tail(child)
                continue
            if tag == "br":
                out.append(" ")
                _tail(child)
                continue
            if tag == "img":
                src = child.get("src", "")
                w = int(child.get("width") or 0)
                h = int(child.get("height") or 0)
                out.append(f"⟦IMG:{src}|{w}x{h}⟧")
                _tail(child)
                continue
            if tag == "a":
                walk(child)
                _tail(child)
                continue
            if tag in ("i", "sub", "sup"):
                run = [child]
                nxt = child.getnext()
                while nxt is not None and isinstance(nxt.tag, str) and nxt.tag in ("i", "sub", "sup"):
                    run.append(nxt)
                    nxt = nxt.getnext()
                inner_tails = []   # tail элементов внутри прогона (кроме последнего)
                for k, x in enumerate(run[:-1]):
                    tl = re.sub(r"\s+", " ", (x.tail or "")).replace("\u00a0", " ").strip()
                    if tl:
                        inner_tails.append(tl)
                if _run_is_math(run):
                    parts = []
                    for e in run:
                        f, _ = _math_frag(e)
                        parts.append(f)
                    lat = "".join(parts).strip()
                    # операторы внутри math-прогона — редкость; сохраняем как есть + QA
                    if inner_tails:
                        qa_run_note = " ".join(inner_tails)
                        lat = (lat + " " + re.sub(r"\s+", " ", qa_run_note)).strip()
                    out.append("$" + re.sub(r"[ \t]+", "", lat) + "$")
                    _tail(run[-1])
                else:
                    segs = []
                    for x in run:
                        t = (x.text or "").strip()
                        if t:
                            segs.append(t)
                        tl = re.sub(r"\s+", " ", (x.tail or "")).replace("\u00a0", " ").strip()
                        if tl:
                            segs.append(tl)
                    txt = " ".join(segs)
                    out.append(txt.replace("\u00a0", " "))
                consumed_ids.update(id(x) for x in run)
                continue
            walk(child)
            _tail(child)

    walk(el)
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def display_latex(node):
    """Текстовая формула (div с <i>/<sub>/<sup>) → строка LaTeX.

    Операторы между элементами (=, ·, скобки) живут в .tail — сохраняются как есть.
    """
    parts: list[str] = []

    def w(nd):
        t = re.sub(r"\s+", " ", (nd.text or "")).strip()
        if t:
            parts.append(t)
        for c in list(nd):
            tag = c.tag if isinstance(c.tag, str) else None
            if tag is None:
                continue
            if tag in ("i", "sub", "sup"):
                f, _ = _math_frag(c)
                if f:
                    parts.append(f)
            tl = re.sub(r"\s+", " ", (c.tail or "")).strip()
            if tl:
                parts.append(tl)

    w(node)
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- таблицы

def table_to_md_mn(tbl_el, assets: dict[str, str], qa: list[dict], save_fn) -> tuple[list[str], int]:
    """Таблица → (строки MD, n_rows). GFM если нет слияний, иначе HTML-профиль.

    Картинки в ячейках (формулы) — скачиваются и заменяются на <img src=assets/…>.
    """
    from normconv.html2md import table_to_md as _table_to_md_cntd

    # сначала: скачать картинки из ячеек, заменить элементы
    for img in tbl_el.findall(".//img"):
        src = img.get("src", "")
        if not src:
            continue
        w0 = int(img.get("width") or 0)
        h0 = int(img.get("height") or 0)
        fname = save_fn(img)
        qa.append({"kind": "formula_image_in_table", "uid": f"eu{len(qa):03d}",
                   "dims": f"{w0}x{h0}"})
        if fname:
            new_img = lhtml.fromstring(
                '<div><img src="assets/%s" style="vertical-align:middle;"/></div>' % fname)[0]
            img.getparent().replace(img, new_img)

    lines, _ = _table_to_md_cntd(tbl_el, assets, qa)
    n_rows = len(tbl_el.findall(".//tr"))
    return lines, n_rows


# ---------------------------------------------------------------- основной проход

def convert_mn(src: Path, out_dir: Path, doc_meta: dict, base_url: str = "",
               download_imgs: bool = True) -> dict:
    """Конвертация Меганорм HTML → канон MD. Возвращает отчёт (qa)."""
    raw = src.read_text(encoding="utf-8", errors="replace")
    root = lhtml.fromstring(f"<div id='root'>{raw}</div>")

    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    qa: list[dict] = []
    assets: dict[str, str] = {}   # src → filename (dedup by sha256 inside _save_image)
    stats = {"paragraphs": 0, "tables": 0, "images": 0, "formulas": 0,
             "figures": 0, "clauses": 0, "notes": 0}

    def save_image(img_el_or_src) -> str | None:
        """img-элемент или src → assets/<sha12>.png; имя файла (dedup)."""
        if isinstance(img_el_or_src, str):
            src_attr = img_el_or_src
        else:
            src_attr = img_el_or_src.get("src", "")
        key = src_attr[:200]
        if key in assets:
            return assets[key]
        data = b""
        m = re.match(r"data:image[^;]*;base64,(.+)$", src_attr, re.S)
        if m:
            try:
                data = base64.b64decode(re.sub(r"\s+", "", m.group(1)))
            except Exception:
                qa.append({"kind": "truncated_data_uri", "src": src_attr[:80]})
                return None
        elif src_attr.startswith("http") and download_imgs:
            try:
                req = urllib.request.Request(src_attr, headers={
                    "User-Agent": "normbase-bot/1.0",
                    "Referer": base_url or "https://meganorm.ru/",
                })
                data = urllib.request.urlopen(req, timeout=20).read()
            except Exception as e:
                qa.append({"kind": "image_download_failed", "url": src_attr[:160], "error": str(e)})
                return None
        elif not src_attr.startswith("http") and download_imgs and base_url:
            url = base_url.rstrip("/") + "/" + src_attr.lstrip("/")
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "normbase-bot/1.0",
                    "Referer": base_url,
                })
                data = urllib.request.urlopen(req, timeout=20).read()
            except Exception as e:
                qa.append({"kind": "image_download_failed", "url": url[:160], "error": str(e)})
                return None
        if not data:
            return None
        h = sha256_bytes(data)
        ext = ".png" if data[:4] == b"\x89PNG" else (".gif" if data[:3] == b"GIF" else ".bin")
        name = f"{h[:12]}{ext}"
        (assets_dir / name).write_bytes(data)
        assets[key] = name
        return name

    # ---------- оглавление: якорь → уровень заголовка ----------
    toc_levels: dict[str, int] = {}   # anchor id → 2|3|4
    for details in root.findall(".//details"):
        summ = (details.findtext("summary") or "").strip()
        if summ != "Содержание":
            continue
        for a in details.findall(".//a"):
            href = a.get("href", "")
            m = re.fullmatch(r"#([A-Za-z0-9]+)", href)
            if not m:
                continue
            div = a.getparent()
            while div is not None and div.tag != "div":
                div = div.getparent()
            style = (div.get("style", "") if div is not None else "")
            ml = re.search(r"margin-left:\s*(\d+)pt", style)
            lvl = {25: 2, 50: 3, 75: 4}.get(int(ml.group(1)) if ml else 0, None)
            if lvl:
                toc_levels[m.group(1)] = lvl

    # ---------- тело: плоская последовательность блоков ----------
    container = root.find(".//div[@class='contener_doc']")
    children = list(container) if container is not None else list(root)
    # отбрасываем вендорную шапку до первого заголовка-якоря (Предисловие/Утвержден)
    start = None
    for idx, el in enumerate(children):
        if isinstance(el.tag, str) and re.sub(r"\s+", " ", (el.text_content() or "")).strip() == "Предисловие":
            start = idx
            break
    if start is None:
        start = 0
        for idx, el in enumerate(children):
            if isinstance(el.tag, str) and el.get("class", "").startswith("s2B"):
                start = idx
                break

    md: list[str] = []
    note_seq: dict[str, int] = {}
    pending_caption: str | None = None      # «Таблица N» перед таблицей
    pending_amend_after_table: str | None = None
    pending_image: tuple[str, str, int, int] | None = None  # (src,w,h) img-only div
    last_clause_no: str | None = None
    current_scope = "u"                     # номер раздела / буква приложения для ID

    def scope_id(prefix: str, seq_key: str | None = None) -> str:
        return f"{prefix}{current_scope}" if prefix in ("f", "e") else ""

    def emit_heading(level: int, text: str):
        nonlocal current_scope
        md.append("#" * level + " " + re.sub(r"\s+", " ", text).strip())
        md.append("")
        m = re.fullmatch(r"(\d+)", re.sub(r"\s+", " ", text).strip().split(" ")[0]) if False else None
        first = re.split(r"\s+", re.sub(r"\s+", " ", text).strip(), maxsplit=1)[0]
        if level == 2 and first.isdigit():
            current_scope = first

    def emit_note(label: str, rest: str):
        nonlocal last_clause_no
        kind = "n" if label.lower().startswith("прим") else "x"
        parent = last_clause_no.replace(".", "-").rstrip("-") if last_clause_no else f"s{current_scope}"
        seq_key = f"{kind}{parent}"
        note_seq[seq_key] = note_seq.get(seq_key, 0) + 1
        bid = f"{seq_key}-{note_seq[seq_key]}"
        stats["notes"] += 1
        md.append(f"> *{label}* — {rest}" if rest else f"> *{label}*")
        md.append("")
        md.append(f"^{bid}")
        md.append("")

    def emit_table_block(lines: list[str]):
        nonlocal pending_caption, pending_amend_after_table
        cap = pending_caption
        pending_caption = None
        if cap:
            tnum_m = re.match(r"(?:Таблица|ТАБЛИЦА)\s+([0-9А-Яа-я][\d\.]*[а-я]?)", cap)
            num = tnum_m.group(1) if tnum_m else "u"
            rest = (cap[tnum_m.end():].strip(" —-") if tnum_m else "").strip()
            bid = "t" + re.sub(r"[^0-9A-Za-zА-Яа-я]", "-", num.replace(".", "")).rstrip("-")
            line = f"*Таблица* {num}" + (f" — {rest}" if rest else "") + f" ^{bid}"
        else:
            stats["tables"] += 1
            u = f"tu{stats['tables']:03d}"
            qa.append({"kind": "table_no_caption", "uid": u})
            line = f"*Таблица* ^{u}"
        md.append(line)
        md.extend(lines)
        if pending_amend_after_table:
            md.append("")
            md.append(f"**({pending_amend_after_table}).**")
            md.append("")
            pending_amend_after_table = None

    def emit_figure(caption_text: str):
        nonlocal pending_image, stats
        cap = re.sub(r"\s+", " ", caption_text).strip()
        m = re.match(r"Рисунок\s+([\wА-Яа-я.]+)(?:\s*-\s*(.*))?$", cap)
        num = m.group(1) if m else "?"
        title = (m.group(2) or "").strip() if m else ""
        fname = None
        dims = ("0x0", 0, 0)
        if pending_image:
            src_attr, w, h = pending_image[0], pending_image[1], pending_image[2]
            fname = save_image(src_attr)
            dims = (f"{w}x{h}", w, h)
        stats["figures"] += 1
        u = f"f{current_scope}-{stats['figures']}" if False else None
        # ID рисунка: по номеру из источника (7-1, А.2…), fallback — счётчик
        fig_num_id = re.sub(r"[^0-9A-Za-zА-Яа-я]", "-", num).strip("-")
        u = f"f{fig_num_id}" if fig_num_id else f"fu{stats['figures']:03d}"
        alt = f"Рисунок {num}" + (f" — {title}" if title else "")
        md.append(f"![{alt}](assets/{fname})" if fname else "⟦IMG-LOST⟧")
        md.append("")
        md.append(f"^{u}")
        md.append("")
        pending_image = None

    def emit_formula(latex: str, tag: str | None):
        nonlocal stats
        stats["formulas"] += 1
        u = f"e{current_scope}-{stats['formulas']}" if False else None
        # ID формулы — по номеру раздела + счётчик (как html2md)
        u = f"e{current_scope}-{stats['formulas']}"
        expr = re.sub(r"\s+", " ", latex).strip()
        # операторные символы вне math-прогонов: «·» → \cdot, П (кириллица) → \Pi
        expr = expr.replace("·", "\\cdot").replace("П", "\\Pi")
        if tag:
            md.append(f"$${expr}\\tag{{{tag}}}$")
        else:
            md.append(f"$${expr}$")
        md.append("")
        md.append(f"^{u}")
        md.append("")

    def emit_formula_image(tag: str | None, src_attr: str | None = None):
        nonlocal pending_image, stats
        if src_attr is not None:
            fname = save_image(src_attr)
            w = h = 0
        elif pending_image is not None:
            fname = save_image(pending_image[0])
            w = pending_image[1]
            h = pending_image[2]
        else:
            fname, w, h = None, 0, 0
        stats["formulas"] += 1
        u = f"eu{stats['formulas']:03d}"
        qa.append({"kind": "formula_image", "uid": u, "dims": f"{w}x{h}",
                   "tag": tag or None})
        alt = f"Формула{f' ({tag})' if tag else ''}: запись не подтверждена, см. изображение"
        md.append(f"![{alt}](assets/{fname})" if fname else "⟦IMG-LOST⟧")
        md.append("")
        md.append(f"^{u}")
        md.append("")
        pending_image = None

    def is_amend_note(text: str) -> bool:
        t = text.strip()
        return (t.startswith("(") and len(t) < 200
                and re.search(r"(в ред\.|Изменени[яй]\s+N\s+\d+)", t))

    def norm_amend_line(text: str) -> str:
        # «(в ред. Изменения N 4, утв. …)» → «*(в ред. Изменения № 4)*»
        nums = re.findall(r"Изменени[яй]\s+N\s+(\d+)", text)
        if len(nums) == 1:
            return f"*(в ред. Изменения № {nums[0]})*"
        if len(nums) > 1:
            joined = ", ".join(f"№ {n}" for n in nums)
            return f"*(в ред. Изменений {joined})*"
        return f"**({_norm_amend_note(text.strip('()'))}).**"

    i = start
    while i < len(children):
        el = children[i]
        tag = el.tag if isinstance(el.tag, str) else None
        if tag is None:
            i += 1
            continue
        cls = (el.get("class") or "") if tag == "div" else ""

        # ---------- таблицы ----------
        if tag == "table":
            lines, n_rows = table_to_md_mn(el, assets, qa, save_image)
            emit_table_block(lines)
            md.append("")
            i += 1
            continue

        if tag != "div":
            i += 1
            continue

        text = inline_mn(el).strip()
        has_anchor = el.find("a[@id]") is not None

        # пустые/спейсеры
        if not text:
            i += 1
            continue

        # img-only div → буфер рисунка/формулы-картинки
        img_m = re.fullmatch(r"⟦IMG:([^⟧]*)⟧", text)
        if img_m and cls.startswith("s0"):
            spec = img_m.group(1)
            src_attr, dims = spec.split("|", 1)
            w, h = (int(x) for x in dims.split("x")) if "x" in dims else (0, 0)
            pending_image = (src_attr, w, h)
            i += 1
            continue

        # ---------- приложения: «Приложение X» (s0 aR bG, <b>, якорь) → H2 ----------
        mapp = re.fullmatch(r"Приложение\s+([А-Я])\s*", text)
        if mapp and has_anchor:
            letter = mapp.group(1)
            title = ""
            consumed = 0
            nxt_el = children[i + 1] if i + 1 < len(children) else None
            if nxt_el is not None and isinstance(nxt_el.tag, str) \
                    and (nxt_el.get("class") or "").startswith("s2B"):
                t2 = inline_mn(nxt_el).strip()
                if re.fullmatch(r"[А-ЯЁA-Z\s\-—]+", t2) and len(t2) < 100:
                    title = t2.title().replace("Ii", "II")
                    consumed = 1
            emit_heading(2, f"Приложение {letter}" + (f" — {title}" if title else ""))
            current_scope = letter
            i += 1 + consumed
            continue

        # ---------- заголовки (класс s2B* + якорь; уровень из оглавления) ----------
        if cls.startswith("s2B") and has_anchor:
            anchor_id = el.find("a[@id]").get("id", "")
            lvl = toc_levels.get(anchor_id, 3)
            # библиография (капсом) — точное совпадение; прочие капс-заголовки
            # остаются на уровне оглавления
            if text.strip() == "БИБЛИОГРАФИЯ":
                emit_heading(2, "Библиография")
                current_scope = "bib"
                i += 1
                continue
            first_word = re.split(r"\s+", text, maxsplit=1)[0]
            _ = first_word  # scope обновляется в emit_heading по цифре
            emit_heading(lvl if lvl in (2, 3, 4) else 3, text)
            i += 1
            continue

        # ---------- подпись таблицы «Таблица N» ----------
        mtab = re.match(r"^Таблица\s+([0-9А-Яа-я][\d\.]*[а-я]?)(?:\s*[-—–]\s*(.*))?$", text)
        if mtab:
            num_c = mtab.group(1)
            title_c = (mtab.group(2) or "").strip()
            pending_caption = f"Таблица {num_c}" + (f" — {title_c}" if title_c else "")
            i += 1
            continue

        # ---------- подпись рисунка «Рисунок N - …» ----------
        mfig = re.match(r"^Рисунок\s+([\wА-Яа-я.]+)(?:\s*-\s*(.*))?$", text)
        if mfig and has_anchor:
            emit_figure(text)
            i += 1
            continue

        # ---------- tag-div формулы «(N)» после картинки ----------
        mtag = re.fullmatch(r"\((\d+)\)", text.strip())
        if mtag and pending_image is not None:
            emit_formula_image(mtag.group(1))
            i += 1
            continue

        # ---------- пометы изменений (отдельный абзац) ----------
        if is_amend_note(text):
            md.append(norm_amend_line(text))
            md.append("")
            i += 1
            continue

        # ---------- примечания / примеры ----------
        nm = NOTE_RE.match(text)
        if nm and len(nm.group(1)) <= 12:
            label, rest = nm.group(1), (nm.group(2) or "").strip().lstrip(". ")
            emit_note(label, rest)
            i += 1
            continue

        # ---------- пункты N.N / N.N.N[а-я] ----------
        cm = CLAUSE_RE.match(text)
        if cm and re.fullmatch(r"\d+\.\d+(?:\.\d+)*[а-яё]?", cm.group(1)) \
                and not (cm.group(2) or "").lstrip().startswith(("-", "—", "–")):
            num, rest = cm.group(1), (cm.group(2) or "").strip()
            last_clause_no = re.sub(r"[а-яё]$", "", num)
            bid = clause_block_id(num)
            stats["clauses"] += 1
            line = f"{num} {rest}".rstrip() + f" ^{bid}" if rest else f"{num} ^{bid}"
            md.append(line.rstrip())
            md.append("")
            i += 1
            continue

        # ---------- формула: центрированный div с хвостом «(N)» ----------
        if cls.startswith("s0") and "aC" in cls.split():
            mtag2 = re.search(r",?\s*\((\d+)\)\s*$", text)
            if mtag2:
                body = text[:mtag2.start()].strip().rstrip(",").strip()
                img_m2 = re.search(r"\u27e6IMG:([^|\u27e7]*)\|", body)
                if img_m2:
                    # картинка-формула с тегом (N) в том же div
                    emit_formula_image(mtag2.group(1), src_attr=img_m2.group(1))
                else:
                    # формульный div → LaTeX напрямую из DOM (операторы из tail)
                    if el.find(".//i") is not None or el.find(".//sub") is not None \
                            or el.find(".//sup") is not None:
                        full = display_latex(el)
                        m3 = re.search(r",?\s*\((\d+)\)\s*$", full)
                        body_latex = full[:m3.start()].strip() if m3 else full.strip()
                    else:
                        body_latex = re.sub(r"\$([^\$]+)\$", r"\1", body)
                    emit_formula(body_latex, mtag2.group(1))
                i += 1
                continue

        # ---------- обычный абзац ----------
        text = re.sub(r"\s+", " ", text).strip()
        md.append(text)
        md.append("")
        stats["paragraphs"] += 1
        i += 1
    stats["images"] = len(assets)

    # финальная нормализация: инлайн-пометы изменений внутри абзацев
    ABZ_RE = re.compile(r"^\(абзац\s+введ[ен]+о?\s+Изменени\w*\s+N\s+(\d+)[^)]*\)$")

    def _abz(m):
        return f"*(абзац введён Изменением \u2116 {m.group(1)})*"

    norm_lines = []
    for ln in md:
        mabz = ABZ_RE.match(ln.strip())
        if mabz:
            norm_lines.append(_abz(mabz))
            continue
        if AMEND_INLINE_RE.search(ln) and not ln.startswith(("**(", "*(в ред.")):
            def _repl(m):
                return f"*(в ред. Изменения № {m.group(1)})*"
            ln2 = AMEND_INLINE_RE.sub(_repl, ln)
            norm_lines.append(ln2)
        else:
            norm_lines.append(ln)

    body = "\n".join(norm_lines).rstrip() + "\n"

    # ---------- Содержание (Obsidian-слой) ----------
    headings_collected: list[tuple[int, str]] = []
    for ln in body.splitlines():
        mh = re.match(r"^(#{2,4})\s+(.+)$", ln)
        if mh:
            headings_collected.append((len(mh.group(1)), mh.group(2).strip()))
    toc_lines = ["## Содержание", ""]
    for lvl, htext in headings_collected:
        if htext == "Содержание":
            continue
        indent = "  " * (lvl - 2)
        toc_lines.append(f"{indent}- [[#{htext}]]")
    # вставляем перед «## Введение» (как в издании — после Предисловия)
    body_lines = body.splitlines()
    insert_at = None
    for idx, ln in enumerate(body_lines):
        if ln.strip() == "## Введение":
            insert_at = idx
            break
    if insert_at is None:
        # нет Введения — после первого H2
        for idx, ln in enumerate(body_lines):
            if re.match(r"^## ", ln):
                insert_at = len(body_lines)
                break
    else:
        pass
    if insert_at is not None and "## Содержание" not in body:
        # ищем конец Предисловия: вставляем перед Введением, отступив пустую строку
        block = toc_lines + [""]
        body_lines[insert_at:insert_at] = block
        body = "\n".join(body_lines).rstrip() + "\n"

    # ---------- H1 и фронтмэттер ----------
    words = len(body.split())
    headings: dict[str, int] = {}
    for ln in body.splitlines():
        mh = re.match(r"^(#{1,6})\s", ln)
        if mh:
            lv = str(len(mh.group(1)))
            headings[lv] = headings.get(lv, 0) + 1
    doc_meta["stats_words"] = words
    doc_meta["stats_headings"] = headings

    h1 = f"# {doc_meta['designation']} — {doc_meta['title']}"
    fm = build_frontmatter_mn(doc_meta, stats, src)
    out_md = out_dir / f"{doc_meta['note_name']}.md"
    out_md.write_text(fm + "\n" + h1 + "\n\n" + body, encoding="utf-8")

    report = {
        "source": str(src),
        "output": str(out_md),
        "stats": stats,
        "qa_items": qa,
    }
    (out_dir / ".qa.meganorm2md.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # канонический сайдкар <note>.qa.json — единая схема для catalog/dashboard/chunker
    canonical = {
        "doc_id": doc_meta["doc_id"],
        "words": words,
        "headings": headings,
        "clauses": stats.get("clauses", 0),
        "tables": stats.get("tables", 0),
        "formulas": stats.get("formulas", 0),
        "figures": stats.get("figures", 0),
        "images": stats.get("images", 0),
        "review": qa,
        "meta": {
            "doc_id": doc_meta["doc_id"],
            "designation": doc_meta["designation"],
            "kind": "sp",
            "number": doc_meta.get("number", ""),
            "title": doc_meta["title"],
            "year": str(doc_meta.get("year") or ""),
            "status": doc_meta.get("status", "unknown"),
            "amendments": doc_meta.get("amendments", []),
            "language": "ru",
            "license": "официальный документ (ГК РФ ст.1259 п.6)",
        },
        "confidence": doc_meta.get("confidence", 0.85),
        "counts": {
            "tables": stats.get("tables", 0),
            "tables_merged": 0,
            "formulas": stats.get("formulas", 0),
            "formulas_low_confidence": sum(1 for q in qa if q["kind"] == "formula_image"),
            "images": stats.get("images", 0),
        },
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "tool_version": "0.1.0",
    }
    (out_dir / f"{doc_meta['note_name']}.qa.json").write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_frontmatter_mn(meta: dict, stats: dict, src: Path) -> str:
    sha = sha256_bytes(src.read_bytes())
    fm = f"""---
doc_id: {meta['doc_id']}
designation: {meta['designation']}
kind: sp
number: {meta['number']}
title: {meta['title']}
aliases:
  - {meta['designation']}
  - {meta['alias_versionless']}
year: {meta['year']}
language: ru
status: {meta.get('status', 'unknown')}
amendments: [{', '.join(str(x) for x in meta.get('amendments', []))}]

source:
  kind: html
  file: {src.name}
  sha256: {sha}
  uri: "{meta['uri']}"
vendor: Меганорм (meganorm.ru)

conversion:
  tool: normconv-meganorm2md
  version: 0.1.0
  converted_at: "{_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}"
  confidence: {meta.get('confidence', 0.85)}
  needs_review: true
  review_items: {stats['formulas'] + stats['figures']}
  verified: false

blocks: {{clauses: {stats['clauses']}, tables: {stats['tables']}, formulas: {stats['formulas']}, figures: {stats['figures']}}}
stats: {{words: {meta.get('stats_words', 0)}, headings: {str(meta.get('stats_headings', {})).replace("'", '"')}, tables: {stats['tables']}, formulas: {stats['formulas']}}}
license: официальный документ (ГК РФ ст.1259 п.6)
---
"""
    return fm


# ---------------------------------------------------------------- CLI

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Меганорм HTML → canonical Markdown")
    ap.add_argument("source", help=".html файл (страница meganorm.ru)")
    ap.add_argument("--out", required=True, help="каталог выхода (<data>/<cat>)")
    ap.add_argument("--doc-id", required=True)
    ap.add_argument("--designation", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--number", default="")
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--alias-versionless", default="")
    ap.add_argument("--uri", default="")
    ap.add_argument("--amendments", default="", help="1-4")
    ap.add_argument("--base-url", default="",
                    help="базовый URL для относительных src картинок (каталог страницы)")
    ap.add_argument("--status", default="unknown")
    ap.add_argument("--confidence", type=float, default=0.85)
    ap.add_argument("--no-imgs", action="store_true")
    args = ap.parse_args(argv)

    amendments = []
    if re.fullmatch(r"\d+-\d+", args.amendments):
        a, b = map(int, args.amendments.split("-"))
        amendments = list(range(a, b + 1))
    meta = {
        "doc_id": args.doc_id,
        "note_name": args.designation,
        "designation": args.designation,
        "title": args.title,
        "number": args.number or re.sub(r"^\w+ ", "", args.designation),
        "year": args.year,
        "alias_versionless": args.alias_versionless or re.sub(r"\.\d{4}$", "", args.designation),
        "uri": args.uri,
        "amendments": amendments,
        "status": args.status,
        "confidence": args.confidence,
    }
    out_dir = Path(args.out) / meta["designation"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = convert_mn(Path(args.source), out_dir, meta,
                     base_url=args.base_url, download_imgs=not args.no_imgs)
    print(json.dumps({"stats": rep["stats"], "qa_items": len(rep["qa_items"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
