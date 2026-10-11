"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All paths and credentials come from the environment; nothing is hard-coded."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    storage_root: Path = Field(default=Path("./storage"), validation_alias="STORAGE_ROOT")
    entsoe_raw_dir: Path | None = Field(default=None, validation_alias="ENTSOE_RAW_DIR")
    legacy_processed_dir: Path | None = Field(default=None, validation_alias="LEGACY_PROCESSED_DIR")
    entsoe_api_token: SecretStr | None = Field(default=None, validation_alias="ENTSOE_API_TOKEN")
    entsoe_env_file: Path | None = Field(default=None, validation_alias="ENTSOE_ENV_FILE")
    threads: int = Field(default=4, ge=1, validation_alias="THREADS")
    r2_account_id: str | None = Field(default=None, validation_alias="R2_ACCOUNT_ID")
    r2_bucket: str | None = Field(default=None, validation_alias="R2_BUCKET")
    r2_access_key_id: SecretStr | None = Field(default=None, validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: SecretStr | None = Field(
        default=None, validation_alias="R2_SECRET_ACCESS_KEY"
    )
    hf_token: SecretStr | None = Field(default=None, validation_alias="HF_TOKEN")
    hf_repo_id: str = Field(default="hsilvosa/entsoe-observatory", validation_alias="HF_REPO_ID")

    def require_raw_dir(self) -> Path:
        if self.entsoe_raw_dir is None:
            raise ValueError("ENTSOE_RAW_DIR is not set")
        return self.entsoe_raw_dir

    def resolve_token(self) -> SecretStr:
        """Return the API token from the environment or from ENTSOE_ENV_FILE (live pulls only)."""
        if self.entsoe_api_token is not None:
            return self.entsoe_api_token
        if self.entsoe_env_file is not None:
            for line in self.entsoe_env_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "ENTSOE_API_TOKEN" and value.strip():
                    return SecretStr(value.strip().strip("\"'"))
        raise ValueError("ENTSOE_API_TOKEN is not available")

    def layer_dir(self, layer: str, dataset: str | None = None) -> Path:
        base = self.storage_root / layer
        return base / dataset if dataset else base
