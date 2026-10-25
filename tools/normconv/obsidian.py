"""Obsidian-слой: имена заметок, алиасы, вики-ссылки с резолвингом на актуальную
версию, очистка от вендорских пометок (КонсультантПлюс/Техэксперт).

База знаний делается для Obsidian + RAG, практичная (для строителей и
проектировщиков, не для юристов). Отсюда три принципа:

1. Имя заметки = обозначение документа кириллицей («СП 22.13330.2016»), а не
   транслит. Так `[[СП 22.13330.2016]]` работает в Obsidian «из коробки».
2. У каждой заметки в `aliases` — беспроигрышные формы: полная, без года
   («СП 22.13330») и обозначения заменённых документов («СНиП 2.02.01-83»).
   Поэтому ссылка `[[ГОСТ 31937]]` ведёт на актуальный `ГОСТ 31937-2024`,
   а `[[СНиП 31-03-2001]]` — на заменивший его `СП 56.13330.2021`.
3. Все упоминания сторонних справочных систем вычищаются: служебные строки,
   ссылки, `alt` у картинок, редакционные примечания («КонсультантПлюс:
   примечание…») приводятся к нейтральному виду без названия вендора.
"""
from __future__ import annotations

import os
import re

# --------------------------------------------------------------------------- #
# обозначения документов
# --------------------------------------------------------------------------- #
PREFIXES = ["ГОСТ Р ИСО", "ГОСТ Р МЭК", "ГОСТ Р ЕН", "ГОСТ ИСО", "ГОСТ Р", "ГОСТ",
            "СП", "СНиП", "РД", "МДС", "МДК", "ВСН", "ТСН", "НПБ", "СТО", "СТ СЭВ",
            "ОСТ", "ТУ", "СанПиН", "СП", "Пособие", "ФЕР", "ТЕР", "ГЭСН", "СН", "РМ"]
# обозначение в тексте: префикс + номер (с дефисами/точками) + необязательный год
DESIG_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in PREFIXES) + r")"
    r"[\s\u00a0]?(\d+(?:[.\-]\d+)*)"          # номер: 27751 / 22.13330 / 2.02.01-83 / 12-03
    r"(?:[-–.\s]((?:19|20)\d{2}))?"            # необязательный год
    r"(\*)?"                                   # звёздочка у СНиП
)
VENDOR_WORDS = re.compile(
    r"консультант\s*плюс|консультантплюс|техэксперт|tehexpert|docs\.cntd|кодекс|"
    r"нормакс|normacs|гарант|gusk?n|стройинф|stroyinf", re.I)


def versionless(designation: str) -> str:
    """«ГОСТ 31937-2024» -> «ГОСТ 31937»; «СП 22.13330.2016» -> «СП 22.13330»;
    «СНиП 2.02.01-83*» -> «СНиП 2.02.01»."""
    s = designation.strip().rstrip("*").strip()
    # отрезать год: «.2016», «-2024», « 2016»
    s = re.sub(r"[-–.\s](?:19|20)\d{2}$", "", s)
    # «СНиП 2.02.01-83» -> «СНиП 2.02.01»: отрезать хвостовой -NN ТОЛЬКО у старой
    # формы с точечным номером. «СНиП 12-03-2001» -> «СНиП 12-03» (не «СНиП 12»).
    s = re.sub(r"(\d+\.\d[\d.]*)[-–]\d{2,3}$", r"\1", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def note_name(designation: str) -> str:
    """Имя файла заметки: безопасно для ФС и Obsidian, но читаемо (кириллица)."""
    s = designation.strip()
    s = re.sub(r'[\\/:*?"<>|]', "-", s)   # запрещённые в именах файлов символы
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" .")


