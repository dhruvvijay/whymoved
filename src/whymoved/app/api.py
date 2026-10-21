"""FastAPI adapter. Every route is a thin call into WhyMovedService (service.py).

Run:  python scripts/run_api.py        then open http://127.0.0.1:8000/docs

Authentication: send `Authorization: Bearer <token>` (the token comes from POST /auth/login).
Errors are JSON: {"error": {"code": "...", "message": "..."}}.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from whymoved.app.config import Settings
from whymoved.app.errors import ServiceError, TooLarge
from whymoved.app.service import WhyMovedService


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class WorkspaceIn(BaseModel):
    name: str = Field(max_length=200)


class MemberIn(BaseModel):
    email: str = Field(max_length=254)


class SampleIn(BaseModel):
    kind: str = "single"
    seed: int = 7


class AnalysisIn(BaseModel):
    dataset_id: str
    title: str | None = Field(default=None, max_length=200)
    mode: str | None = None
    metric: str | None = Field(default=None, max_length=200)
    period_a: list[str] | None = None
    period_b: list[str] | None = None
    dimensions: list[str] | None = None
    currency: str | None = Field(default=None, max_length=8)
    indian: bool | None = None
    revenue_col: str | None = None
    units_col: str | None = None
    cost_col: str | None = None
    market_col: str | None = None
    bridge_dimensions: list[str] | None = None


class ShareIn(BaseModel):
    days: int | None = None


def create_app(settings: Settings | None = None, service: WhyMovedService | None = None
               ) -> FastAPI:
    settings = settings or Settings.from_env()
    svc = service or WhyMovedService(settings)
    app = FastAPI(title="WhyMoved API", version="0.10.0",
                  description="Explains where a business metric moved. Never why.")
    app.state.service = svc
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"error": {"code": exc.code, "message": exc.message}})

    def bearer(authorization: str | None = Header(default=None)) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return None

    def current_user(token: str | None = Depends(bearer)) -> dict:
        return svc.authenticate(token)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---- accounts
    @app.post("/auth/register", status_code=201)
    def register(body: Credentials) -> dict:
        return svc.register(body.email, body.password)

    @app.post("/auth/login")
    def login(body: Credentials) -> dict:
        return svc.login(body.email, body.password)

    @app.post("/auth/logout", status_code=204)
    def logout(token: str | None = Depends(bearer)) -> None:
        svc.logout(token)

    @app.get("/me")
    def me(user: dict = Depends(current_user)) -> dict:
        return user

    # ---- workspaces
    @app.post("/workspaces", status_code=201)
    def create_workspace(body: WorkspaceIn, user: dict = Depends(current_user)) -> dict:
        return svc.create_workspace(user, body.name)

    @app.get("/workspaces")
    def list_workspaces(user: dict = Depends(current_user)) -> list[dict]:
        return svc.list_workspaces(user)

    @app.delete("/workspaces/{workspace_id}", status_code=204)
    def delete_workspace(workspace_id: str, user: dict = Depends(current_user)) -> None:
        svc.delete_workspace(user, workspace_id)

    @app.get("/workspaces/{workspace_id}/members")
    def list_members(workspace_id: str, user: dict = Depends(current_user)) -> list[dict]:
        return svc.list_members(user, workspace_id)

    @app.post("/workspaces/{workspace_id}/members", status_code=201)
    def add_member(workspace_id: str, body: MemberIn, user: dict = Depends(current_user)) -> dict:
        return svc.add_member(user, workspace_id, body.email)

    # ---- datasets
    @app.post("/workspaces/{workspace_id}/datasets", status_code=201)
    async def upload_dataset(workspace_id: str, file: UploadFile = File(...),
                             user: dict = Depends(current_user)) -> dict:
        limit = settings.max_upload_bytes
        payload = await file.read(limit + 1)  # never read more than the limit allows
        if len(payload) > limit:
            raise TooLarge(f"the file is larger than {limit // 2**20} MB")
        # profiling a file can take seconds, so keep it off the event loop
        return await run_in_threadpool(svc.upload_dataset, user, workspace_id,
                                       file.filename or "", payload)

    @app.post("/workspaces/{workspace_id}/datasets/sample", status_code=201)
    def create_sample(workspace_id: str, body: SampleIn, user: dict = Depends(current_user)
                      ) -> dict:
        return svc.create_sample(user, workspace_id, body.kind, body.seed)

    @app.get("/workspaces/{workspace_id}/datasets")
    def list_datasets(workspace_id: str, user: dict = Depends(current_user)) -> list[dict]:
        return svc.list_datasets(user, workspace_id)

    @app.get("/workspaces/{workspace_id}/datasets/{dataset_id}")
    def get_dataset(workspace_id: str, dataset_id: str, user: dict = Depends(current_user)
                    ) -> dict:
        return svc.get_dataset(user, workspace_id, dataset_id)

    @app.delete("/workspaces/{workspace_id}/datasets/{dataset_id}", status_code=204)
    def delete_dataset(workspace_id: str, dataset_id: str, user: dict = Depends(current_user)
                       ) -> None:
        svc.delete_dataset(user, workspace_id, dataset_id)

    # ---- analyses
    @app.post("/workspaces/{workspace_id}/analyses", status_code=201)
    def run_analysis(workspace_id: str, body: AnalysisIn, user: dict = Depends(current_user)
                     ) -> dict:
        params = body.model_dump(exclude_none=True, exclude={"dataset_id", "title"})
        return svc.run_analysis(user, workspace_id, body.dataset_id, params, body.title)

    @app.get("/workspaces/{workspace_id}/analyses")
    def list_analyses(workspace_id: str, user: dict = Depends(current_user)) -> list[dict]:
        return svc.list_analyses(user, workspace_id)

    @app.get("/workspaces/{workspace_id}/analyses/{analysis_id}")
    def get_analysis(workspace_id: str, analysis_id: str, user: dict = Depends(current_user)
                     ) -> dict:
        return svc.get_analysis(user, workspace_id, analysis_id)

    @app.delete("/workspaces/{workspace_id}/analyses/{analysis_id}", status_code=204)
    def delete_analysis(workspace_id: str, analysis_id: str, user: dict = Depends(current_user)
                        ) -> None:
        svc.delete_analysis(user, workspace_id, analysis_id)

    # ---- share links
    @app.post("/workspaces/{workspace_id}/analyses/{analysis_id}/shares", status_code=201)
    def create_share(workspace_id: str, analysis_id: str, body: ShareIn,
                     user: dict = Depends(current_user)) -> dict:
        return svc.create_share(user, workspace_id, analysis_id, body.days)

    @app.get("/workspaces/{workspace_id}/analyses/{analysis_id}/shares")
    def list_shares(workspace_id: str, analysis_id: str, user: dict = Depends(current_user)
                    ) -> list[dict]:
        return svc.list_shares(user, workspace_id, analysis_id)

    @app.delete("/workspaces/{workspace_id}/analyses/{analysis_id}/shares/{share_id}",
                status_code=204)
    def revoke_share(workspace_id: str, analysis_id: str, share_id: str,
                     user: dict = Depends(current_user)) -> None:
        svc.revoke_share(user, workspace_id, analysis_id, share_id)

    @app.get("/shared/{token}")
    def shared(token: str) -> dict:
        return svc.get_shared(token)

    return app


# `uvicorn whymoved.app.api:app` creates the app with settings from the environment.
def __getattr__(name: str):
    if name == "app":
        return create_app()
    raise AttributeError(name)
