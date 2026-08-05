import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
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
from app.models.db import init_db, SCHEMA_VERSION

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Available in every template so the nav can show a Logout link only when auth is on.
templates.env.globals["auth_enabled"] = auth.auth_enabled
# So the sidebar footer reports the real running version instead of a hardcoded one.
templates.env.globals["app_version"] = __version__


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


logger = logging.getLogger(__name__)
app = FastAPI(title="Playforge", version=__version__, lifespan=lifespan)


# Paths reachable without a session (login itself, static assets, health probe).
_PUBLIC_PREFIXES = ("/login", "/static", "/health")


# script-src is permissive on purpose. Every page template carries an inline
# <script> block, and the vendored htmx builds handlers with Function()/eval(),
# so 'unsafe-inline'/'unsafe-eval' are load-bearing — dropping them blanks the UI.
# The value here is in the directives that cost nothing to enforce:
#   connect-src 'self'  an injected script can't exfiltrate to another host
#   frame-ancestors     clickjacking
#   object-src/base-uri/form-action  plugin and base-tag injection, form hijack
# script-src 'self' still blocks loading code from another origin, which is worth
# having now that no asset comes from a CDN.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])

_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",          # for anything predating frame-ancestors
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Set security headers on every response, without overriding an explicit one."""
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


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

# Monaco is baked into the image at /opt/playforge/vendor (see Dockerfile) rather
# than under app/static, so the dev compose override's bind-mount of backend/app
# can't hide it. Missing outside the image — the editor is the only thing affected,
# so warn instead of refusing to start.
VENDOR_DIR = Path(os.getenv("PLAYFORGE_VENDOR_DIR", "/opt/playforge/vendor"))
if VENDOR_DIR.is_dir():
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")
else:
    logging.getLogger(__name__).warning(
        "vendor dir %s not found — the Monaco editor won't load. Run the app from the "
        "Docker image, or set PLAYFORGE_VENDOR_DIR.", VENDOR_DIR)
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
    """Liveness + DB ping. Returns 503 when the database is unreachable so
    `docker compose`/k8s healthchecks can restart the container automatically."""
    from sqlalchemy import text
    from app.models.db import SessionLocal as _SL

    db_ok = True
    try:
        async with _SL() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        logger.exception("health check DB error: %s", e)

    payload = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "data_dir": str(settings.data_dir),
        "db": "ok" if db_ok else "error",
    }
    if not db_ok:
        return JSONResponse(payload, status_code=503)
    return payload


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


def _client_ip(request: Request) -> str:
    """Peer address for throttling. Not X-Forwarded-For: uvicorn runs without
    --proxy-headers, so a client could set that header itself and dodge lockout."""
    return request.client.host if request.client else "unknown"


@app.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    if not auth.auth_enabled():
        return RedirectResponse(url="/", status_code=303)

    client = _client_ip(request)
    remaining = auth.lockout_remaining(client)
    if remaining > 0:
        # 429 + Retry-After rather than 401: the password wasn't even checked.
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"Too many failed attempts. Try again in {int(remaining) + 1}s."},
            status_code=429, headers={"Retry-After": str(int(remaining) + 1)})

    if auth.check_password(password):
        auth.record_success(client)
        return _set_session(RedirectResponse(url="/", status_code=303), request)

    penalty = auth.record_failure(client)
    if penalty > 0:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"Too many failed attempts. Try again in {int(penalty)}s."},
            status_code=429, headers={"Retry-After": str(int(penalty))})
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
