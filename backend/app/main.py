import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core import auth

from app.api import projects as projects_api
from app.api import runs as runs_api
from app.api import dashboard as dashboard_api
from app.api import credentials as credentials_api
from app.api import templates as templates_api
from app.api import ai as ai_api
from app.api import schedules as schedules_api
from app.api import library as library_api
from app.api import environments as environments_api
from app.core.scheduler import get_scheduler, load_all as load_schedules
from app.core.config import settings
from app.core import doc_index
from app.models.db import init_db

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Available in every template so the nav can show a Logout link only when auth is on.
templates.env.globals["auth_enabled"] = auth.auth_enabled


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler = get_scheduler()
    scheduler.start()
    await load_schedules()
    # Warm the BM25 doc index in the background so the first chat doesn't pay the
    # one-off `ansible-doc` enumeration cost (which could blow the request timeout).
    asyncio.create_task(asyncio.to_thread(doc_index.search_modules, "warm up", 1))
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Playforge", lifespan=lifespan)


# Paths reachable without a session (login itself, static assets, health probe).
_PUBLIC_PREFIXES = ("/login", "/static", "/health")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Gate every request behind a session cookie when ANSIBLE_GUI_PASSWORD is set.
    No-op when auth is disabled (no password) — keeps existing installs working."""
    if not auth.auth_enabled():
        return await call_next(request)
    path = request.url.path
    if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    if auth.verify_token(request.cookies.get(auth.SESSION_COOKIE)):
        return await call_next(request)
    # Unauthenticated: APIs get 401 JSON, pages get redirected to /login.
    if path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(projects_api.router)
app.include_router(runs_api.router)
app.include_router(dashboard_api.router)
app.include_router(credentials_api.router)
app.include_router(templates_api.router)
app.include_router(ai_api.router)
app.include_router(schedules_api.router)
app.include_router(library_api.router)
app.include_router(environments_api.router)


@app.get("/health")
async def health():
    return {"status": "ok", "data_dir": str(settings.data_dir)}


def _set_session(resp, request: Request):
    secure = request.url.scheme == "https"
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_token(), max_age=7 * 24 * 3600,
                    httponly=True, samesite="lax", secure=secure)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if not auth.auth_enabled() or auth.verify_token(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    if not auth.auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    if auth.check_password(password):
        return _set_session(RedirectResponse(url="/", status_code=303), request)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": "Wrong password."}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"active_nav": "dashboard"})


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    return templates.TemplateResponse(request, "projects.html", {"active_nav": "projects"})


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    return templates.TemplateResponse(
        request, "project.html",
        {"project_id": project_id, "active_nav": "projects"},
    )


@app.get("/projects/{project_id}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, project_id: str, run_id: int):
    return templates.TemplateResponse(
        request, "run_detail.html",
        {"project_id": project_id, "run_id": run_id, "active_nav": "projects"},
    )


@app.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    return templates.TemplateResponse(request, "runs.html", {"active_nav": "runs"})


@app.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request):
    return templates.TemplateResponse(request, "assistant.html", {"active_nav": "assistant"})


@app.get("/credentials", response_class=HTMLResponse)
async def credentials_page(request: Request):
    return templates.TemplateResponse(request, "credentials.html", {"active_nav": "credentials"})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {"active_nav": "settings"})


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    return templates.TemplateResponse(request, "schedules.html", {"active_nav": "schedules"})


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)


if __name__ == "__main__":
    run()
