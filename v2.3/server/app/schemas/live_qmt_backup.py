from pydantic import Field

from app.schemas.market_data import MarketDataRequest


class LiveQMTBackupRequest(MarketDataRequest):
    """Immutable diagnostic copy; never enters the strategy data store."""
    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
