from dataclasses import dataclass
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_ids: frozenset[int] = frozenset(
        int(value.strip())
        for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if value.strip()
    )
    telegram_local_api_url: str = os.getenv("TELEGRAM_LOCAL_API_URL", "").rstrip("/")
    qbit_host: str = os.getenv("QBIT_HOST", "http://127.0.0.1:8080")
    qbit_user: str = os.getenv("QBIT_USERNAME", "admin")
    qbit_password: str = os.getenv("QBIT_PASSWORD", "")
    quarantine: Path = Path(os.getenv("QUARANTINE_DIR", "/srv/teletorrent/quarantine"))
    approved: Path = Path(os.getenv("APPROVED_DIR", "/srv/teletorrent/approved"))
    rejected: Path = Path(os.getenv("REJECTED_DIR", "/srv/teletorrent/rejected"))
    db_path: Path = Path(os.getenv("DATABASE_PATH", "/var/lib/teletorrent/teletorrent.db"))
    dashboard_host: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8787"))
    dashboard_user: str = os.getenv("DASHBOARD_USERNAME", "admin")
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "")
    scan_interval: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "10"))
    auto_approve: bool = _bool("AUTO_APPROVE_SAFE_FILES", True)
    delete_rejected: bool = _bool("DELETE_REJECTED_FILES", False)
    max_torrent_mb: int = int(os.getenv("MAX_TORRENT_FILE_MB", "20"))


CONFIG = Config()
