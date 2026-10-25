"""
OMML (Office Math Markup Language, `m:` namespace inside .docx) -> LaTeX.

Pandoc does this through an XSLT (omml2mml.xsl -> MathML -> texmath). We do it
directly, which means: no XSLT dependency, we keep control over the output
style (KaTeX-friendly), and -- most importantly -- we can report *confidence*
per formula, so that low-confidence formulas are flagged for manual review and
kept as images instead of silently producing wrong LaTeX.

Only the OMML subset actually used by normative documents (СП/ГОСТ/СНиП) is
mapped 1:1. Everything else is rendered literally + counted as "unknown", which
drives the confidence score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from lxml import etree

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def m(tag: str) -> str:
    return "{%s}%s" % (M, tag)


def w(tag: str) -> str:
    return "{%s}%w" % (W, tag)


# --------------------------------------------------------------------------- #
# mappings
# --------------------------------------------------------------------------- #

NARY_MAP = {
    "\u2211": "\\sum",
    "\u220f": "\\prod",
    "\u222b": "\\int",
    "\u222e": "\\oint",
    "\u22c3": "\\bigcup",
    "\u22c2": "\\bigcap",
    "\u22c0": "\\bigwedge",
    "\u22c1": "\\bigvee",
    "sum": "\\sum",
    "prod": "\\prod",
    "int": "\\int",
    "contInt": "\\oint",
    "union": "\\bigcup",
    "intersect": "\\bigcap",
    "and": "\\bigwedge",
    "or": "\\bigvee",
    "limLow": "\\lim",
    "limUpp": "\\lim",
}

# operators whose limits go above/below (with \limits), not to the side
LIMITS_BELOW = {"\\sum", "\\prod", "\\bigcup", "\\bigcap", "\\bigwedge", "\\bigvee", "\\lim"}

FUNC_MAP = {
    "sin": "\\sin",
    "cos": "\\cos",
    "tan": "\\tan",
    "cot": "\\cot",
    "sec": "\\sec",
    "csc": "\\csc",
    "arcsin": "\\arcsin",
    "arccos": "\\arccos",
    "arctan": "\\arctan",
    "sinh": "\\sinh",
    "cosh": "\\cosh",
    "tanh": "\\tanh",
    "log": "\\log",
    "ln": "\\ln",
    "lg": "\\lg",
    "exp": "\\exp",
    "max": "\\max",
    "min": "\\min",
    "lim": "\\lim",
    "sup": "\\sup",
    "inf": "\\inf",
    "det": "\\det",
    "dim": "\\dim",
    "mod": "\\bmod",
    "gcd": "\\gcd",
}

ACCENT_MAP = {
    "\u0302": "\\hat",        # combining circumflex
    "\u0303": "\\tilde",      # combining tilde
    "\u0304": "\\bar",        # combining macron
    "\u0305": "\\overline",
    "\u0306": "\\breve",      # combining breve
    "\u0307": "\\dot",
    "\u0308": "\\ddot",
    "\u20d7": "\\vec",        # combining right arrow above
    "\u005e": "\\hat",
    "\u007e": "\\tilde",
    "\u00af": "\\bar",
    "\u02d9": "\\dot",
    "\u00a8": "\\ddot",
    "\u2192": "\\vec",
}

DELIM_MAP = {
    "(": "(",
    ")": ")",
    "[": "[",
    "]": "]",
    "{": "\\{",
    "}": "\\}",
    "|": "|",
    "\u2016": "\\|",
    "\u27e8": "\\langle",
    "\u27e9": "\\rangle",
    "\u230a": "\\lfloor",
    "\u230b": "\\rfloor",
    "\u2308": "\\lceil",
    "\u2309": "\\rceil",
    "": ".",
    "\u2062": ".",
}

MATRIX_ENV = {
    "(": "pmatrix",
    "[": "bmatrix",
    "{": "Bmatrix",
    "|": "vmatrix",
    "\u2016": "Vmatrix",
}

GREEK_LATex = {}  # placeholder, greek is passed through as unicode (KaTeX ok)


# --------------------------------------------------------------------------- #
# converter
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    known: int = 0
    unknown: int = 0
    unknown_tags: list = field(default_factory=list)
    fallbacks: list = field(default_factory=list)

    @property
    def confidence(self) -> float:
        total = self.known + self.unknown
        if total == 0:
            return 1.0
        return round(self.known / total, 3)


class Omml2Latex:
    def __init__(self, stats: Stats | None = None):
        self.stats = stats or Stats()

    # -- helpers ----------------------------------------------------------- #

    def _unknown(self, el) -> str:
        tag = etree.QName(el).localname if isinstance(el.tag, str) else str(el.tag)
        self.stats.unknown += 1
        if tag not in self.stats.unknown_tags:
            self.stats.unknown_tags.append(tag)
        return ""

    def _known(self) -> None:
        self.stats.known += 1

    def _plain(self, s: str) -> str:
        """Escape a literal math/text run for LaTeX."""
        out = []
        for ch in s:
            if ch in "%&#_$":
                out.append("\\" + ch)
            elif ch == "\\":
                out.append("\\textbackslash{}")
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _is_simple(s: str) -> bool:
        return len(s.strip()) <= 1

    def _braced(self, s: str) -> str:
        s = s.strip()
        if s == "":
            return "{}"
        if self._is_simple(s):
            return s
        return "{" + s + "}"

    @staticmethod
    def _first_text(el) -> str:
        if el is None:
            return ""
        for t in el.iter(m("t")):
            if t.text:
                return t.text.strip()
        return (el.text or "").strip()

    # -- entry points ------------------------------------------------------ #

    def convert(self, omath) -> str:
        """Convert one <m:oMath> (inline) or <m:oMathPara> (display) element."""
        if omath is None:
            return ""
        if etree.QName(omath).localname == "oMathPara":
            parts = [self.conv(child) for child in omath if isinstance(child.tag, str)]
            body = " ".join(p for p in parts if p.strip())
            # multiple <m:oMath> inside one oMathPara => aligned block
            n_eq = len([c for c in omath if etree.QName(c).localname == "oMath"])
            if n_eq > 1:
                return "\\begin{aligned} " + " \\\\ ".join(p for p in parts if p.strip()) + " \\end{aligned}"
            return body
        return self.conv(omath)

    def conv(self, el) -> str:
        if not isinstance(el.tag, str):
            return ""
        name = etree.QName(el).localname
        handler = getattr(self, "_" + name, None)
        if handler is None:
            # unknown element: still convert children so nothing is lost silently
            inner = "".join(self.conv(c) for c in el if isinstance(c.tag, str))
            if name not in ("rPr", "ctrlPr", "argPr", "degHide", "subHide", "supHide"):
                self._unknown(el)
            return inner
        return handler(el)

    # -- OMML elements ----------------------------------------------------- #

    def _oMath(self, el) -> str:
        return "".join(self.conv(c) for c in el if isinstance(c.tag, str))

    def _oMathPara(self, el) -> str:
        return self.convert(el)

    def _r(self, el) -> str:
        self._known()
        texts = []
        for child in el:
            if not isinstance(child.tag, str):
                continue
            ln = etree.QName(child).localname
            if ln == "t":
                texts.append(child.text or "")
            elif ln == "rPr":
                pass  # run properties: sty (p/b/i), nor, scr, lit -> mostly ignorable
            else:
                self._unknown(child)
        s = "".join(texts)
        if s == "":
            return ""
        # detect function-ish names so that `sin x` does not render as italic s·i·n
        return self._plain(s)

    def _f(self, el) -> str:
        """fraction"""
        self._known()
        num = self.conv(self._child(el, "num")) or "{}"
        den = self.conv(self._child(el, "den")) or "{}"
        ftype = self._val(el, "fPr", "type")
        if ftype == "lin":
            return "%s/%s" % (self._braced(num), self._braced(den))
        if ftype == "skw":
            return "\\sfrac{%s}{%s}" % (num, den)
        if ftype == "noBar":
            return "\\genfrac{}{}{0pt}{}{%s}{%s}" % (num, den)
        return "\\frac{%s}{%s}" % (num, den)

    def _rad(self, el) -> str:
        self._known()
        deg = self.conv(self._child(el, "deg"))
        body = self.conv(self._child(el, "e")) or "{}"
        hide = self._val(el, "radPr", "degHide")
        if deg.strip() and hide != "1":
            return "\\sqrt[%s]{%s}" % (deg, body)
        return "\\sqrt{%s}" % body

    def _nary(self, el) -> str:
        self._known()
        chr_ = self._val(el, "naryPr", "chr")
        lim_loc = self._val(el, "naryPr", "limLoc") or "subSup"
        sub = self.conv(self._child(el, "sub"))
        sup = self.conv(self._child(el, "sup"))
        body = self.conv(self._child(el, "e"))

        if chr_ is None or chr_ == "":
            name = "\\int"  # OMML default for <m:nary> is the integral
        elif chr_ in NARY_MAP:
            name = NARY_MAP[chr_]
        else:
            name = "\\mathop{%s}" % self._plain(chr_)
            self.stats.fallbacks.append("nary:chr=%r" % chr_)

        s = name
        if name in LIMITS_BELOW or lim_loc == "undOvr":
            s += "\\limits"
        if sub.strip():
            s += "_{%s}" % sub
        if sup.strip():
            s += "^{%s}" % sup
        if body.strip():
            s += " " + (self._braced(body) if not self._is_simple(body) else body)
        return s

    def _sSup(self, el) -> str:
        self._known()
        base = self.conv(self._child(el, "e")) or "{}"
        sup = self.conv(self._child(el, "sup")) or "{}"
        return "%s^{%s}" % (self._braced(base), sup)

    def _sSub(self, el) -> str:
        self._known()
        base = self.conv(self._child(el, "e")) or "{}"
        sub = self.conv(self._child(el, "sub")) or "{}"
        return "%s_{%s}" % (self._braced(base), sub)

    def _sSubSup(self, el) -> str:
        self._known()
        base = self.conv(self._child(el, "e")) or "{}"
        sub = self.conv(self._child(el, "sub")) or "{}"
        sup = self.conv(self._child(el, "sup")) or "{}"
        return "%s_{%s}^{%s}" % (self._braced(base), sub, sup)

    def _sPre(self, el) -> str:
        self._known()
        base = self.conv(self._child(el, "e")) or "{}"
        sub = self.conv(self._child(el, "sub")) or "{}"
        sup = self.conv(self._child(el, "sup")) or "{}"
        return "{}_{%s}^{%s}%s" % (sub, sup, self._braced(base))

    def _d(self, el) -> str:
        """delimiters"""
        self._known()
        beg = self._val(el, "dPr", "begChr")
        end = self._val(el, "dPr", "endChr")
        if beg is None:
            beg = "("
        if end is None:
            end = ")"
        lb = DELIM_MAP.get(beg, "\\l" + beg.strip("\\") if len(beg) == 1 else beg)
        rb = DELIM_MAP.get(end, "\\r" + end.strip("\\") if len(end) == 1 else end)
        if beg not in DELIM_MAP:
            self.stats.fallbacks.append("d:begChr=%r" % beg)
        if end not in DELIM_MAP:
            self.stats.fallbacks.append("d:endChr=%r" % end)
        grow = self._val(el, "dPr", "grow")
        eqs = [self.conv(c) for c in el if etree.QName(c).localname == "e"]
        inner = ", ".join(e for e in eqs if e.strip()) or " "
        if grow == "0":
            return "%s%s%s" % (lb, inner, rb)
        return "\\left%s%s\\right%s" % (lb, inner, rb)

    def _func(self, el) -> str:
        self._known()
        fname_el = self._child(el, "fName")
        name_raw = self._first_text(fname_el) if fname_el is not None else ""
        # strip trailing punctuation word leaves behind
        key = "".join(ch for ch in name_raw if ch.isalpha())
        latex_name = FUNC_MAP.get(key)
        arg = self.conv(self._child(el, "e")) or "{}"
        if latex_name is None:
            if name_raw:
                self.stats.fallbacks.append("func:%r" % name_raw)
                return "\\mathrm{%s}\\,%s" % (self._plain(name_raw), self._braced(arg))
            return "\\mathrm{%s}\\,%s" % ("", self._braced(arg))
        return "%s %s" % (latex_name, self._braced(arg))

    def _acc(self, el) -> str:
        self._known()
        chr_ = self._val(el, "accPr", "chr") or "\u0302"
        base = self.conv(self._child(el, "e")) or "{}"
        macro = ACCENT_MAP.get(chr_)
        if macro is None:
            self.stats.fallbacks.append("acc:chr=%r" % chr_)
            return "\\overset{%s}{%s}" % (self._plain(chr_), base)
        if macro == "\\overline":
            return "\\overline{%s}" % base
        return "%s{%s}" % (macro, base)

    def _bar(self, el) -> str:
        self._known()
        pos = self._val(el, "barPr", "pos") or "bot"
        base = self.conv(self._child(el, "e")) or "{}"
        return ("\\overline{%s}" % base) if pos == "top" else ("\\underline{%s}" % base)

    def _groupChr(self, el) -> str:
        self._known()
        pos = self._val(el, "groupChrPr", "pos") or "bot"
        chr_ = self._val(el, "groupChrPr", "chr") or "\u23b5"
        base = self.conv(self._child(el, "e")) or "{}"
        text = self.conv(self._child(el, "oMath")) or ""
        macro = "\\underbrace" if pos == "bot" else "\\overbrace"
        if chr_ not in ("\u23b5", "\u23de"):
            self.stats.fallbacks.append("groupChr:chr=%r" % chr_)
        if text.strip():
            return "%s{%s}_{%s}" % (macro, base, text) if pos == "bot" else "%s{%s}^{%s}" % (macro, base, text)
        return "%s{%s}" % (macro, base)

    def _limLow(self, el) -> str:
        self._known()
        base = self.conv(self._child(el, "e")) or "{}"
        lim = self.conv(self._child(el, "lim")) or "{}"
        return "%s\\limits_{%s}" % (base, lim)

    def _limUpp(self, el) -> str:
        self._known()
        base = self.conv(self._child(el, "e")) or "{}"
        lim = self.conv(self._child(el, "lim")) or "{}"
        return "%s\\limits^{%s}" % (base, lim)

    def _m(self, el) -> str:
        """matrix"""
        self._known()
        beg = self._val(el, "mPr", "begChr")
        end = self._val(el, "mPr", "endChr")
        env = None
        if beg in MATRIX_ENV and end == {  # mirrored pair
            "(": ")",
            "[": "]",
            "{": "}",
            "|": "|",
            "\u2016": "\u2016",
        }.get(beg):
            env = MATRIX_ENV[beg]
        elif beg == "" and end == "":
            env = "matrix"
        if env is None:
            self.stats.fallbacks.append("m:beg=%r end=%r" % (beg, end))
            env = "matrix"
        rows = []
        for mr in [c for c in el if etree.QName(c).localname == "mr"]:
            cells = [self.conv(c).strip() for c in mr if etree.QName(c).localname == "e"]
            if cells:
                rows.append(" & ".join(c if c else "{}" for c in cells))
        return "\\begin{%s} %s \\end{%s}" % (env, " \\\\ ".join(rows), env)

    def _eqArr(self, el) -> str:
        self._known()
        lines = []
        for e in [c for c in el if etree.QName(c).localname == "e"]:
            txt = self.conv(e).strip()
            if txt:
                lines.append(txt)
        if len(lines) <= 1:
            return lines[0] if lines else ""
        return "\\begin{aligned} %s \\end{aligned}" % " \\\\ ".join(lines)

    def _box(self, el) -> str:
        self._known()
        return self.conv(self._child(el, "e")) or ""

    def _e(self, el) -> str:
        self._known()
        return "".join(self.conv(c) for c in el if isinstance(c.tag, str))

    def _num(self, el) -> str:
        return self._e(el)

    def _den(self, el) -> str:
        return self._e(el)

    def _deg(self, el) -> str:
        return self._e(el)

    def _sub(self, el) -> str:
        return self._e(el)

    def _sup(self, el) -> str:
        return self._e(el)

    def _lim(self, el) -> str:
        return self._e(el)

    def _fName(self, el) -> str:
        return self._e(el)

    def _borderBox(self, el) -> str:
        self._known()
        return "\\boxed{%s}" % (self.conv(self._child(el, "e")) or "")

    def _phant(self, el) -> str:
        self._known()
        return "\\phantom{%s}" % (self.conv(self._child(el, "e")) or "")

    def _arr(self, el) -> str:
        self._known()
        rows = []
        for e in [c for c in el if etree.QName(c).localname == "e"]:
            txt = self.conv(e).strip()
            if txt:
                rows.append(txt)
        if not rows:
            return ""
        return "\\begin{array}{l} %s \\end{array}" % " \\\\ ".join(rows)

    def _brk(self, el) -> str:
        return "\\;"

    def _t(self, el) -> str:
        return self._plain(el.text or "")

    # property containers -> ignore
    def _fPr(self, el): return ""
    def _radPr(self, el): return ""
    def _naryPr(self, el): return ""
    def _sSupPr(self, el): return ""
    def _sSubPr(self, el): return ""
    def _sSubSupPr(self, el): return ""
    def _dPr(self, el): return ""
    def _funcPr(self, el): return ""
    def _accPr(self, el): return ""
    def _barPr(self, el): return ""
    def _groupChrPr(self, el): return ""
    def _limLowPr(self, el): return ""
    def _limUppPr(self, el): return ""
    def _mPr(self, el): return ""
    def _eqArrPr(self, el): return ""
    def _boxPr(self, el): return ""
    def _arrPr(self, el): return ""
    def _borderBoxPr(self, el): return ""
    def _phantPr(self, el): return ""
    def _sPrePr(self, el): return ""
    def _mathPr(self, el): return ""
    def _ctrlPr(self, el): return ""
    def _argPr(self, el): return ""
    def _degHide(self, el): return ""
    def _subHide(self, el): return ""
    def _supHide(self, el): return ""

    # -- xml utils --------------------------------------------------------- #

    @staticmethod
    def _child(el, name: str):
        for c in el:
            if isinstance(c.tag, str) and etree.QName(c).localname == name:
                return c
        return None

    @staticmethod
    def _val(el, pr_name: str, val_name: str):
        pr = None
        for c in el:
            if isinstance(c.tag, str) and etree.QName(c).localname == pr_name:
                pr = c
                break
        if pr is None:
            return None
        for c in pr:
            if isinstance(c.tag, str) and etree.QName(c).localname == val_name:
                return c.get(m("val"))
        return None


def omml_to_latex(el) -> tuple[str, float, list, list]:
    """Return (latex, confidence, unknown_tags, fallbacks)."""
    stats = Stats()
    latex = Omml2Latex(stats).convert(el)
    latex = _tidy(latex)
    return latex, stats.confidence, stats.unknown_tags, stats.fallbacks


def _tidy(s: str) -> str:
    import re

    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("{}{}", "{}")
    s = re.sub(r"\\left\(\s*\\right\)", "()", s)
    return s