def build_aliases(designation: str, superseded: list | None = None) -> list:
    """Алиасы заметки: полная форма, без года, и обозначения заменённых документов."""
    al = []
    full = designation.strip()
    vl = versionless(full)
    for a in (full, vl):
        if a and a not in al:
            al.append(a)
    for s in (superseded or []):
        s = s.strip()
        if s and s not in al:
            al.append(s)
        sv = versionless(s)
        if sv and sv not in al:
            al.append(sv)
    return al


# --------------------------------------------------------------------------- #
# реестр актуальных версий
# --------------------------------------------------------------------------- #
class VersionRegistry:
    """Соответствие «беспроигрышное обозначение -> актуальная заметка + её алиас».

    Заполняется из каталога базы (что уже скачано) и из внешнего реестра
    (ФГИС СТ / ФЦС) — этим занимается локальный агент. Пустой реестр не ломает
    конвертацию: ссылки становятся «висячими» `[[ГОСТ 27751]]`, что в Obsidian
    нормально (красная ссылка = «документ ещё не добавлен»).
    """

    def __init__(self, mapping: dict | None = None):
        # ключ — versionless, значение — {"note": имя заметки, "alias": беспроигрышный алиас}
        self.map: dict = {}
        for k, v in (mapping or {}).items():
            self.add(k, v)

    def add(self, base: str, note: str):
        base = versionless(base)
        self.map[base] = {"note": note_name(note), "alias": versionless(note)}
        # замена семейства (СНиП -> СП): старое обозначение тоже ведёт на новую заметку
        self.map.setdefault(note, self.map[base])

    def resolve(self, designation: str) -> str:
        """Вернуть алиас, на который должна вести ссылка (versionless актуальной версии)."""
        base = versionless(designation)
        hit = self.map.get(base)
        if hit:
            return hit["alias"]
        return base   # нет в реестре -> версия без года того, что написано


def load_registry(path: str) -> VersionRegistry:
    reg = VersionRegistry()
    if not path or not os.path.exists(path):
        return reg
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception:
        data = _tiny_yaml(open(path, encoding="utf-8").read())
    for base, note in (data.get("версии") or data.get("versions") or data).items():
        if isinstance(note, str):
            reg.add(base, note)
        elif isinstance(note, dict) and note.get("актуальный"):
            reg.add(base, note["актуальный"])
    for repl in (data.get("замены") or data.get("replacements") or []):
        if isinstance(repl, dict) and repl.get("старый") and repl.get("новый"):
            reg.map[versionless(repl["старый"])] = {
                "note": note_name(repl["новый"]), "alias": versionless(repl["новый"])}
    return reg


def _tiny_yaml(text: str) -> dict:
    out, cur = {}, None
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = raw.strip()
        if line.startswith("- "):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v:
                out[k] = v.strip('"')
            else:
                out[k] = {}
                cur = k
    return out


# --------------------------------------------------------------------------- #
# очистка от вендорских пометок
# --------------------------------------------------------------------------- #
VENDOR_LINE_RE = re.compile(
    r"^\s*(?:>\s*)?(?:документ предоставлен|информация о документе|"
    r"подготовлен с использованием|ссылка на документ предоставлен)", re.I)

# «КонсультантПлюс: примечание. В официальном тексте документа, видимо, допущена
#  опечатка: X»  ->  «> [!warning] Опечатка в официальном тексте\n> X»
CP_NOTE_RE = re.compile(
    r"(?:^|\n)\s*(?:>\s*)?(?:\[!note\]\s*)?КонсультантПлюс:\s*примечание\.\s*", re.I)
TYPO_RE = re.compile(
    r"В официальном тексте документа,?\s*видимо,?\s*допущена опечатка:?\s*", re.I)
FORMULA_OFFICIAL_RE = re.compile(
    r"(Формул\w+|Обозначени\w+|Текст|Нумерация)\s+дан\w+\s+в соответствии с официальным текстом[^.\n]*\.?", re.I)


