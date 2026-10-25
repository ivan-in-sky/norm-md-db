"""Metadata extraction: designation, title, issuing body, year, amendments.

Normative documents have a very regular title page, so a rule-based parser gets
us 90% of the way; everything else is left empty and flagged for the operator to
fill in the sidecar YAML (which always wins over auto-detection).
"""
from __future__ import annotations

import re

PREFIXES = [
    ("СП", "sp"),
    ("СНиП", "snip"),
    ("СВОД ПРАВИЛ", "sp"),
    ("ГОСТ Р ИСО", "gost-r-iso"),
    ("ГОСТ Р МЭК", "gost-r-mek"),
    ("ГОСТ Р ЕН", "gost-r-en"),
    ("ГОСТ Р", "gost-r"),
    ("ГОСТ ИСО", "gost-iso"),
    ("ГОСТ", "gost"),
    ("РД", "rd"),
    ("СТО", "sto"),
    ("СТ СЭВ", "st-sev"),
    ("СТ ЦКС", "st-cks"),
    ("МДС", "mds"),
    ("МДК", "mdk"),
    ("Пособие", "posobie"),
    ("ПОСОБИЕ", "posobie"),
    ("ВСН", "vsn"),
    ("ТСН", "tsn"),
    ("НПБ", "npb"),
    ("ФЕР", "fer"),
    ("ТЕР", "ter"),
    ("ГЭСН", "gesn"),
    ("СН", "sn"),
    ("РМ", "rm"),
    ("РТМ", "rtm"),
    ("ОСТ", "ost"),
    ("ТУ", "tu"),
    ("СанПиН", "sanpin"),
    ("САНПИН", "sanpin"),
    ("Свод правил", "sp"),
]

DESIG_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p, _ in PREFIXES) + r")"
    r"\s*(\d+(?:[.\-/]\d+)*)\s*[-–—]?\s*((?:19|20)\d{2})?"
)
AMEND_RE = re.compile(
    r"(?:изменени\w+|Изм\.?)\s*(?:N|№|No|N\s)?\.?\s*(\d{1,2})"
    r"(?:\s*[-–—]\s*(\d{1,2}))?", re.I)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
ISSUER_NORMALIZE = {
    "Министерства строительства и жилищно-коммунального хозяйства Российской Федерации": "Минстрой России",
    "Министерство строительства и жилищно-коммунального хозяйства Российской Федерации": "Минстрой России",
    "Министерства строительства и жилищно-коммунального хозяйства РФ": "Минстрой России",
    "Федерального агентства по техническому регулированию и метрологии": "Росстандарт",
    "Федеральное агентство по техническому регулированию и метрологии": "Росстандарт",
    "Государственного комитета Российской Федерации по строительству и жилищно-коммунальному комплексу": "Госстрой России",
    "Министерства Российской Федерации по делам гражданской обороны, чрезвычайным ситуациям и ликвидации последствий стихийных бедствий": "МЧС России",
}

ISSUERS = [
    ("Минстрой России", "minstroy"),
    ("Министерство строительства", "minstroy"),
    ("Росстандарт", "rosstandart"),
    ("Госстандарт", "gosstandart"),
    ("Минтранс", "mintrans"),
    ("Минэнерго", "minenergo"),
    ("МЧС России", "mchs"),
    ("Ростехнадзор", "rostekhnadzor"),
    ("Роспотребнадзор", "rospotrebnadzor"),
    ("Росжелдор", "roszheldor"),
    ("Росморречфлот", "rosmorrechflot"),
    ("Правительство РФ", "government"),
    ("Правительство Российской Федерации", "government"),
    ("Госстрой", "gosstroy"),
]


VENDOR_RE = re.compile(
    r"(?:документ предоставлен|информация о документе|подготовлен с использованием|"
    r"ссылка на документ предоставлена)\s*[—-]?\s*(консультантплюс|гарант|кодекс|техэксперт|normacs)",
    re.I)


def _norm_label(x: str) -> str:
    return re.sub(r"\s+", "", (x or "").upper().replace("Ё", "Е"))


