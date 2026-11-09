import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .store import Store


def create_app(store: Store, *, chat=None, launch_key: str | None = None) -> FastAPI:
    from .access import AccessControlUnavailable
    from .budget import SpendingLimitExceeded
    from .chat import Chat, ChatBusy, ChatRequest, ChatUnavailable
    from .semantic import SemanticUnavailable

    chat = chat or Chat(store)
    token = store.token()
    session_token = secrets.token_urlsafe(32)
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials and secrets.compare_digest(credentials.credentials, token):
            return
        if secrets.compare_digest(request.cookies.get("mneme_session", ""), session_token):
            if request.method not in {"GET", "HEAD"}:
                expected_origin = str(request.base_url).rstrip("/")
                if request.headers.get("origin") != expected_origin:
                    raise HTTPException(403, "Same-origin request required")
            return
        raise HTTPException(401, "Invalid bearer token")

    app = FastAPI(
        title="Work Memory",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    static = Path(__file__).parent / "web"

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(static / "index.html")

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(static / name)

    @app.get("/connect/{key}", include_in_schema=False)
    def connect(key: str):
        nonlocal launch_key
        if not launch_key or not secrets.compare_digest(key, launch_key):
            raise HTTPException(401, "Link expired")
        launch_key = None
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("mneme_session", session_token, httponly=True, samesite="strict")
        return response

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def unavailable(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": "Resource unavailable"})

    @app.exception_handler(SemanticUnavailable)
    async def semantic_unavailable(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail": str(exc), "fallback": "lexical"})

    @app.exception_handler(AccessControlUnavailable)
    async def access_unavailable(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail": str(exc)})

    api = APIRouter(dependencies=[Depends(authorize)])
    api.get("/v1/status")(store.status)
    api.get("/v1/search")(store.search)
    api.get("/v1/facts")(store.facts)
    api.get("/v1/observations/{oid}")(store.observation)
    api.get("/v1/facts/{fact_id}/history")(store.history)
    api.get("/v1/chat/info")(chat.info)

    @api.post("/v1/chat")
    def answer(request: ChatRequest):
        try:
            return chat.answer(request)
        except ChatBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        except SpendingLimitExceeded as exc:
            raise HTTPException(429, "Budget cloud non disponibile o esaurito.") from exc
        except (ChatUnavailable, RuntimeError, TimeoutError, OSError) as exc:
            raise HTTPException(
                503,
                "Haiku non disponibile o risposta non verificabile. "
                "Controlla Docker, login aziendale e log privati prima di riprovare.",
            ) from exc

    app.include_router(api)
    return app
