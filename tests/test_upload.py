import httpx
import pytest

from app.files.upload import (
    BodyTooLarge,
    MalformedForm,
    UnsupportedForm,
    body_limit,
    clean_name,
    clean_text,
    parse_form,
)

KB = 1024


class FakeRequest:
    def __init__(self, content: bytes, content_type: str, length: bool = True, chunk: int = 4096):
        self.headers = {"content-type": content_type}
        if length:
            self.headers["content-length"] = str(len(content))
        self._content = content
        self._chunk = chunk
        self.consumed = 0

    async def stream(self):
        for i in range(0, len(self._content), self._chunk):
            part = self._content[i : i + self._chunk]
            self.consumed += len(part)
            yield part


def multipart(files) -> FakeRequest:
    req = httpx.Request("POST", "https://x/", files=files)
    return FakeRequest(req.read(), req.headers["content-type"])


def raw_multipart(body: bytes, length: bool = True) -> FakeRequest:
    return FakeRequest(body, "multipart/form-data; boundary=BND", length=length)


async def parse(req, max_file_bytes=1024 * KB, max_files=5):
    return await parse_form(req, max_file_bytes=max_file_bytes, max_files=max_files)


async def test_fields_and_files():
    form = await parse(multipart([("text", (None, "привет")), ("files", ("a.txt", b"abc"))]))
    assert form.fields == {"text": "привет"} and form.error is None
    assert [(f.name, f.data) for f in form.files] == [("a.txt", b"abc")]


async def test_non_multipart_unsupported():
    with pytest.raises(UnsupportedForm):
        await parse(FakeRequest(b"text=hi", "application/x-www-form-urlencoded"))


async def test_oversized_part_header_rejected():
    body = (
        b"--BND\r\nContent-Disposition: form-data; name=\"text\"\r\nX-Pad: "
        + b"a" * (9 * KB)
        + b"\r\n\r\nhi\r\n--BND--\r\n"
    )
    with pytest.raises(MalformedForm):
        await parse(raw_multipart(body))


async def test_too_many_part_headers_rejected():
    headers = b"".join(b"X-H%d: v\r\n" % i for i in range(20))
    body = (
        b"--BND\r\nContent-Disposition: form-data; name=\"text\"\r\n"
        + headers
        + b"\r\nhi\r\n--BND--\r\n"
    )
    with pytest.raises(MalformedForm):
        await parse(raw_multipart(body))


async def test_total_body_cap_by_content_length():
    req = multipart([("files", (f"f{i}.txt", b"a" * 900 * KB)) for i in range(5)])
    with pytest.raises(BodyTooLarge):
        await parse(req, max_file_bytes=100 * KB)
    assert req.consumed == 0  # refusal before reading the body


async def test_total_body_cap_streamed_without_length():
    limit = body_limit(10 * KB, 1)
    files = [("files", (f"f{i}.txt", b"a" * 9 * KB)) for i in range(400)]
    req = httpx.Request("POST", "https://x/", files=files)
    fake = FakeRequest(req.read(), req.headers["content-type"], length=False)
    with pytest.raises(BodyTooLarge):
        await parse(fake, max_file_bytes=10 * KB, max_files=1)
    assert fake.consumed <= limit + fake._chunk


async def test_excess_files_dropped_without_buffering(monkeypatch):
    seen: list[tuple[bool, int]] = []
    from app.files import upload

    orig = upload._Collector.on_part_data

    def spy(self, data, start, end):
        orig(self, data, start, end)
        seen.append((self.part.dropped, len(self.part.buf)))

    monkeypatch.setattr(upload._Collector, "on_part_data", spy)
    files = [("files", (f"f{i}.txt", b"a" * 50 * KB)) for i in range(7)]
    form = await parse(multipart(files))
    assert len(form.files) == 5
    assert form.error == "Не больше 5 файлов в сообщении"
    dropped = [n for d, n in seen if d]
    assert dropped and all(n == 0 for n in dropped)  # extra parts are not buffered


def _file_part(filename: bytes, data: bytes) -> bytes:
    return (
        b'--BND\r\nContent-Disposition: form-data; name="files"; filename="' + filename
        + b'"\r\nContent-Type: application/octet-stream\r\n\r\n' + data + b"\r\n"
    )


async def test_empty_file_input_skipped():
    # httpx cannot do filename="" - build the body by hand, like a browser
    body = b'--BND\r\nContent-Disposition: form-data; name="text"\r\n\r\nx\r\n'
    body += _file_part(b"", b"") + b"--BND--\r\n"
    form = await parse(raw_multipart(body))
    assert form.fields == {"text": "x"} and form.files == [] and form.error is None


async def test_file_without_name_but_with_data_kept():
    form = await parse(raw_multipart(_file_part(b"", b"data") + b"--BND--\r\n"))
    assert [(f.name, f.data) for f in form.files] == [("file", b"data")]


async def test_empty_file_inputs_do_not_count_toward_limit():
    body = b"".join(_file_part(b"", b"") for _ in range(6))
    body += b"".join(_file_part(b"f%d.txt" % i, b"a") for i in range(5)) + b"--BND--\r\n"
    form = await parse(raw_multipart(body))
    assert len(form.files) == 5 and form.error is None


def test_clean_helpers():
    assert clean_text("a\x00b\x1b\n\r\tc\x7f") == "ab\n\r\tc"
    assert clean_name("C:\\dir\\x\x00y.txt") == "xy.txt"
    assert clean_name("\x00") == "file"
