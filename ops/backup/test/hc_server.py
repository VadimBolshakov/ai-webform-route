"""Фейковый healthchecks.io для тестов: записывает каждый запрос в /tmp/hc/requests.jsonl.

Строка журнала: {"method", "path", "body", "size" (байт в теле)}. Запрос на /reset очищает журнал
(и сам не записывается).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path("/tmp/hc/requests.jsonl")
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = raw.decode("utf-8", "replace")
        with LOCK:
            if self.path == "/reset":
                LOG.write_text("")
            else:
                record = {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "size": len(raw),
                }
                with LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    do_GET = do_POST = _handle

    def log_message(self, format, *args):  # noqa: A002 — сигнатура базового класса
        pass


if __name__ == "__main__":
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.touch()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
