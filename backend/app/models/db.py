from datetime import datetime
from sqlalchemy import String, DateTime, Integer, Text, ForeignKey, Boolean, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


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


def _soft_migrate(sync_conn) -> None:
    """Idempotent SQLite migration: add columns that may not exist in old DBs."""
    cols = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(runs)").fetchall()}
    if "template_id" not in cols:
        sync_conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN template_id INTEGER")
    if "environment_id" not in cols:
        sync_conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN environment_id INTEGER")
    if "schedule_id" not in cols:
        sync_conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN schedule_id INTEGER")
    if "artifacts_json" not in cols:
        sync_conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN artifacts_json TEXT DEFAULT ''")
    sched_cols = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(schedules)").fetchall()}
    if sched_cols and "timezone" not in sched_cols:
        sync_conn.exec_driver_sql("ALTER TABLE schedules ADD COLUMN timezone VARCHAR(64) DEFAULT ''")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_soft_migrate)
