"""订单批次 hash、domain/account 校验及客户端硬风控。"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from live_client.config import LiveClientConfig


@dataclass(frozen=True)
class ValidatedBatch:
    trade_date: str
    rebalance_id: str
    attempt_number: int
    batch_id: str
    batch_sha256: str
    target_hash: str
    orders: tuple[dict, ...]

    def as_payload(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "rebalance_id": self.rebalance_id,
            "attempt_number": self.attempt_number,
            "batch_id": self.batch_id,
            "batch_sha256": self.batch_sha256,
            "target_hash": self.target_hash,
            "orders": list(self.orders),
        }


def validate_order_batch(
    orders: list[dict], trade_date: str, cfg: LiveClientConfig,
) -> ValidatedBatch:
    if not orders:
        raise ValueError("订单批次为空")
    common_fields = (
        "execution_domain", "qmt_account_alias", "target_id", "rebalance_id",
        "attempt_id", "attempt_number", "batch_id", "batch_sha256", "target_hash",
    )
    for field in common_fields:
        values = {order.get(field) for order in orders}
        if None in values or "" in values or len(values) != 1:
            raise ValueError(f"订单批次 {field} 缺失或混合")
    first = orders[0]
    if first["execution_domain"] != "live":
        raise ValueError("live client 收到非 live 订单")
    if first["qmt_account_alias"] != cfg.account_alias:
        raise ValueError("server order account_alias 与 live client 不一致")
    canonical_orders = []
    seen_order_ids = set()
    buy = sell = 0.0
    for order in orders:
        if order.get("valid_date") != trade_date:
            raise ValueError("订单 valid_date 与目标交易日不一致")
        if order.get("order_id") in seen_order_ids:
            raise ValueError("订单批次含重复 order_id")
        seen_order_ids.add(order.get("order_id"))
        symbol = order.get("symbol")
        if symbol not in cfg.allowed_symbols:
            raise ValueError(f"订单标的超出 client ETF 白名单: {symbol}")
        direction = order.get("direction")
        if direction not in {"BUY", "SELL"}:
            raise ValueError("订单方向非法")
        quantity = order.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("订单数量非法")
        if direction == "BUY" and quantity % 100:
            raise ValueError("ETF 买入数量必须是 100 份整数倍")
        reference = float(order.get("execution_reference_price") or 0)
        limit = float(order.get("limit_price") or 0)
        if not all(math.isfinite(value) and value > 0 for value in (reference, limit)):
            raise ValueError("订单原价参考/限价非法")
        offset_bps = abs(limit / reference - 1) * 10_000
        if offset_bps > cfg.max_price_offset_bps + 0.01:
            raise ValueError("订单限价偏离超过 client 上限")
        notional = quantity * limit
        if notional > cfg.max_single_order_notional:
            raise ValueError("订单单笔金额超过 client 上限")
        if direction == "BUY":
            buy += notional
        else:
            sell += notional
        canonical_orders.append({
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "reference_price": round(reference, 6),
            "limit_price": round(limit, 3),
        })
    if len(orders) > cfg.max_daily_orders:
        raise ValueError("订单数超过 client 日上限")
    if buy > cfg.max_daily_buy_notional:
        raise ValueError("买入金额超过 client 日上限")
    if sell > cfg.max_daily_sell_notional:
        raise ValueError("卖出金额超过 client 日上限")
    if buy + sell > cfg.max_daily_turnover_notional:
        raise ValueError("总成交额超过 client 日上限")
    batch_payload = {
        "rebalance_id": first["rebalance_id"],
        "attempt_number": first["attempt_number"],
        "trade_date": trade_date,
        "orders": sorted(canonical_orders, key=lambda item: item["symbol"]),
    }
    actual_sha = hashlib.sha256(
        json.dumps(batch_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if actual_sha != first["batch_sha256"]:
        raise ValueError("server order batch_sha256 重算不一致")
    if first["batch_id"] != f"hb_{actual_sha}":
        raise ValueError("server order batch_id 与 hash 不一致")
    normalized = tuple(sorted(orders, key=lambda item: item["symbol"]))
    return ValidatedBatch(
        trade_date=trade_date,
        rebalance_id=first["rebalance_id"],
        attempt_number=int(first["attempt_number"]),
        batch_id=first["batch_id"],
        batch_sha256=actual_sha,
        target_hash=first["target_hash"],
        orders=normalized,
    )


def validate_account_capacity(batch: ValidatedBatch, cash: float, positions: dict[str, int]) -> None:
    available_cash = float(cash)
    holdings = {code: int(qty) for code, qty in positions.items()}
    for order in sorted(batch.orders, key=lambda item: item["direction"] != "SELL"):
        symbol = order["symbol"]
        qty = int(order["quantity"])
        notional = qty * float(order["limit_price"])
        if order["direction"] == "SELL":
            if holdings.get(symbol, 0) < qty:
                raise ValueError(f"QMT 可卖持仓不足: {symbol}")
            holdings[symbol] = holdings.get(symbol, 0) - qty
            available_cash += notional * 0.999
        else:
            # 额外保留 0.1% 交易成本/价格误差缓冲。
            required = notional * 1.001
            if available_cash < required:
                raise ValueError(f"QMT 可用资金不足: {symbol}")
            available_cash -= required
