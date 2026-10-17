import base64
import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.files.extract import AttachmentError, extract_attachment

FIX = Path(__file__).parent / "fixtures"
MAX = 10 * 1024 * 1024


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def decode_url(url: str) -> tuple[str, Image.Image]:
    head, b64 = url.split(",", 1)
    mime = head.removeprefix("data:").removesuffix(";base64")
    return mime, Image.open(io.BytesIO(base64.b64decode(b64)))


async def test_extract_pdf():
    a = await extract_attachment("sample.pdf", fx("sample.pdf"), max_bytes=MAX)
    assert a.kind == "text" and "Hello PDF World" in a.text and a.image_data_url is None


async def test_extract_docx():
    a = await extract_attachment("s.docx", fx("sample.docx"), max_bytes=MAX)
    assert a.kind == "text" and "Привет, DOCX мир" in a.text


async def test_extract_text_utf8():
    a = await extract_attachment("a.txt", "Привет".encode("utf-8-sig"), max_bytes=MAX)
    assert a.text == "Привет"


async def test_extract_text_cp1251():
    a = await extract_attachment("a.txt", fx("cp1251.txt"), max_bytes=MAX)
    assert a.text == "Привет, мир! Кодировка cp1251."


async def test_extract_pdf_without_text_raises():
    with pytest.raises(AttachmentError, match="В PDF нет текстового слоя"):
        await extract_attachment("scan.pdf", fx("scan.pdf"), max_bytes=MAX)


async def test_extension_lies():
    a = await extract_attachment("fake.pdf", fx("fake.pdf"), max_bytes=MAX)
    assert a.kind == "image" and a.image_data_url.startswith("data:image/jpeg;base64,")


async def test_image_exif_stripped():
    assert b"TestCam" in fx("photo.jpg")
    a = await extract_attachment("photo.jpg", fx("photo.jpg"), max_bytes=MAX)
    mime, img = decode_url(a.image_data_url)
    assert mime == "image/jpeg" and len(img.getexif()) == 0
    assert b"TestCam" not in base64.b64decode(a.image_data_url.split(",", 1)[1])


async def test_image_downscaled_to_2048():
    buf = io.BytesIO()
    Image.new("RGB", (4000, 1000), "red").save(buf, "PNG")
    a = await extract_attachment("big.png", buf.getvalue(), max_bytes=MAX)
    mime, img = decode_url(a.image_data_url)
    assert mime == "image/jpeg" and img.size == (2048, 512)


async def test_alpha_png_stays_png():
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10), (1, 2, 3, 100)).save(buf, "PNG")
    a = await extract_attachment("a.png", buf.getvalue(), max_bytes=MAX)
    mime, img = decode_url(a.image_data_url)
    assert mime == "image/png" and img.mode == "RGBA"


async def test_animated_gif_first_frame():
    buf = io.BytesIO()
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    a = await extract_attachment("a.gif", buf.getvalue(), max_bytes=MAX)
    mime, img = decode_url(a.image_data_url)
    assert mime == "image/jpeg" and getattr(img, "n_frames", 1) == 1


async def test_decompression_bomb_rejected():
    buf = io.BytesIO()
    Image.new("1", (7000, 7000)).save(buf, "PNG")  # 49 Mpx, tiny file
    with pytest.raises(AttachmentError, match="Изображение слишком большое"):
        await extract_attachment("bomb.png", buf.getvalue(), max_bytes=MAX)


async def test_too_large_rejected():
    with pytest.raises(AttachmentError, match="слишком большой"):
        await extract_attachment("a.txt", b"a" * 101, max_bytes=100)


async def test_binary_rejected():
    with pytest.raises(AttachmentError, match="Бинарный файл не поддерживается"):
        await extract_attachment("a.bin", b"abc\x00def", max_bytes=MAX)


async def test_non_docx_zip_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hi")
    with pytest.raises(AttachmentError, match="Бинарный файл не поддерживается"):
        await extract_attachment("a.zip", buf.getvalue(), max_bytes=MAX)


async def test_zip_bomb_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"\0" * (51 * 1024 * 1024))
    assert len(buf.getvalue()) < 1024 * 1024
    with pytest.raises(AttachmentError):
        await extract_attachment("b.docx", buf.getvalue(), max_bytes=MAX)


async def test_undecodable_text():
    # 0x98 is undefined in cp1251 and invalid UTF-8
    with pytest.raises(AttachmentError, match="Не удалось определить кодировку"):
        await extract_attachment("a.txt", b"abc\x98\xff", max_bytes=MAX)


async def test_truncation():
    a = await extract_attachment("a.txt", b"x" * 100_050, max_bytes=MAX)
    assert a.text == "x" * 100_000 + "\n[…обрезано]"


async def test_timeout(monkeypatch):
    import time

    from app.files import extract

    monkeypatch.setattr(extract, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(extract, "_extract_sync", lambda n, d: time.sleep(0.3))
    with pytest.raises(AttachmentError, match="Файл обрабатывается слишком долго"):
        await extract.extract_attachment("a.txt", b"x", max_bytes=MAX)
