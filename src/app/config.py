"""Settings. One place for every environment-supplied value; nothing else reads `os.environ`."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration, prefixed `APP_`.

    @audit the defaults below are development credentials. A deployment must supply its own;
    nothing here is safe outside compose on a laptop.
    """

    model_config = SettingsConfigDict(env_prefix="APP_", extra="ignore")

    version: str = "0.1.0"

    database_url: str = "postgresql+psycopg://app:app@db:5432/app"

    object_store_endpoint: str = "http://storage:9000"
    object_store_access_key: str = "minioadmin"
    object_store_secret_key: str = "minioadmin"
    object_store_bucket: str = "artifacts"


settings = Settings()