def _strip_md(x: str) -> str:
    return re.sub(r"^#{1,6}\s*|\*\*|__|(?<!\w)[*_](?!\w)", "", x or "").strip()


def _collapse_spaced(x: str) -> str:
    # «М ЕЖГОСУДАРСТВЕННЫЙСТАНДАР Т» — OCR-разрядка одиночных букв
    return re.sub(r"(?:(?<=\s)|^)((?:[А-Яа-яA-Za-z]\s){2,}[А-Яа-яA-Za-z])(?=$|[\s.,;:!?)—-])",
                  lambda m: m.group(1).replace(" ", ""), x or "", flags=re.M)


def collapse(s: str) -> str:
    return " ".join((s or "").split())


def slugify(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE)
    s = s.replace("_", "-")
    return re.sub(r"-+", "-", s).strip("-")


def find_designations(text: str) -> list:
    """Все обозначения нормативных документов в строке."""
    out = []
    text = (text or "").replace("\u00a0", " ")
    for mm in DESIG_RE.finditer(text):
        d = _mk_designation(mm)
        if d and d not in out:
            out.append(d)
    return out


def parse_designation(text: str) -> dict | None:
    mm = DESIG_RE.search((text or "").replace("\u00a0", " "))
    if not mm:
        return None
    return _mk_designation(mm)


def _mk_designation(mm) -> dict | None:
    raw_prefix, number, year = mm.group(1), mm.group(2), mm.group(3)
    kind = dict(PREFIXES).get(raw_prefix, raw_prefix.lower())
    if raw_prefix.upper().startswith("СВОД ПРАВИЛ") or raw_prefix.upper().startswith("Свод правил"):
        kind = "sp"
    designation = "%s %s" % (raw_prefix, number)
    if year and not number.endswith(year):
        designation += ("." if kind == "sp" else "-") + year
    return {
        "prefix": raw_prefix,
        "kind": kind,
        "number": number,
        "year": year,
        "designation": designation,
        "doc_id": "%s-%s" % (kind, slugify(number))
                  + ("-%s" % year if year and not number.endswith(year) else ""),
    }


