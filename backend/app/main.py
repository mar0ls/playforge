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
from app.api import users as users_api
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
    from app.core import bootstrap
    await bootstrap.prepare()
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
_PUBLIC_PREFIXES = ("/login", "/setup", "/static", "/health")


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


def _deny(path: str, status: int, detail: str):
    """APIs get JSON, pages get sent to the login screen."""
    if path.startswith("/api/"):
        return JSONResponse({"detail": detail}, status_code=status)
    if status == 401:
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({"detail": detail}, status_code=status)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Authenticate, then authorise.

    Three modes, resolved per request so creating the first account switches the
    app over without a restart:

    * **accounts exist** — the cookie names a user; the role decides what they may
      call (see core/authz).
    * **no accounts, ANSIBLE_GUI_PASSWORD set** — the pre-existing shared-password
      behaviour, unchanged. There is no user to attribute or restrict, so the
      session is treated as full access.
    * **neither** — no auth at all, as before.

    The user is re-read from the database on every request rather than trusted
    from the cookie, so disabling or deleting an account ends its sessions
    immediately instead of at token expiry.
    """
    path = request.url.path
    if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    from app.core import authz, users as users_core

    multi_user = await users_core.multi_user_enabled()

    if not multi_user:
        if not auth.auth_enabled():
            request.state.user = None
            return await call_next(request)
        if not auth.verify_token(request.cookies.get(auth.SESSION_COOKIE)):
            return _deny(path, 401, "authentication required")
        request.state.user = None
        return await call_next(request)

    session = auth.read_token(request.cookies.get(auth.SESSION_COOKIE))
    if session is None or session.user_id is None:
        return _deny(path, 401, "authentication required")

    user = await users_core.get(session.user_id)
    if user is None or user.disabled:
        return _deny(path, 401, "authentication required")

    request.state.user = user
    if path.startswith("/api/") and not authz.allowed(user.role, request.method, path):
        return JSONResponse(
            {"detail": f"role '{user.role}' is not allowed to do this"}, status_code=403)
    return await call_next(request)


# Registered after require_login on purpose. Starlette wraps middleware in reverse
# registration order, so the last one added is the outermost — which is what makes
# these headers reach responses that require_login short-circuits (401/403), not
# just the ones that get through to a route.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Set security headers on every response, without overriding an explicit one."""
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


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
app.include_router(users_api.router)


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


def _set_session(resp, request: Request, user_id: int | None = None):
    secure = request.url.scheme == "https"
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_token(user_id), max_age=7 * 24 * 3600,
                    httponly=True, samesite="lax", secure=secure)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    from app.core import users as users_core

    multi_user = await users_core.multi_user_enabled()
    if not multi_user and not auth.auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    if auth.verify_token(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": error, "multi_user": multi_user})


def _client_ip(request: Request) -> str:
    """Peer address for throttling. Not X-Forwarded-For: uvicorn runs without
    --proxy-headers, so a client could set that header itself and dodge lockout."""
    return request.client.host if request.client else "unknown"


@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    from app.core import users as users_core

    multi_user = await users_core.multi_user_enabled()
    if not multi_user and not auth.auth_enabled():
        return RedirectResponse(url="/", status_code=303)

    # Throttling comes first in both modes: a locked-out client shouldn't get its
    # password checked, and shouldn't learn whether a username exists either.
    client = _client_ip(request)
    remaining = auth.lockout_remaining(client)
    if remaining > 0:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"Too many failed attempts. Try again in {int(remaining) + 1}s.",
             "multi_user": multi_user},
            status_code=429, headers={"Retry-After": str(int(remaining) + 1)})

    if multi_user:
        user = await users_core.authenticate(username, password)
        ok, user_id = (user is not None), (user.id if user else None)
        # Deliberately the same message for a bad username and a bad password.
        error = "Wrong username or password."
    else:
        ok, user_id = auth.check_password(password), None
        error = "Wrong password."

    if ok:
        auth.record_success(client)
        return _set_session(RedirectResponse(url="/", status_code=303), request, user_id)

    penalty = auth.record_failure(client)
    if penalty > 0:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"Too many failed attempts. Try again in {int(penalty)}s.",
             "multi_user": multi_user},
            status_code=429, headers={"Retry-After": str(int(penalty))})
    return templates.TemplateResponse(request, "login.html",
                                      {"error": error, "multi_user": multi_user},
                                      status_code=401)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, error: str = ""):
    from app.core import bootstrap

    if bootstrap.setup_token() is None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": error})


@app.post("/setup")
async def setup_submit(request: Request, token: str = Form(""), username: str = Form(""),
                       password: str = Form("")):
    """Create the first administrator.

    Requires the token printed to the container log. Without it, publishing the
    port would mean whoever reached it first could claim the instance — the
    window between `docker compose up -d` and the operator opening a browser.
    """
    from app.core import bootstrap, users as users_core

    if bootstrap.setup_token() is None:
        return RedirectResponse(url="/", status_code=303)

    # Throttled like /login: the token is the only thing standing here, so it
    # must not be guessable at speed.
    client = _client_ip(request)
    remaining = auth.lockout_remaining(client)
    if remaining > 0:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": f"Too many attempts. Try again in {int(remaining) + 1}s."},
            status_code=429, headers={"Retry-After": str(int(remaining) + 1)})

    if not bootstrap.check_setup_token(token):
        auth.record_failure(client)
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Wrong setup token. It is printed in the container log."},
            status_code=403)

    try:
        user = await users_core.create(username, password, users_core.ADMIN)
    except users_core.UserError as e:
        return templates.TemplateResponse(request, "setup.html",
                                          {"error": str(e)}, status_code=400)

    auth.record_success(client)
    bootstrap.clear_setup_token()
    logger.warning("first administrator %r created via /setup", user.username)
    return _set_session(RedirectResponse(url="/", status_code=303), request, user.id)


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


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    """Account management. The API behind it is admin-only; a non-admin reaching
    this page sees an empty table rather than a 403 on the page itself."""
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(request, "users.html", {
        "active_nav": "users",
        "current_user_id": getattr(user, "id", None),
    })


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
