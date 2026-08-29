"""QMT gateway；xtquant 仅在真实 live 模式下延迟导入。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_client.config import LiveClientConfig


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    available_cash: float
    total_asset: float
    positions: dict[str, int]
    sellable_positions: dict[str, int]


@dataclass(frozen=True)
class SubmissionResult:
    local_order_id: str | None
    status: str
    detail: str | None = None
    execution_meta: dict | None = None


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    last_price: float
    bid1: float
    ask1: float
    source_time: datetime
    captured_at: datetime
    price_tick: float
    up_limit: float
    down_limit: float
    is_trading: bool
    iopv: float | None = None


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    account_id: str
    stock_code: str
    order_id: int
    order_sysid: str
    order_time: int
    order_volume: int
    price: float
    traded_volume: int
    traded_price: float
    order_status: int
    status_msg: str
    strategy_name: str
    order_remark: str


def _first_positive(value: Any) -> float | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    # numpy arrays are intentionally handled without importing numpy.
    elif hasattr(value, "__len__") and not isinstance(value, (str, bytes, dict)):
        value = value[0] if len(value) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _first_present(payload: dict, *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _tick_time(value: Any) -> datetime:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("QMT tick 缺少可验证的行情时间") from exc
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).astimezone()
    except (OSError, OverflowError, ValueError) as exc:
        raise RuntimeError("QMT tick 行情时间非法") from exc


def _broker_order_snapshot(order: Any) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        account_id=str(getattr(order, "account_id", "")),
        stock_code=str(getattr(order, "stock_code", "")),
        order_id=int(getattr(order, "order_id")),
        order_sysid=str(getattr(order, "order_sysid", "") or ""),
        order_time=int(getattr(order, "order_time", 0) or 0),
        order_volume=int(getattr(order, "order_volume", 0) or 0),
        price=float(getattr(order, "price", 0) or 0),
        traded_volume=int(getattr(order, "traded_volume", 0) or 0),
        traded_price=float(getattr(order, "traded_price", 0) or 0),
        order_status=int(getattr(order, "order_status")),
        status_msg=str(getattr(order, "status_msg", "") or ""),
        strategy_name=str(getattr(order, "strategy_name", "") or ""),
        order_remark=str(getattr(order, "order_remark", "") or ""),
    )


def classify_qmt_settlement_status(
    constants, qmt_status: int, filled_quantity: int, ordered_quantity: int,
) -> str:
    """只把明确的 QMT 终态映射为 server 状态，未知/活动委托 fail closed。"""
    if filled_quantity >= ordered_quantity:
        return "FILLED"
    active = {
        value
        for value in (
            getattr(constants, "ORDER_UNREPORTED", None),
            getattr(constants, "ORDER_WAIT_REPORTING", None),
            getattr(constants, "ORDER_REPORTED", None),
            getattr(constants, "ORDER_REPORTED_CANCEL", None),
            getattr(constants, "ORDER_PART_SUCC", None),
            getattr(constants, "ORDER_UNKNOWN", None),
        )
        if value is not None
    }
    if qmt_status in active:
        raise RuntimeError("QMT 委托尚未终结")
    if qmt_status == getattr(constants, "ORDER_JUNK", None):
        return "REJECTED"
    cancelled = {
        value
        for value in (
            getattr(constants, "ORDER_CANCELED", None),
            getattr(constants, "ORDER_PARTSUCC_CANCEL", None),
            getattr(constants, "ORDER_PART_CANCEL", None),
        )
        if value is not None
    }
    if qmt_status in cancelled:
        return "PARTIAL" if filled_quantity > 0 else "CANCELLED"
    raise RuntimeError(f"QMT 委托状态未识别: {qmt_status}")


class MockQMTGateway:
    def __init__(self, state_path: Path, expected_account_id: str):
        self.payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
        self.expected_account_id = expected_account_id
        self.sequence = int(self.payload.get("order_sequence_start", 10000))

    def connect(self) -> None:
        if self.payload.get("connect_ok", True) is not True:
            raise RuntimeError("mock QMT connection failed")

    def close(self) -> None:
        return None

    def account_snapshot(self) -> AccountSnapshot:
        account_id = str(self.payload.get("account_id", ""))
        if account_id != self.expected_account_id:
            raise RuntimeError("mock QMT account 与配置不一致")
        return AccountSnapshot(
            account_id=account_id,
            available_cash=float(self.payload.get("available_cash", 0)),
            total_asset=float(
                self.payload.get("total_asset", self.payload.get("available_cash", 0))
            ),
            positions={
                str(code): int(qty)
                for code, qty in dict(self.payload.get("positions", {})).items()
            },
            sellable_positions={
                str(code): int(qty)
                for code, qty in dict(
                    self.payload.get(
                        "sellable_positions", self.payload.get("positions", {})
                    )
                ).items()
            },
        )

    def submit(self, order: dict) -> SubmissionResult:
        if order["symbol"] in set(self.payload.get("reject_symbols", [])):
            return SubmissionResult(None, "REJECTED", "mock configured rejection")
        self.sequence += 1
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        return SubmissionResult(
            str(self.sequence),
            "SUBMITTED",
            execution_meta={
                "arrival_reference_price": order["execution_reference_price"],
                "arrival_reference_time": now,
                "submitted_price": order["limit_price"],
                "submitted_time": now,
                "qmt_order_id": str(self.sequence),
                "iopv": self.payload.get("iopv", {}).get(order["symbol"]),
                "iopv_time": now,
            },
        )

    def settlement_results(self, submissions: list[dict]) -> list[dict]:
        fill_ratios = dict(self.payload.get("fill_ratios", {}))
        results = []
        for row in submissions:
            ratio = float(fill_ratios.get(row["symbol"], 1.0))
            quantity = int(row["quantity"] * ratio)
            status = "FILLED" if quantity >= row["quantity"] else (
                "PARTIAL" if quantity > 0 else "CANCELLED"
            )
            results.append({
                "order_id": row["order_id"],
                "filled_quantity": quantity,
                "filled_price": float(row["limit_price"]) if quantity else 0.0,
                "status": status,
                "symbol": row["symbol"],
                "direction": row["direction"],
                **dict(row.get("execution_meta") or {}),
            })
        return results


class XtQMTGateway:
    """真实 QMT adapter。构造不会连接；调用 connect 才触达 MiniQMT。"""

    def __init__(self, cfg: LiveClientConfig):
        self.cfg = cfg
        self.trader = None
        self.account = None
        self.xtconstant = None
        self.xtdata = None

    def connect(self) -> None:
        from xtquant import xtconstant, xtdata
        from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
        from xtquant.xttype import StockAccount

        xtdata.data_dir = str(self.cfg.userdata_dir)
        trader = XtQuantTrader(str(self.cfg.userdata_dir), self.cfg.session_id)
        trader.register_callback(XtQuantTraderCallback())
        trader.start()
        if trader.connect() != 0:
            trader.stop()
            raise RuntimeError("QMT connect() 失败")
        account = StockAccount(self.cfg.account_id)
        if trader.subscribe(account) != 0:
            trader.stop()
            raise RuntimeError("QMT subscribe() 失败")
        self.trader = trader
        self.account = account
        self.xtconstant = xtconstant
        self.xtdata = xtdata

    def close(self) -> None:
        if self.trader is not None:
            self.trader.stop()
            self.trader = None

    def account_snapshot(self) -> AccountSnapshot:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        asset = self.trader.query_stock_asset(self.account)
        if asset is None:
            raise RuntimeError("QMT query_stock_asset 返回空")
        positions = self.trader.query_stock_positions(self.account)
        if positions is None:
            raise RuntimeError("QMT query_stock_positions 返回空")
        raw_account_id = (
            getattr(asset, "account_id", None)
            or getattr(asset, "m_strAccountID", None)
        )
        if raw_account_id is None:
            raise RuntimeError("QMT asset 缺少 account_id，无法完成实盘账户二次校验")
        account_id = str(raw_account_id)
        if account_id != self.cfg.account_id:
            raise RuntimeError("QMT 返回账户与 live 配置不一致")
        cash = getattr(asset, "cash", None)
        if cash is None:
            cash = getattr(asset, "m_dCash", None)
        if cash is None:
            raise RuntimeError("QMT asset 缺少可用资金字段")
        mapped = {}
        sellable = {}
        for position in positions:
            code = str(
                getattr(position, "stock_code", None)
                or getattr(position, "m_strInstrumentID", "")
            )
            total_qty = getattr(position, "volume", None)
            if total_qty is None:
                total_qty = getattr(position, "m_nVolume", None)
            sellable_qty = getattr(position, "can_use_volume", None)
            if sellable_qty is None:
                sellable_qty = getattr(position, "m_nCanUseVolume", None)
            if code and total_qty is not None and int(total_qty) > 0:
                mapped[code] = int(total_qty)
            if code and sellable_qty is not None and int(sellable_qty) > 0:
                sellable[code] = int(sellable_qty)
        total_asset = getattr(asset, "total_asset", None)
        if total_asset is None:
            total_asset = getattr(asset, "m_dBalance", None)
        if total_asset is None:
            raise RuntimeError("QMT asset 缺少总资产字段")
        return AccountSnapshot(
            account_id, float(cash), float(total_asset), mapped, sellable,
        )

    def market_quote(self, symbol: str) -> MarketQuote:
        if self.xtdata is None:
            raise RuntimeError("QMT 尚未连接")
        ticks = self.xtdata.get_full_tick([symbol])
        tick = (ticks or {}).get(symbol)
        if not isinstance(tick, dict):
            raise RuntimeError(f"QMT 实时行情缺失: {symbol}")
        detail = self.xtdata.get_instrument_detail(symbol)
        if not isinstance(detail, dict):
            raise RuntimeError(f"QMT 合约信息缺失: {symbol}")
        last = _first_positive(_first_present(tick, "lastPrice", "last_price", "open"))
        bid1 = _first_positive(_first_present(tick, "bidPrice", "bid_price"))
        ask1 = _first_positive(_first_present(tick, "askPrice", "ask_price"))
        if last is None or bid1 is None or ask1 is None:
            raise RuntimeError(f"QMT 最新价/一档盘口非法: {symbol}")
        iopv = _first_positive(
            _first_present(tick, "iopv", "IOPV", "fundIOPV", "fund_iopv")
        )
        price_tick = _first_positive(detail.get("PriceTick"))
        up_limit = _first_positive(detail.get("UpStopPrice"))
        down_limit = _first_positive(detail.get("DownStopPrice"))
        if price_tick is None or up_limit is None or down_limit is None:
            raise RuntimeError(f"QMT 价格档位/涨跌停信息非法: {symbol}")
        source_time = _tick_time(_first_present(tick, "time", "timetag"))
        return MarketQuote(
            symbol=symbol,
            last_price=last,
            bid1=bid1,
            ask1=ask1,
            source_time=source_time,
            captured_at=datetime.now(timezone.utc).astimezone(),
            price_tick=price_tick,
            up_limit=up_limit,
            down_limit=down_limit,
            is_trading=bool(detail.get("IsTrading")),
            iopv=iopv,
        )

    def cancelable_orders(self) -> list[BrokerOrderSnapshot]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        orders = self.trader.query_stock_orders(self.account, True)
        if orders is None:
            raise RuntimeError("QMT 可撤委托查询返回空值，无法区分失败与空列表")
        return [_broker_order_snapshot(order) for order in orders]

    def orders_by_remark(self, remark: str) -> list[BrokerOrderSnapshot]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        orders = self.trader.query_stock_orders(self.account, False)
        if orders is None:
            raise RuntimeError("QMT 当日委托查询返回空值，无法区分失败与空列表")
        return [
            _broker_order_snapshot(order)
            for order in orders
            if str(getattr(order, "order_remark", "") or "") == remark
        ]

    def canary_orders(self) -> list[BrokerOrderSnapshot]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        orders = self.trader.query_stock_orders(self.account, False)
        if orders is None:
            raise RuntimeError("QMT 当日委托查询返回空值，无法区分失败与空列表")
        return [
            _broker_order_snapshot(order)
            for order in orders
            if str(getattr(order, "strategy_name", "") or "") == "hydra_canary"
        ]

    def query_order(self, order_id: int) -> BrokerOrderSnapshot | None:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        order = self.trader.query_stock_order(self.account, int(order_id))
        return _broker_order_snapshot(order) if order is not None else None

    def submit_canary(
        self, *, symbol: str, quantity: int, limit_price: float, remark: str,
    ) -> int:
        if self.trader is None or self.account is None or self.xtconstant is None:
            raise RuntimeError("QMT 尚未连接")
        order_id = self.trader.order_stock(
            self.account,
            symbol,
            self.xtconstant.STOCK_BUY,
            int(quantity),
            self.xtconstant.FIX_PRICE,
            float(limit_price),
            "hydra_canary",
            remark,
        )
        if order_id is None or int(order_id) <= 0:
            raise RuntimeError(f"QMT canary order_stock 返回失败: {order_id}")
        return int(order_id)

    def cancel_order(self, order_id: int) -> None:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        result = self.trader.cancel_order_stock(self.account, int(order_id))
        if result != 0:
            raise RuntimeError(f"QMT canary 撤单指令失败: {result}")

    def submit(self, order: dict) -> SubmissionResult:
        if (
            self.trader is None
            or self.account is None
            or self.xtconstant is None
            or self.xtdata is None
        ):
            raise RuntimeError("QMT 尚未连接")
        ticks = self.xtdata.get_full_tick([order["symbol"]])
        tick = (ticks or {}).get(order["symbol"])
        if not tick:
            raise RuntimeError(f"QMT 到达价缺失: {order['symbol']}")
        arrival = tick.get("lastPrice") or tick.get("last_price") or tick.get("open")
        if arrival is None or float(arrival) <= 0:
            raise RuntimeError(f"QMT 到达价非法: {order['symbol']}")
        iopv = tick.get("iopv") or tick.get("IOPV")
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        order_type = (
            self.xtconstant.STOCK_BUY
            if order["direction"] == "BUY"
            else self.xtconstant.STOCK_SELL
        )
        remark = f"{order['order_id'][:16]}|{order['target_hash'][:12]}"
        local_id = self.trader.order_stock(
            self.account,
            order["symbol"],
            order_type,
            int(order["quantity"]),
            self.xtconstant.FIX_PRICE,
            float(order["limit_price"]),
            "hydra_live",
            remark,
        )
        if local_id is None or int(local_id) < 0:
            return SubmissionResult(
                str(local_id) if local_id is not None else None,
                "REJECTED",
                "QMT order_stock returned failure",
            )
        return SubmissionResult(
            str(local_id),
            "SUBMITTED",
            execution_meta={
                "arrival_reference_price": float(arrival),
                "arrival_reference_time": now,
                "submitted_price": float(order["limit_price"]),
                "submitted_time": now,
                "qmt_order_id": str(local_id),
                "iopv": float(iopv) if iopv is not None and float(iopv) > 0 else None,
                "iopv_time": now if iopv is not None else None,
            },
        )

    def settlement_results(self, submissions: list[dict]) -> list[dict]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接")
        trades = self.trader.query_stock_trades(self.account)
        orders = self.trader.query_stock_orders(self.account)
        if trades is None or orders is None:
            raise RuntimeError("QMT 成交/委托查询返回空")
        aggregates: dict[int, dict] = {}
        for trade in trades:
            local_id = int(trade.order_id)
            item = aggregates.setdefault(local_id, {"quantity": 0, "amount": 0.0})
            item["quantity"] += int(trade.traded_volume)
            item["amount"] += float(trade.traded_price) * int(trade.traded_volume)
        order_map = {int(order.order_id): order for order in orders}
        results = []
        for row in submissions:
            local_id = int(row["local_order_id"])
            fill = aggregates.get(local_id, {"quantity": 0, "amount": 0.0})
            qty = int(fill["quantity"])
            qmt_order = order_map.get(local_id)
            if qmt_order is None:
                raise RuntimeError(f"QMT 委托列表缺少本地订单: {local_id}")
            try:
                status = classify_qmt_settlement_status(
                    self.xtconstant,
                    qmt_order.order_status,
                    qty,
                    int(row["quantity"]),
                )
            except RuntimeError as exc:
                raise RuntimeError(f"QMT 委托 {local_id}: {exc}") from exc
            results.append({
                "order_id": row["order_id"],
                "filled_quantity": qty,
                "filled_price": round(fill["amount"] / qty, 4) if qty else 0.0,
                "status": status,
                "symbol": row["symbol"],
                "direction": row["direction"],
                **dict(row.get("execution_meta") or {}),
            })
        return results
