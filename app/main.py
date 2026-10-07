from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import sessionmaker

from app import api
from app.config import Settings
from app.database import Base, make_engine
from app.services.generator import PdfCertificateGenerator
from app.services.processor import JobDispatcher


def create_app(settings: Settings | None = None, generator=None) -> FastAPI:
    """App factory - tests inject their own settings and (optionally) a fake generator."""
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    generator = generator or PdfCertificateGenerator(settings.font_path)
    dispatcher = JobDispatcher(session_factory, generator, settings.storage_dir,
                               settings.public_base_url, settings.worker_threads,
                               settings.sync_processing)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        dispatcher.recover()  # resume anything interrupted by a restart
        yield
        dispatcher.shutdown()
        engine.dispose()

    app = FastAPI(
        title="Bulk Certificate Generator API",
        description="Submit a list of recipients, track progress, download PDF certificates.",
        version="1.1.0", lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.dispatcher = dispatcher
    app.state.generator = generator
    app.include_router(api.router)
    app.include_router(api.public_router)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")  # friendlier than a 404 for anyone opening the base URL

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    return app