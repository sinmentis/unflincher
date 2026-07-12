"""Environment-based settings. All defaults are safe for local dev; production values
are injected via the Quadlet unit's Environment=/Secret= directives (see deploy/quadlet/)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    llm_model: str
    batch_concurrency: int
    llm_concurrency: int
    cf_team_domain: str
    cf_access_aud: str
    operator_email: str
    require_access_auth: bool


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("UNFLINCHER_DB", "unflincher.dev.db"),
        llm_model=os.environ.get("UNFLINCHER_LLM_MODEL", "claude-sonnet-4.6"),
        batch_concurrency=int(os.environ.get("UNFLINCHER_BATCH_CONCURRENCY", "3")),
        llm_concurrency=int(os.environ.get("UNFLINCHER_LLM_CONCURRENCY", "4")),
        cf_team_domain=os.environ.get("UNFLINCHER_CF_TEAM_DOMAIN", ""),
        cf_access_aud=os.environ.get("UNFLINCHER_CF_ACCESS_AUD", ""),
        operator_email=os.environ.get("UNFLINCHER_OPERATOR_EMAIL", ""),
        require_access_auth=os.environ.get("UNFLINCHER_REQUIRE_ACCESS_AUTH", "true").lower() == "true",
    )
