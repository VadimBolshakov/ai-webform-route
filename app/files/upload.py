"""In-memory multipart parsing with per-file limits.

Starlette's parser spools uploads >1 MB to a temp file; attachments must never touch the
disk, so the request body is parsed here into bytes with hard caps. Oversized parts are
discarded as they stream in (memory stays bounded) and reported as a user-facing error.
"""

from dataclasses import dataclass, field

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

MAX_FIELDS = 20
MAX_FIELD_BYTES = 32_000 * 4  # 32000 символов UTF-8


class MalformedForm(Exception):
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


def _clean_name(raw: str) -> str:
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "file"


@dataclass
class _Part:
    name: str = ""
    filename: str | None = None
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
        self.disposition = b""
        self.n_files = 0
        self.n_fields = 0

    def fail(self, message: str) -> None:
        if self.form.error is None:
            self.form.error = message

    def on_part_begin(self) -> None:
        self.part = _Part()
        self.disposition = b""

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value += data[start:end]

    def on_header_end(self) -> None:
        if self.header_name.lower() == b"content-disposition":
            self.disposition = self.header_value
        self.header_name = b""
        self.header_value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self.disposition)
        if b"name" not in options:
            raise MalformedForm("part without name")
        self.part.name = options[b"name"].decode("utf-8", "replace")
        if b"filename" in options:
            self.part.filename = _clean_name(options[b"filename"].decode("utf-8", "replace"))
        else:
            self.n_fields += 1
            if self.n_fields > MAX_FIELDS:
                self.part.dropped = True
                self.fail("Некорректный запрос")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self.part
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
        if not part.filename and not part.buf:
            return  # пустое поле выбора файла
        self.n_files += 1
        if self.n_files > self.max_files:
            self.fail(f"Не больше {self.max_files} файлов в сообщении")
            return
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


async def parse_form(request: Request, *, max_file_bytes: int, max_files: int) -> ParsedForm:
    """Parse multipart (or urlencoded) body fully in memory. Raises MalformedForm."""
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    form = ParsedForm()
    if content_type == b"application/x-www-form-urlencoded":
        data = await request.form(max_files=0, max_fields=MAX_FIELDS)
        form.fields = {k: v for k, v in data.items() if isinstance(v, str)}
        return form
    if content_type != b"multipart/form-data":
        return form
    boundary = params.get(b"boundary")
    if not boundary:
        raise MalformedForm("missing boundary")
    collector = _Collector(form, max_file_bytes, max_files)
    parser = MultipartParser(boundary, collector.callbacks())
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    except MalformedForm:
        raise
    except Exception as exc:
        raise MalformedForm(type(exc).__name__) from exc
    return form
