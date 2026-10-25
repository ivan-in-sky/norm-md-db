"""Style map: Word style id -> semantic kind (heading level, caption, footnote...).

Normative documents in Russian use a mix of styles:
  * built-in "Heading 1..9" / "Заголовок 1..9"
  * ГОСТ template styles: "Заголовок 1 (ГОСТ)", "1 (СП)", custom names
  * numbering-driven pseudo-headings (a paragraph numbered 5.2.3 with style Normal)
  * caption styles "Название", "Рисунок", "Таблица", "Подпись"
So we resolve kind by (a) style name/id regex, (b) w:outlineLvl, (c) content regex.
"""
from __future__ import annotations

import re

from .ooxml import NS, child, ln, val, w

HEADING_NAME_RE = re.compile(
    r"^(?:heading|заголовок|zagolovok|title\s*\d|раздел|подраздел)\s*(\d)?", re.I
)
CAPTION_RE = re.compile(r"^(caption|название|подпись|надрисунок|надтаблицей|подрисунок|подрисуночная)", re.I)
FOOTNOTE_TEXT_RE = re.compile(r"^(footnote\s*text|сноска|текст\s*сноски)", re.I)
QUOTE_RE = re.compile(r"^(quote|цитата|выделенный)", re.I)


class StyleMap:
    def __init__(self, styles_xml=None, numbering_xml=None):
        self.by_id: dict[str, dict] = {}
        self.num_to_abs: dict[str, str] = {}
        self.abs_def: dict[str, dict] = {}
        if styles_xml is not None:
            self._load_styles(styles_xml)
        if numbering_xml is not None:
            self._load_numbering(numbering_xml)

    # ------------------------------------------------------------------ #
    def _load_styles(self, root):
        for st in root:
            if ln(st) != "style":
                continue
            sid = st.get(w("styleId"))
            if not sid:
                continue
            name_el = child(st, "name")
            name = name_el.get(w("val")) if name_el is not None else sid
            kind = None
            level = None
            mm = HEADING_NAME_RE.match(name) or HEADING_NAME_RE.match(sid)
            if mm:
                kind = "heading"
                level = int(mm.group(1)) if mm.group(1) else 1
            elif CAPTION_RE.match(name) or CAPTION_RE.match(sid):
                kind = "caption"
            elif FOOTNOTE_TEXT_RE.match(name) or FOOTNOTE_TEXT_RE.match(sid):
                kind = "footnote_text"
            elif QUOTE_RE.match(name):
                kind = "quote"
            outline = val(child(st, "pPr"), "outlineLvl")
            if kind is None and outline is not None:
                try:
                    lvl = int(outline) + 1
                    if 1 <= lvl <= 9:
                        kind, level = "heading", lvl
                except ValueError:
                    pass
            based_on = val(st, "basedOn")
            self.by_id[sid] = {
                "id": sid,
                "name": name,
                "kind": kind,
                "level": level,
                "outline": outline,
                "basedOn": based_on,
                "type": st.get(w("type")),
                "numId": val(child(st, "pPr"), "numPr") is not None,
            }
        # inherit kind from basedOn chain (max depth 8)
        for sid in list(self.by_id):
            cur = self.by_id[sid]
            depth = 0
            while cur.get("kind") is None and cur.get("basedOn") and depth < 8:
                parent = self.by_id.get(cur["basedOn"])
                if parent is None:
                    break
                if parent.get("kind"):
                    cur["kind"] = parent["kind"]
                    cur["level"] = parent.get("level") or cur.get("level")
                    break
                cur = parent
                depth += 1

    def _load_numbering(self, root):
        for el in root:
            if ln(el) == "num":
                nid = el.get(w("numId"))
                ab = child(el, "abstractNumId")
                if nid and ab is not None:
                    self.num_to_abs[nid] = ab.get(w("val"))
            elif ln(el) == "abstractNum":
                aid = el.get(w("abstractNumId"))
                levels = {}
                for lvl in [c for c in el if ln(c) == "lvl"]:
                    ilvl = lvl.get(w("ilvl"))
                    fmt = val(lvl, "numFmt") or "decimal"
                    txt = val(lvl, "lvlText") or ""
                    start = val(lvl, "start") or "1"
                    levels[ilvl] = {"fmt": fmt, "text": txt, "start": start}
                self.abs_def[aid] = levels

    # ------------------------------------------------------------------ #
    def kind_of(self, style_id) -> tuple[str | None, int | None]:
        info = self.by_id.get(style_id or "")
        if not info:
            return None, None
        return info.get("kind"), info.get("level")

    def fmt_of(self, num_id, ilvl):
        if num_id is None:
            return None
        aid = self.num_to_abs.get(str(num_id))
        defs = self.abs_def.get(aid)
        if not defs:
            return None
        return defs.get(str(ilvl))


class NumberingResolver:
    """Recompute auto-numbers for numbered paragraphs (Word stores no literal number)."""

    def __init__(self, stylemap: StyleMap):
        self.stylemap = stylemap
        self.counters: dict[str, dict[int, int]] = {}

    def number_for(self, num_id, ilvl) -> str | None:
        if num_id is None:
            return None
        aid = self.stylemap.num_to_abs.get(str(num_id))
        if aid is None:
            return None
        defs = self.stylemap.abs_def.get(aid)
        if not defs:
            return None
        try:
            ilvl = int(ilvl or 0)
        except (TypeError, ValueError):
            ilvl = 0
        cnt = self.counters.setdefault(aid, {})
        for lv in list(cnt):
            if lv > ilvl:
                cnt[lv] = 0
        start = int(defs.get(str(ilvl), {}).get("start") or 1)
        cnt[ilvl] = cnt.get(ilvl, 0) + 1 if cnt.get(ilvl) else start
        fmt = defs.get(str(ilvl), {}).get("fmt") or "decimal"
        if fmt in ("none",):
            return None
        lvl_text = defs.get(str(ilvl), {}).get("text") or ""
        if "%" in lvl_text:
            out = lvl_text
            for i in range(0, 10):
                if f"%{i+1}" in out:
                    v = cnt.get(i, int(defs.get(str(i), {}).get("start") or 1))
                    out = out.replace(f"%{i+1}", _fmt_num(v, defs.get(str(i), {}).get("fmt") or "decimal"))
            return out
        parts = []
        for lv in range(0, ilvl + 1):
            v = cnt.get(lv, int(defs.get(str(lv), {}).get("start") or 1))
            parts.append(_fmt_num(v, defs.get(str(lv), {}).get("fmt") or "decimal"))
        return ".".join(parts)


def _fmt_num(n: int, fmt: str) -> str:
    if fmt == "lowerLetter" or fmt == "lowerRomanLetter":
        return chr(ord("a") + (n - 1) % 26)
    if fmt == "upperLetter":
        return chr(ord("A") + (n - 1) % 26)
    if fmt in ("lowerRoman",):
        return _roman(n).lower()
    if fmt in ("upperRoman",):
        return _roman(n)
    return str(n)


def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
            (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"),
            (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out
