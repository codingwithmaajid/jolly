from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.audit import router as audit_router
from backend.api.bolna import router as bolna_router
from backend.api.calls import router as calls_router
from backend.api.cases import router as cases_router
from backend.api.health import router as health_router
from backend.api.updates import router as updates_router
from backend.core.config import Settings, get_settings
from backend.core.db import init_db, make_engine
from backend.core.logging import configure_logging

load_dotenv()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
    )

    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    init_db(app.state.engine)
    if settings.seed_demo_data:
        from sqlmodel import Session

        from backend.data.seed import seed

        with Session(app.state.engine) as session:
            seed(session)

    from sqlmodel import Session as _Session

    from backend.services.embeddings import get_embedder
    from backend.services.ingest import ensure_index
    from backend.services.llm import get_llm

    app.state.embedder = get_embedder(settings)
    app.state.llm = get_llm(settings)
    try:
        with _Session(app.state.engine) as session:
            ensure_index(session, app.state.embedder)
    except Exception as exc:  # e.g. Gemini quota: fall back to the offline embedder
        import logging

        logging.getLogger(__name__).warning("Embedding index failed (%s); using offline embedder.", exc)
        from backend.services.embeddings import HashingEmbedder

        app.state.embedder = HashingEmbedder()
        with _Session(app.state.engine) as session:
            ensure_index(session, app.state.embedder)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    register_exception_handlers(app, settings)
    register_routes(app, settings)

    return app


def register_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "service": "legal-agent",
            "version": settings.app_version,
        }

    app.include_router(health_router)
    app.include_router(calls_router)
    app.include_router(bolna_router)
    app.include_router(cases_router)
    app.include_router(updates_router)
    app.include_router(audit_router)


def register_exception_handlers(app: FastAPI, settings: Settings) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "HTTP_ERROR",
                    "message": exc.detail,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        if settings.app_env.lower() != "production":
            message = str(exc) or "An unexpected error occurred."
        else:
            message = "An unexpected error occurred."

        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": message,
                }
            },
        )


app = create_app()