def extract(lines: list[str], fallback_stem: str = "") -> dict:
    """lines = first ~60 non-empty paragraphs of the document, in order."""
    text = "\n".join(lines)
    meta = {
        "doc_id": "",
        "designation": "",
        "kind": "",
        "number": "",
        "title": "",
        "title_full": "",
        "title_lines": [],
        "issued": "",
        "year": None,
        "status": "",
        "supersedes": [],
        "supersedes_designations": [],
        "issuing_body": "",
        "amendments": [],
        "exclusions": [],
        "replaces": [],
        "referenced_docs": [],
        "auto_detected": True,
        "needs_review": [],
    }
    # На титульном листе СП почти всегда два обозначения: действующее (СП …) и то,
    # взамен которого он введён (СНиП …). Берём все и выбираем по приоритету:
    #   1) совпадение с именем файла (самый надёжный признак),
    #   2) СП/ГОСТ Р/ГОСТ — «новое» обозначение, а не СНиП/СН,
    #   3) последнее в титульной зоне (в ГОСТ-титуле оно идёт ниже).
    cands = []
    for ln_ in lines[:26]:
        for d in find_designations(ln_):
            if not any(c["designation"] == d["designation"] for c in cands):
                cands.append(d)
    stem_norm = re.sub(r"[^0-9a-zа-я]+", "", (fallback_stem or "").lower().replace("ё", "е"))

    def score(d):
        number_norm = re.sub(r"[^0-9]", "", d["number"])
        s1 = 100 if number_norm and number_norm in re.sub(r"[^0-9]", "", stem_norm) else 0
        s2 = {"sp": 60, "gost-r": 55, "gost": 50, "rd": 40, "mds": 35}.get(d["kind"], 20)
        return (s1, s2)

    desig = max(cands, key=score) if cands else None
    if desig and len(cands) > 1:
        others = [c["designation"] for c in cands if c is not desig]
        meta["other_designations"] = others
        for c in cands:
            if c is not desig and c["kind"] in ("snip", "sn", "vsn"):
                meta.setdefault("supersedes", []).append(c["doc_id"])
                meta.setdefault("supersedes_designations", []).append(c["designation"])
                meta["needs_review"].append("supersedes")
    if desig:
        meta.update({k: v for k, v in desig.items() if k in meta})
        meta["doc_id"] = desig["doc_id"]
        meta["designation"] = desig["designation"]
    else:
        meta["needs_review"].append("designation")
        meta["doc_id"] = slugify(fallback_stem) or "unknown"

    # title: first ALL-CAPS-ish line that is not the designation / not "ИЗДАНИЕ"
    SKIP = {"ИЗДАНИЕ ОФИЦИАЛЬНОЕ", "МОСКВА", "СВОД ПРАВИЛ", "НАЦИОНАЛЬНЫЙ СТАНДАРТ",
            "СТАНДАРТ", "ГОСУДАРСТВЕННЫЙ СТАНДАРТ", "МЕЖГОСУДАРСТВЕННЫЙ СТАНДАРТ",
            "СТРОИТЕЛЬНЫЕ НОРМЫ И ПРАВИЛА", "РУКОВОДЯЩИЙ ДОКУМЕНТ", "ПОСОБИЕ",
            "АКТУАЛИЗИРОВАННАЯ РЕДАКЦИЯ", "ИЗДАНИЕ ОФИЦИАЛЬНОЕ", "ПРЕДИСЛОВИЕ",
            "СОДЕРЖАНИЕ", "ВВЕДЕНИЕ", "С ИЗМЕНЕНИЕМ", "С ИЗМЕНЕНИЯМИ"}
    # строка-метка, а не название: «АКТУАЛИЗИРОВАННАЯ РЕДАКЦИЯ», «СНиП 2.02.01-83*»,
    # «С изменением N 1», иноязычное название
    LABEL_RE = re.compile(
        r"^(АКТУАЛИЗИРОВАННАЯ РЕДАКЦИЯ|ИЗДАНИЕ ОФИЦИАЛЬНОЕ|С\s*ИЗМЕНЕНИ\w+|"
        r"ИЗМЕНЕНИЕ\s+\d|СНиП|СП|ГОСТ|РД|МДС|ВСН|ТСН|ОСТ|СТО|ТУ|СанПиН)\b", re.I)
    LATIN_RE = re.compile(r"^[\sA-Za-z0-9.,'\-/()]+$")
    SKIP_NORM = {_norm_label(x) for x in SKIP}
    cand = []
    for ln_ in lines[:30]:
        raw = ln_.strip()
        if not raw or len(raw) < 4:
            continue
        is_heading = raw.startswith("#")
        s = _strip_md(raw)                     # «# **ЗДАНИЯ…**» -> «ЗДАНИЯ…»
        s = _collapse_spaced(s)                # «М ЕЖГОСУДАРСТВЕННЫЙСТАНДАР Т» -> слитно
        if _norm_label(s) in SKIP_NORM:
            continue
        if LABEL_RE.match(s.strip('"«» ')) and len(s) < 60:
            continue
        if LATIN_RE.match(s):
            continue
        if re.search(r"\b(МОСКВА|20\d\d|19\d\d)\b", s) and len(s) < 30:
            continue
        if DESIG_RE.search(s) and len(s) < 40:  # строка с самим обозначением
            continue
        letters = [c for c in s if c.isalpha()]
        if not letters:
            continue
        upper_ratio = sum(1 for c in letters if c.isupper()) / max(1, len(letters))
        # заголовок, помеченный «#», почти всегда и есть название документа
        score = (2.0 if is_heading else 0.0) + upper_ratio
        cand.append((score, len(s), s))
    if cand:
        cand.sort(key=lambda x: (-x[0], -x[1]))
        meta["title"] = cand[0][2].strip()
    if not meta["title"]:
        meta["title"] = meta.get("designation") or fallback_stem
        meta["needs_review"].append("title")
    meta["title_full"] = meta["title"]

    if desig and desig.get("year"):
        meta["year"] = desig["year"]
    if not meta.get("year"):
        for ln_ in lines[:20]:
            mm = YEAR_RE.search(ln_)
            if mm:
                meta["year"] = mm.group(0)
                break

    flat = collapse(re.sub(r"\([^)]*\)", lambda m: " " + m.group(0)[1:-1] + " ", text))
    for line in lines[:8] + lines[-8:]:
        mv = VENDOR_RE.search(line)
        if mv:
            meta["vendor"] = mv.group(1)
            break

    for body, slug in ISSUERS:
        if body.lower() in flat.lower():
            meta["issuing_body"] = ISSUER_NORMALIZE.get(body, body)
            break
    if not meta["issuing_body"]:
        mm = re.search(
            r"(?:утвержден\w*|принят\w*|введ[её]н\w*)\s+"
            r"(приказом|постановлением|распоряжением)\s+"
            r"((?:Мин[\w-]+|Рос[\w]+|Гос[\w]+|МЧС|Правительств\w+)[\w .-]*?)(?:\s+России)?\s+"
            r"от\s+(\d{1,2}[.\s]\d{1,2}[.\s]\d{4}|\d{1,2}\s+\S+\s+\d{4})",
            flat, re.I)
        if mm:
            meta["issuing_body"] = ISSUER_NORMALIZE.get(mm.group(2).strip(), mm.group(2).strip())
            meta["issued"] = mm.group(3).strip()
    if not meta["issued"]:
        mi = re.search(r"\bот\s+(\d{1,2}\s+[а-яё]+\s+\d{4}\s*г?\.?|\d{1,2}[.]\d{1,2}[.]\d{4})", flat, re.I)
        if mi:
            meta["issued"] = collapse(mi.group(1))
        else:
            meta["needs_review"].append("issuing_body")

    # the whole title page, for a human/agent to refine `title` later
    meta["title_lines"] = [s.strip() for s in lines[:14]
                           if s.strip() and not VENDOR_RE.search(s)]
    if len(meta["title"]) > 110:
        meta["needs_review"].append("title")

    for mm in re.finditer(r"[«\"']?((?:СНиП|СП|ГОСТ\s*Р?|ВСН|РД)\s*[\d.]+-?\d*\*?)[»\"']?", text):
        cand = mm.group(1).strip()
        d2 = parse_designation(cand)
        if d2 and d2["doc_id"] != meta.get("doc_id") and d2["doc_id"] not in meta.get("supersedes", []):
            # на титуле СП строка «СНиП X» означает «взамен СНиП X»
            meta.setdefault("supersedes", []).append(d2["doc_id"])
            meta.setdefault("needs_review", []).append("supersedes")

    for mm in AMEND_RE.finditer(text):
        lo = mm.group(1)
        hi = mm.group(2)
        if lo and hi:
            span = [str(n) for n in range(int(lo), int(hi) + 1)]
        else:
            span = [lo]
        for a in span:
            if a and a not in meta["amendments"]:
                meta["amendments"].append(a)
    # «Исключены с 08.01.2024. - Изменение N 5» — признак актуализированной редакции
    excl = re.findall(r"Исключен\w*\s+с\s+(\d{2}\.\d{2}\.\d{4})", text)
    if excl:
        meta["exclusions"] = sorted(set(excl))
        meta.setdefault("needs_review", []).append("amendments")

    # documents mentioned anywhere -> useful for the reference graph
    for line in lines:
        for d in re.finditer(DESIG_RE, line.replace("\u00a0", " ")):
            raw = "%s %s" % (d.group(1), d.group(2))
            if raw not in meta["referenced_docs"]:
                meta["referenced_docs"].append(raw)
    if meta.get("designation") and meta["designation"] in meta["referenced_docs"]:
        meta["referenced_docs"].remove(meta["designation"])

    meta["status"] = ""  # filled by catalog sync against ФГИС/Минстрой
    meta["needs_review"] = sorted(set(meta["needs_review"]))
    return meta
