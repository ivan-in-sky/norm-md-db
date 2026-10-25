"""Generate a synthetic .docx that mimics the *hard* parts of a real СП/ГОСТ:
numbered headings, paragraph lists, a table with merged cells, OMML formulas,
a figure with caption, a footnote, a cross-reference field and a «Примечание».

Used to smoke-test normconv without shipping copyrighted documents.
"""
import base64
import io
import sys

from PIL import Image, ImageDraw
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def m_el(tag, **attrs):
    e = OxmlElement("m:" + tag)
    for k, v in attrs.items():
        e.set(qn("m:" + k), v)
    return e


def m_run(text, sty=None):
    r = m_el("r")
    if sty:
        rpr = m_el("rPr")
        rpr.append(m_el("sty", val=sty))
        r.append(rpr)
    t = m_el("t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    return r


def frac(num_els, den_els):
    f = m_el("f")
    f.append(m_el("fPr"))
    num = m_el("num")
    for e in num_els:
        num.append(e)
    den = m_el("den")
    for e in den_els:
        den.append(e)
    f.append(num)
    f.append(den)
    return f


def ssub(base_els, sub_els):
    s = m_el("sSub")
    s.append(m_el("sSubPr"))
    e = m_el("e")
    for x in base_els:
        e.append(x)
    sb = m_el("sub")
    for x in sub_els:
        sb.append(x)
    s.append(e)
    s.append(sb)
    return s


def ssup(base_els, sup_els):
    s = m_el("sSup")
    s.append(m_el("sSupPr"))
    e = m_el("e")
    for x in base_els:
        e.append(x)
    sp = m_el("sup")
    for x in sup_els:
        sp.append(x)
    s.append(e)
    s.append(sp)
    return s


def rad(deg_els, body_els):
    r = m_el("rad")
    rpr = m_el("radPr")
    rpr.append(m_el("degHide", val="0"))
    r.append(rpr)
    deg = m_el("deg")
    for x in deg_els:
        deg.append(x)
    e = m_el("e")
    for x in body_els:
        e.append(x)
    r.append(deg)
    r.append(e)
    return r


def nary(chr_, sub_els, sup_els, body_els, lim_loc="subSup"):
    n = m_el("nary")
    npr = m_el("naryPr")
    npr.append(m_el("chr", val=chr_))
    npr.append(m_el("limLoc", val=lim_loc))
    n.append(npr)
    sb = m_el("sub")
    for x in sub_els:
        sb.append(x)
    sp = m_el("sup")
    for x in sup_els:
        sp.append(x)
    e = m_el("e")
    for x in body_els:
        e.append(x)
    n.append(sb)
    n.append(sp)
    n.append(e)
    return n


def func(name, arg_els):
    f = m_el("func")
    f.append(m_el("funcPr"))
    fn = m_el("fName")
    fn.append(m_run(name, sty="p"))
    e = m_el("e")
    for x in arg_els:
        e.append(x)
    f.append(fn)
    f.append(e)
    return f


def delim(beg, end, inner_els):
    d = m_el("d")
    dpr = m_el("dPr")
    dpr.append(m_el("begChr", val=beg))
    dpr.append(m_el("endChr", val=end))
    d.append(dpr)
    e = m_el("e")
    for x in inner_els:
        e.append(x)
    d.append(e)
    return d


def oMath(*els, para=False):
    if para:
        p = m_el("oMathPara")
        om = m_el("oMath")
        for e in els:
            om.append(e)
        p.append(om)
        return p
    om = m_el("oMath")
    for e in els:
        om.append(e)
    return om


def add_math_paragraph(doc, *els):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p._p.append(oMath(*els, para=True))
    return p


def add_inline_math(par, *els):
    par._p.append(oMath(*els))


def make_figure(path):
    img = Image.new("RGB", (640, 260), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 620, 240], outline="black", width=2)
    d.line([20, 130, 620, 130], fill="black", width=1)
    d.text((40, 60), "A = 1200", fill="black")
    d.text((40, 160), "h = 300", fill="black")
    img.save(path)


