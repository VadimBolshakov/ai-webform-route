"""Regenerate binary test fixtures: python tests/fixtures/make_fixtures.py"""
import io
from pathlib import Path

from docx import Document
from PIL import Image
from pypdf import PdfWriter

OUT = Path(__file__).parent


def text_pdf(text: str) -> bytes:
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


def main() -> None:
    (OUT / "sample.pdf").write_bytes(text_pdf("Hello PDF World"))

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    (OUT / "scan.pdf").write_bytes(buf.getvalue())

    d = Document()
    d.add_paragraph("Привет, DOCX мир")
    d.save(OUT / "sample.docx")

    (OUT / "cp1251.txt").write_bytes("Привет, мир! Кодировка cp1251.".encode("cp1251"))

    img = Image.new("RGB", (64, 48), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    img.save(OUT / "photo.jpg", "JPEG", exif=exif)

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(buf, "PNG")
    (OUT / "fake.pdf").write_bytes(buf.getvalue())


if __name__ == "__main__":
    main()
