"""Runtime configuration loaded from environment variables and CLI flags."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — avoids a python-dotenv dependency."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    """All runtime knobs in one place."""

    # Comma Connect
    comma_jwt: str = ""
    comma_dongle_id: str = ""
    comma_api_base: str = "https://api.comma.ai"

    # OSM read-side
    overpass_url: str = "https://overpass-api.de/api/interpreter"

    # Map matcher
    valhalla_url: str = "https://valhalla1.openstreetmap.de"

    # Analysis defaults
    threshold_mph: float = 10.0
    speed_percentile: float = 85.0
    min_samples_per_way: int = 30
    min_speed_mph_for_freeflow: float = 5.0
    units: str = "mph"  # "mph" or "kmh"

    # Output
    output_dir: Path = field(default_factory=lambda: Path("output"))

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Config:
        if env_file is not None:
            _load_dotenv(env_file)
        else:
            _load_dotenv(Path(".env"))

        cfg = cls(
            comma_jwt=os.environ.get("COMMA_JWT", ""),
            comma_dongle_id=os.environ.get("COMMA_DONGLE_ID", ""),
            comma_api_base=os.environ.get("COMMA_API_BASE", "https://api.comma.ai"),
            overpass_url=os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
            valhalla_url=os.environ.get("VALHALLA_URL", "https://valhalla1.openstreetmap.de"),
        )
        return cfg

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not getattr(self, k)]
        if missing:
            raise RuntimeError(
                f"Missing required config values: {', '.join(missing)}. "
                "Set them in .env or pass via CLI flags."
            )
