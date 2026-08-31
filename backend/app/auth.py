import hmac

from fastapi import HTTPException, Request, status

from app.config import get_settings


def require_internal_token(request: Request) -> None:
    """Guard every endpoint except `/healthz`, `/public/flaky`, and the webhook.

    `/internal/metrics` is guarded too: the tunnel routes the whole hostname, so
    "internal" is a name, not a boundary (SPEC §8).
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = get_settings().internal_api_token
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
