# -*- coding: utf-8 -*-
"""html2md — vendor HTML (docs.cntd.ru, блоки API) → канонический Markdown.

Концепция источников с 2026-11-04: основной текст действующей редакции —
консолидированный vendor HTML спецсистем (изменения включены в текст).
Контракт вывода — docs/format.md (§3 скелет, §4 пункты, §5 формулы, §6 таблицы,
§7 рисунки, §8 примечания/пометы, §9 ID, Приложение Б фронтмэттер).

Вход:  .sources/cntd-<doc>.html — конкатенация блоков API
       api.docs.cntd.ru/document/<id>/content/text/block/{1..N}
Выход: <out>/<ОБЗ>/<ОБЗ>.md + assets/ (PNG, дедупликация по sha256)

Правила консервативности (format.md §2, §8.2): не угадывать — неразрешённое
идёт в QA-очередь (отчёт .qa.html2md.json рядом с выходом), а не в текст.
"""
from __future__ import annotations

import base64
import os
import hashlib
import json
import re
import urllib.request
from pathlib import Path

from lxml import html as lhtml

# ---------------------------------------------------------------- символы

GREEK = {
    "α": "\\alpha", "β": "\\beta", "γ": "\\gamma", "δ": "\\delta", "ε": "\\varepsilon",
    "ζ": "\\zeta", "η": "\\eta", "θ": "\\theta", "ι": "\\iota", "κ": "\\kappa",
    "λ": "\\lambda", "μ": "\\mu", "ν": "\\nu", "ξ": "\\xi", "ο": "\\omicron",
    "π": "\\pi", "ρ": "\\rho", "σ": "\\sigma", "τ": "\\tau", "υ": "\\upsilon",
    "φ": "\\varphi", "χ": "\\chi", "ψ": "\\psi", "ω": "\\omega",
    "Γ": "\\Gamma", "Δ": "\\Delta", "Θ": "\\Theta", "Λ": "\\Lambda",
    "Ξ": "\\Xi", "Π": "\\Pi", "Σ": "\\Sigma", "Φ": "\\Phi", "Ψ": "\\Psi", "Ω": "\\Omega",
}

# латинские переменные, которые в тексте идут курсивом как величины
LATIN_VARS = set("abcdefghijklmnopqrstuvwxyz")


def _latex_char(ch: str) -> str | None:
    if ch in GREEK:
        return GREEK[ch]
    return None


# ---------------------------------------------------------------- утилиты

_QA_TRUNCATED: list[str] = []
_FAILED_URLS: set[str] = set()


def _qa_truncated(msg: str):
    if len(_QA_TRUNCATED) < 50:
        _QA_TRUNCATED.append(msg)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+){1,3})[\. ]+\s*(.*)$", re.S)
ENUM_RE = re.compile(r"^\(([а-яё]|[0-9]+)\)\s*(.*)$", re.S)
NOTE_RE = re.compile(
    r"^(Примечание|Пример|Примечания|Примеры)\b\s*[:\-—]?\s*(.*)$", re.S | re.I)
AMEND_RE = re.compile(r"^\(?((?:Введ|Исключ|Изменен|Утвержден)[^)]*)\)?\.?$", re.S)


def clause_block_id(number: str) -> str:
    return "c" + number.replace(".", "-")


def enum_block_id(parent_clause: str, marker: str) -> str:
    m = marker.lower().strip(").")
    if m.isdigit():
        m = "p" + m
    return f"i{clause_block_id(parent_clause)[1:]}-{m}"


def _norm_amend_note(note: str) -> str:
    # вендорный артефакт cntd: «Изм. N 5» → каноническое «Изм. № 5» (format.md §8.1)
    note = re.sub(r"(Изм\.\s*)N\b", r"\g<1>№", note)
    return re.sub(r"\s+", " ", note).strip()


# ---------------------------------------------------------------- инлайн

