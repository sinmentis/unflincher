"""Environment-based settings. All defaults are safe for local dev; production values
are injected via the Quadlet unit's Environment=/Secret= directives (see deploy/quadlet/)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    llm_model: str
    batch_concurrency: int
    cf_team_domain: str
    cf_access_aud: str
    operator_email: str
    require_access_auth: bool


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("DIARY_DB", "diary.dev.db"),
        llm_model=os.environ.get("DIARY_LLM_MODEL", "anthropic/claude-sonnet-4-5"),
        batch_concurrency=int(os.environ.get("DIARY_BATCH_CONCURRENCY", "3")),
        cf_team_domain=os.environ.get("DIARY_CF_TEAM_DOMAIN", ""),
        cf_access_aud=os.environ.get("DIARY_CF_ACCESS_AUD", ""),
        operator_email=os.environ.get("DIARY_OPERATOR_EMAIL", ""),
        require_access_auth=os.environ.get("DIARY_REQUIRE_ACCESS_AUTH", "true").lower() == "true",
    )
