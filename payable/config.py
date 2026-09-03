"""Runtime configuration.

Every integration degrades to an offline-deterministic mode when its credentials
are absent, so the whole system runs with `pip install -r requirements.txt` and
nothing else. `Settings.describe()` reports exactly which mode each subsystem is
in -- the demo should never leave a judge guessing what was real.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- merchant service ---
    catalog_path: Path = field(default_factory=lambda: DATA_DIR / "catalog.json")
    base_url: str = field(default_factory=lambda: _env("PAYABLE_BASE_URL", "http://127.0.0.1:8000"))

    # --- payments ---
    razorpay_key_id: str = field(default_factory=lambda: _env("RAZORPAY_KEY_ID"))
    razorpay_key_secret: str = field(default_factory=lambda: _env("RAZORPAY_KEY_SECRET"))
    razorpay_api_base: str = field(
        default_factory=lambda: _env("RAZORPAY_API_BASE", "https://api.razorpay.com/v1")
    )

    # --- audit log ---
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL"))
    audit_jsonl_path: Path = field(
        default_factory=lambda: Path(_env("PAYABLE_AUDIT_PATH", str(DATA_DIR / "audit.jsonl")))
    )

    # --- buyer agent reasoning ---
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("PAYABLE_LLM_MODEL", "claude-sonnet-5"))
    llm_enabled: bool = field(default_factory=lambda: _env_bool("PAYABLE_USE_LLM", False))

    # --- mandate signing ---
    mandate_secret: str = field(
        default_factory=lambda: _env("PAYABLE_MANDATE_SECRET", "dev-only-mandate-secret")
    )

    # --- fault injection (benchmark realism) ---
    payment_failure_rate: float = field(
        default_factory=lambda: float(_env("PAYABLE_PAYMENT_FAILURE_RATE", "0.12"))
    )
    seed: int = field(default_factory=lambda: _env_int("PAYABLE_SEED", 1733))

    @property
    def razorpay_live(self) -> bool:
        """True when real Razorpay test-mode API calls should be attempted."""
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    @property
    def llm_live(self) -> bool:
        return bool(self.llm_enabled and self.anthropic_api_key)

    def describe(self) -> dict:
        return {
            "payments": (
                f"razorpay-api ({self.razorpay_key_id[:12]}...)"
                if self.razorpay_live
                else "simulated (no RAZORPAY_KEY_ID)"
            ),
            "audit_log": f"redis-streams ({self.redis_url})" if self.redis_enabled else f"jsonl ({self.audit_jsonl_path.name})",
            "buyer_reasoning": f"llm ({self.llm_model})" if self.llm_live else "deterministic-planner",
            "seed": self.seed,
        }


SETTINGS = Settings()
