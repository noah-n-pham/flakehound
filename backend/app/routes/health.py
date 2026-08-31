from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only — deliberately touches no database.

    One of exactly three unauthenticated endpoints (SPEC §8).
    """
    return {"status": "ok"}