def clean_vendor_notes(text: str, report=None) -> str:
    """Привести редакционные примечания СПС к нейтральному виду без названия вендора."""
    n = {"typo": 0, "official": 0, "note": 0, "inline": 0}

    def repl_note(m):
        return "\n> [!note] "

    # сначала — «опечатка» (самый содержательный случай)
    def repl_typo(m):
        n["typo"] += 1
        return "\n> [!warning] Опечатка в официальном тексте\n> "
    text = re.sub(r"(?:^|\n)\s*(?:>\s*)?(?:\[!note\]\s*)?КонсультантПлюс:\s*примечание\.\s*"
                  r"В официальном тексте документа,?\s*видимо,?\s*допущена опечатка:?\s*",
                  repl_typo, text, flags=re.I)

    # «Формула/Обозначения даны в соответствии с официальным текстом»
    def repl_official(m):
        n["official"] += 1
        return "\n> [!note] %s — по официальному тексту документа" % m.group(1)
    text = re.sub(r"(?:^|\n)\s*(?:>\s*)?(?:\[!note\]\s*)?КонсультантПлюс:\s*примечание\.\s*"
                  r"(Формул\w+|Обозначени\w+|Текст|Нумерация)\s+дан\w+\s+в соответствии с "
                  r"официальным текстом[^.\n]*\.?", repl_official, text, flags=re.I)

    # прочие «КонсультантПлюс: примечание. X» -> нейтральный callout
    text = re.sub(r"(?:^|\n)\s*(?:>\s*)?(?:\[!note\]\s*)?КонсультантПлюс:\s*примечание\.\s*",
                  lambda m: (n.__setitem__("note", n["note"] + 1), "\n> [!note] ")[1],
                  text, flags=re.I)

    # вендорное «примечание» внутри строк и HTML-экранированных вложенных таблиц
    # (кейс СП 56: «&lt;td&gt;КонсультантПлюс: примечание. Текст … дан в
    #  соответствии с официальным текстом документа.») — обезличиваем на месте
    def repl_inline(m):
        n["inline"] += 1
        return "Примечание. "
    text = re.sub(r"(?:Консультант\s*Плюс|КонсультантПлюс|Техэксперт|Кодекс)\s*:\s*"
                  r"примечание\.\s*", repl_inline, text, flags=re.I)

    # остаточные упоминания вендора в alt (в т.ч. HTML-экранированные внутри таблиц)
    text = re.sub(r'alt="[^"]*(?:Консультант\s*Плюс|КонсультантПлюс|Техэксперт)[^"]*"',
                  'alt=""', text, flags=re.I)
    text = re.sub(r'alt=&quot;[^&]*(?:Консультант\s*Плюс|КонсультантПлюс|Техэксперт)[^&]*&quot;',
                  'alt=&quot;&quot;', text, flags=re.I)
    # Кодекс: «Изменение N 1 внесено изготовителем базы данных по тексту ИУС N 4, 2023»
    # — факт (ИУС) остаётся, упоминание изготовителя базы уходит
    text = re.sub(r"\s*изготовител\w+ базы данных", "", text, flags=re.I)

    if report is not None:
        report.add_issue("info", "vendor_notes_cleaned",
                         "редакционных примечаний СПС обезличено: опечатки=%d, «по офиц. тексту»=%d, "
                         "прочие=%d, inline=%d"
                         % (n["typo"], n["official"], n["note"], n["inline"]))
    return text


