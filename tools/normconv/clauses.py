"""catalog/CLAUSES.md — индекс пунктов: block id ↔ номер строки ↔ готовые ссылки.

Зачем: на GitHub нет синтаксиса для произвольных якорей в .md (якоря генерируются
только из заголовков, а `<a id="…">` вырезается санитайзером). Зато всегда работает
ссылка на строку файла — `blob/<ref>/path#L412`. Этот модуль строит таблицу
«пункт → строка → ссылка для GitHub → ссылка для Obsidian», и CI перегенерирует её
на каждый коммит, поэтому номера строк не устаревают.

Использование:
    python -m normconv.clauses md --out catalog/CLAUSES.md \
        --repo https://github.com/org/stroy-norms --ref main
"""
from __future__ import annotations

import json
import os
import re
import sys

BID_RE = re.compile(r"\^(c[A-Za-z0-9][A-Za-z0-9-]*)\s*$")
CLAUSE_RE = re.compile(r"^\*\*(\d+(?:\.\d+)*|[А-ЯA-Z][\w.-]*\d)\*\*")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_CAP_RE = re.compile(r"^(Таблица\s*[\dА-Я.]+)")
FIG_CAP_RE = re.compile(r"^(Рисунок\s*[\dА-Я.]+)")
T_BID_RE = re.compile(r"\^(t\d+|e\d+|f\d+)\s*$")


def clause_from_block_id(bid: str) -> str:
    body = bid[1:] if bid.startswith("c") else bid
    if re.match(r"^[\d-]+$", body):
        return body.replace("-", ".")
    return body.replace("-", ".").upper()


def anchor_from_heading(title: str) -> str:
    a = title.strip().lower().replace("ё", "е")
    a = re.sub(r"[^\w\- ]", "", a, flags=re.UNICODE)
    return re.sub(r"[ ]+", "-", a).strip("-")


def scan(path: str) -> list[dict]:
    rows = []
    section = ""
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh.read().split("\n"), 1):
            m = HEADING_RE.match(line)
            if m:
                section = m.group(2).strip()
                rows.append({"line": n, "kind": "heading", "target": section,
                             "block_id": "", "anchor": anchor_from_heading(section),
                             "section": section})
                continue
            m = BID_RE.search(line)
            if m and CLAUSE_RE.match(line.strip()):
                num = CLAUSE_RE.match(line.strip()).group(1)
                rows.append({"line": n, "kind": "clause", "target": num,
                             "block_id": m.group(1), "anchor": "", "section": section})
                continue
            m = T_BID_RE.search(line)
            if m:
                bid = m.group(1)
                kind = {"t": "table", "e": "formula", "f": "figure"}[bid[0]]
                rows.append({"line": n, "kind": kind, "target": bid,
                             "block_id": bid, "anchor": "", "section": section})
    return rows


def build(md_dir: str, out_path: str, repo_url: str = "", ref: str = "main") -> int:
    files = []
    for dirpath, dirnames, fnames in os.walk(md_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "_")) and not d.endswith(".assets")]
        for f in sorted(fnames):
            if f.endswith(".md") and not f.endswith(".qa.md"):
                files.append(os.path.join(dirpath, f))
    files.sort()

    base = os.path.dirname(os.path.abspath(md_dir.rstrip("/")))
    L = ["# Указатель пунктов",
         "",
         "Сгенерировано автоматически (`normconv clauses`) — не редактируйте вручную.",
         "",
         "Как пользоваться:",
         "",
         "- **Obsidian**: `[[<файл без расширения>#^<block id>]]`, например `[[sp-63-13330-2018#^c8-1-6]]`;",
         "- **GitHub**: ссылка на строку файла (`#L…`) из колонки ниже. Для внешних цитат используйте",
         "  релизный тег вместо `main`, иначе номер строки «поедет»;",
         "- **агент/скрипт**: `norms.get(doc_id, clause)` или `grep -n \"\\^c8-1-6$\" md/<doc_id>.md`.",
         ""]

    total = 0
    per_doc = []
    for p in files:
        rel = os.path.relpath(p, base).replace(os.sep, "/")
        doc_id = os.path.splitext(os.path.basename(p))[0]
        rows = scan(p)
        per_doc.append((doc_id, rel, rows))
        total += len([r for r in rows if r["kind"] == "clause"])

    L.append("Документов: **%d**, пунктов: **%d**." % (len(per_doc), total))
    L.append("")
    for doc_id, rel, rows in per_doc:
        L.append("## %s" % doc_id)
        L.append("")
        L.append("`%s`" % rel)
        L.append("")
        L.append("| объект | блок | строка | GitHub | Obsidian | раздел |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for r in rows:
            if r["kind"] == "heading":
                gh = ("[заголовок](%s/blob/%s/%s#%s)" % (repo_url.rstrip("/"), ref, rel, r["anchor"])
                      if repo_url else "#%s" % r["anchor"])
                obs = "`[[%s#%s]]`" % (doc_id, r["target"])
                L.append("| **%s** | — | %d | %s | %s | — |" % (r["target"], r["line"], gh, obs))
            else:
                gh = ("[L%d](%s/blob/%s/%s#L%d)" % (r["line"], repo_url.rstrip("/"), ref, rel, r["line"])
                      if repo_url else "L%d" % r["line"])
                obs = "`[[%s#^%s]]`" % (doc_id, r["block_id"])
                label = r["target"] if r["kind"] == "clause" else "%s (%s)" % (r["target"], r["kind"])
                L.append("| %s | `^%s` | %d | %s | %s | %s |" % (
                    label, r["block_id"], r["line"], gh, obs, r["section"][:40]))
        L.append("")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    # машинная версия — для MCP-сервера и ботов
    with open(os.path.splitext(out_path)[0] + ".jsonl", "w", encoding="utf-8") as fh:
        for doc_id, rel, rows in per_doc:
            for r in rows:
                fh.write(json.dumps({"doc_id": doc_id, "path": rel, **r}, ensure_ascii=False) + "\n")
    return total


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    md_dir = args[0] if args else "md"
    out = "catalog/CLAUSES.md"
    repo = ""
    ref = "main"
    if "--out" in args:
        out = args[args.index("--out") + 1]
    if "--repo" in args:
        repo = args[args.index("--repo") + 1]
    if "--ref" in args:
        ref = args[args.index("--ref") + 1]
    n = build(md_dir, out, repo, ref)
    print("пунктов в указателе: %d -> %s" % (n, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
