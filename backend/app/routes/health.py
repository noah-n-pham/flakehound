from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only — deliberately touches no database.

    One of exactly three unauthenticated endpoints.

    `version` is the commit the image was built from. Nothing pushes a deploy to the
    box any more; the box pulls, so this is how CI learns that the image it built is
    the one now answering. It names a public commit of a repository whose whole
    history is public, so it discloses nothing.
    """
    return {"status": "ok", "version": get_settings().git_sha}
