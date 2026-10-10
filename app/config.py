"""Secure configuration loaded from environment variables.

No secrets are hardcoded. All values come from the environment / .env file.
See .env.example for placeholders and where to obtain each value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime settings. Construct via get_settings() so tests can monkeypatch env."""

    provider: str = field(default_factory=lambda: _get("OG_PROVIDER", "gsma-sandbox"))
    client_id: str = field(default_factory=lambda: _get("OG_CLIENT_ID", ""))
    # Secrets: excluded from repr so a Settings dump can never leak them.
    client_secret: str = field(
        default_factory=lambda: _get("OG_CLIENT_SECRET", ""), repr=False
    )
    bearer_token: str = field(
        # Portal-issued bearer access token, read ONLY from the local
        # environment (OG_SIM_SWAP_BEARER_TOKEN). Never hardcode, print, log,
        # or commit. Used verbatim as the Authorization header; no token
        # endpoint is involved in bearer mode.
        default_factory=lambda: _get("OG_SIM_SWAP_BEARER_TOKEN", ""),
        repr=False,
    )
    api_base_url: str = field(default_factory=lambda: _get("OG_API_BASE_URL", "").rstrip("/"))
    token_url: str = field(default_factory=lambda: _get("OG_TOKEN_URL", ""))
    bc_authorize_url: str = field(default_factory=lambda: _get("OG_BC_AUTHORIZE_URL", ""))
    sim_swap_scope: str = field(
        # No default: the exact scope string(s) MUST be copied from the selected
        # sandbox portal. Providers differ (aggregator purpose syntax vs plain
        # CAMARA scopes); guessing breaks authentication.
        default_factory=lambda: _get("OG_SIM_SWAP_SCOPE", "")
    )
    auth_flow: str = field(default_factory=lambda: _get("OG_AUTH_FLOW", "mock").lower())
    timeout_seconds: float = field(
        default_factory=lambda: _get_float("OG_TIMEOUT_SECONDS", 10.0)
    )
    default_lookback_hours: int = field(
        default_factory=lambda: _get_int("OG_DEFAULT_LOOKBACK_HOURS", 72)
    )
    max_lookback_hours: int = field(
        default_factory=lambda: _get_int("OG_MAX_LOOKBACK_HOURS", 2400)
    )
    app_name: str = field(default_factory=lambda: _get("OG_APP_NAME", "cybersafe-sim-swap"))
    sim_swap_api_version: str = field(
        # Deployed CAMARA major version of the provider: "v1" or "v2".
        # Default "v2" (v2.1.0). Only the URL path suffix and 3-legged
        # phone-number strictness differ; OG_API_BASE_URL still carries the
        # full versioned path verbatim.
        default_factory=lambda: _get("OG_SIM_SWAP_API_VERSION", "v2").lower().strip(" /")
    )

    @property
    def is_v1(self) -> bool:
        return self.sim_swap_api_version in ("v1", "1", "1.0", "1.0.0", "v1.0.0")

    @property
    def is_live_configured(self) -> bool:
        """True only when a real HTTP integration can be attempted."""
        if self.auth_flow == "mock":
            return False
        if self.auth_flow == "bearer":
            # Portal-issued token carries its own scopes; no token endpoint,
            # client secret, or scope var is needed. Token itself is required.
            return bool(self.bearer_token and self.api_base_url)
        if not (self.client_id and self.client_secret and self.api_base_url and self.token_url):
            return False
        if not self.sim_swap_scope:
            return False
        if self.auth_flow == "ciba" and not self.bc_authorize_url:
            return False
        return True

    @property
    def missing_config(self) -> list[str]:
        missing: list[str] = []
        if self.auth_flow == "mock":
            return missing
        if self.auth_flow == "bearer":
            if not self.bearer_token:
                missing.append("OG_SIM_SWAP_BEARER_TOKEN")
            if not self.api_base_url:
                missing.append("OG_API_BASE_URL")
            return missing
        if not self.client_id:
            missing.append("OG_CLIENT_ID")
        if not self.client_secret:
            missing.append("OG_CLIENT_SECRET")
        if not self.api_base_url:
            missing.append("OG_API_BASE_URL")
        if not self.token_url:
            missing.append("OG_TOKEN_URL")
        if not self.sim_swap_scope:
            missing.append("OG_SIM_SWAP_SCOPE")
        if self.auth_flow == "ciba" and not self.bc_authorize_url:
            missing.append("OG_BC_AUTHORIZE_URL")
        return missing


def get_settings() -> Settings:
    return Settings()
