"""In-memory multipart parsing with hard limits.

Starlette's parser spools uploads >1 MB to a temp file; attachments must never touch the
disk, so the request body is parsed here into bytes. Memory is bounded: per-part header
size/count, per-file and per-field size, file count (excess parts are dropped as soon as
their headers arrive) and a total body cap counted on the raw stream. Small urlencoded
bodies (chat rename) are read the same way, with a byte cap.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

MAX_FIELDS = 20
MAX_FIELD_BYTES = 32_000 * 4  # 32000 символов UTF-8
MAX_HEADER_BYTES = 8 * 1024  # на часть, суммарно
MAX_HEADERS = 16  # на часть
BODY_SLACK = 256 * 1024  # границы, заголовки частей

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CONTROL_ALL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(text: str) -> str:
    """Drop NUL and other C0 control chars (Postgres rejects NUL); keep \\t \\n \\r."""
    return _CONTROL.sub("", text)


def clean_name(raw: str) -> str:
    name = _CONTROL_ALL.sub("", raw).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "file"


class MalformedForm(Exception):
    pass


class BodyTooLarge(MalformedForm):
    pass


class UnsupportedForm(MalformedForm):
    pass


@dataclass
class UploadedFile:
    name: str
    data: bytes


@dataclass
class ParsedForm:
    fields: dict[str, str] = field(default_factory=dict)
    files: list[UploadedFile] = field(default_factory=list)
    error: str | None = None  # первая ошибка лимитов (по-русски)


@dataclass
class _Part:
    name: str = ""
    filename: str | None = None  # None — обычное поле
    raw_filename_empty: bool = False
    counted: bool = False
    buf: bytearray = field(default_factory=bytearray)
    dropped: bool = False


class _Collector:
    def __init__(self, form: ParsedForm, max_file_bytes: int, max_files: int) -> None:
        self.form = form
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.part = _Part()
        self.header_name = b""
        self.header_value = b""
        self.header_bytes = 0
        self.header_count = 0
        self.disposition = b""
        self.n_files = 0
        self.n_fields = 0

    def fail(self, message: str) -> None:
        if self.form.error is None:
            self.form.error = message

    def on_part_begin(self) -> None:
        self.part = _Part()
        self.disposition = b""
        self.header_bytes = 0
        self.header_count = 0

    def _header_data(self, n: int) -> None:
        self.header_bytes += n
        if self.header_bytes > MAX_HEADER_BYTES:
            raise MalformedForm("part headers too large")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_data(end - start)
        self.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_data(end - start)
        self.header_value += data[start:end]

    def on_header_end(self) -> None:
        self.header_count += 1
        if self.header_count > MAX_HEADERS:
            raise MalformedForm("too many part headers")
        if self.header_name.lower() == b"content-disposition":
            self.disposition = self.header_value
        self.header_name = b""
        self.header_value = b""

    def _count_file(self) -> None:
        part = self.part
        part.counted = True
        self.n_files += 1
        if self.n_files > self.max_files:
            part.dropped = True
            self.fail(f"Не больше {self.max_files} файлов в сообщении")

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self.disposition)
        if b"name" not in options:
            raise MalformedForm("part without name")
        part = self.part
        part.name = options[b"name"].decode("utf-8", "replace")
        if b"filename" in options:
            raw = options[b"filename"].decode("utf-8", "replace")
            part.filename = clean_name(raw)
            part.raw_filename_empty = not raw.strip()
            if not part.raw_filename_empty:
                self._count_file()  # лишние файлы отбрасываются сразу, без буферизации
        else:
            self.n_fields += 1
            if self.n_fields > MAX_FIELDS:
                part.dropped = True
                self.fail("Некорректный запрос")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self.part
        if part.filename is not None and not part.counted and end > start:
            self._count_file()  # файл без имени, но с данными
        if part.dropped:
            return
        part.buf += data[start:end]
        limit = self.max_file_bytes if part.filename is not None else MAX_FIELD_BYTES
        if len(part.buf) > limit:
            part.dropped = True
            part.buf = bytearray()
            if part.filename is not None:
                mb = self.max_file_bytes // (1024 * 1024)
                self.fail(f"Файл «{part.filename}» больше {mb} МБ")
            else:
                self.fail("Сообщение длиннее 32000 символов")

    def on_part_end(self) -> None:
        part = self.part
        if part.dropped:
            return
        if part.filename is None:
            self.form.fields.setdefault(part.name, part.buf.decode("utf-8", "replace"))
            return
        if part.raw_filename_empty and not part.buf:
            return  # пустое поле выбора файла
        self.form.files.append(UploadedFile(part.filename, bytes(part.buf)))

    def callbacks(self) -> dict:
        return {
            name: getattr(self, name)
            for name in (
                "on_part_begin",
                "on_header_field",
                "on_header_value",
                "on_header_end",
                "on_headers_finished",
                "on_part_data",
                "on_part_end",
            )
        }


def body_limit(max_file_bytes: int, max_files: int) -> int:
    return max_files * max_file_bytes + MAX_FIELDS * MAX_FIELD_BYTES + BODY_SLACK


async def parse_form(request: Request, *, max_file_bytes: int, max_files: int) -> ParsedForm:
    """Parse a multipart body fully in memory.

    Raises UnsupportedForm (not multipart), BodyTooLarge, MalformedForm.
    """
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    if content_type != b"multipart/form-data":
        raise UnsupportedForm("multipart/form-data required")
    boundary = params.get(b"boundary")
    if not boundary:
        raise MalformedForm("missing boundary")
    limit = body_limit(max_file_bytes, max_files)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge("body too large")
    form = ParsedForm()
    collector = _Collector(form, max_file_bytes, max_files)
    parser = MultipartParser(boundary, collector.callbacks())
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise BodyTooLarge("body too large")
            parser.write(chunk)
        parser.finalize()
    except MalformedForm:
        raise
    except Exception as exc:
        raise MalformedForm(type(exc).__name__) from exc
    return form


async def parse_urlencoded(request: Request, *, max_bytes: int) -> dict[str, str]:
    """Small application/x-www-form-urlencoded body → {name: first value}, read with a cap.

    Raises UnsupportedForm (other content type), BodyTooLarge.
    """
    content_type, _ = parse_options_header(request.headers.get("content-type", ""))
    if content_type != b"application/x-www-form-urlencoded":
        raise UnsupportedForm("application/x-www-form-urlencoded required")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise BodyTooLarge("body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise BodyTooLarge("body too large")
    fields: dict[str, str] = {}
    for name, value in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True):
        fields.setdefault(name, value)
    return fields
