"""Application settings, read from environment variables (all optional)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/certificates.db"
    storage_dir: Path = Path("./data/certificates")
    max_recipients_per_job: int = 1000
    worker_threads: int = 2
    # True = generate inside the request (used by tests / tiny deployments).
    sync_processing: bool = False
    # Base URL embedded in each certificate's QR code for verification.
    public_base_url: str = "http://localhost:8000"
    # Optional .ttf for non-Latin names (e.g. Noto Sans Devanagari).
    font_path: str | None = None
    # If set, every non-public endpoint requires header "X-API-Key: <value>".
    api_key: str | None = None

    def __post_init__(self) -> None:
        if self.worker_threads < 1:
            raise ValueError("WORKER_THREADS must be >= 1")
        if self.max_recipients_per_job < 1:
            raise ValueError("MAX_RECIPIENTS_PER_JOB must be >= 1")

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            database_url=os.getenv("DATABASE_URL", d.database_url),
            storage_dir=Path(os.getenv("STORAGE_DIR", str(d.storage_dir))),
            max_recipients_per_job=int(os.getenv("MAX_RECIPIENTS_PER_JOB", d.max_recipients_per_job)),
            worker_threads=int(os.getenv("WORKER_THREADS", d.worker_threads)),
            sync_processing=os.getenv("SYNC_PROCESSING", "false").lower() in {"1", "true", "yes"},
            public_base_url=os.getenv("PUBLIC_BASE_URL", d.public_base_url).rstrip("/"),
            font_path=os.getenv("FONT_PATH") or None,
            api_key=os.getenv("API_KEY") or None,
        )