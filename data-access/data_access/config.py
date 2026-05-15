from pydantic_settings import BaseSettings, SettingsConfigDict


class DataAccessSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://localhost/nowlez_munshi_shared"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE_SECONDS: int = 3600


settings = DataAccessSettings()
