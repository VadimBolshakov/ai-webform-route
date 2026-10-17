import logging
from typing import Protocol

from fastapi import Request

logger = logging.getLogger("notify")


class Notifier(Protocol):
    async def alert(self, text: str) -> None: ...


class LogNotifier:
    async def alert(self, text: str) -> None:
        logger.warning(text)


def get_notifier(request: Request) -> Notifier:
    return request.app.state.notifier
