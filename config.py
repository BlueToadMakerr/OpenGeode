import os
from dotenv import load_dotenv

# Use the .env file to configure! (Look in .env.example)
load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # Server
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "3000"))

    # Base URL
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:3000")
    FRONT_URL: str = os.getenv("FRONT_URL", "https://geode-sdk.org")

    # GitHub OAuth
    GITHUB_CLIENT_ID: str = os.getenv("GITHUB_CLIENT_ID", "")
    GITHUB_CLIENT_SECRET: str = os.getenv("GITHUB_CLIENT_SECRET", "")
    GITHUB_CALLBACK_URL: str = os.getenv(
        "GITHUB_CALLBACK_URL", "http://localhost:3000/v1/login/github/callback"
    )

    # Auth / JWT secrets
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

    # GitHub device flow
    DEVICE_FLOW_EXPIRE_SECONDS: int = int(os.getenv("DEVICE_FLOW_EXPIRE_SECONDS", "900"))
    DEVICE_FLOW_POLL_INTERVAL: int = int(os.getenv("DEVICE_FLOW_POLL_INTERVAL", "5"))

    # Storage
    DATABASE_DIR: str = os.getenv("DATABASE_DIR", "database")
    DATABASE_SQLITE_FILE: str = os.getenv(
        "DATABASE_SQLITE_FILE", os.path.join("database", "database.sqlite3")
    )
    MODS_STORAGE_DIR: str = os.getenv("MODS_STORAGE_DIR", os.path.join("database", "mods_storage"))
    LOGOS_DIR: str = os.getenv("LOGOS_DIR", os.path.join("database", "mods_storage", "logos"))
    ATTACHMENTS_DIR: str = os.getenv(
        "ATTACHMENTS_DIR", os.path.join("database", "mods_storage", "attachments")
    )

    # Attachment upload permissions:
    # 1 = anyone, 2 = admins + verified developers,
    # 3 = admins + developers with an approved mod,
    # 4 = admins only, 5 = disabled.
    ATTACHMENT_PERMISSIONS: int = int(os.getenv("ATTACHMENT_PERMISSIONS", "1"))

    # Uploads Limits
    MAX_ATTACHMENT_SIZE_BYTES: int = int(os.getenv("MAX_ATTACHMENT_SIZE_BYTES", str(8 * 1024 * 1024)))
    MAX_ATTACHMENTS_PER_COMMENT: int = int(os.getenv("MAX_ATTACHMENTS_PER_COMMENT", "5"))
    MAX_GEODE_FILE_SIZE_BYTES: int = int(os.getenv("MAX_GEODE_FILE_SIZE_BYTES", str(64 * 1024 * 1024)))

    # Pagination
    DEFAULT_PER_PAGE: int = int(os.getenv("DEFAULT_PER_PAGE", "10"))
    MAX_PER_PAGE: int = int(os.getenv("MAX_PER_PAGE", "100"))

    # Admin bootstrap
    BOOTSTRAP_ADMINS: list[str] = [
        u.strip().lower()
        for u in os.getenv("BOOTSTRAP_ADMINS", "").split(",")
        if u.strip()
    ]

settings = Settings()
