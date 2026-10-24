from datetime import datetime
from sqlalchemy import String, DateTime, Integer, Text, ForeignKey, Boolean, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models import migrations
from app.models.migrations import SCHEMA_VERSION  # re-exported: callers read it from here


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    description: Mapped[str] = mapped_column(Text, default="")

    runs: Mapped[list["Run"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    environments: Mapped[list["Environment"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    templates: Mapped[list["RunTemplate"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class User(Base):
    """A named account with a role.

    Nothing enforces roles yet — this is the schema half of multi-user. Auth still
    falls back to the single shared password when the table is empty, so existing
    installs keep working.

    `password_hash` carries its own KDF parameters (see core/users.py); it is never
    exposed by the API.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin | operator | viewer
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Credential(Base):
    """A reusable secret (SSH key, vault password, become password, WireGuard config).

    The secret value itself lives in a 0600 file under <data_dir>/credentials/<id>.priv,
    not in the DB. The DB row is metadata + public material (e.g. SSH public key).
    """
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))   # ssh_key | vault_password | become_password | wireguard_key
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    public_part: Mapped[str] = mapped_column(Text, default="")  # SSH pub key, optional
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Environment(Base):
    """A named environment within a project (e.g. production, staging).

    Holds the inventory path for that environment and an optional default credential
    so users can pick "prod" once instead of restating inventory + key on every run.
    """
    __tablename__ = "environments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    inventory_path: Mapped[str] = mapped_column(String(512), default="")
    default_credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped[Project] = relationship(back_populates="environments")


class RunTemplate(Base):
    """A saved set of run parameters. One-click replay."""
    __tablename__ = "run_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    playbook: Mapped[str] = mapped_column(String(512))
    inventory: Mapped[str] = mapped_column(String(512), default="")
    environment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[str] = mapped_column(String(512), default="")
    skip_tags: Mapped[str] = mapped_column(String(512), default="")
    limit: Mapped[str] = mapped_column(String(512), default="")
    check: Mapped[bool] = mapped_column(Boolean, default=False)
    syntax_check: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_vars_json: Mapped[str] = mapped_column(Text, default="{}")
    credential_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped[Project] = relationship(back_populates="templates")


class AppSetting(Base):
    """Generic key/value app configuration that can change at runtime.

    Used for AI provider config so users can pick provider, key, model, timeout
    from the UI instead of editing docker-compose. Values flagged `encrypted=True`
    are Fernet-sealed using the same master key that protects credentials.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Schedule(Base):
    """Cron-style trigger that fires a saved RunTemplate.

    APScheduler holds an in-memory copy of every enabled schedule; the row here
    is the durable source of truth (and the audit trail for last_run_at).
    """
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    template_id: Mapped[int] = mapped_column(Integer)  # references RunTemplate.id
    cron_expr: Mapped[str] = mapped_column(String(64))  # 5-field crontab
    timezone: Mapped[str] = mapped_column(String(64), default="")  # IANA name; '' = UTC
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditEvent(Base):
    """An append-only record of a sensitive action, for the repudiation side of
    the threat model: who did what, when, and whether it succeeded.

    Covers the two surfaces that previously left no trail of their own — the AI
    agent's mutating/destructive actions, and reads of decrypted credential
    material (vault reveal, credentials pulled into a run). Runs themselves are
    already attributed by `Run.user_id`; this table is for everything else.

    `user_id` is nullable for the same reason it is on `Run`: in single-password
    and no-auth mode there is no account to attribute, so attribution only starts
    meaning something once accounts exist. The row is written regardless — an
    unattributed "a secret was revealed" is still worth having.

    New table, so `create_all` builds it on every boot (fresh or existing) and no
    migration step is needed — same reasoning as the `users` table in _m002.
    """
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)   # e.g. agent.write_file, secret.vault_view
    target: Mapped[str] = mapped_column(String(512), default="")  # project id, path, credential name
    detail: Mapped[str] = mapped_column(Text, default="")         # short human-readable context
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    environment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # nulled = ad-hoc; set = scheduled
    playbook: Mapped[str] = mapped_column(String(512))
    inventory: Mapped[str] = mapped_column(String(512), default="")
    tags: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stats_json: Mapped[str] = mapped_column(Text, default="")
    failures_json: Mapped[str] = mapped_column(Text, default="")
    artifacts_json: Mapped[str] = mapped_column(Text, default="")  # files the run wrote into the repo
    # Who started it. Null for runs from before multi-user, for scheduled runs,
    # and while the app is in single-password mode — the audit trail only starts
    # meaning something once accounts exist.
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    project: Mapped[Project] = relationship(back_populates="runs")


engine = create_async_engine(settings.db_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Per-connection SQLite tuning for the scheduler+UI concurrency we have.

    - WAL: readers (UI) don't block the writer (a scheduled run finalising its row),
      which is the main source of `database is locked` in this app.
    - busy_timeout: wait up to 5s for a lock instead of erroring immediately.
    - synchronous=NORMAL: safe with WAL, much less fsync overhead.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


async def init_db() -> None:
    """Create missing tables, then bring the schema up to `SCHEMA_VERSION`.

    The freshness check has to happen before `create_all` — that's the only
    moment a brand-new database is distinguishable from a pre-0.1.0 one that was
    never version-stamped. See `app.models.migrations`.
    """
    async with engine.begin() as conn:
        fresh = await conn.run_sync(migrations.is_fresh_database)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(lambda sync_conn: migrations.run(sync_conn, fresh=fresh))