def _inline_math_run(nodes) -> tuple[str, bool]:
    """Собрать непрерывный «математический» прогон из i/sub/sup/текста.

    Возвращает (latex, is_math). Консервативно: math только если в прогоне
    есть греческая буква ИЛИ латинская переменная с sub/sup; иначе — обычный текст.
    """
    has_greek = False
    has_subsup_var = False

    def emit(base: str, sub: list[str], sup: list[str]):
        nonlocal has_subsup_var
        core = base.strip()
        if not core and not sub and not sup:
            return ""
        for ch in core:
            if ch in GREEK:
                has_greek = True
            elif ch.isalpha() and ch.lower() in LATIN_VARS:
                pass
        if (sub or sup) and any(c.isalpha() for c in core):
            has_subsup_var = True
        # греческие → latex-команды, остальное как есть (латиница/цифры)
        lat = "".join(_latex_char(c) if _latex_char(c) else c for c in core)
        lat = re.sub(r"\s+", "", lat)
        if sub:
            idx = "_{" + " ".join(sub).strip().replace(" ", "") + "}"
            # текстовые индексы (буквенные >1 символа) — \mathrm{}
            inner = " ".join(sub).strip()
            if re.fullmatch(r"[A-Za-zА-Яа-яЁё]{2,}", inner.replace(" ", "")):
                idx = "_{\\mathrm{" + inner.replace(" ", "") + "}}"
            else:
                idx = "_{" + inner.replace(" ", "") + "}"
            lat += idx
        if sup:
            inner = " ".join(sup).strip()
            if re.fullmatch(r"[A-Za-zА-Яа-яЁё]{2,}", inner.replace(" ", "")):
                lat += "^{\\mathrm{" + inner.replace(" ", "") + "}}"
            else:
                lat += "^{" + inner.replace(" ", "") + "}"
        return "$" + lat + "$"

    buf_base, buf_sub, buf_sup = [], [], []
    for node in nodes:
        tag = node.tag if isinstance(node.tag, str) else None
        txt = (node.text or "").replace("\u00a0", " ")
        if tag is None:
            continue
        if tag == "i":
            buf_base.append(txt.strip())
        elif tag == "sub":
            buf_sub.append((node.text or "").strip())
        elif tag == "sup":
            buf_sup.append((node.text or "").strip())
    base = "".join(buf_base)
    sub = [s for s in buf_sub if s]
    sup = [s for s in buf_sup if s]
    is_math = has_greek or (has_subsup_var and bool(base.strip()))
    return emit(base, sub, sup), is_math


def inline_to_md(el) -> str:
    """Текст элемента с инлайн-разметкой → Markdown.

    <a> → текст; i/sub/sup прогон → $...$ (если math-run); b → **...** только
    для помет изменений (решает вызывающий), иначе текст.
    """
    out: list[str] = []

    def walk(node):
        pre = node.text or ""
        if pre:
            out.append(pre.replace("\u00a0", " "))
        for child in list(node):
            tag = child.tag if isinstance(child.tag, str) else None
            if tag is None:
                continue
            if tag == "br":
                out.append(" ")
                continue
            if tag == "img":
                src = child.get("src", "")
                w = int(child.get("width") or 0)
                h = int(child.get("height") or 0)
                out.append(f"⟦IMG:{src[:64]}|{w}x{h}⟧")
                continue
            if tag == "a":
                walk(child)
                continue
            if tag in ("i", "sub", "sup"):
                # собрать прогон: текущий i + следующие sub/sup/i подряд
                run = [child]
                nxt = child.getnext()
                while nxt is not None and (nxt.tag in ("i", "sub", "sup")):
                    run.append(nxt)
                    nxt = nxt.getnext()
                md, is_math = _inline_math_run(run)
                if is_math and "$" in md:
                    out.append(md)
                else:                    # fallback: просто текст прогона без разметки
                    txt = " ".join((x.text or "").strip() for x in run)
                    out.append(txt.replace("\u00a0", " "))
                continue
            if tag == "b":
                walk(child)
                continue
            walk(child)
        tail = node.tail or ""
        if tail:
            out.append(tail.replace("\u00a0", " "))

    walk(el)
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


# ---------------------------------------------------------------- таблицы

