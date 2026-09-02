import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import get_settings

AUTHORIZED_REPOS_HEADER = "x-authorized-repo-ids"


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


def require_authorized_repos(request: Request) -> list[int]:
    """The repo ids this request may see, from `X-Authorized-Repo-Ids` (SPEC §8b).

    The BFF resolves the set from GitHub's installations API and sends it on every
    read; the bearer token is what makes the header trustworthy, because only the
    Next.js server holds that token and the browser never reaches this service.

    **A missing header is a 400, not an empty set and certainly not "everything".**
    The BFF always knows the answer — a user with no installations sends an empty
    header deliberately — so absence means a caller that forgot, and the loud
    failure is what stops that caller from silently reading every repo. An empty
    *value* is a real answer and yields an empty list, which every query below
    turns into no rows.
    """
    if AUTHORIZED_REPOS_HEADER not in request.headers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"missing {AUTHORIZED_REPOS_HEADER} header",
        )

    raw = request.headers[AUTHORIZED_REPOS_HEADER].strip()
    if not raw:
        return []

    try:
        return [int(part) for part in raw.split(",")]
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{AUTHORIZED_REPOS_HEADER} must be a comma-separated list of integers",
        ) from None


AuthorizedRepos = Annotated[list[int], Depends(require_authorized_repos)]
