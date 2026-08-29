from pydantic import BaseModel, Field


class CanaryStageRequest(BaseModel):
    execution_domain: str = Field(pattern="^live$")
    account_alias: str = Field(min_length=1)
    trade_date: str = Field(pattern=r"^\d{8}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(pattern=r"^(510300\.SH|159915\.SZ)$")
    quantity: int = Field(default=100, ge=100, le=100)
    reference_price: float = Field(gt=0)
    limit_price: float = Field(gt=0)