def table_to_md(tbl_el, assets: dict[str, str], qa: list[dict]) -> tuple[list[str], str | None]:
    """Таблица → (строки MD, caption|None). GFM если нет слияний, иначе HTML."""
    rows = tbl_el.findall(".//tr")
    grid: list[list] = []  # [row][col] = cell dict {text, rowspan, colspan}
    occupied: dict[tuple[int, int], bool] = {}

    for ri, tr in enumerate(rows):
        cells = tr.findall("td") + tr.findall("th")
        row_cells = []
        k = 0   # индекс следующей реальной ячейки строки
        ci = 0  # курсор колонки
        while True:
            # пропуск колонок, занятых rowspan из строк выше
            while (ri, ci) in occupied:
                ci += 1
            if k >= len(cells):  # все реальные ячейки размещены — строка готова
                break
            cell_el = cells[k]
            k += 1
            rs = int(cell_el.get("rowspan") or 1)
            cs = int(cell_el.get("colspan") or 1)
            text = inline_to_md(cell_el).strip()
            row_cells.append({"text": text, "rowspan": rs, "colspan": cs})
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(ri + dr, ci + dc)] = True
            ci += 1
        grid.append(row_cells)

    n_rows = len(grid)
    max_cols = max((sum(c["colspan"] for c in row) for row in grid), default=0)
    has_merge = any(c["rowspan"] > 1 or c["colspan"] > 1 for row in grid for c in row)

    # caption: ищем в первой строке ячейку-название (одна ячейка на всю ширину,
    # текст без цифр-табличных), либо возвращаем None (caption был отдельным p).
    lines: list[str] = []
    if not has_merge and max_cols >= 2:
        # GFM: первая строка — головка
        head = grid[0]
        def cell_gfm(c):
            t = c["text"].replace("|", "\\|").replace("\n", " ")
            return re.sub(r"\s+", " ", t).strip()
        lines.append("| " + " | ".join(cell_gfm(c) for c in head) + " |")
        lines.append("|" + "---|" * max_cols)
        for row in grid[1:]:
            cells = [cell_gfm(c) if ci < len(row) else "" for ci, c in enumerate(row)]
            # выравнивание по max_cols
            while len(cells) < max_cols:
                cells.append("")
            lines.append("| " + " | ".join(cells[:max_cols]) + " |")
    else:
        # HTML-профиль (слияния / многоуровневая головка) — format.md §6
        def esc(t):
            return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") \
                if not re.search(r"<(i|sub|sup|b)>", t) else t
        lines.append("<table>")
        for ri, row in enumerate(grid):
            row_parts: list[str] = ["<tr>"]
            ci = 0
            idx = 0
            prior: set = set()
            for r in range(ri):
                ci0 = 0
                for c2 in grid[r]:
                    while (r, ci0) in prior:
                        ci0 += 1
                    rs2, cs2 = c2["rowspan"], c2["colspan"]
                    if r + rs2 > ri:
                        for dc in range(cs2):
                            prior.add((ri, ci0 + dc))
                    ci0 += cs2
            while ci < max_cols and idx < len(row):
                if (ri, ci) in prior:
                    ci += 1
                    continue
                c = row[idx]
                rs, cs = c["rowspan"], c["colspan"]
                tag = "th" if rs > 1 or cs > 1 else ("th" if ri == 0 else "td")
                attrs = ""
                if rs > 1:
                    attrs += f' rowspan="{rs}"'
                if cs > 1:
                    attrs += f' colspan="{cs}"'
                scope = ' scope="col"' if tag == "th" and ri == 0 else (' scope="row"' if tag == "th" else "")
                text_html = c["text"]
                # инлайн-математика в HTML-ячейке → <i>/<sub> (format.md §6)
                text_html = re.sub(r"\$([A-Za-z\\{}_^,\s]+)\$", _latex_to_html, text_html)
                row_parts.append(f"<{tag}{attrs}{scope}>{text_html}</{tag}>")
                ci += cs
                idx += 1
            while ci < max_cols:
                row_parts.append("<td></td>")
                ci += 1
            row_parts.append("</tr>")
            lines.append("".join(row_parts))
        lines.append("</table>")
    return lines, None