def build(docx_path, asset_png):
    make_figure(asset_png)

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)

    # ---- title block -----------------------------------------------------
    p = doc.add_paragraph()
    try:
        p.style = doc.styles["Title"]
    except Exception:
        pass
    p.add_run("СВОД ПРАВИЛ")
    p = doc.add_paragraph("СП 999.13330.2026")
    p = doc.add_paragraph("КОНСТРУКЦИИ ЖЕЛЕЗОБЕТОННЫЕ")
    p = doc.add_paragraph("Тестовый документ для проверки конвейера normconv")
    p = doc.add_paragraph("Издание официальное")
    p = doc.add_paragraph("Москва 2026")
    doc.add_paragraph()

    # ---- область применения ---------------------------------------------
    doc.add_heading("Область применения", level=1)
    doc.add_paragraph(
        "1.1 Настоящий свод правил распространяется на железобетонные конструкции "
        "зданий и сооружений и устанавливает требования к их расчёту и конструированию."
    )
    par = doc.add_paragraph("1.2 Расчёт прочности нормального сечения выполняется по формуле ")
    add_inline_math(
        par,
        m_run("M"),
        m_run("\u2264"),
        frac([m_run("R")], [m_run("b")]),
        m_run("\u22c5"),
        ssub([m_run("A")], [m_run("s")]),
        m_run("\u22c5"),
        ssub([m_run("h")], [m_run("0")]),
    )
    par.add_run(" , где ")
    add_inline_math(par, ssub([m_run("h")], [m_run("0")]))
    par.add_run(" — рабочая высота сечения.")

    # ---- нумерованные пункты ---------------------------------------------
    doc.add_heading("Расчёт по деформациям", level=1)
    doc.add_heading("Общие положения", level=2)
    doc.add_paragraph(
        "3.1.1 Прогибы элементов не должны превышать предельных значений, "
        "установленных в таблице 1."
    )
    doc.add_paragraph(
        "3.1.2 Для элементов с трещинами в растянутой зоне жёсткость определяется "
        "по формуле"
    )
    add_math_paragraph(
        doc,
        ssub([m_run("B")], [m_run("red")]),
        m_run("="),
        frac(
            [ssub([m_run("M")], [m_run("crc")])],
            [m_run("\u03c8"), ssub([m_run("M")], [m_run("tot")])],
        ),
        m_run("\u22c5"),
        ssub([m_run("B")], [m_run("cr")]),
        m_run("+"),
        delim(
            "(",
            ")",
            [m_run("1"), m_run("\u2212"),
             frac([ssub([m_run("M")], [m_run("crc")])],
                  [ssub([m_run("M")], [m_run("tot")])])],
        ),
        m_run("\u22c5"),
        ssub([m_run("B")], [m_run("red2")]),
    )
    doc.add_paragraph("где")
    for txt in [
        None,
    ]:
        pass
    # «где» list with formulas
    par = doc.add_paragraph()
    par.paragraph_format.left_indent = Cm(1.0)
    add_inline_math(par, ssub([m_run("M")], [m_run("crc")]))
    par.add_run(" — момент образования трещин;")
    par = doc.add_paragraph()
    par.paragraph_format.left_indent = Cm(1.0)
    add_inline_math(par, ssub([m_run("M")], [m_run("tot")]))
    par.add_run(" — полный момент от внешних нагрузок.")

    doc.add_paragraph(
        "3.1.3 Суммарный прогиб определяется интегрированием кривизны:"
    )
    add_math_paragraph(
        doc,
        m_run("f"),
        m_run("="),
        nary(
            "\u222b",
            [m_run("0")],
            [m_run("l")],
            [frac([m_run("M"), m_run("("), m_run("x"), m_run(")")],
                  [ssub([m_run("B")], [m_run("red")])])],
        ),
        m_run("\u22c5"),
        m_run("\u03c6"),
        m_run("("),
        m_run("x"),
        m_run(")"),
        m_run("\u2009"),
        m_run("d"),
        m_run("x"),
    )
    doc.add_paragraph("3.1.4 Коэффициент ")
    par = doc.paragraphs[-1]
    add_inline_math(par, m_run("\u03c8"))
    par.add_run(" принимается по таблице 2 в зависимости от ")
    add_inline_math(par, rad([m_run("3")], [m_run("R")]))
    par.add_run(".")

    # ---- таблица с объединёнными ячейками ---------------------------------
    doc.add_paragraph("Таблица 1 — Предельные прогибы элементов")
    tbl = doc.add_table(rows=4, cols=4)
    tbl.style = "Table Grid"
    # header row with horizontal merge
    hdr = tbl.rows[0]
    hdr.cells[0].text = "Элемент"
    hdr.cells[1].merge(hdr.cells[2])
    hdr.cells[1].text = "Предельный прогиб, мм"
    hdr.cells[3].text = "Примечание"
    # vertical merge in column 0
    tbl.rows[1].cells[0].text = "Балки и плиты"
    tbl.rows[2].cells[0].merge(tbl.rows[1].cells[0])
    tbl.rows[1].cells[1].text = "при l < 6 м"
    tbl.rows[1].cells[2].text = "1/200 l"
    tbl.rows[1].cells[3].text = "—"
    tbl.rows[2].cells[1].text = "при l ≥ 6 м"
    tbl.rows[2].cells[2].text = "30"
    tbl.rows[2].cells[3].text = "но не более 1/250 l"
    tbl.rows[3].cells[0].text = "Ригели"
    tbl.rows[3].cells[1].text = "—"
    tbl.rows[3].cells[2].text = "20"
    tbl.rows[3].cells[3].text = "см. 3.1.2"

    doc.add_paragraph()
    note = doc.add_paragraph()
    note.add_run("П р и м е ч а н и е — ").bold = True
    note.add_run("Для элементов с l > 7,5 м прогиб уточняется расчётом.")

    # ---- рисунок ---------------------------------------------------------
    doc.add_picture(asset_png, width=Cm(12))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap = doc.add_paragraph("Рисунок 1 — Схема сечения элемента")
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ---- сноска ----------------------------------------------------------
    par = doc.add_paragraph(
        "4.1 Армирование выполняется в соответствии с требованиями настоящего свода правил"
    )
    run = par.add_run("")
    fld = OxmlElement("w:footnoteReference")
    fld.set(qn("w:id"), "1")
    run._r.append(fld)

    # ---- перекрёстная ссылка (REF field) ---------------------------------
    par = doc.add_paragraph("4.2 Требования к защитному слою приведены в ")
    r = par.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = ' REF _Ref12345 \\h '
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    res = OxmlElement("w:t")
    res.text = "пункте 3.1.2"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    r._r.append(fld_begin)
    r._r.append(instr)
    r._r.append(fld_sep)
    r._r.append(res)
    r._r.append(fld_end)
    par.add_run(" .")

    doc.add_heading("Требования к материалам", level=1)
    doc.add_paragraph(
        "5.1 Класс бетона по прочности на сжатие должен быть не ниже B25 для "
        "конструкций, эксплуатируемых при температуре ниже минус 40 °C."
    )
    doc.add_paragraph(
        "5.2 Значения расчётных сопротивлений приведены в приложении Б "
        "(см. также ГОСТ 26633 и СП 63.13330.2018, пункт 6.1)."
    )

    # ---- footnote part ----------------------------------------------------
    fn_part = doc.part.package.part_related_by(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
    ) if False else None

    doc.save(docx_path)
    # add footnotes.xml manually (python-docx has no API for it)
    add_footnotes(docx_path)


def add_footnotes(docx_path):
    import zipfile
    import shutil

    footnotes_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
             xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>
  <w:footnote w:id="1"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>
    <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r>
    <w:r><w:t xml:space="preserve"> В редакции изменения N 1, введённого приказом Минстроя России.</w:t></w:r>
  </w:p></w:footnote>
</w:footnotes>"""

    tmp = docx_path + ".tmp"
    with zipfile.ZipFile(docx_path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = data  # unchanged
            if item.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>',
                )
            if item.filename == "word/_rels/document.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rIdFoot1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>',
                )
            zout.writestr(item, data)
        zout.writestr("word/footnotes.xml", footnotes_xml)
    shutil.move(tmp, docx_path)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample_sp.docx"
    build(out, "/tmp/fig1.png")
    print("built", out)
