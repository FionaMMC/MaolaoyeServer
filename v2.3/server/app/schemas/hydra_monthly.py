"""Windows sends only frozen data, never monthly weights or strategy money."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from app.schemas.hydra_data import HydraDataManifest


class FrozenStream(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: HydraDataManifest
    parquet_base64: str = Field(max_length=24 * 1024 * 1024)


class HydraMonthlySnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_domain: Literal["live"] = "live"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    as_of_date: str = Field(pattern=r"^\d{8}$")
    streams: dict[str, FrozenStream]

    @model_validator(mode="after")
    def complete_set(self):
        keys = {"model_hfq", "execution_raw", "corporate_actions", "trading_calendar"}
        if set(self.streams) != keys:
            raise ValueError("必须一次交付四份冻结流")
        for key, stream in self.streams.items():
            if (
                stream.manifest.stream != "hydra_" + key
                or stream.manifest.as_of_date != self.as_of_date
            ):
                raise ValueError("四份流必须属于同一研究日期且类型对应")
        return self