def strip_vendor_lines(text: str, report=None) -> str:
    """Удалить служебные строки справочных систем (заголовок/подвал экспорта)."""
    out, removed = [], 0
    for line in text.split("\n"):
        if VENDOR_LINE_RE.match(line) or re.search(
                r"(Документ предоставлен|Информация о документе)\s*(КонсультантПлюс|Консультант Плюс|Техэксперт)",
                line, re.I):
            removed += 1
            continue
        # Кодекс/Техэксперт: «… см. по ссылке. - Примечание изготовителя базы данных.»
        if re.search(r"Примечание изготовителя базы данных", line, re.I):
            removed += 1
            continue
        if re.search(r"Текст\s+сравнени\w+[^.\n]{0,120}см\.\s+по ссылке", line, re.I):
            removed += 1
            continue
        # скрытый колонтитул СПС «Кодекс» (прямые/ёлочки-кавычки, «см. в ярлыке»,
        # склейка нескольких служебных фраз в одну строку — кейс ГОСТ 27751-2014)
        if re.search(r"ИС\s*[\"«']?\s*Кодекс", line, re.I) \
                or re.search(r"см\.\s+(?:в\s+)?ярлык\w*\s*[\"«']?\s*Примечания", line, re.I) \
                or re.search(r"Внимание!\s+(?:Документ включен в доказательную базу|"
                             r"О порядке применения документа)", line, re.I) \
                or re.search(r"подготовлен\w*[^.\n]{0,60}(?:АО|ЗАО|НТЦ)\s*[\"«']?\s*"
                             r"(?:Кодекс|Техэксперт|Консультант)", line, re.I):
            removed += 1
            continue
        out.append(line)
    if report is not None and removed:
        report.add_issue("info", "vendor_lines_removed",
                         "удалено служебных строк СПС: %d" % removed)
    return "\n".join(out)


def normalize_amendment_markers(text: str, report=None) -> str:
    """Маркеры изменений — оставить факт, но обезличить и привести к единому виду.

    «(абзац введен Изменением N 2, утв. Приказом Минстроя России от 17.12.2021 N 956/пр)»
      ->  курсивная сноска без ссылки на отдельную заметку (изменения не отдельные объекты):
    «*(абзац введён Изменением N 2, утв. Приказом Минстроя России от 17.12.2021 N 956/пр)*»
    """
    count = {"n": 0}

    def repl(m):
        count["n"] += 1
        inner = m.group(0).strip()
        inner = inner.replace("введен ", "введён ").replace("введена ", "введена ")
        inner = re.sub(r"Изм\.\s*N\s*(\d+)", r"Изменение N \1", inner, flags=re.I)
        inner = re.sub(r"\s{2,}", " ", inner)
        # снять уже имеющийся курсив/жирный, обернуть целиком
        inner = inner.strip("*_")
        return "*%s*" % inner

    # маркеры вида «(в ред. Изменения N …)», «(абзац введён Изменением N …)»,
    # «(пункт в редакции Изменения N …)», «(Измененная редакция, Изм. N …)» —
    # в круглых скобках, в конце абзаца
    text = re.sub(
        r"\((?:измененн\w+\s+редакци\w+|в ред\.|абзац|пункт|подпункт|таблиц\w+|приложени\w+|"
        r"в редакции|дополнен|исключен|введён|введен)[^()\n]{0,160}?(?:Изменени|Изм\.)[^()\n]{0,120}?\)",
        repl, text, flags=re.I)
    if report is not None and count["n"]:
        report.add_issue("info", "amendment_markers",
                         "маркеров изменений нормализовано: %d" % count["n"])
    return text


