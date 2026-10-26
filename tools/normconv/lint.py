"""Lint canonical Markdown: the CI gate that keeps the corpus trustworthy.

Checks are deliberately boring and mechanical — everything a human would have to
verify by eye across 2000 documents:
  frontmatter completeness, heading hierarchy, clause numbering continuity,
  duplicate/unknown anchors, missing assets, table span arithmetic,
  $$ pairing, unresolved cross-reference placeholders, non-printable characters.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse

REQUIRED_FM = ["doc_id", "designation", "kind", "title", "status", "language",
               "license", "source", "conversion", "stats"]
CLAUSE_MD_RE = re.compile(r"\^(c\d[A-Za-z0-9-]*)\s*$")
BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9][A-Za-z0-9-]*)\s*$")
BLOCK_REF_RE = re.compile(r"\]\(#\^([A-Za-z0-9][A-Za-z0-9-]*)\)")
BAD_BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([^\s]*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
IMG_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
IMG_HTML_RE = re.compile(r'<img[^>]+src="([^"]+)"')
# цель ссылки — один токен без пробелов (иначе это формула вида [1+k(b-b0)/b0](d+d0),
# а не markdown-ссылка); «title» допустим только в кавычках — как в настоящем markdown
LINK_RE = re.compile(r"\[[^\]]*\]\(([^()\s]+)(?:\s+[\"'][^()]*[\"'])?\)")
TABLE_OPEN_RE = re.compile(r"<table\b")
TABLE_CLOSE_RE = re.compile(r"</table>")
PUA = "\ue000\ue001"


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML-subset parser (dicts, lists, scalars, 2-space indent).

    Enough for the frontmatter normconv generates; anything exotic degrades to
    strings, which lint treats as "present".
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    root: dict = {}
    # stack of (indent, container, is_list)
    stack: list = [(-1, root, False)]
    pending_key = None          # (indent, key, parent) waiting to learn its type
    for raw in text[3:end].split("\n"):
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root, False)]
        parent, is_list = stack[-1][1], stack[-1][2]

        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if ":" in item and not item.startswith(('"', "'")):
                # list of objects: `- key: value`
                obj: dict = {}
                k, v = item.split(":", 1)
                obj[k.strip()] = _scalar(v.strip())
                if isinstance(parent, list):
                    parent.append(obj)
                stack.append((indent, obj, False))
            else:
                if isinstance(parent, list):
                    parent.append(_scalar(item))
                elif pending_key:
                    lst = []
                    pending_key[2][pending_key[1]] = lst
                    lst.append(_scalar(item))
                    pending_key = None
                    stack.append((indent - 1, lst, True))
            continue

        if ":" not in stripped:
            continue
        k, v = stripped.split(":", 1)
        k, v = k.strip(), v.strip()
        if isinstance(parent, list):
            continue
        if v == "":
            parent[k] = {}
            pending_key = (indent, k, parent)
            stack.append((indent, parent[k], False))
        else:
            parent[k] = [] if v == "[]" else ({} if v == "{}" else _scalar(v))
            pending_key = None
    return root



def _scalar(v: str):
    v = (v or "").strip()
    if v.startswith('"') and v.endswith('"') and len(v) > 1:
        return v[1:-1]
    if v.startswith("'") and v.endswith("'") and len(v) > 1:
        return v[1:-1]
    if v in ("null", "~", ""):
        return None
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def lint_text(text: str, fname: str) -> list[dict]:
    issues: list[dict] = []

    def add(severity, code, msg, line=0):
        issues.append({"severity": severity, "code": code, "msg": msg, "file": fname, "line": line})

    lines = text.split("\n")
    fm = parse_frontmatter(text)

    # ---- frontmatter ----
    for k in REQUIRED_FM:
        if k not in fm or fm[k] in ("", None, [], {}):
            add("error" if k in ("doc_id", "title", "status", "license") else "warning",
                "fm_missing", "в frontmatter нет поля `%s`" % k)
    if fm.get("status") in ("unknown", None, ""):
        add("warning", "fm_status_unknown", "статус действия документа не определён — сверить с ФГИС/Минстрой")
    conf = ((fm.get("conversion") or {}) if isinstance(fm.get("conversion"), dict) else {}).get("confidence")
    if isinstance(conf, (int, float)) and conf < 0.9:
        add("warning", "low_confidence", "confidence=%.2f — документ в очереди на ручную проверку" % conf)

    # ---- control characters / placeholders ----
    for i, ln in enumerate(lines, 1):
        if any(ch in ln for ch in PUA):
            add("error", "unresolved_xref", "неразрешённый плейсхолдер перекрёстной ссылки", i)
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", ln):
            add("warning", "control_chars", "непечатаемые символы", i)
        if "TODO" in ln and "<!--" not in ln:
            add("info", "todo", "TODO в тексте", i)

    # ---- headings ----
    prev_level = 0
    anchors: dict[str, int] = {}
    block_ids: dict[str, int] = {}
    # сначала собираем ВСЕ объявленные идентификаторы блоков (обычно в конце строк)
    for i, ln in enumerate(lines, 1):
        mb = BLOCK_ID_RE.search(ln)
        if mb:
            bid = mb.group(1)
            if bid in block_ids:
                add("error", "duplicate_block_id", "идентификатор блока ^%s встречается дважды" % bid, i)
            block_ids.setdefault(bid, i)
            anchors["^" + bid] = anchors.get("^" + bid, 0) + 1
            anchors[bid] = anchors.get(bid, 0) + 1
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*$", bid):
                add("error", "bad_block_id", "^%s: допускаются только латиница, цифры и дефис (Obsidian)" % bid, i)
    headings = []
    for i, ln in enumerate(lines, 1):
        m = HEADING_RE.match(ln)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append((i, level, title))
        if prev_level and level > prev_level + 1:
            add("warning", "heading_skip", "пропуск уровня заголовка H%d -> H%d" % (prev_level, level), i)
        prev_level = level
        a = _anchor(title)
        anchors[a] = anchors.get(a, 0) + 1
        anchors["#" + a] = anchors.get("#" + a, 0) + 1
        if len(title) > 200:
            add("warning", "heading_too_long", "заголовок длиннее 200 символов — вероятно, это пункт", i)
    for a, n in anchors.items():
        if n > 1:
            add("warning", "duplicate_anchor", "якорь `#%s` встречается %d раз" % (a, n))
    if headings and headings[0][1] != 1:
        add("info", "no_h1", "документ не начинается с H1")

    # ---- clause numbering continuity ----
    clauses = []          # (number, line, text)
    for i, ln in enumerate(lines, 1):
        m = CLAUSE_MD_RE.search(ln)
        if m:
            clauses.append((_number_from_block_id(m.group(1)), i, ln.strip()))
            continue
        m2 = HEADING_RE.match(ln)
        if m2:
            m3 = re.match(r"^#{1,6}\s+(\d+(?:\.\d+)*)\s", ln)
            if m3:
                clauses.append((m3.group(1), i, ln.strip()))
    # служебная зона (Предисловие: «1 ИСПОЛНИТЕЛЬ … 5 ЗАРЕГИСТРИРОВАН») даёт номера
    # 1..5, конфликтующие с нумерацией разделов, — отсекаем всё до «1 Область применения»
    start = 0
    for idx, (num, _line, text) in enumerate(clauses):
        if num == "1" and re.search(r"Область применения", text, re.I):
            start = idx
            break
    seq_issues = _check_numbering([c for c, _, _ in clauses[start:]])
    for msg in seq_issues:
        add("warning", "numbering", msg)

    # ---- assets ----
    base = os.path.dirname(fname)
    for i, ln in enumerate(lines, 1):
        for m in list(IMG_MD_RE.finditer(ln)) + list(IMG_HTML_RE.finditer(ln)):
            src = m.group(1)
            if src.startswith(("http://", "https://", "data:")):
                continue
            p = os.path.normpath(os.path.join(base, src))
            if not os.path.exists(p):
                add("error", "missing_asset", "нет файла `%s`" % src, i)

    # ---- tables ----
    open_t = sum(len(TABLE_OPEN_RE.findall(l)) for l in lines)
    close_t = sum(len(TABLE_CLOSE_RE.findall(l)) for l in lines)
    if open_t != close_t:
        add("error", "table_unbalanced", "<table>=%d, </table>=%d" % (open_t, close_t))
    for i, ln in enumerate(lines, 1):
        if "<tr" in ln and "</tr>" not in ln:
            add("warning", "tr_multiline", "<tr> не закрыт в той же строке (ломает парсеры)", i)
        if ln.count("<td") + ln.count("<th") > 0 and ln.count("<td") != ln.count("</td>"):
            add("warning", "td_unbalanced", "несбалансированные <td> в строке", i)

    # ---- math ----
    dd = text.count("$$")
    if dd % 2:
        add("error", "math_unbalanced", "нечётное число `$$` (%d)" % dd)
    for i, ln in enumerate(lines, 1):
        if ln.startswith("$$") and i < len(lines) and lines[i].startswith("$$"):
            add("info", "math_empty_block", "пустой блок формулы", i)

    # ---- footnotes ----
    used = set(re.findall(r"\[\^([A-Za-z0-9_]+)\](?!:)", text))
    defined = set(re.findall(r"^\[\^([A-Za-z0-9_]+)\]:", text, re.M))
    for u in sorted(used - defined):
        add("error", "footnote_undefined", "сноска [^%s] без определения" % u)
    for d in sorted(defined - used):
        add("info", "footnote_unused", "определение [^%s] не используется" % d)

    # ---- links ----
    for i, ln in enumerate(lines, 1):
        for m in LINK_RE.finditer(ln):
            tgt = m.group(1)
            if tgt.startswith("#^"):
                bid = tgt[2:]
                if bid not in block_ids:
                    add("warning", "broken_block_ref", "ссылка на несуществующий блок ^%s" % bid, i)
                else:
                    add("info", "obsidian_block_ref",
                        "ссылка на блок работает в Obsidian/веб-зеркале, но не на GitHub", i)
            elif tgt.startswith("#"):
                if tgt[1:] not in anchors:
                    add("warning", "broken_anchor", "ссылка на несуществующий якорь %s" % tgt, i)
            elif tgt.startswith(("http://", "https://")):
                continue
            elif not tgt.startswith("mailto:"):
                path = urllib.parse.unquote(tgt.split("#")[0])
                p = os.path.normpath(os.path.join(base, path))
                if path and not os.path.exists(p):
                    add("warning", "broken_file_link", "ссылка на отсутствующий файл %s" % tgt, i)
    return issues


def _number_from_block_id(block_id: str) -> str:
    """c8-1-6 -> 8.1.6 (для проверки непрерывности нумерации)."""
    body = block_id[1:] if block_id.startswith("c") else block_id
    return body.replace("-", ".") if re.match(r"^[\d-]+$", body) else block_id


def _anchor(text: str) -> str:
    """Якорь заголовка по правилам GitHub: пунктуация -> дефис, пробелы -> дефис,
    схлопывание дефисов, кириллица сохраняется («Таблица Б.1» -> «таблица-б-1»)."""
    a = text.strip().lower().replace("ё", "е")
    a = re.sub(r"[^\w\- ]", "-", a, flags=re.UNICODE)
    a = re.sub(r"[\s]+", "-", a)
    return re.sub(r"-+", "-", a).strip("-")


def _check_numbering(clauses: list[str]) -> list[str]:
    """Detect gaps/duplicates in clause numbering (per parent prefix)."""
    out = []
    seen = set()
    dupes = []
    for c in clauses:
        if c in seen:
            dupes.append(c)
        seen.add(c)
    if dupes:
        out.append("повторяющиеся номера пунктов: %s" % ", ".join(sorted(set(dupes))[:10]))
    by_parent: dict[str, list[int]] = {}
    for c in clauses:
        if not re.match(r"^\d+(\.\d+)*$", c):
            continue
        parts = c.split(".")
        parent = ".".join(parts[:-1]) or "0"
        try:
            by_parent.setdefault(parent, []).append(int(parts[-1]))
        except ValueError:
            continue
    gaps = []
    for parent, nums in by_parent.items():
        uniq = sorted(set(nums))
        expected = list(range(uniq[0], uniq[0] + len(uniq)))
        if uniq != expected:
            missing = sorted(set(range(uniq[0], uniq[-1] + 1)) - set(uniq))
            if missing:
                gaps.append("%s.* пропущены %s" % (parent, ",".join(map(str, missing[:8]))))
    out.extend(gaps[:10])
    return out


def lint_path(target: str) -> list[dict]:
    if os.path.isfile(target):
        files = [target]
    else:
        files = []
        for dirpath, dirnames, fnames in os.walk(target):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith((".", "_")) and not d.endswith(".assets")]
            for f in sorted(fnames):
                if f.endswith(".md") and not f.endswith((".qa.md", ".COMPARISON.md")) \
                        and f not in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md",
                                      "NOTICE.md", "SECURITY.md", "AGENTS.md", "INDEX.md",
                                      "CLAUSES.md", "LICENSE-data"):
                    files.append(os.path.join(dirpath, f))
    issues = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                issues.extend(lint_text(fh.read(), f))
        except (OSError, UnicodeDecodeError) as exc:
            issues.append({"severity": "error", "code": "unreadable", "msg": str(exc),
                           "file": f, "line": 0})
    return issues


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    issues = []
    for t in args:
        issues.extend(lint_path(t))
    by = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        by[i["severity"]] = by.get(i["severity"], 0) + 1
        if i["severity"] != "info":
            print("%-7s %-20s %s:%s %s" % (i["severity"].upper(), i["code"], i["file"],
                                            i.get("line") or "-", i["msg"][:120]))
    print(json.dumps(by, ensure_ascii=False))
    return 1 if by["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
