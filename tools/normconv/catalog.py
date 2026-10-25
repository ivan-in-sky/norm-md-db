"""Catalog: rebuild the machine-readable index of the corpus from QA reports.

catalog.jsonl / catalog.csv / catalog.yaml / INDEX.md are *derived* artifacts:
they are regenerated from md/*/*.qa.json in CI, never edited by hand. The single
place a human edits document metadata is the `overrides/<doc_id>.yaml` sidecar,
which always wins over auto-detection.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

FIELDS = ["doc_id", "designation", "kind", "number", "title", "year", "issued",
          "issuing_body", "status", "status_checked", "amendments", "supersedes",
          "superseded_by", "topics", "ics", "language", "license", "words",
          "tables", "tables_merged", "formulas", "images", "confidence",
          "review_items", "path", "sha256", "references"]


def load_overrides(md_dir: str) -> dict:
    root = os.path.dirname(os.path.abspath(md_dir.rstrip("/")))
    odir = os.path.join(root, "overrides")
    out = {}
    if not os.path.isdir(odir):
        return out
    for f in sorted(os.listdir(odir)):
        if not f.endswith((".yaml", ".yml", ".json")):
            continue
        p = os.path.join(odir, f)
        try:
            if f.endswith(".json"):
                data = json.load(open(p, encoding="utf-8"))
            else:
                data = _tiny_yaml(open(p, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("doc_id"):
            out[data["doc_id"]] = data
    return out


def _tiny_yaml(text: str) -> dict:
    """Very small flat/sub-nested YAML reader for override sidecars."""
    out: dict = {}
    cur_key = None
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- ") and cur_key:
            out.setdefault(cur_key, [])
            if isinstance(out[cur_key], list):
                out[cur_key].append(line[2:].strip().strip('"'))
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v == "":
            cur_key = k
            out.setdefault(k, {})
        else:
            cur_key = None
            out[k] = v.strip('"') if not v.startswith("[") else json.loads(v.replace("'", '"'))
    return out


def rebuild(md_dir: str, out_dir: str) -> int:
    docs = []
    for dirpath, dirnames, fnames in os.walk(md_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "_")) and not d.endswith(".assets")]
        for f in sorted(fnames):
            if not f.endswith(".qa.json"):
                continue
            try:
                qa = json.load(open(os.path.join(dirpath, f), encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            meta = qa.get("meta") or {}
            counts = qa.get("counts") or {}
            md_rel = os.path.relpath(os.path.join(dirpath, f[:-8] + ".md"),
                                     os.path.dirname(os.path.abspath(md_dir.rstrip("/")))).replace(os.sep, "/")
            rec = {
                "doc_id": qa.get("doc_id") or meta.get("doc_id") or "",
                "designation": meta.get("designation") or "",
                "kind": meta.get("kind") or "",
                "number": meta.get("number") or "",
                "title": meta.get("title") or "",
                "year": meta.get("year") or "",
                "issued": meta.get("issued") or "",
                "issuing_body": meta.get("issuing_body") or "",
                "status": meta.get("status") or "unknown",
                "status_checked": meta.get("status_checked") or "",
                "amendments": meta.get("amendments") or [],
                "supersedes": meta.get("supersedes") or [],
                "superseded_by": meta.get("superseded_by") or "",
                "topics": meta.get("topics") or [],
                "ics": meta.get("ics") or [],
                "language": meta.get("language") or "ru",
                "license": meta.get("license") or "",
                "words": qa.get("words") or 0,
                "tables": counts.get("tables", 0),
                "tables_merged": counts.get("tables_merged", 0),
                "formulas": counts.get("formulas", 0),
                "images": counts.get("images", 0),
                "confidence": qa.get("confidence") or 0,
                "review_items": len(qa.get("review") or []),
                "path": md_rel,
                "sha256": (meta.get("source") or {}).get("sha256", ""),
                "references": meta.get("referenced_docs") or [],
            }
            docs.append(rec)

    over = load_overrides(md_dir)
    if over:
        root = os.path.dirname(os.path.abspath(md_dir.rstrip("/")))
        odir = os.path.join(root, "overrides")
        names = os.listdir(odir) if os.path.isdir(odir) else []
        for rec in docs:
            o = over.get(rec["doc_id"])
            if not o:
                continue
            for k, v in o.items():
                if v not in (None, "", [], {}):
                    rec[k] = v
            rec["override"] = next((f for f in names if f.startswith(rec["doc_id"])), "")

    docs.sort(key=lambda d: (d["kind"], d["designation"] or d["doc_id"]))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "catalog.jsonl"), "w", encoding="utf-8") as fh:
        for d in docs:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "catalog.csv"), "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        wr.writeheader()
        for d in docs:
            row = dict(d)
            for k in ("amendments", "supersedes", "topics", "ics", "references"):
                row[k] = "; ".join(map(str, row.get(k) or []))
            wr.writerow(row)
    with open(os.path.join(out_dir, "catalog.yaml"), "w", encoding="utf-8") as fh:
        fh.write("# generated by `normconv catalog` — do not edit by hand\n")
        fh.write("# hand edits belong to overrides/<doc_id>.yaml\n")
        fh.write("generated_at: %s\n" % datetime.now(timezone.utc).isoformat(timespec="seconds"))
        fh.write("documents: %d\n" % len(docs))
        fh.write("docs:\n")
        for d in docs:
            fh.write("  - doc_id: %s\n" % d["doc_id"])
            for k in FIELDS[1:]:
                v = d.get(k)
                if isinstance(v, list):
                    if not v:
                        continue
                    fh.write("    %s:\n" % k)
                    for x in v:
                        fh.write("      - %s\n" % x)
                elif v in (None, ""):
                    continue
                else:
                    fh.write("    %s: %s\n" % (k, json.dumps(v, ensure_ascii=False)
                                               if isinstance(v, str) and (":" in v or "#" in v) else v))

    # human index
    by_kind: dict[str, list] = {}
    for d in docs:
        by_kind.setdefault(d["kind"] or "other", []).append(d)
    L = ["# Каталог корпуса", "",
         "Документов: **%d**. Сгенерировано автоматически %s — не редактируйте вручную."
         % (len(docs), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), "",
         "| kind | документов | слов | таблиц | формул |", "| --- | --- | --- | --- | --- |"]
    for k in sorted(by_kind):
        ds = by_kind[k]
        L.append("| `%s` | %d | %d | %d | %d |" % (
            k, len(ds), sum(d["words"] for d in ds), sum(d["tables"] for d in ds),
            sum(d["formulas"] for d in ds)))
    L.append("")
    for k in sorted(by_kind):
        L += ["## %s" % k.upper(), "",
              "| обозначение | название | год | статус | conf | путь |",
              "| --- | --- | --- | --- | --- | --- |"]
        for d in by_kind[k]:
            L.append("| %s | %s | %s | %s | %.2f | [`%s`](../%s) |" % (
                d["designation"] or d["doc_id"], (d["title"] or "")[:70], d["year"] or "—",
                d["status"], d["confidence"], d["path"], d["path"]))
        L.append("")
    need = [d for d in docs if d["status"] in ("unknown", "") or d["confidence"] < 0.9 or d["review_items"]]
    if need:
        L += ["## Требует внимания (%d)" % len(need), ""]
        for d in sorted(need, key=lambda x: x["confidence"])[:60]:
            L.append("- `%.2f` **%s** — review: %d, статус: %s" % (
                d["confidence"], d["designation"] or d["doc_id"], d["review_items"], d["status"]))
        L.append("")
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return len(docs)
