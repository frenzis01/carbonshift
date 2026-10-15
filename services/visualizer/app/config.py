from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

shared_services_root = Path(__file__).resolve().parents[2]
if (shared_services_root / "common").is_dir():
    sys.path.insert(0, str(shared_services_root))

from common.snapshots import default_snapshot_directory


@dataclass
class Settings:
    carbonshift_url: str = os.environ.get("CARBONSHIFT_URL", "http://carbonshift:8080").rstrip("/")
    carbonshift_api_key: str | None = os.environ.get("CARBONSHIFT_API_KEY") or None
    provider_url: str = os.environ.get("PROVIDER_URL", "http://provider:9100").rstrip("/")
    host: str = os.environ.get("VISUALIZER_HOST", "0.0.0.0")
    port: int = int(os.environ.get("VISUALIZER_PORT", "8500"))
    request_timeout_seconds: float = float(os.environ.get("VISUALIZER_TIMEOUT_SECONDS", "3.0"))
    snapshots_dir: Path = default_snapshot_directory()


settings = Settings()
