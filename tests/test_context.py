from types import SimpleNamespace

from app.chat.context import build_messages, estimate_tokens, user_content
from app.files.extract import Attachment


def _user(lang="ru", prompt=""):
    return SimpleNamespace(answer_language=lang, system_prompt=prompt)


def _msg(role, content, attachments=None):
    return SimpleNamespace(role=role, content=content, attachments=attachments or [])


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2


def test_build_context_language_and_prompt():
    ru = build_messages(_user("ru", "Будь краток"), [], "привет", 1000)
    assert ru[0] == {"role": "system", "content": "Отвечай на русском языке.\n\nБудь краток"}
    assert ru[-1] == {"role": "user", "content": "привет"}
    en = build_messages(_user("en"), [], "hi", 1000)
    assert en[0] == {"role": "system", "content": "Answer in English."}


def test_build_context_history_attachments():
    hist = [
        _msg(
            "user",
            "сравни",
            [
                {"name": "a.txt", "kind": "text", "text": "содержимое"},
                {"name": "p.png", "kind": "image"},
            ],
        ),
        _msg("assistant", "ок"),
    ]
    out = build_messages(_user(), hist, "дальше", 10000)
    first = out[1]["content"]
    assert first.startswith('<file name="a.txt">')
    assert "содержимое</file>" in first.replace("\n", "")
    assert "[изображение: p.png]" in first
    assert first.endswith("сравни")
    assert "data:" not in first
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]


def test_build_context_trims_oldest():
    hist = [_msg("user", "a" * 300), _msg("assistant", "b" * 300), _msg("user", "c" * 300)]
    sys_tokens = estimate_tokens("Отвечай на русском языке.")
    # room for system + new + two history messages (100 tokens each)
    budget = sys_tokens + estimate_tokens("новое") + 200
    out = build_messages(_user(), hist, "новое", budget)
    contents = [m["content"][0] for m in out[1:]]
    assert contents == ["b", "c", "н"]  # oldest dropped, chronological order kept


def test_build_context_keeps_last_user_message_over_budget():
    out = build_messages(_user(), [_msg("user", "x" * 900)], "y" * 3000, 10)
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[1]["content"] == "y" * 3000


def test_user_content_text_and_images():
    atts = [
        Attachment(name="a.txt", kind="text", text="T"),
        Attachment(name="i.jpg", kind="image", image_data_url="data:image/jpeg;base64,AAA"),
    ]
    assert user_content("q", atts[:1]) == '<file name="a.txt">\nT\n</file>\n\nq'
    parts = user_content("q", atts)
    assert parts[0] == {"type": "text", "text": '<file name="a.txt">\nT\n</file>\n\nq'}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}
    assert user_content("q", []) == "q"


def test_image_part_estimated_flat_1000():
    parts = user_content("", [Attachment(name="i", kind="image", image_data_url="data:x;base64,A")])
    hist = [_msg("user", "z" * 30)]  # 10 tokens
    sys_tokens = estimate_tokens("Отвечай на русском языке.")
    out = build_messages(_user(), hist, parts, sys_tokens + 1000 + 9)
    assert len(out) == 2  # history (10) doesn't fit
    out = build_messages(_user(), hist, parts, sys_tokens + 1000 + 10)
    assert len(out) == 3


def test_context_overflow_message():
    from app.chat.context import context_overflow

    user = _user("ru")  # system prompt of 25 characters -> 9 tokens
    assert context_overflow(user, "x" * 300, 24000) is None
    content = user_content("вопрос", [Attachment("big.txt", "text", text="a" * 90_000)])
    msg = context_overflow(user, content, 24000)
    assert msg == (
        "Вложения слишком большие для контекста модели (≈31 тыс. токенов при лимите 24 тыс.). "
        "Сократите файлы."
    )
    # exactly at the boundary - allowed; the user's system prompt is counted
    assert context_overflow(user, "x" * (3 * (24000 - 9)), 24000) is None
    assert context_overflow(_user("ru", "p" * 30), "x" * (3 * (24000 - 9)), 24000) is not None
