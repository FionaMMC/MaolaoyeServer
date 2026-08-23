"""QMT gateway；xtquant 仅在真实 live 模式下延迟导入。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
