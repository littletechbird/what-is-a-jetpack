"""Text extraction from html, docx, txt, and pdf files. Documents here are synthetic."""
import os
import tempfile
import time
import unittest
import zipfile

from helpers import write

import extract  # noqa: E402
from extract import clean, extract as extract_file, html_text  # noqa: E402

POSIX = os.name == "posix" and hasattr(os, "mkfifo")


class HtmlTests(unittest.TestCase):
    def test_comments_are_dropped_closed_or_not(self):
        self.assertEqual(html_text("<p>keep</p><!-- gone --><p>also</p>"), ["keep", "also"])
        self.assertEqual(html_text("<p>keep</p><!-- gone\n<p>still a comment</p>"), ["keep"])
        self.assertEqual(html_text("<p>a</p><!--x--!><p>b</p>"), ["a", "b"])
        self.assertEqual(html_text("<p>a</p><![CDATA[gone]]><p>b</p>"), ["a", "b"])

    def test_attributes_never_become_text(self):
        raw = "<a\n  href='https://example.invalid/x'\n  title=\"tooltip words\"\n  data-x='1 > 0'\n>shown</a>"
        self.assertEqual(html_text(raw), ["shown"])
        self.assertEqual(html_text("<img alt='alt words' src='x.png'><p>body</p>"), ["body"])

    def test_scripts_styles_and_templates_are_dropped(self):
        raw = (
            "<p>one</p>"
            "<script >var a = '<p>fake</p>';</script >"
            "<SCRIPT\n type='module'>upper()</SCRIPT\n>"
            "<style media='x'>p{color:red}</style>"
            "<noscript>enable js</noscript><template><p>tmpl</p></template>"
            "<svg><text>svgtext</text></svg>"
            "<script/>selfclosedscript</script><p>two</p>"
        )
        self.assertEqual(html_text(raw), ["one", "two"])

    def test_an_unclosed_script_hides_the_rest_and_finishes_quickly(self):
        self.assertEqual(html_text("<p>before</p><script>after()"), ["before"])
        hostile = "<p>x</p>" + "<script " * 40_000 + "<p>" * 40_000 + "<!--" + "a" * 500_000
        started = time.perf_counter()
        html_text(hostile)
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_entities_blocks_and_tables(self):
        raw = (
            "<h1>Title &amp; more</h1><p>caf&eacute; &#937; &nbsp;x</p>"
            "<table><tr><th>Size</th><th>Torque</th></tr><tr><td>M8</td><td>M8</td></tr></table>"
            "<div>a<br>b</div>"
        )
        self.assertEqual(html_text(raw), ["Title & more", "café Ω x", "Size | Torque", "M8 | M8", "a", "b"])

    def test_html_file_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.html")
            write(path, "<p>" + "word " * 2000 + "</p><p>" + "next " * 2000 + "</p>")
            pages, kind, label = extract_file(path)
        self.assertEqual((kind, label), ("html", "part"))
        self.assertGreaterEqual(len(pages), 2)
        self.assertTrue(all(len(page) <= 12_000 for page in pages))


def _docx(path, build):
    from docx import Document

    document = Document()
    build(document)
    document.save(path)


TEXTBOX_RUN = (
    '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml">'
    "<mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wps:txbx><w:txbxContent>"
    "<w:p><w:r><w:t>boxed warning label</w:t></w:r></w:p>"
    "</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
    "<mc:Fallback><w:pict><v:textbox><w:txbxContent>"
    "<w:p><w:r><w:t>boxed warning label</w:t></w:r></w:p>"
    "</w:txbxContent></v:textbox></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
)
DELETED_RUN = (
    '<w:del xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:id="1" w:author="x">'
    "<w:r><w:delText>deletedword</w:delText></w:r></w:del>"
)


class DocxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "doc.docx")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tables_stay_in_order_and_keep_repeated_cells(self):
        def build(document):
            document.add_paragraph("before the table")
            table = document.add_table(rows=2, cols=3)
            for col, value in enumerate(("Size", "Torque", "Torque")):
                table.cell(0, col).text = value
            for col, value in enumerate(("M8", "25", "25")):
                table.cell(1, col).text = value
            document.add_paragraph("after the table")

        _docx(self.path, build)
        pages, kind, label = extract_file(self.path)
        self.assertEqual((kind, label), ("docx", "part"))
        lines = [line for line in "".join(pages).split("\n") if line]
        self.assertEqual(lines, ["before the table", "Size | Torque | Torque", "M8 | 25 | 25", "after the table"])

    def test_headers_footers_text_boxes_and_tracked_changes(self):
        from docx.oxml import parse_xml

        def build(document):
            section = document.sections[0]
            section.header.paragraphs[0].text = "header revision line"
            section.footer.paragraphs[0].text = "footer page note"
            para = document.add_paragraph("body with a box")
            para._p.append(parse_xml(TEXTBOX_RUN))
            para._p.append(parse_xml(DELETED_RUN))
            document.add_paragraph("second body line")

        _docx(self.path, build)
        text = "".join(extract_file(self.path)[0])
        lines = [line for line in text.split("\n") if line]
        self.assertEqual(
            lines,
            ["header revision line", "body with a box", "boxed warning label", "second body line", "footer page note"],
        )
        self.assertNotIn("deletedword", text)

    def test_oversized_docx_is_refused_before_it_is_inflated(self):
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<a>" + " " * 200_000 + "</a>")
        old = extract.MAX_DOCX_XML_BYTES
        extract.MAX_DOCX_XML_BYTES = 100_000
        try:
            with self.assertRaisesRegex(ValueError, "size limit"):
                extract_file(self.path)
        finally:
            extract.MAX_DOCX_XML_BYTES = old

    def test_a_broken_docx_raises(self):
        write(self.path, "not a zip")
        with self.assertRaises(Exception):
            extract_file(self.path)


class TextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_form_feeds_split_pages_and_a_trailing_one_is_ignored(self):
        write(self.path("a.txt"), "one\ftwo\f")
        self.assertEqual(extract_file(self.path("a.txt")), (["one", "two"], "txt", "page"))

    def test_bom_and_cp1252_fallback(self):
        with open(self.path("bom.txt"), "wb") as handle:
            handle.write("\ufeffcafé".encode("utf-8"))
        self.assertEqual(extract_file(self.path("bom.txt"))[0], ["café"])
        with open(self.path("old.txt"), "wb") as handle:
            handle.write("caf\xe9 \x93quoted\x94".encode("latin-1"))
        self.assertEqual(extract_file(self.path("old.txt"))[0], ["café \u201cquoted\u201d"])

    def test_large_text_is_refused(self):
        write(self.path("big.txt"), "x" * 5000)
        old = extract.MAX_TEXT_BYTES
        extract.MAX_TEXT_BYTES = 1000
        try:
            with self.assertRaisesRegex(ValueError, "larger than"):
                extract_file(self.path("big.txt"))
        finally:
            extract.MAX_TEXT_BYTES = old

    @unittest.skipUnless(POSIX, "needs mkfifo")
    def test_special_files_are_refused_without_reading(self):
        os.mkfifo(self.path("pipe.txt"))
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            extract_file(self.path("pipe.txt"))
        if os.path.exists("/dev/zero"):
            os.symlink("/dev/zero", self.path("zero.txt"))
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                extract_file(self.path("zero.txt"))

    def test_unsupported_type(self):
        write(self.path("a.rtf"), "x")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            extract_file(self.path("a.rtf"))

    def test_clean_strips_controls_soft_hyphens_and_normalizes(self):
        raw = "lock\u00adout\r\nred\x1b[31m text\u202e\x07 ｆｕｌｌ 10⁶\n\n\n\n\nend"
        self.assertEqual(clean(raw), "lockout\nred[31m text full 10^6\n\nend")

    def test_pdf_pages(self):
        import pymupdf

        path = self.path("two.pdf")
        doc = pymupdf.open()
        for words in ("first page words", "second page words"):
            doc.new_page().insert_text((72, 72), words)
        doc.save(path)
        doc.close()
        pages, kind, label = extract_file(path)
        self.assertEqual((kind, label), ("pdf", "page"))
        self.assertEqual([page.strip() for page in pages], ["first page words", "second page words"])


if __name__ == "__main__":
    unittest.main()
