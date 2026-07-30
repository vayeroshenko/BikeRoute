from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from idf_commute.planning.scoring import ScoreMode


class LocationConfig(BaseModel):
    latitude: float
    longitude: float

    @property
    def navitia_coord(self) -> str:
        return f"{self.longitude:.6f};{self.latitude:.6f}"


class LocationsConfig(BaseModel):
    home: LocationConfig
    work: LocationConfig


class CandidateStation(BaseModel):
    query: str
    id: str | None = None
    label: str
    required_line_id: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        if isinstance(value, int):
            return str(value)
        return value


class ProbeConfig(BaseModel):
    coverage: str = "fr-idf"
    navitia_datetime: str | None = None
    stop_monitoring_stop_id: str | None = None
    geovelo_profile: str = "MEDIAN"
    geovelo_bike_type: str = "TRADITIONAL"
    geovelo_average_speed_kmh: int = Field(default=16, ge=5, le=45)


class BicycleConfig(BaseModel):
    profile: str = "MEDIAN"
    bike_type: str = "TRADITIONAL"
    average_speed_kmh: int = Field(default=16, ge=5, le=45)
    preferred_bike_minutes: float = Field(default=20, ge=0)
    max_bike_minutes: float = Field(default=25, gt=0)
    parking_buffer_minutes: float = Field(default=4, ge=0)
    target_line_id: str = "line:IDFM:C01743"
    target_line_label: str = "RER B"


class ScoreWeightOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    door_to_door: float | None = Field(default=None, ge=0)
    bike_penalty: float | None = Field(default=None, ge=0)
    transfers: float | None = Field(default=None, ge=0)
    disruptions: float | None = Field(default=None, ge=0)
    freshness: float | None = Field(default=None, ge=0)
    cycling_comfort: float | None = Field(default=None, ge=0)


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ScoreMode = ScoreMode.BALANCED
    weights: ScoreWeightOverrides = Field(default_factory=ScoreWeightOverrides)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = "Europe/Paris"
    locations: LocationsConfig
    candidate_stations: list[CandidateStation] = Field(default_factory=list)
    line_queries: list[str] = Field(default_factory=lambda: ["RER B", "4602", "21", "22"])
    bicycle: BicycleConfig = Field(default_factory=BicycleConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    probe: ProbeConfig = Field(default_factory=ProbeConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> AppConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        return cls.model_validate(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="IDF_COMMUTE_",
        extra="ignore",
    )

    prim_api_key: SecretStr | None = Field(default=None, validation_alias="PRIM_API_KEY")
    api_key_header: str = "apiKey"
    navitia_base_url: HttpUrl = HttpUrl(
        "https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia"
    )
    disruptions_url: HttpUrl = HttpUrl(
        "https://prim.iledefrance-mobilites.fr/marketplace/disruptions_bulk/disruptions/v2"
    )
    geovelo_url: HttpUrl = HttpUrl(
        "https://prim.iledefrance-mobilites.fr/marketplace/computedroutes"
    )
    stop_monitoring_url: HttpUrl = HttpUrl(
        "https://prim.iledefrance-mobilites.fr/marketplace/stop-monitoring"
    )
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    def require_api_key(self) -> SecretStr:
        if self.prim_api_key is None or not self.prim_api_key.get_secret_value():
            raise MissingAccessError(
                "PRIM_API_KEY is missing. Create a PRIM token and subscribe to the "
                "required APIs before running a live probe."
            )
        return self.prim_api_key


class MissingAccessError(RuntimeError):
    """Raised when a live probe cannot authenticate."""


def coordinate_secrets(config: AppConfig) -> set[str]:
    values: set[str] = set()
    for location in (config.locations.home, config.locations.work):
        values.update(
            {
                str(location.latitude),
                str(location.longitude),
                f"{location.latitude:.6f}",
                f"{location.longitude:.6f}",
                location.navitia_coord,
            }
        )
    return values


def load_config(path: Path) -> AppConfig:
    return AppConfig.from_yaml(path)


def safe_settings_summary(settings: Settings) -> dict[str, Any]:
    return {
        "api_key": "<configured>" if settings.prim_api_key else "<missing>",
        "api_key_header": settings.api_key_header,
        "navitia_base_url": str(settings.navitia_base_url),
        "disruptions_url": str(settings.disruptions_url),
        "geovelo_url": str(settings.geovelo_url),
        "stop_monitoring_url": str(settings.stop_monitoring_url),
    }
