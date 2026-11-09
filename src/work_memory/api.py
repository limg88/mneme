import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .store import Store


def create_app(store: Store) -> FastAPI:
    from .access import AccessControlUnavailable
    from .semantic import SemanticUnavailable

    token = store.token()
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(401, "Invalid bearer token")

    app = FastAPI(
        title="Work Memory",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authorize)],
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

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

    app.get("/v1/status")(store.status)
    app.get("/v1/search")(store.search)
    app.get("/v1/facts")(store.facts)
    app.get("/v1/observations/{oid}")(store.observation)
    app.get("/v1/facts/{fact_id}/history")(store.history)
    return app
