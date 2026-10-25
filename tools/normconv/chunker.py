"""Chunker: canonical Markdown -> RAG chunks (JSONL) + atomic elements.

Principles that matter more than any clever splitting algorithm:
  1. A clause (пункт) is never cut in half. Better a 900-token chunk than two
     meaningless 450-token ones.
  2. A table is atomic and is *also* emitted separately with its caption and the
     nearest preceding sentence, because a table alone is unsearchable.
  3. A display formula travels with the sentence that introduces it.
  4. Every chunk carries the breadcrumb (документ > раздел > подраздел > пункт) and
     the document status — so the model can refuse to quote a repealed СП.
  5. Chunk ids are deterministic (hash of doc_id + clause path), so re-indexing
     is idempotent and you can diff two builds of the index.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .lint import parse_frontmatter

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CLAUSE_RE = re.compile(r"\^(c\d[A-Za-z0-9-]*)\s*$")      # только пункты: ^c8-1-6
ENUM_ID_RE = re.compile(r"\^((?:fm-|i)[A-Za-z0-9-]+)\s*$")  # служебная зона и перечисления   # Obsidian block id
CLAUSE_START_RE = re.compile(r"^(?:>\s*)?\*\*([0-9А-ЯA-Z][0-9А-Яa-z.]*[0-9А-Я])\*\*")
ANY_BLOCK_ID_RE = re.compile(r"\s*\^[A-Za-z0-9][A-Za-z0-9-]*\s*$")
TABLE_ID_RE = re.compile(r"<!-- ([\w.-]+-tbl\d+):")
TBL_CAP_RE = re.compile(r"^(Таблица\s*\d+[а-яa-z.]*)\s*[—–-]\s*(.*)$")
FIG_CAP_RE = re.compile(r"^(Рисунок\s*\d+[а-яa-z.]*)\s*[—–-]\s*(.*)$", re.I)
# вики-ссылка Obsidian: [[Цель]], [[Цель#^c8-1-6]], [[Цель|алиас]] (цель не содержит [[ ] | #)
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
CHARS_PER_TOKEN = 3.4  # Cyrillic-heavy text: ~3.4 chars per token for modern BPE


def est_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def cid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def extract_refs(text: str, own: set | None = None) -> list:
    """Вики-ссылки чанка -> отсортированный список versionless-обозначений.

    `[[ГОСТ 27751]]`, `[[СП 22.13330|СНиП 2.02.01-83]]`, `[[СП 56.13330.2021#^c8-1]]`
    дают «ГОСТ 27751», «СП 22.13330», «СП 56.13330». Ссылки на сам документ
    (own — его designation/aliases/note_name и их versionless-формы) исключаются:
    для графа retrieval они шум. Список детерминирован (sorted) — индекс идемпотентен.
    """
    from .obsidian import versionless
    refs = []
    seen = set()
    for m in WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        if not target:
            continue
        base = versionless(target)
        if own and (target in own or base in own):
            continue
        if base not in seen:
            seen.add(base)
            refs.append(base)
    return sorted(refs)


def split_blocks(md_body: str) -> list[dict]:
    """Split into typed blocks; tables and $$..$$ stay atomic."""
    lines = md_body.split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        ln = lines[i]

        if ln.strip().startswith("<table"):
            buf = [ln]
            i += 1
            closed = "</table>" in ln
            while i < len(lines) and not closed:
                buf.append(lines[i])
                if "</table>" in lines[i]:
                    closed = True
                i += 1
            blocks.append({"type": "table", "text": "\n".join(buf).strip()})
            continue

        if ln.strip() == "$$":
            buf = [ln]
            i += 1
            while i < len(lines):
                buf.append(lines[i])
                i += 1
                if lines[i - 1].strip() == "$$":
                    break
            blocks.append({"type": "math", "text": "\n".join(buf).strip()})
            continue

        m = HEADING_RE.match(ln)
        if m:
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            i += 1
            continue

        m = CLAUSE_RE.search(ln)
        if m and CLAUSE_START_RE.match(ln.strip()):
            block_id = m.group(1)
            number = _clause_number_from_block_id(block_id)
            body = [CLAUSE_RE.sub("", ln).rstrip()]
            i += 1
            # пункт может продолжаться несколькими абзацами, формулой и «где»-блоком
            while i < len(lines):
                nxt = lines[i]
                st = nxt.strip()
                if (HEADING_RE.match(nxt) or not st
                        or st.startswith("<table") or st.startswith("**Таблица")
                        or CLAUSE_START_RE.match(st)):
                    break
                body.append(nxt)
                i += 1
            text = "\n".join(body).strip()
            blocks.append({"type": "clause", "number": number, "block_id": block_id, "text": text})
            continue

        if ln.strip().startswith("<!--"):
            blocks.append({"type": "marker", "text": ln.strip()})
            i += 1
            continue

        if not ln.strip():
            i += 1
            continue

        buf = [ln]
        i += 1
        while i < len(lines) and lines[i].strip() and not HEADING_RE.match(lines[i]) \
                and not CLAUSE_RE.match(lines[i].strip()) and not lines[i].strip().startswith("<table") \
                and not lines[i].strip() == "$$" and not lines[i].strip().startswith("<!--"):
            buf.append(lines[i])
            i += 1
        text = "\n".join(buf).strip()
        kind = "para"
        if re.match(r"^\s*(\d+(?:\.\d+)*|[a-zа-я]\)|\-)\s+", text):
            kind = "list"
        if text.startswith(">"):
            kind = "note"
        blocks.append({"type": kind, "text": text})
    return blocks


def chunk_document(path: str, max_tokens: int = 700, min_tokens: int = 120,
                   emit_atoms: bool = True) -> tuple[list[dict], list[dict]]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    fm = parse_frontmatter(text)
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end > 0:
            body = body[end + 4:].lstrip("\n")

    qa = {}
    qa_path = path[:-3] + ".qa.json"
    if os.path.exists(qa_path):
        try:
            qa = json.load(open(qa_path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            qa = {}

    doc = {
        "doc_id": fm.get("doc_id") or os.path.splitext(os.path.basename(path))[0],
        "designation": fm.get("designation") or "",
        "kind": fm.get("kind") or "",
        "title": fm.get("title") or "",
        "year": fm.get("year") or "",
        "status": fm.get("status") or "unknown",
        "issuing_body": fm.get("issuing_body") or "",
        "amendments": fm.get("amendments") or [],
        "license": fm.get("license") or "",
        "language": fm.get("language") or "ru",
        "confidence": (fm.get("conversion") or {}).get("confidence") if isinstance(fm.get("conversion"), dict) else None,
        "path": os.path.relpath(path).replace(os.sep, "/"),
    }

    # собственные обозначения документа: ссылки на них в meta.refs не попадают
    own = set()
    for key in ("designation", "note_name", "doc_id"):
        v = fm.get(key)
        if v:
            own.add(str(v).strip())
    for a in (fm.get("aliases") or []):
        if a:
            own.add(str(a).strip())
    from .obsidian import versionless
    own |= {versionless(o) for o in own}

    blocks = split_blocks(body)
    chunks: list[dict] = []
    atoms: list[dict] = []

    crumbs: dict[int, str] = {}
    buf: list[dict] = []
    buf_tokens = 0
    current_clause = ""
    pending_caption = ""

    def crumb_list() -> list[str]:
        return [crumbs[l] for l in sorted(crumbs) if crumbs.get(l)]

    def flush(reason: str = "size"):
        nonlocal buf, buf_tokens
        if not buf:
            return
        text_parts = []
        for b in buf:
            text_parts.append(b["text"])
        content = "\n\n".join(t for t in text_parts if t.strip())
        # block id нужны для ссылок, но не для эмбеддинга
        embed_text = "\n".join(ANY_BLOCK_ID_RE.sub("", l) for l in content.split("\n"))
        if not content.strip():
            buf, buf_tokens = [], 0
            return
        clause_numbers = sorted({b["number"] for b in buf if b.get("number")})
        bc = crumb_list()
        header = " > ".join([doc["designation"] or doc["doc_id"]] + bc)
        chunk_text = "%s\n\n%s" % (header, embed_text)
        rec = {
            "chunk_id": cid(doc["doc_id"], "|".join(clause_numbers) or reason,
                            hashlib.sha1(content.encode()).hexdigest()[:10]),
            "doc": doc,
            "breadcrumb": bc,
            "clause": clause_numbers[0] if len(clause_numbers) == 1 else "",
            "clauses": clause_numbers,
            "content": content,
            "text": chunk_text,
            "tokens_est": est_tokens(chunk_text),
            "n_tables": sum(1 for b in buf if b["type"] == "table"),
            "n_formulas": content.count("$$") // 2 + len(re.findall(r"(?<!\$)\$(?!\$)[^$\n]+\$(?!\$)", content)),
            "has_image": bool(re.search(r"<img|!\[", content)),
            "anchor": _anchor(bc[-1]) if bc else "",
            "pages": [],
            "meta": {"refs": extract_refs(content, own)},
        }
        chunks.append(rec)
        buf, buf_tokens = [], 0

    for idx, b in enumerate(blocks):
        t = b["type"]
        if t == "heading":
            # a new heading closes the previous section chunk
            flush("heading")
            crumbs[b["level"]] = b["text"]
            for deeper in [l for l in crumbs if l > b["level"]]:
                crumbs.pop(deeper, None)
            buf.append({"type": "heading", "text": "%s %s" % ("#" * b["level"], b["text"])})
            buf_tokens += est_tokens(b["text"]) + 2
            continue
        if t == "marker":
            continue
        if t == "table":
            # tables are atomic: close the current chunk, emit the table on its own
            flush("table")
            if not pending_caption:
                pending_caption = _nearest_caption(blocks, idx)
            content = b["text"]
            bc = crumb_list()
            rec = {
                "chunk_id": cid(doc["doc_id"], "table", hashlib.sha1(content.encode()).hexdigest()[:10]),
                "doc": doc,
                "breadcrumb": bc,
                "clause": current_clause,
                "clauses": [current_clause] if current_clause else [],
                "content": content,
                "text": "%s > %s\n\n%s" % (doc["designation"] or doc["doc_id"], " > ".join(bc), content),
                "tokens_est": est_tokens(content),
                "n_tables": 1,
                "n_formulas": 0,
                "has_image": False,
                "kind": "table",
                "caption": pending_caption,
                "pages": [],
                "meta": {"refs": extract_refs(content, own)},
            }
            chunks.append(rec)
            if emit_atoms:
                atoms.append({"doc_id": doc["doc_id"], "type": "table", "caption": pending_caption,
                              "clause": current_clause, "breadcrumb": bc,
                              "html": content, "text": _html_table_to_text(content),
                              "refs": extract_refs(content, own)})
            pending_caption = ""
            continue
        if t == "clause":
            current_clause = b["number"]
        if t in ("para", "list"):
            mm = TBL_CAP_RE.match(b["text"].lstrip("*").rstrip("*").strip())
            if mm:
                pending_caption = b["text"].strip("* ").strip()
            else:
                mf = FIG_CAP_RE.match(b["text"].strip("* ").strip())
                if mf:
                    pending_caption = b["text"].strip("* ").strip()
        bt = est_tokens(b["text"])
        if buf_tokens + bt > max_tokens and buf:
            flush("size")
        buf.append(b)
        buf_tokens += bt

    flush("eof")
    if chunks and not chunks[0]["breadcrumb"]:
        chunks[0]["kind"] = "title_page"

    # merge undersized neighbouring chunks of the same section
    merged: list[dict] = []
    for c in chunks:
        if merged and c.get("kind") != "table" and merged[-1].get("kind") != "table":
            prev = merged[-1]
            if (prev["tokens_est"] + c["tokens_est"] < max_tokens
                    and prev["breadcrumb"] == c["breadcrumb"]):
                prev["content"] += "\n\n" + c["content"]
                prev["text"] += "\n\n" + c["content"]
                prev["tokens_est"] = est_tokens(prev["text"])
                prev["clauses"] = sorted(set(prev["clauses"]) | set(c["clauses"]))
                prev["clause"] = prev["clauses"][0] if len(prev["clauses"]) == 1 else ""
                prev["n_tables"] += c["n_tables"]
                prev["meta"]["refs"] = sorted(set(prev["meta"]["refs"]) | set(c["meta"]["refs"]))
                continue
        merged.append(c)

    # граф ссылок документа: union всех meta.refs -> doc.references (payload для
    # retrieval-расширения «нашёл пункт СП -> подтяни связанные ГОСТы»)
    all_refs = set()
    for c in merged:
        all_refs.update(c.get("meta", {}).get("refs") or [])
    doc["references"] = sorted(all_refs)
    return merged, atoms




def _clause_number_from_block_id(block_id: str) -> str:
    """c8-1-6 -> 8.1.6 (идентификаторы перечислений и служебной зоны не участвуют)."""
    body = block_id[1:]
    return body.replace("-", ".") if re.match(r"^[\d-]+$", body) else body


def _nearest_caption(blocks: list[dict], idx: int) -> str:
    """Nearest preceding `<!-- caption: ... -->` / figure caption marker."""
    for j in range(idx - 1, max(-1, idx - 6), -1):
        t = blocks[j]["text"]
        m = re.search(r"<!-- caption: ([^>]*) -->", t)
        if m:
            return m.group(1).strip()
        m = FIG_CAP_RE.search(t)
        if m:
            return m.group(1).strip()
        if blocks[j]["type"] in ("heading", "clause"):
            break
    return ""


def _html_table_to_text(html: str) -> str:
    """Plain-text rendering of the <table> for keyword search / embeddings."""
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    out = []
    for r in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)
        cells = [re.sub(r"<[^>]+>", " ", c).replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&amp;", "&").strip() for c in cells]
        out.append(" | ".join(cells))
    return "\n".join(out)


def _anchor(text: str) -> str:
    a = (text or "").strip().lower().replace("ё", "е")
    a = re.sub(r"[^\w\- ]", "", a, flags=re.UNICODE)
    return re.sub(r"[ ]+", "-", a).strip("-")


def chunk_dir(md_dir: str, out_path: str, min_tokens: int = 120, max_tokens: int = 700,
              overlap: int = 0, fmt: str = "jsonl") -> int:
    files = []
    for dirpath, dirnames, fnames in os.walk(md_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "_")) and not d.endswith(".assets")]
        for f in sorted(fnames):
            if f.endswith(".md") and not f.endswith(".qa.md"):
                files.append(os.path.join(dirpath, f))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    base = os.path.splitext(out_path)[0]
    n = 0
    stats = {"documents": 0, "chunks": 0, "atoms": 0, "mean_tokens": 0.0, "max_tokens": 0}
    tok_sum = 0
    with open(out_path, "w", encoding="utf-8") as fh, \
            open(base + ".atoms.jsonl", "w", encoding="utf-8") as fa:
        for p in files:
            try:
                chunks, atoms = chunk_document(p, max_tokens=max_tokens, min_tokens=min_tokens)
            except Exception as exc:  # keep the batch alive
                print("  ! %s: %s" % (p, exc))
                continue
            stats["documents"] += 1
            for c in chunks:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
                n += 1
                tok_sum += c["tokens_est"]
                stats["max_tokens"] = max(stats["max_tokens"], c["tokens_est"])
            for a in atoms:
                fa.write(json.dumps(a, ensure_ascii=False) + "\n")
                stats["atoms"] += 1
    stats["chunks"] = n
    stats["mean_tokens"] = round(tok_sum / max(1, n), 1)
    with open(base + ".stats.json", "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)
    return n
