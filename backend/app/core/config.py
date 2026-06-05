from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANSIBLE_GUI_", env_file=".env")

    data_dir: Path = Path(__file__).resolve().parents[3] / "data"
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def credentials_dir(self) -> Path:
        return self.data_dir / "credentials"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()
settings.projects_dir.mkdir(parents=True, exist_ok=True)
settings.credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
