# -*- coding: utf-8 -*-
"""Сверка cntd-sp22.html (docs.cntd.ru) с заметкой СП 22.13330.2016.md по номерам пунктов."""
import re, html as htmllib, difflib, json

HTML = 'база/источники/cntd-sp22.html'
MD = 'база/документы/СП 22.13330.2016.md'
OUT = 'наработки/сп22-cntd-сверка.md'

EXCLUDED = {'4.18', '13.5'} | {f'10.{i}' for i in range(10, 22)}  # Изм. 4/5

def norm(s):
    s = htmllib.unescape(s)
    for a, b in [('−', '-'), ('–', '-'), ('\u00a0', ' '), ('«', ''), ('»', ''),
                 ('“', '"'), ('”', '"'), ('’', "'"), ('‘', "'"), ('\u202f', ' ')]:
        s = s.replace(a, b)
    return re.sub(r'\s+', ' ', s).strip().lower()

# ---------- HTML: <p id="P####"> → текст ----------
raw = open(HTML, encoding='utf-8').read()
paras = re.findall(r'<p[^>]*id="(P[0-9A-Fa-f]+(?:_\d+)?)"[^>]*>(.*?)</p>', raw, re.S)
html_clauses = {}   # num -> (pid, text)
for pid, body in paras:
    txt = re.sub(r'<img[^>]*>', '', body)          # картинки выкидываем
    txt = re.sub(r'<[^>]+>', ' ', txt)             # теги → пробел
    txt = htmllib.unescape(txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    m = re.match(r'^(\d+(?:\.\d+){1,4})\.?\s', txt + ' ')
    if not m:
        continue
    num = m.group(1)
    text = txt[m.end():].strip()
    if num in html_clauses and html_clauses[num][0] != pid:
        # дубликат номера (подпункты/повторы) — не трогаем основной
        pass
    else:
        html_clauses[num] = (pid, text)

# ---------- MD: строки "N.N.N текст ^c..." + заголовки + термины в > ----------
md_clauses = {}
def add_md(num, text):
    num = num.strip().rstrip('.')
    if re.fullmatch(r'\d+(?:\.\d+){1,4}', num) and text:
        md_clauses.setdefault(num, text)
for line in open(MD, encoding='utf-8').read().splitlines():
    m = re.match(r'^(\d+(?:\.\d+){1,4})\s+(.*?)(?:\s+\^c[\w\-]+)?\s*$', line)
    if m:
        add_md(m.group(1), m.group(2).strip())
        continue
    m = re.match(r'^#{2,3}\s+(\d+(?:\.\d+){1,4})[.\s]+(.*)$', line)
    if m:
        add_md(m.group(1), m.group(2).strip())
        continue
    m = re.match(r'^>\s*\*\*(\d+(?:\.\d+){1,4})\*\*\s*.*?[:：]\s*(.*?)(?:\s+\^c[\w\-]+)?\s*$', line)
    if m:
        add_md(m.group(1), m.group(2).strip())

# ---------- сравнение ----------
only_html, only_md, differ = [], [], []
for num in sorted(html_clauses, key=lambda n: [int(x) for x in n.split('.')]):
    if num not in md_clauses:
        only_html.append((num, html_clauses[num][1]))
for num in sorted(md_clauses, key=lambda n: [int(x) for x in n.split('.')]):
    if num not in html_clauses:
        only_md.append((num, md_clauses[num]))

common = set(html_clauses) & set(md_clauses)
diffs = []
for num in common:
    a, b = norm(html_clauses[num][1]), norm(md_clauses[num])
    if a != b:
        r = difflib.SequenceMatcher(None, a, b).ratio()
        diffs.append((r, num))
diffs.sort()

# ---------- отчёт ----------
L = []
L.append('# Сверка СП 22: cntd-sp22.html ↔ база/документы/СП 22.13330.2016.md\n')
L.append(f'- HTML-пунктов (с номером): {len(html_clauses)}; MD-пунктов: {len(md_clauses)}')
L.append(f'- Общие номера: {len(common)}; только в HTML: {len(only_html)}; только в MD: {len(only_md)}; текст различается: {len(diffs)}\n')

excl_h = [x for x in only_html if x[0] in EXCLUDED]
real_h = [x for x in only_html if x[0] not in EXCLUDED]
L.append(f'## Только в HTML ({len(only_html)}, из них исключённые Изм.4/5: {len(excl_h)})\n')
for num, t in real_h:
    L.append(f'- **{num}** {t[:120]}')
if excl_h:
    L.append(f'\n(исключены из заметки штатно: {", ".join(n for n,_ in sorted(excl_h))})\n')

L.append(f'## Только в MD ({len(only_md)})\n')
for num, t in only_md:
    L.append(f'- **{num}** {t[:120]}')

L.append(f'\n## Текст различается ({len(diffs)}, по возрастанию сходства)\n')
for r, num in diffs:
    a = html_clauses[num][1][:150]
    b = md_clauses[num][:150]
    L.append(f'### {num} (sim={r:.2f})\n- HTML: {a}\n- MD:   {b}\n')

open(OUT, 'w', encoding='utf-8').write('\n'.join(L))
print('report ->', OUT)
print(json.dumps({'html': len(html_clauses), 'md': len(md_clauses), 'common': len(common),
                  'only_html': len(only_html), 'excl_in_only_html': len(excl_h),
                  'only_md': len(only_md), 'differ': len(diffs)}, ensure_ascii=False))
