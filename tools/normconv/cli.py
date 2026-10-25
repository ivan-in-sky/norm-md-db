"""CLI: batch conversion .doc/.docx -> canonical Markdown + QA reports.

  python -m normconv.cli convert sources/ --out md/ [--jobs 4] [--force]
  python -m normconv.cli convert sources/sp-63.docx --out md/
  python -m normconv.cli report md/ --out md/_reports/
  python -m normconv.cli lint md/sp-63.13330.2018.md
  python -m normconv.cli chunk md/ --out index/chunks.jsonl

Conversion is cached by sha256 of the source + tool version + options hash, so a
full re-run over thousands of documents only reprocesses what changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import __version__

SUPPORTED_IN = (".docx", ".doc")
STATE_FILE = ".normconv-state.json"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def find_sources(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("out", "md", "index")]
        for f in sorted(filenames):
            if f.startswith("~$"):
                continue
            if f.lower().endswith(SUPPORTED_IN):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def normalize_doc_to_docx(path: str, workdir: str) -> str | None:
    """.doc -> .docx through headless LibreOffice (the only reliable free route).

    Returns path to the converted .docx, or None if LibreOffice is unavailable.
    Caveat to record in the QA report: legacy Word Binary can lose OMML formulas,
    so a .doc whose formulas became images must go through the image fallback.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    os.makedirs(workdir, exist_ok=True)
    cmd = [soffice, "--headless", "--norestore", "--convert-to", "docx", "--outdir", workdir, path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(workdir, stem + ".docx")
    return out if os.path.exists(out) else None


def convert_one(args) -> dict:
    path, outdir, opts_dict, force = args
    from .docx2md import Docx2Md, Options
    opts = Options(**opts_dict)
    rec = {"source": path, "ok": False}
    t0 = time.time()
    work = path
    converted_from = None
    try:
        if path.lower().endswith(".doc"):
            wd = os.path.join(outdir, "_tmp_docx")
            work = normalize_doc_to_docx(path, wd)
            if work is None:
                rec["error"] = "libreoffice not available for .doc"
                rec["hint"] = "установите libreoffice или сконвертируйте .doc вручную"
                return rec
            converted_from = "doc"
            opts.source_kind = "doc"
        if not os.path.exists(work):
            rec["error"] = "source vanished after normalisation"
            return rec
        sha = sha256_file(path)
        state_path = os.path.join(outdir, STATE_FILE)
        # opts.source_uri может быть задан пользователем (--source-uri): тогда
        # file://-fallback не перетирает его (провенанс важнее локального пути).
        state = {}
        if os.path.exists(state_path):
            try:
                state = json.load(open(state_path, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        key = os.path.abspath(path)
        ohash = hashlib.sha256(json.dumps(opts_dict, sort_keys=True).encode()).hexdigest()[:12]
        prev = state.get(key)
        if not force and prev and prev.get("sha256") == sha and prev.get("opts") == ohash \
                and prev.get("tool") == __version__ and os.path.exists(prev.get("md", "")):
            rec.update({"ok": True, "cached": True, "md": prev["md"], "doc_id": prev.get("doc_id"),
                        "confidence": prev.get("confidence")})
            return rec
        conv = Docx2Md(work, opts)
        if not opts.source_uri:
            opts.source_uri = "file://" + os.path.abspath(path)
        conv.opts.source_uri = opts.source_uri
        md_path, rep = conv.convert(outdir, stem=os.path.splitext(os.path.basename(path))[0])
        rec.update({"ok": True, "cached": False, "md": md_path, "doc_id": rep.get("doc_id"),
                    "confidence": rep.get("confidence"),
                    "words": rep.get("words"), "tables": rep["counts"]["tables"],
                    "formulas": rep["counts"]["formulas"], "images": rep["counts"]["images"],
                    "review_items": len(rep.get("review", [])),
                    "issues": len([i for i in rep.get("issues", []) if i["severity"] in ("error", "warning")]),
                    "converted_from": converted_from, "sha256": sha, "opts_hash": ohash})
        state[key] = {"sha256": sha, "opts": ohash, "tool": __version__, "md": md_path,
                      "doc_id": rep.get("doc_id"), "confidence": rep.get("confidence"),
                      "at": time.time()}
        os.makedirs(outdir, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001 - batch must not die on one file
        import traceback
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        rec["traceback"] = traceback.format_exc()[-1500:]
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_convert(a) -> int:
    from .docx2md import Options
    opts_dict = {
        "math": a.math, "tables": a.tables, "notes": a.notes,
        "resolve_xrefs": not a.no_xrefs, "numbered_as_heading": not a.no_auto_headings,
        "lang": a.lang, "license": a.license, "source_kind": "docx",
        "min_confidence": a.min_confidence,
        "obsidian": not a.no_obsidian, "registry_path": a.registry,
        "glyphs_path": a.glyphs, "meta_override_path": a.meta_override,
        "source_uri": a.source_uri,
    }
    os.makedirs(a.out, exist_ok=True)
    sources = []
    for s in a.sources:
        sources.extend(find_sources(s))
    if not sources:
        print("нет входных файлов (.doc/.docx)", file=sys.stderr)
        return 2
    print("найдено документов: %d" % len(sources))
    results = []
    jobs = max(1, a.jobs)
    payload = [(s, a.out, opts_dict, a.force) for s in sources]
    if jobs == 1:
        for p in payload:
            r = convert_one(p)
            results.append(r)
            _print_result(r)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(convert_one, p): p[0] for p in payload}
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                _print_result(r)
    summary = _summarise(results, a.out)
    print(json.dumps(summary["totals"], ensure_ascii=False, indent=2))
    return 0 if summary["totals"]["failed"] == 0 else 1


def _print_result(r):
    if r.get("ok"):
        print("  ✓ %-52s conf=%.2f %s" % (
            os.path.basename(r.get("md") or r["source"])[:52],
            r.get("confidence") or 0,
            "(cache)" if r.get("cached") else "%ss" % r.get("seconds", 0)))
    else:
        print("  ✗ %-52s %s" % (os.path.basename(r["source"])[:52], r.get("error", "")[:90]))


def _summarise(results, outdir) -> dict:
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    low = [r for r in ok if (r.get("confidence") or 1) < 0.9]
    totals = {
        "total": len(results), "converted": len([r for r in ok if not r.get("cached")]),
        "cached": len([r for r in ok if r.get("cached")]), "failed": len(failed),
        "low_confidence": len(low),
        "mean_confidence": round(sum(r.get("confidence") or 0 for r in ok) / max(1, len(ok)), 3),
        "needs_review": sum(r.get("review_items") or 0 for r in ok),
        "seconds": round(sum(r.get("seconds") or 0 for r in results), 1),
    }
    os.makedirs(os.path.join(outdir, "_reports"), exist_ok=True)
    p = os.path.join(outdir, "_reports", "batch.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"totals": totals, "results": results}, fh, ensure_ascii=False, indent=1)
    lines = ["# Отчёт о пакетной конвертации", "", "```json", json.dumps(totals, ensure_ascii=False, indent=2), "```", ""]
    if failed:
        lines += ["## Ошибки", ""]
        for r in failed:
            lines.append("- `%s` — %s" % (r["source"], r.get("error", "")[:200]))
        lines.append("")
    if low:
        lines += ["## Низкая уверенность (<0.9) — проверить в первую очередь", ""]
        for r in sorted(low, key=lambda x: x.get("confidence") or 0):
            lines.append("- `%.2f` %s" % (r.get("confidence") or 0, os.path.basename(r.get("md", ""))))
        lines.append("")
    with open(os.path.join(outdir, "_reports", "batch.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return {"totals": totals}


def cmd_report(a) -> int:
    """Aggregate all *.qa.json into a single triage dashboard."""
    qas = []
    for dirpath, _, files in os.walk(a.md):
        for f in files:
            if f.endswith(".qa.json"):
                try:
                    qas.append(json.load(open(os.path.join(dirpath, f), encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    pass
    qas.sort(key=lambda d: d.get("confidence", 1))
    tot = {
        "documents": len(qas),
        "mean_confidence": round(sum(d.get("confidence", 0) for d in qas) / max(1, len(qas)), 3),
        "words": sum(d.get("words", 0) for d in qas),
        "tables": sum(d["counts"]["tables"] for d in qas if "counts" in d),
        "tables_merged": sum(d["counts"]["tables_merged"] for d in qas if "counts" in d),
        "formulas": sum(d["counts"]["formulas"] for d in qas if "counts" in d),
        "formulas_low_confidence": sum(d["counts"]["formulas_low_confidence"] for d in qas if "counts" in d),
        "images": sum(d["counts"]["images"] for d in qas if "counts" in d),
        "review_items": sum(len(d.get("review", [])) for d in qas),
        "errors": sum(len([i for i in d.get("issues", []) if i["severity"] == "error"]) for d in qas),
    }
    L = ["# Дашборд корпуса", "", "```json", json.dumps(tot, ensure_ascii=False, indent=2), "```", "",
         "## Очередь на ручную проверку (по возрастанию уверенности)", "",
         "| conf | документ | слов | табл. | слиян. | формул | формул⚠ | review |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for d in qas:
        c = d.get("counts", {})
        L.append("| %.2f | `%s` | %d | %d | %d | %d | %d | %d |" % (
            d.get("confidence", 0), d.get("doc_id", "?"), d.get("words", 0), c.get("tables", 0),
            c.get("tables_merged", 0), c.get("formulas", 0), c.get("formulas_low_confidence", 0),
            len(d.get("review", []))))
    L.append("")
    kinds = {}
    for d in qas:
        for r in d.get("review", []):
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    if kinds:
        L += ["## Типы ручных доработок", ""]
        for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
            L.append("- `%s`: %d" % (k, v))
        L.append("")
    out = os.path.join(a.md, "_reports", "dashboard.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    with open(os.path.join(a.md, "_reports", "dashboard.json"), "w", encoding="utf-8") as fh:
        json.dump({"totals": tot, "documents": [{k: v for k, v in d.items()
                                                if k in ("doc_id", "confidence", "words", "counts", "meta")}
                                               for d in qas]}, fh, ensure_ascii=False, indent=1)
    print(json.dumps(tot, ensure_ascii=False, indent=2))
    print("-> %s" % out)
    return 0


def cmd_lint(a) -> int:
    from .lint import lint_path
    issues = []
    for target in a.targets:
        issues.extend(lint_path(target))
    by_sev = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        if a.verbose or i["severity"] != "info":
            print("%-7s %-22s %s:%s %s" % (i["severity"].upper(), i["code"], i["file"],
                                            i.get("line", "-"), i["msg"][:110]))
    print("\n%s" % json.dumps(by_sev, ensure_ascii=False))
    return 1 if by_sev["error"] else 0


def cmd_chunk(a) -> int:
    from .chunker import chunk_dir
    n = chunk_dir(a.md, a.out, min_tokens=a.min_tokens, max_tokens=a.max_tokens,
                  overlap=a.overlap, fmt=a.format)
    print("чанков: %d -> %s" % (n, a.out))
    return 0


def cmd_catalog(a) -> int:
    from .catalog import rebuild
    n = rebuild(a.md, a.out)
    print("документов в каталоге: %d -> %s" % (n, a.out))
    return 0


def cmd_clauses(a) -> int:
    from .clauses import build
    n = build(a.md, a.out, repo_url=a.repo, ref=a.ref)
    print("пунктов в указателе: %d -> %s" % (n, a.out))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="normconv", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="normconv " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="пакетная конвертация .doc/.docx -> Markdown + QA")
    c.add_argument("sources", nargs="+")
    c.add_argument("--out", default="md")
    c.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    c.add_argument("--force", action="store_true", help="игнорировать кэш")
    c.add_argument("--math", choices=["latex", "image", "keep"], default="latex")
    c.add_argument("--tables", choices=["html", "md", "both"], default="html")
    c.add_argument("--notes", choices=["quote", "plain"], default="quote")
    c.add_argument("--lang", default="ru")
    c.add_argument("--license", default="CC0-1.0")
    c.add_argument("--min-confidence", type=float, default=0.9)
    c.add_argument("--no-xrefs", action="store_true")
    c.add_argument("--no-auto-headings", action="store_true",
                   help="не считать нумерованные абзацы заголовками")
    c.add_argument("--no-obsidian", action="store_true",
                   help="не делать вики-ссылки/алиасы/чистку (канонический Markdown как есть)")
    c.add_argument("--registry", default="",
                   help="реестр актуальных версий (yaml) для резолвинга [[ссылок]]")
    c.add_argument("--glyphs", default="",
                   help="словарь символов-картинок (правки/символы-<doc>.yaml, docs/08)")
    c.add_argument("--meta-override", default="",
                   help="ручные поля фронтмэттера (правки/<doc_id>.yaml)")
    c.add_argument("--source-uri", default="",
                   help="URI происхождения файла (origin_url из inventory.csv); "
                        "по умолчанию file://<абсолютный путь>")
    c.set_defaults(func=cmd_convert)

    r = sub.add_parser("report", help="сводный дашборд по всем QA-отчётам")
    r.add_argument("md", nargs="?", default="md")
    r.set_defaults(func=cmd_report)

    l = sub.add_parser("lint", help="линт канонического Markdown")
    l.add_argument("targets", nargs="+")
    l.add_argument("-v", "--verbose", action="store_true")
    l.set_defaults(func=cmd_lint)

    ch = sub.add_parser("chunk", help="нарезка на чанки для RAG (JSONL/Parquet-подобный)")
    ch.add_argument("md", nargs="?", default="md")
    ch.add_argument("--out", default="index/chunks.jsonl")
    ch.add_argument("--min-tokens", type=int, default=120)
    ch.add_argument("--max-tokens", type=int, default=700)
    ch.add_argument("--overlap", type=int, default=0)
    ch.add_argument("--format", choices=["jsonl", "md"], default="jsonl")
    ch.set_defaults(func=cmd_chunk)

    cat = sub.add_parser("catalog", help="пересобрать catalog.yaml/csv из frontmatter")
    cat.add_argument("md", nargs="?", default="md")
    cat.add_argument("--out", default="catalog")
    cat.set_defaults(func=cmd_catalog)

    cl = sub.add_parser("clauses", help="указатель пунктов: block id -> строка -> ссылки (GitHub/Obsidian)")
    cl.add_argument("md", nargs="?", default="md")
    cl.add_argument("--out", default="catalog/CLAUSES.md")
    cl.add_argument("--repo", default="", help="https://github.com/<org>/<repo>")
    cl.add_argument("--ref", default="main")
    cl.set_defaults(func=cmd_clauses)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
