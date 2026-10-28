import hmac

from fastapi import HTTPException, Request


def verify_csrf(supplied, session) -> None:
    """403 unless `supplied` matches the session's CSRF token."""
    if not isinstance(supplied, str) or not supplied:
        raise HTTPException(status_code=403)
    if not hmac.compare_digest(supplied.encode(), session.csrf_token.encode()):
        raise HTTPException(status_code=403)


async def require_csrf(request: Request, session) -> None:
    """Compares X-CSRF-Token or the csrf_token form field with the session token."""
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        try:
            form = await request.form()
        except Exception:
            form = {}
        supplied = form.get("csrf_token")
    verify_csrf(supplied, session)