def _latex_to_html(m) -> str:
    body = m.group(1)
    def sub_repl(mm):
        inner = mm.group(2).strip()
        if inner.startswith("\\mathrm{"):
            inner = inner[9:-1]
        else:
            inner = re.sub(r"[{}]", "", inner)
        return "<sub>" + inner + "</sub>"

    def sup_repl(mm):
        inner = mm.group(2).strip()
        if inner.startswith("\\mathrm{"):
            inner = inner[9:-1]
        else:
            inner = re.sub(r"[{}]", "", inner)
        return "<sup>" + inner + "</sup>"

    body = re.sub(r"_\{([^}]*)\}", sub_repl, body)
    body = re.sub(r"\^\{([^}]*)\}", sup_repl, body)
    greek_rev = {v[1:]: k for k, v in GREEK.items()}
    for cmd, ch in greek_rev.items():
        body = body.replace(f"\\{cmd}", ch)
    return "<i>" + body.strip() + "</i>"


# ---------------------------------------------------------------- основной проход

def convert_html_file(src: Path, out_dir: Path, doc_meta: dict, download_imgs: bool = True) -> dict:
    """Конвертация vendor HTML → канон MD. Возвращает отчёт (qa)."""
    raw = src.read_text(encoding="utf-8", errors="replace")
    root = lhtml.fromstring(f"<div id='root'>{raw}</div>")
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    qa: list[dict] = []
    assets: dict[str, str] = {}   # url/b64-key → filename
    stats = {"paragraphs": 0, "tables": 0, "images": 0, "formulas": 0,
             "figures": 0, "clauses": 0, "notes": 0}

    def save_image(src_attr: str) -> str | None:
        """base64 или URL → assets/<sha8>.png; имя файла."""
        key = src_attr[:200]
        if key in assets:
            return assets[key]
        data = b""
        m = re.match(r"data:image[^;]*;base64,(.+)$", src_attr, re.S)
        if m:
            b64 = re.sub(r"\s+", "", m.group(1))
            try:
                data = base64.b64decode(b64)
            except Exception:
                # усечённый data-uri (вендорный артефакт) — QA, не падение
                _qa_truncated(f"truncated data-uri len={len(b64)}: {src_attr[:80]!r}")
                return None
        elif src_attr.startswith("http") and download_imgs:
            if src_attr in _FAILED_URLS:
                return None
            try:
                req = urllib.request.Request(src_attr, headers={
                    "User-Agent": "normbase-bot/1.0",
                    "Referer": "https://docs.cntd.ru/",
                })
                data = urllib.request.urlopen(req, timeout=10).read()
            except Exception as e:
                _FAILED_URLS.add(src_attr)
                qa.append({"kind": "image_download_failed", "url": src_attr, "error": str(e)})
                return None
        if not data:
            return None
        h = sha256_bytes(data)
        ext = ".png" if data[:4] == b"\x89PNG" else (".gif" if data[:3] == b"GIF" else ".bin")
        name = f"{h[:12]}{ext}"
        (assets_dir / name).write_bytes(data)
        assets[key] = name
        return name

    md: list[str] = []
    note_seq: dict[str, int] = {}
    pending_caption: str | None = None
    pending_amend_after_table: str | None = None
    last_clause_no: str | None = None

    def prev_is_clause_line() -> bool:
        return len(md) >= 2 and md[-1] == "" and bool(re.match(r"^\d+(\.\d+)*\s", md[-2]))

    def next_table_within(idx: int, steps: int = 5) -> bool:
        # таблица через пустые <p>, <style> и div-обёртки (вендорная структура cntd)
        for j in range(idx, min(len(children), idx + steps)):
            el2 = children[j]
            if el2.tag == "table":
                return True
            if el2.tag == "div":  # заглядываем внутрь обёртки (1 уровень)
                if any(c.tag == "table" for c in list(el2) if isinstance(c.tag, str)):
                    return True
                continue
            if el2.tag == "style" or (el2.tag == "p" and not re.sub(r"\s+", "", el2.text_content() or "")):
                continue
            return False
        return False

    def emit_table_caption_and(table_lines, caption):
        nonlocal pending_caption
        cap = (caption if caption is not None else pending_caption)
        pending_caption = None
        if cap:
            # format.md §6: *Таблица* [номер] — название ^t…  (одна строка)
            tnum_m = re.match(r"(?:Таблица|ТАБЛИЦА)\s+([\d\.]+)", cap)
            if tnum_m:
                num = tnum_m.group(1)
                rest = cap[tnum_m.end():].strip(" —-")
                bid = "t" + num.replace(".", "-")
                line = f"*Таблица* {num}" + (f" — {rest}" if rest else "") + f" ^{bid}"
            else:
                stats["tables"] += 1
                u = f"tu{stats['tables']:03d}"
                qa.append({"kind": "table_no_number_in_source", "caption": cap[:80], "uid": u})
                line = f"*Таблица* — {cap.strip()} ^{u}"
            md.append(line)
        else:
            stats["tables"] += 1
            u = f"tu{stats['tables']:03d}"
            qa.append({"kind": "table_no_caption", "uid": u})
            md.append(f"*Таблица* ^{u}")
        md.extend(table_lines)

    children = [c for c in list(root) if isinstance(c.tag, str)]
    i = 0
    while i < len(children):
        # div-обёртки (вендорный артефакт cntd: таблицы внутри div) — расшиваем на месте
        if children[i].tag == "div":
            inner = [c for c in list(children[i]) if isinstance(c.tag, str)]
            children[i:i + 1] = inner
            if not inner:
                i += 0  # div пустой — просто удалён, индекс не сдвигается
            continue
        el = children[i]
        tag = el.tag
        if tag in ("style", "script"):
            i += 1
            continue

        # ---------- заголовки ----------
        if tag == "h2":
            text = inline_to_md(el)
            text = re.sub(r"\s+", " ", text).strip()
            md.append(f"## {text}")
            md.append("")
            i += 1
            continue
        if tag == "h3":
            text = inline_to_md(el)
            text = re.sub(r"\s+", " ", text).strip()
            md.append(f"### {text}")
            md.append("")
            i += 1
            continue

        # ---------- таблицы ----------
        if tag == "table":
            # таблица-«картинка» (вендорный артефакт cntd): формула/рисунок в одной ячейке
            tds = el.findall(".//td") + el.findall(".//th")
            imgs = el.findall(".//img")
            text_only = "".join(t.text_content() or "" for t in tds)
            if len(imgs) == 1 and not re.sub(r"\s+", "", text_only):
                img_el = imgs[0]
                src_attr = img_el.get("src", "")
                w = int(img_el.get("width") or 0)
                h = int(img_el.get("height") or 0)
                fname = save_image(src_attr) if src_attr else None
                kind = "e" if (h and w and h < 80) else "f"
                key = "formulas" if kind == "e" else "figures"
                stats[key] += 1
                bid = f"{kind}u{stats[key]:03d}"
                qa.append({"kind": "image_only_table", "uid": bid, "w": w, "h": h})
                if fname:
                    md.append(f"![{src_attr[:60]}](assets/{fname})")
                else:
                    md.append("⟦IMG-LOST⟧")
                md.append("")
                md.append(f"^{bid}")
                md.append("")
            else:
                lines, _ = table_to_md(el, assets, qa)
                emit_table_caption_and(lines, None)
                md.append("")
            if pending_amend_after_table:
                md.append(f"**({pending_amend_after_table}).**")
                md.append("")
                pending_amend_after_table = None
            i += 1
            continue

        # ---------- абзацы ----------
        text = inline_to_md(el)
        cls = el.get("class") or ""
        is_headertext = "headertext" in cls

        if not text.strip():
            i += 1
            continue

        # помета таблицы (headertext перед таблицей)
        if is_headertext:
            nxt = children[i + 1] if i + 1 < len(children) else None
            if nxt is not None and nxt.tag == "table":
                pending_caption = text.strip()
                i += 1
                continue
            # headertext не перед таблицей — проверяем, что это заголовок-подраздел?
            if re.match(r"^\d+\.\d+\s", text):
                md.append(f"### {text}")
                md.append("")
                i += 1
                continue

        # подпись таблицы (cntd): абзац «Таблица N.M …» + пустые <p>/<style> + таблица
        capm = re.match(r"^Таблица\s+([\d\.]+)\s*(.*)$", text.strip())
        if capm and next_table_within(i + 1):
            num, rest = capm.group(1), (capm.group(2) or "").strip()
            amcap = re.match(r"^(.*?)\s*\(((?:Введ|Исключ|Изменен)[^)]*)\)\.?$", rest)
            if amcap:
                rest = amcap.group(1).strip()
                pending_amend_after_table = _norm_amend_note(amcap.group(2))
            else:
                pending_amend_after_table = None
            pending_caption = f"Таблица {num}" + (f" — {rest}" if rest else "")
            i += 1
            continue

        # пометы изменений: (Введен дополнительно, Изм. N X) / (Исключен, ...)
        # и групповые: «N.N, N.N+1 (Введены дополнительно, Изм. N X)»
        tstrip = text.strip()
        am2 = re.match(
            r"^\*?\s*((?:Измененная редакция|Введен(о)? дополнительно|Исключен[ае]?)[^.]{0,60})\.?$",
            tstrip)
        if am2 and len(tstrip) < 150:
            md.append(f"**({_norm_amend_note(am2.group(1).strip())}).**")
            md.append("")
            i += 1
            continue
        am = AMEND_RE.match(tstrip)
        if am and len(tstrip) < 200:
            md.append(f"**({_norm_amend_note(am.group(1).rstrip('.'))}).**")
            md.append("")
            i += 1
            continue
        gm = re.match(r"^([\d,\. ]+)\s*\(((?:Введ|Исключ|Изменен)[^)]*)\)\.?$", tstrip)
        if gm and len(tstrip) < 200:
            md.append(f"{gm.group(1).strip()} **({_norm_amend_note(gm.group(2))}).**")
            md.append("")
            i += 1
            continue

        # примечания / примеры
        nm = NOTE_RE.match(text.strip())
        if nm and len(nm.group(1)) <= 12:
            label, rest = nm.group(1), (nm.group(2) or "").strip().lstrip(". ")
            kind = "n" if label.lower().startswith("прим") else "x"
            parent = last_clause_no.replace(".", "-") if last_clause_no else "u0"
            seq_key = f"{kind}{parent}"
            note_seq[seq_key] = note_seq.get(seq_key, 0) + 1
            bid = f"{seq_key}-{note_seq[seq_key]}"
            stats["notes"] += 1
            md.append(f"> *{label}* — {rest}" if rest else f"> *{label}*")
            md.append("")
            md.append(f"^{bid}")
            md.append("")
            i += 1
            continue

        # пункты N.N / N.N.N (списочные «N.M - текст» с дефисом пунктом не являются)
        cm = CLAUSE_RE.match(text.strip())
        tstrip0 = text.strip()
        if cm and re.fullmatch(r"\d+(\.\d+){1,3}", cm.group(1)) \
                and not (cm.group(2) or "").lstrip().startswith(("-", "—", "–")):
            # фрагмент разрыва страницы: короткий хвост «N.N …);» — сливаем в предыдущий пункт
            if len(tstrip0) < 30 and (tstrip0.endswith(")") or tstrip0.endswith(";")) \
                    and ")" in tstrip0[-4:] and prev_is_clause_line():
                md[-2] = md[-2] + " " + re.sub(r"\s+", " ", tstrip0).strip()
                i += 1
                continue
            num, rest = cm.group(1), (cm.group(2) or "").strip()
            last_clause_no = num
            bid = clause_block_id(num)
            stats["clauses"] += 1
            line = f"{num} {rest}".rstrip() + f" ^{bid}" if rest else f"{num} ^{bid}"
            md.append(line.rstrip())
            md.append("")
            i += 1
            continue

        # позиции перечислений а) / 1)
        em = ENUM_RE.match(text.strip())
        if em and last_clause_no:
            marker, rest = em.group(1), (em.group(2) or "").strip()
            bid = enum_block_id(last_clause_no, marker)
            md.append(f"({marker}) {rest} ^{bid}".rstrip())
            md.append("")
            i += 1
            continue

        # абзац только с картинкой → формула/рисунок
        img_m = re.fullmatch(r"⟦IMG:([^⟧]*)⟧", text.strip())
        if img_m:
            spec = img_m.group(1)
            src_attr, dims = spec.split("|", 1)
            w, h = (int(x) for x in dims.split("x")) if "x" in dims else (0, 0)
            fname = save_image(src_attr)
            # следующий абзац — подпись рисунка?
            nxt_text = ""
            j = i + 1
            while j < len(children):
                nt = inline_to_md(children[j])
                if nt.strip():
                    nxt_text = nt.strip()
                    break
                j += 1
            is_figure = bool(re.match(r"^(Рисунок|РИСУНОК|Схема|КАРТА)", nxt_text)) or h >= 200
            if fname:
                stats["images"] += 1
                if is_figure:
                    stats["figures"] += 1
                    u = f"f{stats['figures']:03d}"
                    md.append(f"![Рисунок (подписи требуют проверки)](assets/{fname})")
                    md.append("")
                    md.append(f"^{u}")
                else:
                    stats["formulas"] += 1
                    u = f"eu{stats['formulas']:03d}"
                    qa.append({"kind": "formula_image", "uid": u, "dims": dims})
                    md.append(f"![Формула: запись не подтверждена, см. изображение](assets/{fname})")
                    md.append("")
                    md.append(f"^{u}")
            md.append("")
            i += 1
            continue

        # обычный абзац (продолжение пункта / предисловие и т.п.)
        text = re.sub(r"\s+", " ", text).strip()
        # хвост разрыва страницы: короткий фрагмент, заканчивающийся «)»/«);» → в предыдущий пункт
        if len(text) < 25 and text.endswith((")", ";", ".")) \
                and ")" in text[-4:] and prev_is_clause_line():
            md[-2] = md[-2] + " " + text
            i += 1
            continue
        md.append(text)
        md.append("")
        i += 1

    body = "\n".join(md).rstrip() + "\n"

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
    # ручные override (tools/config/<doc_id>.yaml) — единственное место ручных правок
    cfg_path = Path(__file__).resolve().parents[1] / "config" / f"{doc_meta['doc_id']}.yaml"
    if cfg_path.exists():
        import yaml as _yaml_cfg
        ov = _yaml_cfg.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for k in ("status", "confidence"):
            if ov.get(k) is not None:
                doc_meta[k] = ov[k]
        if isinstance(ov.get("excluded_clauses"), list):
            doc_meta["excluded_clauses"] = ov["excluded_clauses"]
    h1 = f"# {doc_meta['designation']} — {doc_meta['title']}"
    fm = build_frontmatter(doc_meta, stats, src)
    out_md = out_dir / f"{doc_meta['note_name']}.md"
    out_md.write_text(fm + "\n" + h1 + "\n\n" + body, encoding="utf-8")

    if _QA_TRUNCATED:
        qa.append({"kind": "truncated_data_uri", "count": len(_QA_TRUNCATED),
                   "samples": _QA_TRUNCATED[:10]})
    report = {
        "source": str(src),
        "output": str(out_md),
        "stats": stats,
        "qa_items": qa,
    }
    (out_dir / ".qa.html2md.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def build_frontmatter(meta: dict, stats: dict, src: Path) -> str:
    import datetime
    sha = sha256_bytes(src.read_bytes())
    excl_lines = []
    for ec in meta.get("excluded_clauses", []) or []:
        line = "  - clause: \"%s\"" % ec["clause"]
        if ec.get("since"):
            line += "\n    since: %s" % ec["since"]
        if ec.get("amendment") is not None:
            line += "\n    amendment: %s" % ec["amendment"]
        excl_lines.append(line)
    excl_block = ("excluded_clauses:\n" + "\n".join(excl_lines)) if excl_lines else ""
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
{excl_block}

source:
  kind: html
  file: {src.name}
  sha256: {sha}
  uri: "{meta['uri']}"
vendor: Кодекс (docs.cntd.ru)

conversion:
  tool: normconv-html2md
  version: 0.1.0
  converted_at: "{datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}"
  confidence: {meta.get('confidence', 0.5)}
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
    ap = argparse.ArgumentParser(description="vendor HTML → canonical Markdown")
    ap.add_argument("source", help=".html файл (блоки API docs.cntd.ru)")
    ap.add_argument("--out", required=True, help="каталог выхода (<data>/<cat>/...)")
    ap.add_argument("--doc-id", required=True)
    ap.add_argument("--designation", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--number", default="")
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--alias-versionless", default="")
    ap.add_argument("--uri", default="")
    ap.add_argument("--amendments", default="", help="1-6")
    ap.add_argument("--no-imgs", action="store_true", help="не скачивать внешние картинки (QA)")
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
    }
    out_dir = Path(args.out) / meta["designation"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = convert_html_file(Path(args.source), out_dir, meta,
                            download_imgs=not args.no_imgs)
    print(json.dumps({"stats": rep["stats"], "qa_items": len(rep["qa_items"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
