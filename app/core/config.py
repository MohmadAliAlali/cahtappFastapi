from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DB_NAME: str = "chatdb"
    DB_USER: str = "chatapp"
    DB_PASS: str = "strongpassword"
    DB_HOST: str = "db"
    DB_PORT: int = 5432
    DATABASE_URL: str = "postgresql+asyncpg://chatapp:strongpassword@db:5432/chatdb"
    REDIS_URL: str = "redis://redis:6379/0"
    SECRET_KEY: str = "change-me-to-a-random-secret"
    DISABLE_RATE_LIMIT: bool = False
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024

    @property
    def is_sqlite(self) -> bool:
        return "sqlite" in self.DATABASE_URL

    def validate_secret_key(self) -> None:
        import os
        if self.SECRET_KEY == "change-me-to-a-random-secret":
            if os.environ.get("TESTING") != "1":
                raise ValueError(
                    "SECRET_KEY must be changed from the default value. "
                    "Set it in .env or as an environment variable."
                )

    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    EMAIL_HOST: str = "smtp.example.com"
    EMAIL_PORT: int = 587
    EMAIL_USER: str = "no-reply@domain.com"
    EMAIL_PASS: str = "password"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
settings.validate_secret_key()
