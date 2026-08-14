"""Runtime configuration. Everything is env-overridable so the same image
runs locally, on Render, on Railway or on an Oracle Cloud VM unchanged."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

IST = ZoneInfo("Asia/Kolkata")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------------------------------------------------------------- server
    app_name: str = "Night Ledger"
    host: str = "0.0.0.0"
    port: int = 8000
    # Set this to protect a publicly-deployed instance. Empty = no auth.
    access_token: str = ""
    data_dir: str = "data"

    # Seeded on the very first boot only, so a fresh deploy shows a working
    # dashboard instead of three empty panels. Delete any of them in the UI and
    # they stay deleted — the seed never runs again once the file exists.
    default_watchlist: str = (
        "RELIANCE.NS,HDFCBANK.NS,ICICIBANK.NS,INFY.NS,TCS.NS,TATAMOTORS.NS,"
        "DIXON.NS,TRENT.NS,BEL.NS,HAL.NS,POLYCAB.NS,PERSISTENT.NS"
    )

    # ------------------------------------------------------------------ feed
    # "yahoo"    -> zero credentials, daily bars are fine, intraday is delayed
    # "angelone" -> live tick-by-tick over WebSocket (needs broker credentials)
    feed: str = "yahoo"

    # Angel One SmartAPI (https://smartapi.angelbroking.com)
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_password: str = ""       # the 4-digit / 6-digit PIN
    angel_totp_secret: str = ""    # base32 secret from the TOTP QR code

    # ----------------------------------------------------------- scan cadence
    momentum_poll_seconds: int = 30      # how often the momentum loop ticks
    vcp_scan_minutes: int = 30           # how often daily bars are re-screened
    history_cache_minutes: int = 60      # daily OHLCV cache lifetime

    # -------------------------------------------------------------- strategy
    momentum_window_minutes: int = 15    # the rolling comparison window
    momentum_threshold_pct: float = 2.0  # fire at +2% inside that window
    momentum_cooldown_minutes: int = 30  # per-symbol re-arm delay

    min_contractions: int = 2
    max_contractions: int = 6
    max_first_contraction_pct: float = 35.0   # base must not be a crater
    max_final_contraction_pct: float = 12.0   # last pullback must be tight
    contraction_shrink_ratio: float = 0.80    # each leg <= 80% of the previous
    min_base_days: int = 15                   # ~3 trading weeks
    max_base_days: int = 130                  # ~6 months
    volume_dryup_ratio: float = 0.75          # final-leg vol vs 50d average
    pivot_window: int = 5                     # bars either side of a swing

    # Minervini trend template
    min_pct_above_52w_low: float = 25.0
    max_pct_below_52w_high: float = 25.0
    sma200_slope_lookback: int = 22           # ~1 month of rising 200 SMA
    # ^NSEI = Nifty 50 (always resolves on Yahoo). ^CRSLDX = Nifty 500 is the
    # broader benchmark but is patchy; the history provider falls back to
    # ^NSEI automatically if it comes back empty.
    rs_benchmark: str = "^NSEI"
    min_rs_score: float = 0.0                 # relative strength vs benchmark

    # -------------------------------------------------------- market session
    market_open: str = "09:15"
    market_close: str = "15:30"

    # --------------------------------------------------- optional push relays
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""

    @property
    def is_live_feed(self) -> bool:
        return self.feed == "angelone" and bool(self.angel_api_key)


settings = Settings()
