# Third-party components

Playforge is built on open-source software. Most frontend libraries are vendored
locally (in `backend/app/static/vendor/`); Monaco Editor is loaded from jsDelivr
CDN by default. All components remain under their own licenses, reproduced/
attributed here.

## Vendored JavaScript

| Library | Version | License | Project |
|---------|---------|---------|---------|
| htmx    | 2.0.3   | BSD-2-Clause / Zero-Clause BSD | https://htmx.org |
| Alpine.js | 3.14.1 | MIT | https://alpinejs.dev |
| Lucide  | 1.17.0  | ISC | https://lucide.dev |
| Monaco Editor | 0.52.0 | MIT (loaded from jsDelivr CDN) | https://microsoft.github.io/monaco-editor |

`backend/app/static/vendor/md.js` is original to this project (no third-party code).

## Python dependencies

All Python dependencies are standard public packages from PyPI, declared in
`backend/requirements.txt`. Notable runtime components and their licenses:

- ansible-core, ansible-runner, ansible-lint — GPL-3.0
- FastAPI, Starlette, Uvicorn, Pydantic — MIT / BSD
- SQLAlchemy, aiosqlite — MIT
- cryptography — Apache-2.0 / BSD
- GitPython — BSD-3-Clause
- APScheduler, croniter, httpx, PyYAML, ruamel.yaml — MIT / BSD / Apache

See each package's distribution for the full license text.
