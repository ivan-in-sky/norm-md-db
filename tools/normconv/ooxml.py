"""Low-level OOXML helpers: namespaces, table grid with merged cells, text utils."""
from __future__ import annotations

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def w(tag: str) -> str:
    return "{%s}%s" % (NS["w"], tag)


def m_(tag: str) -> str:
    return "{%s}%s" % (NS["m"], tag)


def ln(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def child(el, name: str):
    for c in el:
        if isinstance(c.tag, str) and ln(c) == name:
            return c
    return None


def val(el, prop: str, attr: str = "val"):
    if el is None:
        return None
    c = child(el, prop)
    return c.get(w(attr)) if c is not None else None


def collapse_ws(s: str) -> str:
    return " ".join(s.split())


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #


def parse_table(tbl) -> dict:
    """Build a rectangular grid model of a w:tbl, expanding gridSpan/vMerge.

    Returns dict with:
      n_rows, n_cols, cells (list of Cell), has_merged, is_simple, rows(list[list[str]])
    Cell = {r, c, rowspan, colspan, text, html, is_repeat_header, style_id, num_id, ilvl}
    """
    grid_cols = child(tbl, "tblGrid")
    n_cols_grid = len(list(grid_cols)) if grid_cols is not None else 0

    raw_rows = [r for r in tbl if ln(r) == "tr"]
    cells = []
    owner: dict[tuple[int, int], int] = {}   # (r,c) -> index of the cell that starts there
    covered: set[tuple[int, int]] = set()    # (r,c) -> spanned by somebody else

    def row_cells(tr):
        out = []
        for tc in [c for c in tr if ln(c) == "tc"]:
            tcpr = child(tc, "tcPr")
            colspan = 1
            if tcpr is not None:
                gs = child(tcpr, "gridSpan")
                if gs is not None:
                    try:
                        colspan = max(1, int(gs.get(w("val"))))
                    except (TypeError, ValueError):
                        colspan = 1
            vm = child(tcpr, "vMerge") if tcpr is not None else None
            vm_val = None
            if vm is not None:
                vm_val = vm.get(w("val")) or "continue"
            out.append((tc, colspan, vm_val))
        return out

    rows_cells = [row_cells(tr) for tr in raw_rows]

    def cell_at_grid_col(ri: int, col: int):
        """Return the (tc, colspan, vmerge) whose start column == col in row ri."""
        c = 0
        for item in rows_cells[ri]:
            if c == col:
                return item
            c += item[1]
        return None

    for ri, tr in enumerate(raw_rows):
        ci = 0
        for tc, colspan, vm_val in rows_cells[ri]:
            if vm_val == "continue":
                # continuation of a vMerge started in a row above: occupies grid
                # columns but has no content and no cell of its own
                for k in range(colspan):
                    covered.add((ri, ci + k))
                ci += colspan
                continue
            while (ri, ci) in owner or (ri, ci) in covered:
                ci += 1
            rowspan = 1
            if vm_val == "restart":
                rj = ri + 1
                while rj < len(raw_rows):
                    nxt = cell_at_grid_col(rj, ci)
                    if nxt is None or nxt[2] != "continue":
                        break
                    rowspan += 1
                    rj += 1

            cell = {
                "r": ri,
                "c": ci,
                "rowspan": rowspan,
                "colspan": colspan,
                "repeat_header": _is_repeat_header(tr),
                "tc": tc,
            }
            idx = len(cells)
            cells.append(cell)
            owner[(ri, ci)] = idx
            for dr in range(rowspan):
                for dc in range(colspan):
                    if dr == 0 and dc == 0:
                        continue
                    covered.add((ri + dr, ci + dc))
            ci += colspan

    n_rows = len(raw_rows)
    n_cols = max([c["c"] + c["colspan"] for c in cells], default=n_cols_grid)
    if n_cols_grid:
        n_cols = max(n_cols, n_cols_grid)
    has_merged = any(c["rowspan"] > 1 or c["colspan"] > 1 for c in cells)
    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "cells": cells,
        "has_merged": has_merged,
        "is_simple": not has_merged,
        "owner": owner,
        "covered": covered,
    }


def _is_repeat_header(tr) -> bool:
    trpr = child(tr, "trPr")
    if trpr is None:
        return False
    return child(trpr, "tblHeader") is not None


def _cell_at(tr, col_index: int):
    ci = 0
    for tc in [c for c in tr if ln(c) == "tc"]:
        tcpr = child(tc, "tcPr")
        colspan = 1
        if tcpr is not None:
            gs = child(tcpr, "gridSpan")
            if gs is not None:
                try:
                    colspan = int(gs.get(w("val")))
                except (TypeError, ValueError):
                    colspan = 1
        if ci == col_index:
            return tc
        ci += colspan
    return None


def cell_blocks(tc):
    """Yield the block-level children of a table cell (paragraphs / nested tables)."""
    return [c for c in tc if ln(c) in ("p", "tbl")]


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

RU_DASHES = "\u2013\u2014\u2012"


def is_note_label(s: str) -> bool:
    t = collapse_ws(s).replace("\u00a0", " ")
    tt = t.replace(" ", "")
    return tt.lower().rstrip(":—-") in {
        "примечание",
        "примечания",
        "note",
        "notes",
    } or (tt.lower().startswith("примечан") and len(tt) < 16)
