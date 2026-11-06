"""Read pdf, docx, html, and txt files into pages of plain text.

Only regular files are read. Text and HTML files over MAX_TEXT_BYTES, and
Word files that would expand past the limits below, are refused instead of
being loaded into memory. HTML is parsed, not matched with regular
expressions, so comments, attributes, scripts, and styles never become text.
"""
import os
import re
import stat
import zipfile
from html.parser import HTMLParser

from redact import normalize

EXTS = {".pdf", ".docx", ".html", ".htm", ".txt"}
MAX_TEXT_BYTES = 64 << 20
MAX_DOCX_XML_BYTES = 256 << 20
MAX_DOCX_BYTES = 1 << 30

# C0 and C1 controls except tab and newline, plus bidirectional overrides.
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean(text):
    text = normalize(text)
    text = text.replace("\u00ad", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_RE.sub("", text)
    text = re.sub(r"[ \t]{3,}", "   ", text)
    text = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", text)
    return text.strip()


def printable(text):
    """Text that is safe to print to a terminal."""
    return CONTROL_RE.sub("?", text or "")


def pack_parts(paras, size=3000):
    parts, cur = [], ""
    for para in paras:
        if len(cur) + len(para) + 1 > size and cur:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    if cur:
        parts.append(cur)
    return parts or [""]


def regular_file(path):
    """Return the size of a regular file. Raise ValueError for FIFOs, devices, and folders."""
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    return info.st_size


def read_text(path):
    size = regular_file(path)
    if size > MAX_TEXT_BYTES:
        raise ValueError(f"larger than {MAX_TEXT_BYTES >> 20} MB")
    with open(path, "rb") as handle:
        raw = handle.read(MAX_TEXT_BYTES + 1)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def pdf_pages(path):
    regular_file(path)
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            raise ValueError("pdf is password protected")
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    return pages or [""], "pdf", "page"


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
W_P, W_TBL, W_TR, W_TC = W_NS + "p", W_NS + "tbl", W_NS + "tr", W_NS + "tc"
W_T, W_TAB, W_BR, W_CR = W_NS + "t", W_NS + "tab", W_NS + "br", W_NS + "cr"
W_DEL, W_TXBX = W_NS + "del", W_NS + "txbxContent"
W_SDT, W_SDT_CONTENT, W_CUSTOM = W_NS + "sdt", W_NS + "sdtContent", W_NS + "customXml"
W_NOBREAK_HYPHEN = W_NS + "noBreakHyphen"


def _docx_guard(path):
    regular_file(path)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
    total = sum(info.file_size for info in infos)
    xml = sum(info.file_size for info in infos if info.filename.endswith((".xml", ".rels")))
    if xml > MAX_DOCX_XML_BYTES or total > MAX_DOCX_BYTES:
        raise ValueError(f"docx expands to {total >> 20} MB, past the size limit")


def _para_text(p):
    """Run text in reading order, including links and tracked insertions. Text boxes are read separately."""
    out = []

    def walk(element):
        for child in element:
            tag = child.tag
            if tag in (W_TXBX, MC_FALLBACK, W_DEL):
                continue
            if tag == W_T:
                out.append(child.text or "")
            elif tag == W_TAB:
                out.append("\t")
            elif tag in (W_BR, W_CR):
                out.append("\n")
            elif tag == W_NOBREAK_HYPHEN:
                out.append("-")
            else:
                walk(child)

    walk(p)
    return "".join(out).strip()


def _text_boxes(p):
    found = []

    def walk(element):
        for child in element:
            if child.tag == MC_FALLBACK:
                continue
            if child.tag == W_TXBX:
                found.append(child)
                continue
            walk(child)

    walk(p)
    return found


def _rows(table):
    for child in table:
        if child.tag == W_TR:
            yield child
        elif child.tag in (W_SDT, W_CUSTOM):
            content = child.find(W_SDT_CONTENT) if child.tag == W_SDT else child
            if content is not None:
                yield from (row for row in content if row.tag == W_TR)


def _blocks(parent, out):
    """Paragraphs and tables in document order. Each table row becomes one line."""
    for child in parent:
        tag = child.tag
        if tag == W_P:
            text = _para_text(child)
            if text:
                out.append(text)
            for box in _text_boxes(child):
                _blocks(box, out)
        elif tag == W_TBL:
            for row in _rows(child):
                cells = []
                for cell in row:
                    if cell.tag != W_TC:
                        continue
                    inner = []
                    _blocks(cell, inner)
                    text = " ".join(inner).strip()
                    if text:
                        cells.append(text)
                if cells:
                    out.append(" | ".join(cells))
        elif tag == W_SDT:
            content = child.find(W_SDT_CONTENT)
            if content is not None:
                _blocks(content, out)
        elif tag == W_CUSTOM:
            _blocks(child, out)


def docx_pages(path):
    _docx_guard(path)
    from docx import Document

    document = Document(path)
    head, body, foot = [], [], []
    seen = set()
    for section in document.sections:
        for part, target in (
            (section.first_page_header, head), (section.header, head), (section.even_page_header, head),
            (section.first_page_footer, foot), (section.footer, foot), (section.even_page_footer, foot),
        ):
            if part.is_linked_to_previous:
                continue
            element = part._element
            if id(element) in seen:
                continue
            seen.add(id(element))
            lines = []
            _blocks(element, lines)
            target.extend(line for line in lines if line not in target)
    _blocks(document.element.body, body)
    return pack_parts(head + body + foot), "docx", "part"


SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
    "tfoot", "thead", "title", "tr", "ul",
}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.cells = 0
        self.closing = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in ("td", "th"):
            if self.cells:
                self.parts.append(" | ")
            self.cells += 1
        elif tag in BLOCK_TAGS:
            if tag == "tr":
                self.cells = 0
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        # Browsers ignore the slash on <script/> and <style/>, so their content stays hidden.
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        # Older Python releases flush an unclosed comment as text when the parser closes.
        if self.closing and data.lstrip().startswith("<!"):
            return
        self.parts.append(data)

    def close(self):
        self.closing = True
        super().close()


def html_text(raw):
    parser = _HTMLText()
    parser.feed(raw)
    parser.close()
    lines = []
    for line in "".join(parser.parts).split("\n"):
        line = re.sub(r"\s+", " ", line).strip(" |")
        if line:
            lines.append(line)
    return lines


def html_pages(path):
    return pack_parts(html_text(read_text(path))), "html", "part"


def txt_pages(path):
    pages = read_text(path).split("\f")
    if len(pages) > 1 and pages[-1].strip() == "":
        pages = pages[:-1]
    return pages or [""], "txt", "page"


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return pdf_pages(path)
    if ext == ".docx":
        return docx_pages(path)
    if ext in {".html", ".htm"}:
        return html_pages(path)
    if ext == ".txt":
        return txt_pages(path)
    raise ValueError("unsupported file type: " + ext)
