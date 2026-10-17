"""Chat context assembly: token estimation, attachment inlining, history trimming."""

import math

from app.files.extract import Attachment
from app.models import Message, User

IMAGE_TOKENS = 1000

_SYSTEM = {"ru": "Отвечай на русском языке.", "en": "Answer in English."}


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3)


def _file_block(name: str, text: str) -> str:
    return f'<file name="{name.replace(chr(34), chr(39))}">\n{text}\n</file>'


def user_content(text: str, attachments: list[Attachment]) -> str | list[dict]:
    """Text attachments become <file> blocks before the text; images become image_url parts."""
    blocks = [_file_block(a.name, a.text or "") for a in attachments if a.kind == "text"]
    full = "\n\n".join([*blocks, text]) if blocks else text
    images = [a for a in attachments if a.kind == "image" and a.image_data_url]
    if not images:
        return full
    parts: list[dict] = [{"type": "text", "text": full}]
    parts += [{"type": "image_url", "image_url": {"url": a.image_data_url}} for a in images]
    return parts


def history_content(msg: Message) -> str:
    blocks = []
    for a in msg.attachments or []:
        if a.get("kind") == "image":
            blocks.append(f"[изображение: {a.get('name', '')}]")
        else:
            blocks.append(_file_block(a.get("name", ""), a.get("text") or ""))
    return "\n\n".join([*blocks, msg.content]) if blocks else msg.content


def _estimate_content(content: str | list[dict]) -> int:
    if isinstance(content, str):
        return estimate_tokens(content)
    total = 0
    for part in content:
        if part.get("type") == "text":
            total += estimate_tokens(part.get("text", ""))
        else:
            total += IMAGE_TOKENS
    return total


def system_prompt(user: User) -> str:
    system = _SYSTEM.get(user.answer_language, _SYSTEM["ru"])
    if user.system_prompt:
        system += "\n\n" + user.system_prompt
    return system


def context_overflow(user: User, new_user_content: list[dict] | str, budget: int) -> str | None:
    """Ошибка для пользователя, если системный промпт + новое сообщение не влезают в бюджет."""
    used = estimate_tokens(system_prompt(user)) + _estimate_content(new_user_content)
    if used <= budget:
        return None
    return (
        f"Вложения слишком большие для контекста модели (≈{math.ceil(used / 1000)} тыс. "
        f"токенов при лимите {budget / 1000:g} тыс.). Сократите файлы."
    )


def build_messages(
    user: User, history: list[Message], new_user_content: list[dict] | str, budget: int
) -> list[dict]:
    system = system_prompt(user)
    used = estimate_tokens(system) + _estimate_content(new_user_content)
    kept: list[dict] = []
    for msg in reversed(history):
        content = history_content(msg)
        cost = estimate_tokens(content)
        if used + cost > budget:
            break
        used += cost
        kept.append({"role": msg.role, "content": content})
    kept.reverse()

    return [
        {"role": "system", "content": system},
        *kept,
        {"role": "user", "content": new_user_content},
    ]
