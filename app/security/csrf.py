import hmac

from fastapi import HTTPException, Request


async def require_csrf(request: Request, session) -> None:
    """Сверяет X-CSRF-Token или поле формы csrf_token с токеном сессии."""
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        try:
            form = await request.form()
        except Exception:
            form = {}
        supplied = form.get("csrf_token")
    if not isinstance(supplied, str) or not supplied:
        raise HTTPException(status_code=403)
    if not hmac.compare_digest(supplied.encode(), session.csrf_token.encode()):
        raise HTTPException(status_code=403)
