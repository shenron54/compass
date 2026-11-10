import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    local_config: Path
    tz: str
    session_secret: str
    cookie_secure: bool
    ib_host: str
    ib_port: int
    ib_client_id: int
    ib_account: str
    flex_token: str
    flex_query_id: str
    fred_api_key: str
    deepseek_api_key: str
    fast_model: str
    deep_model: str
    llm_monthly_cap_usd: float
    llm_price_in_per_m: float
    llm_price_out_per_m: float
    public_hosts: list[str] = field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("COMPASS_DATA_DIR", "data"))
    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        # A random secret per process is fine locally; it only logs everyone out on restart.
        secret = secrets.token_urlsafe(32)
    return Settings(
        data_dir=data_dir,
        db_path=Path(os.environ.get("COMPASS_DB", str(data_dir / "compass.db"))),
        local_config=Path(os.environ.get("COMPASS_LOCAL_CONFIG", "config/local")),
        tz=os.environ.get("COMPASS_TZ", "Asia/Hong_Kong"),
        session_secret=secret,
        cookie_secure=_bool("COOKIE_SECURE", True),
        ib_host=os.environ.get("IB_HOST", "ib-gateway"),
        ib_port=int(os.environ.get("IB_PORT", "4003")),
        ib_client_id=int(os.environ.get("IB_CLIENT_ID", "7")),
        ib_account=os.environ.get("IB_ACCOUNT", ""),
        flex_token=os.environ.get("FLEX_TOKEN", ""),
        flex_query_id=os.environ.get("FLEX_QUERY_ID", ""),
        fred_api_key=os.environ.get("FRED_API_KEY", ""),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        fast_model=os.environ.get("COMPASS_FAST_MODEL", "deepseek-flash"),
        deep_model=os.environ.get("COMPASS_DEEP_MODEL", "deepseek-v4-pro"),
        llm_monthly_cap_usd=float(os.environ.get("LLM_MONTHLY_CAP_USD", "5")),
        # Deliberately on the high side of DeepSeek's list prices so the cap trips early, not late.
        llm_price_in_per_m=float(os.environ.get("LLM_PRICE_IN_PER_M", "0.6")),
        llm_price_out_per_m=float(os.environ.get("LLM_PRICE_OUT_PER_M", "2.2")),
        public_hosts=[h for h in os.environ.get("COMPASS_HOST", "").split(",") if h],
    )