# --------------------------------------------------------------------------- #
# вики-ссылки
# --------------------------------------------------------------------------- #
def wikilink_refs(text: str, registry: VersionRegistry, report=None,
                  self_designation: str = "") -> str:
    """Заменить упоминания других документов на `[[алиас]]` / `[[алиас|как написано]]`.

    Не трогает: frontmatter, формулы ($…$, $$…$$), код, уже существующие `[[…]]`,
    заголовки и строки списка «Нормативные ссылки» (там designation остаётся видимым,
    но становится ссылкой с отображением полного обозначения).
    """
    self_vl = versionless(self_designation) if self_designation else ""
    stats = {"linked": 0, "resolved": 0, "self": 0}

    def make(m):
        prefix, number, year, star = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        written = "%s %s%s%s" % (prefix, number, ("-" + year) if year else "", star)
        written = re.sub(r"\s{2,}", " ", written).strip()
        base = versionless(written)
        if self_vl and base == self_vl:
            stats["self"] += 1
            return written                       # на саму себя не ссылаемся
        alias = registry.resolve(written)
        stats["linked"] += 1
        if alias == base:
            # та же familia, ссылка на актуальную версию -> versionless без pipe
            if year or star:
                stats["resolved"] += 1
            return "[[%s]]" % alias
        # замена семейства (СНиП -> СП): вести на актуальный документ, но показать,
        # что именно цитировал источник
        stats["resolved"] += 1
        return "[[%s|%s]]" % (alias, written.rstrip("*").strip())

    # защищаем формулы и код от подстановки
    protected = []

    def stash(m):
        protected.append(m.group(0))
        return "\x00%d\x00" % (len(protected) - 1)

    guarded = re.sub(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$|```[\s\S]*?```|`[^`\n]+`|\[\[[^\]]+\]\]",
                     stash, text)

    # не ссылаться внутри строк-заголовков и внутри уже размеченного
    out_lines = []
    for line in guarded.split("\n"):
        if line.lstrip().startswith("#") or line.lstrip().startswith("---"):
            out_lines.append(line)
            continue
        out_lines.append(DESIG_RE.sub(make, line))
    guarded = "\n".join(out_lines)

    result = re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], guarded)
    if report is not None:
        report.add_issue("info", "wikilinks",
                         "вики-ссылок проставлено: %d (из них с резолвингом на актуальную версию: %d)"
                         % (stats["linked"], stats["resolved"]))
    return result


# --------------------------------------------------------------------------- #
# сводная обработка готового Markdown
# --------------------------------------------------------------------------- #
def split_frontmatter(md_text: str):
    if md_text.startswith("---"):
        end = md_text.find("\n---", 3)
        if end > 0:
            nl = md_text.find("\n", end + 1)
            return md_text[:nl + 1], md_text[nl + 1:]
    return "", md_text


def postprocess_obsidian(md_text: str, meta: dict, registry: VersionRegistry,
                         report=None) -> str:
    """Прогнать готовый канонический Markdown через Obsidian-преобразования.

    Frontmatter не трогается (иначе `designation: СП 22.13330.2016` превратился бы
    в вики-ссылку и сломал YAML).
    """
    fm, body = split_frontmatter(md_text)
    body = clean_vendor_notes(body, report)
    body = strip_vendor_lines(body, report)
    body = normalize_amendment_markers(body, report)
    body = wikilink_refs(body, registry, report,
                         self_designation=meta.get("designation", ""))
    return fm + body


def apply_glyph_rules(md: str, glyphs_path: str, report=None) -> str:
    """Словарь символов-картинок (docs/08): литеральные замены «дыр» на формулы.

    YAML — список правил {sha256, find, repl, note}: `find` — литеральный текст
    с пробелом на месте выпавшей картинки-символа (или тег <img> для docx),
    `repl` — текст с подстановкой ($\\gamma_f$, м² …). sha256 фиксирует, из
    какого изображения взялся символ. Ноль применений правила — warning
    (вёрстка уехала, правило чинит человек).

    Функция общая для docx2md и pdf2md; живёт здесь, чтобы не тянуть pymupdf.
    """
    import yaml as _yaml
    with open(glyphs_path, encoding="utf-8") as fh:
        rules = _yaml.safe_load(fh) or []
    for r in rules:
        find, repl = r.get("find", ""), r.get("repl", "")
        if not find:
            continue
        n = md.count(find)
        if n:
            md = md.replace(find, repl)
        if report is not None:
            report.add_issue("info" if n else "warning", "glyph_substitution",
                             "символ %s: замен %d (%s)" % (r.get("sha256", "?")[:8], n,
                                                           r.get("note", find[:40])))
    return md
