import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response

from app.apimetrics import UNMATCHED, get_recorder, write_process_metrics
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.logging import configure_logging, get_logger
from app.routes import api, health, internal, public, webhooks

log = get_logger(__name__)


async def sample_process_metrics_forever(interval_seconds: float) -> None:
    """Drain this process's counters into `metrics_snapshots`, once a minute.

    Failures are logged and the loop continues: a metrics write that cannot reach the
    database must never be the reason the API stops answering requests.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with get_sessionmaker()() as session:
                metrics = await write_process_metrics(session)
                await session.commit()
            log.info("api.metrics", series=len(metrics))
        except Exception as exc:
            log.error("api.metrics_failed", error=f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    sampler = asyncio.create_task(
        sample_process_metrics_forever(get_settings().metrics_interval_seconds)
    )
    yield
    sampler.cancel()
    with suppress(asyncio.CancelledError):
        await sampler
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="flakehound", lifespan=lifespan)

    @app.middleware("http")
    async def record_request_latency(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """API latency, labelled by route template rather than path.

        The route is only on the scope once the router has matched, which is after
        `call_next` — and on an unmatched path it never appears at all. Recording in a
        `finally` means a request that raised still contributes the time it spent.
        """
        started = time.perf_counter()
        try:
            return await call_next(request)
        finally:
            route = request.scope.get("route")
            get_recorder().observe_request(
                getattr(route, "path", None) or UNMATCHED, time.perf_counter() - started
            )

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(api.router)
    app.include_router(internal.router)
    app.include_router(public.router)
    return app


app = create_app()
