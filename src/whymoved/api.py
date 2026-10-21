"""HTTP layer. Thin on purpose: each route calls one `Service` method.

Run locally:
    pip install -r requirements-api.txt
    uvicorn whymoved.api:create_app --factory --reload --app-dir src
    # interactive docs: http://127.0.0.1:8000/docs

Settings come from environment variables: WHYMOVED_DB, WHYMOVED_DATA_DIR,
WHYMOVED_MAX_UPLOAD_MB, WHYMOVED_CORS (comma-separated allowed browser origins) and
WHYMOVED_WEB (folder with the browser app; default: the repo's web/ folder, served at /app/).

Note: no `from __future__ import annotations` here, because FastAPI reads the annotations of
route functions at run time.
"""

import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from whymoved.service import Service, ServiceError, ServiceSettings


class RegisterBody(BaseModel):
    email: str
    password: str
    workspace_name: str | None = None


class LoginBody(BaseModel):
    email: str
    password: str


class WorkspaceBody(BaseModel):
    name: str


class MemberBody(BaseModel):
    email: str


class AnalysisBody(BaseModel):
    kind: str | None = None
    revenue: str | None = None
    units: str | None = None
    cost: str | None = None
    market: str | None = None
    model: str | None = None
    mapping: dict[str, str] | None = None
    from_period: str | None = None
    to_period: str | None = None
    financial: bool | None = None
    metric: str | None = None
    period_a: list[str] | None = None
    period_b: list[str] | None = None
    mode: str = "previous"
    currency: str = ""
    indian: bool = False
    dimensions: list[str] | None = None


class ShareBody(BaseModel):
    days: int | None = None


class NoteBody(BaseModel):
    body: str
    public: bool = False


def settings_from_env() -> ServiceSettings:
    s = ServiceSettings()
    s.db_path = os.environ.get("WHYMOVED_DB", s.db_path)
    s.data_dir = os.environ.get("WHYMOVED_DATA_DIR", s.data_dir)
    if os.environ.get("WHYMOVED_MAX_UPLOAD_MB"):
        s.max_upload_bytes = int(os.environ["WHYMOVED_MAX_UPLOAD_MB"]) * 1024 * 1024
    return s


def create_app(service: Service | None = None) -> FastAPI:
    svc = service or Service(settings_from_env())
    app = FastAPI(title="WhyMoved", version="0.10",
                  description="Finds where a business metric moved. Not why.")
    origins = [o.strip() for o in os.environ.get(
        "WHYMOVED_CORS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status,
                            content={"error": {"code": exc.code, "message": exc.message}})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > svc.s.max_upload_bytes + 1024 * 1024:
            return JSONResponse(status_code=413, content={"error": {
                "code": "too_large", "message": "the request body is too large"}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/app"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def bearer(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ServiceError(401, "not_authenticated", "sign in again")
        return authorization[7:].strip()

    def current_user(token: str = Depends(bearer)) -> dict:
        return svc.authenticate(token)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---- accounts
    @app.post("/auth/register", status_code=201)
    def register(body: RegisterBody):
        return svc.register(body.email, body.password, body.workspace_name)

    @app.post("/auth/login")
    def login(body: LoginBody):
        return svc.login(body.email, body.password)

    @app.post("/auth/logout", status_code=204)
    def logout(token: str = Depends(bearer)):
        svc.logout(token)

    @app.get("/me")
    def me(user: dict = Depends(current_user)):
        return svc.me(user)

    # ---- workspaces
    @app.post("/workspaces", status_code=201)
    def create_workspace(body: WorkspaceBody, user: dict = Depends(current_user)):
        return svc.create_workspace(user, body.name)

    @app.get("/workspaces/{wid}/members")
    def members(wid: str, user: dict = Depends(current_user)):
        return svc.members(user, wid)

    @app.post("/workspaces/{wid}/members", status_code=201)
    def add_member(wid: str, body: MemberBody, user: dict = Depends(current_user)):
        return svc.add_member(user, wid, body.email)

    # ---- datasets
    @app.get("/workspaces/{wid}/datasets")
    def list_datasets(wid: str, user: dict = Depends(current_user)):
        return svc.list_datasets(user, wid)

    @app.post("/workspaces/{wid}/datasets", status_code=201)
    def upload(wid: str, file: UploadFile = File(...), name: str | None = Form(default=None),
               user: dict = Depends(current_user)):
        data = file.file.read(svc.s.max_upload_bytes + 1)
        return svc.upload_dataset(user, wid, file.filename or "upload", data, name)

    @app.post("/workspaces/{wid}/demo", status_code=201)
    def demo(wid: str, user: dict = Depends(current_user)):
        return svc.create_demo(user, wid)

    @app.get("/datasets/{did}")
    def get_dataset(did: str, user: dict = Depends(current_user)):
        return svc.get_dataset(user, did)

    @app.delete("/datasets/{did}", status_code=204)
    def delete_dataset(did: str, user: dict = Depends(current_user)):
        svc.delete_dataset(user, did)

    # ---- analyses
    @app.post("/datasets/{did}/analyses", status_code=201)
    def create_analysis(did: str, body: AnalysisBody, user: dict = Depends(current_user)):
        return svc.create_analysis(user, did, body.model_dump(exclude_none=True))

    @app.get("/datasets/{did}/analyses")
    def list_analyses(did: str, user: dict = Depends(current_user)):
        return svc.list_analyses(user, did)

    @app.get("/analyses/{aid}")
    def get_analysis(aid: str, user: dict = Depends(current_user)):
        return svc.get_analysis(user, aid)

    @app.delete("/analyses/{aid}", status_code=204)
    def delete_analysis(aid: str, user: dict = Depends(current_user)):
        svc.delete_analysis(user, aid)

    @app.get("/datasets/{did}/scan")
    def scan(did: str, user: dict = Depends(current_user)):
        return svc.scan(user, did)

    @app.post("/analyses/{aid}/notes", status_code=201)
    def add_note(aid: str, body: NoteBody, user: dict = Depends(current_user)):
        return svc.add_note(user, aid, body.body, body.public)

    @app.delete("/analyses/{aid}/notes/{nid}", status_code=204)
    def delete_note(aid: str, nid: str, user: dict = Depends(current_user)):
        svc.delete_note(user, aid, nid)

    @app.get("/compare")
    def compare(a: str, b: str, user: dict = Depends(current_user)):
        return svc.compare(user, a, b)

    # ---- sharing
    @app.post("/analyses/{aid}/share", status_code=201)
    def create_share(aid: str, body: ShareBody, user: dict = Depends(current_user)):
        return svc.create_share(user, aid, body.days)

    @app.delete("/analyses/{aid}/share")
    def revoke_share(aid: str, user: dict = Depends(current_user)):
        return {"revoked": svc.revoke_shares(user, aid)}

    @app.get("/shared/{token}")
    def shared(token: str):
        return svc.shared_result(token)

    web_dir = Path(os.environ.get("WHYMOVED_WEB") or Path(__file__).resolve().parents[2] / "web")
    if (web_dir / "index.html").is_file():
        app.mount("/app", StaticFiles(directory=str(web_dir), html=True), name="web")

        @app.get("/", include_in_schema=False)
        def home():
            return RedirectResponse("/app/")

    return app
