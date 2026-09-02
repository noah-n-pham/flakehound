from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import dispose_engine
from app.logging import configure_logging
from app.routes import api, health, internal, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="flakehound", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(api.router)
    app.include_router(internal.router)
    return app


app = create_app()
