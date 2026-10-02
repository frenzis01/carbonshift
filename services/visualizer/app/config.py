from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    carbonshift_url: str = os.environ.get("CARBONSHIFT_URL", "http://carbonshift:8080").rstrip("/")
    client_url: str = os.environ.get("CLIENT_URL", "http://client:8100").rstrip("/")
    provider_url: str = os.environ.get("PROVIDER_URL", "http://provider:9100").rstrip("/")
    host: str = os.environ.get("VISUALIZER_HOST", "0.0.0.0")
    port: int = int(os.environ.get("VISUALIZER_PORT", "8500"))
    request_timeout_seconds: float = float(os.environ.get("VISUALIZER_TIMEOUT_SECONDS", "3.0"))


settings = Settings()
