"""Simulated exchange and gateway that behave like QMT, for offline OMS tests.

Never routes anything. The fill model is the one in
reports/hydra_policy_20261002/policy_replay.py (auction prices from daily
bars, 5 bp slippage, participation cap, buy touch fill at the limit,
fee max(5, 1 bp)), so a system replay on this exchange can be compared with
the research replay cycle by cycle.

QMT habits reproduced on purpose:

* order status codes 50/51/52/53/54/55/56/57; a cancel request only moves
  50 -> 51 (55 -> 52) and becomes 54 (53) on the next clock advance;
* after 15:00 unfilled day orders keep showing 50/55, never "cancelled"
  (observed 2026-09-04);
* no cancels 09:20-09:25 and 14:57-15:00; orders only 09:15-11:30, 13:00-15:00;
* ``order_stock`` returns a positive id, or -1 on validation failure;
* T+1 on buys, sale proceeds usable at once, buys freeze limit x qty + fee;
* queries return the current trading day only; remarks are truncated.

Matching runs when the clock crosses 09:25 (opening auction: orders entered
09:15-09:25, at the official open), 14:55 (resting orders whose limit the
day's low/high traded through fill at the limit) and 15:00 (closing auction:
every order still open, at the official close). Pending cancels are applied
before matching on each advance, so a 14:55 cancel request does not stop the
14:55 touch fill but does keep the order out of the closing auction.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from live_client.gateway import (
    OMS_STRATEGY_NAME,
    QMT_FIX_PRICE,
    QMT_STOCK_BUY,
    QMT_STOCK_SELL,
    AccountSnapshot,
    BrokerOrderSnapshot,
    SubmissionResult,
    _broker_order_snapshot,
    oms_submission_result,
    oms_trade_row,
    validate_limit_order,
)
from live_client.qmt_day_order_policy import CHINA_TIMEZONE

LOT = 100
TICK = .001
STATUS_MSG = {50: "已报", 51: "已报待撤", 52: "部成待撤", 53: "部撤", 54: "已撤",
              55: "部成", 56: "已成", 57: "废单"}
OPEN_STATUSES = frozenset({50, 51, 52, 55})   # still holding frozen cash/shares
MATCHABLE_STATUSES = frozenset({50, 55})
OPEN_AUCTION, TOUCH, CLOSE_AUCTION = "092500", "145500", "150000"


def fee(gross: float) -> float:
    return max(5., gross * .0001) if gross else 0.


def _buy_price(reference: float, slip_bps: float) -> float:
    return round(math.ceil(reference * (1 + slip_bps / 1e4) / TICK - 1e-8) * TICK, 3)


def _sell_price(reference: float, slip_bps: float) -> float:
    return round(math.floor(reference * (1 - slip_bps / 1e4) / TICK + 1e-8) * TICK, 3)


def _frozen_cash(quantity: int, limit: float) -> float:
    return quantity * limit + fee(quantity * limit) if quantity > 0 else 0.


@dataclass
class _Order:
    order_id: int
    symbol: str
    side: str
    quantity: int
    price: float
    remark: str
    strategy_name: str
    entered: str
    order_time: int
    status: int = 50
    status_msg: str = STATUS_MSG[50]
    traded_volume: int = 0
    traded_amount: float = 0.
    frozen_cash: float = 0.
    frozen_shares: int = 0

    @property
    def remaining(self) -> int:
        return self.quantity - self.traded_volume


class SimExchange:
    """One account at a simulated exchange.

    ``faults`` (a plain set, edit it freely):
      * ``submit_raises_after_accept``: order_stock books the order, then raises;
      * ``submit_minus1``: order_stock returns -1 and books nothing;
      * ``trades_hang``: SimQMTGateway.day_trades reports a timeout (None);
      * ``disconnect_next_call``: the next SimQMTGateway call raises, once.
    The first three stay active until removed; the last is consumed.
    """

    def __init__(self, bars: dict[tuple[str, str], dict], *, cash: float, positions: dict[str, int],
                 participation: float = .01, slip_bps: float = 5., remark_limit: int = 24):
        """``bars`` maps (YYYYMMDD, symbol) to a daily bar with open, high,
        low, close, volume (lots of 100 shares) and optional suspendFlag."""
        self.bars = dict(bars)
        self.participation = float(participation)
        self.slip_bps = float(slip_bps)
        self.remark_limit = int(remark_limit)
        self.faults: set[str] = set()
        self._trading_days = frozenset(day for day, _ in self.bars)
        self._closes: dict[str, list[tuple[str, float]]] = {}
        for (day, symbol), bar in sorted(self.bars.items()):
            if bar.get("close") and float(bar["close"]) > 0:
                self._closes.setdefault(symbol, []).append((day, float(bar["close"])))
        self.cash = float(cash)  # including cash frozen by open buy orders
        self._positions = {s: int(q) for s, q in positions.items() if int(q) > 0}
        self._sellable = dict(self._positions)  # held before today, not yet sold today
        self.date: str | None = None
        self.time: str | None = None
        self._orders: list[_Order] = []
        self._trades: list[dict] = []
        self._capacity_used: dict[tuple[str, str], int] = {}
        self._next_order_id = 1
        self._next_trade_id = 1

    # -- clock -------------------------------------------------------------
    def set_clock(self, trade_date: str, hhmmss: str) -> None:
        if not re.fullmatch(r"\d{8}", trade_date) or not re.fullmatch(r"\d{6}", hhmmss):
            raise ValueError(f"非法时钟: {trade_date} {hhmmss}")
        datetime.strptime(trade_date + hhmmss, "%Y%m%d%H%M%S")
        if self.date is not None and (trade_date, hhmmss) < (self.date, self.time):
            raise ValueError("仿真时钟不能倒退")
        if trade_date != self.date:
            if self.date is not None:
                self._advance("240000")  # finish the old day: its auctions still happen
            self._roll(trade_date)
        self._advance(hhmmss)

    def now(self) -> datetime:
        if self.date is None:
            raise RuntimeError("仿真时钟尚未设置")
        return datetime.strptime(self.date + self.time, "%Y%m%d%H%M%S").replace(tzinfo=CHINA_TIMEZONE)

    def _roll(self, trade_date: str) -> None:
        self.date, self.time = trade_date, "000000"
        self._orders, self._trades, self._capacity_used = [], [], {}
        self._sellable = dict(self._positions)  # T+1: yesterday's buys become sellable

    def _advance(self, hhmmss: str) -> None:
        if hhmmss <= self.time:
            return
        for order in self._orders:
            if order.status in (51, 52):
                self._finish(order, 54 if order.status == 51 else 53)
        previous, self.time = self.time, hhmmss
        if self.date not in self._trading_days:
            return
        for event in (OPEN_AUCTION, TOUCH, CLOSE_AUCTION):
            if previous < event <= hhmmss:
                self.time = event  # fills carry the event time
                self._match(event)
        self.time = hhmmss

    # -- matching ----------------------------------------------------------
    def _bar(self, symbol: str) -> dict | None:
        return self.bars.get((self.date, symbol))

    @staticmethod
    def _tradable(bar: dict | None) -> bool:
        return bar is not None and float(bar.get("volume", 0)) > 0 and not bar.get("suspendFlag", 0)

    def _match(self, event: str) -> None:
        for order in list(self._orders):
            if order.status not in MATCHABLE_STATUSES:
                continue
            bar = self._bar(order.symbol)
            if not self._tradable(bar) or float(bar["high"]) <= float(bar["low"]):
                continue  # research treats a one-price bar as untradable
            if event == OPEN_AUCTION:
                if not "091500" <= order.entered < OPEN_AUCTION:
                    continue
                price = self._auction_price(order, float(bar["open"]))
            elif event == TOUCH:
                if order.entered >= TOUCH:
                    continue
                touched = (float(bar["low"]) < order.price - TICK / 2 if order.side == "BUY"
                           else float(bar["high"]) > order.price + TICK / 2)
                price = order.price if touched else None
            else:
                price = self._auction_price(order, float(bar["close"]))
            if price is not None:
                self._fill(order, price, bar)

    def _auction_price(self, order: _Order, reference: float) -> float | None:
        if order.side == "BUY":
            price = _buy_price(reference, self.slip_bps)
            return price if price - order.price <= 1e-8 else None
        price = _sell_price(reference, self.slip_bps)
        return price if order.price - price <= 1e-8 else None

    def _fill(self, order: _Order, price: float, bar: dict) -> None:
        key = (order.symbol, order.side)
        capacity = int(float(bar["volume"]) * 100 * self.participation)
        quantity = min(order.remaining, capacity - self._capacity_used.get(key, 0))
        if quantity < order.remaining:
            quantity = quantity // LOT * LOT  # partial fills come in whole lots
        if quantity <= 0:
            return
        self._capacity_used[key] = self._capacity_used.get(key, 0) + quantity
        amount = quantity * price
        cost = fee(amount)
        if order.side == "BUY":
            self.cash -= amount + cost
            self._positions[order.symbol] = self._positions.get(order.symbol, 0) + quantity
        else:
            self.cash += amount - cost
            self._positions[order.symbol] -= quantity
            self._sellable[order.symbol] -= quantity
            if not self._positions[order.symbol]:
                del self._positions[order.symbol]
        order.traded_volume += quantity
        order.traded_amount += amount
        self._trades.append({
            "traded_id": f"T{self._next_trade_id:08d}",
            "order_id": order.order_id,
            "order_sysid": f"S{order.order_id:08d}",
            "stock_code": order.symbol,
            "order_type": QMT_STOCK_BUY if order.side == "BUY" else QMT_STOCK_SELL,
            "traded_volume": quantity,
            "traded_price": price,
            "traded_amount": amount,
            "traded_time": int(self.now().timestamp()),
            "commission": cost,
            "strategy_name": order.strategy_name,
            "order_remark": order.remark,
        })
        self._next_trade_id += 1
        if order.remaining == 0:
            self._finish(order, 56)
        else:
            order.status, order.status_msg = 55, STATUS_MSG[55]
            if order.side == "BUY":
                order.frozen_cash = _frozen_cash(order.remaining, order.price)
            else:
                order.frozen_shares = order.remaining

    def _finish(self, order: _Order, status: int, detail: str = "") -> None:
        order.status = status
        order.status_msg = STATUS_MSG[status] + (f": {detail}" if detail else "")
        order.frozen_cash, order.frozen_shares = 0., 0

    # -- broker API --------------------------------------------------------
    def _available_cash(self) -> float:
        return self.cash - sum(o.frozen_cash for o in self._orders if o.status in OPEN_STATUSES)

    def _can_use(self, symbol: str) -> int:
        return self._sellable.get(symbol, 0) - sum(
            o.frozen_shares for o in self._orders if o.symbol == symbol and o.status in OPEN_STATUSES)

    def _entry_open(self) -> bool:
        return (self.date in self._trading_days
                and ("091500" <= self.time < "113000" or "130000" <= self.time < "150000"))

    def order_stock(self, symbol: str, side: str, quantity: int, price: float, remark: str,
                    strategy_name: str = "") -> int:
        if "submit_minus1" in self.faults or self.date is None or not self._entry_open():
            return -1
        if side not in ("BUY", "SELL") or isinstance(quantity, bool) or int(quantity) != quantity:
            return -1
        quantity, price = int(quantity), float(price)
        if (quantity <= 0 or not math.isfinite(price) or price <= 0
                or abs(price / TICK - round(price / TICK)) > 1e-6):
            return -1
        frozen_cash, frozen_shares = 0., 0
        if side == "BUY":
            if quantity % LOT:
                return -1
            frozen_cash = _frozen_cash(quantity, price)
            if frozen_cash > self._available_cash() + 1e-9:
                return -1
        else:
            can_use = self._can_use(symbol)
            if quantity > can_use or (quantity % LOT and quantity % LOT != can_use % LOT):
                return -1
            frozen_shares = quantity
        order = _Order(self._next_order_id, symbol, side, quantity, round(price, 3),
                       str(remark)[:self.remark_limit], strategy_name, self.time,
                       int(self.now().timestamp()), frozen_cash=frozen_cash,
                       frozen_shares=frozen_shares)
        self._next_order_id += 1
        self._orders.append(order)
        if not self._tradable(self._bar(symbol)):
            self._finish(order, 57, "证券停牌或无成交")
        if "submit_raises_after_accept" in self.faults:
            raise RuntimeError("simulated: QMT accepted the order but the response was lost")
        return order.order_id

    def cancel(self, order_id: int) -> int:
        order = next((o for o in self._orders if o.order_id == int(order_id)), None)
        if order is None or self.date is None or not "091500" <= self.time < "150000":
            return -1
        if "092000" <= self.time < "092500" or "145700" <= self.time < "150000":
            return -1
        if order.status == 50:
            order.status, order.status_msg = 51, STATUS_MSG[51]
        elif order.status == 55:
            order.status, order.status_msg = 52, STATUS_MSG[52]
        else:
            return -1
        return 0

    def orders(self) -> list[dict]:
        return [{
            "order_id": o.order_id,
            "order_sysid": f"S{o.order_id:08d}",
            "order_time": o.order_time,
            "stock_code": o.symbol,
            "order_type": QMT_STOCK_BUY if o.side == "BUY" else QMT_STOCK_SELL,
            "order_volume": o.quantity,
            "price_type": QMT_FIX_PRICE,
            "price": o.price,
            "traded_volume": o.traded_volume,
            "traded_price": round(o.traded_amount / o.traded_volume, 6) if o.traded_volume else 0.,
            "order_status": o.status,
            "status_msg": o.status_msg,
            "strategy_name": o.strategy_name,
            "order_remark": o.remark,
        } for o in self._orders]

    def trades(self) -> list[dict]:
        return [dict(trade) for trade in self._trades]

    def _mark(self, symbol: str) -> float | None:
        bar = self._bar(symbol) if self.date else None
        if self._tradable(bar) and self.time >= OPEN_AUCTION:
            return float(bar["close"] if self.time >= CLOSE_AUCTION else bar["open"])
        history = self._closes.get(symbol, [])
        index = bisect.bisect_left(history, (self.date or "", ))
        return history[index - 1][1] if index else None

    def positions(self) -> dict:
        result = {}
        for symbol, volume in sorted(self._positions.items()):
            mark = self._mark(symbol)
            result[symbol] = {
                "volume": volume,
                "can_use_volume": self._can_use(symbol),
                "market_value": volume * mark if mark else 0.,
            }
        return result

    def asset(self) -> dict:
        available = self._available_cash()
        market_value = sum(p["market_value"] for p in self.positions().values())
        return {
            "cash": available,
            "frozen_cash": self.cash - available,
            "market_value": market_value,
            "total_asset": self.cash + market_value,
        }

    def quote(self, symbol: str) -> dict | None:
        mark = self._mark(symbol)
        if not mark:
            return None
        return {"last_price": mark, "is_trading": self._tradable(self._bar(symbol))}


class SimQMTGateway:
    """The part of the XtQMTGateway surface the OMS agent uses, on a SimExchange."""

    def __init__(self, exchange: SimExchange, account_id: str = "SIM_ACCOUNT"):
        self.exchange = exchange
        self.account_id = account_id
        self.connected = False

    def _broker_call(self) -> None:
        faults = self.exchange.faults
        if "disconnect_next_call" in faults:
            faults.discard("disconnect_next_call")
            raise RuntimeError("simulated QMT disconnect")
        if not self.connected:
            raise RuntimeError("QMT 尚未连接")

    def connect(self) -> None:
        self.connected = True
        try:
            self._broker_call()
        except RuntimeError:
            self.connected = False
            raise

    def close(self) -> None:
        self.connected = False

    def account_snapshot(self) -> AccountSnapshot:
        self._broker_call()
        asset = self.exchange.asset()
        positions = self.exchange.positions()
        return AccountSnapshot(
            account_id=self.account_id,
            available_cash=asset["cash"],
            total_asset=asset["total_asset"],
            positions={s: p["volume"] for s, p in positions.items()},
            sellable_positions={s: p["can_use_volume"] for s, p in positions.items()
                                if p["can_use_volume"] > 0},
            position_market_values={s: p["market_value"] for s, p in positions.items()},
        )

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        self._broker_call()
        result = {}
        for symbol in symbols:
            quote = self.exchange.quote(symbol)
            if quote is not None:
                result[symbol] = quote
        return result

    def day_orders(self) -> list[BrokerOrderSnapshot]:
        self._broker_call()
        return [_broker_order_snapshot(SimpleNamespace(account_id=self.account_id, **row))
                for row in self.exchange.orders()]

    def day_trades(self, timeout_seconds: float = 10.0) -> list[dict] | None:
        self._broker_call()
        if "trades_hang" in self.exchange.faults:
            return None  # what XtQMTGateway reports after its bounded wait
        sides = {QMT_STOCK_BUY: "BUY", QMT_STOCK_SELL: "SELL"}
        return [oms_trade_row(SimpleNamespace(**row), sides) for row in self.exchange.trades()]

    def submit_limit(self, *, symbol: str, side: str, quantity: int, limit_price: float,
                     remark: str) -> SubmissionResult:
        validate_limit_order(side, quantity, limit_price)
        if not self.connected:
            raise RuntimeError("QMT 尚未连接")
        try:
            self._broker_call()  # a disconnect here is lost inside the order call
            raw_order_id = self.exchange.order_stock(
                symbol, side, int(quantity), float(limit_price), remark,
                strategy_name=OMS_STRATEGY_NAME,
            )
        except Exception as exc:
            return SubmissionResult(
                None, "UNKNOWN", f"QMT order_stock raised {type(exc).__name__}: {exc}",
            )
        return oms_submission_result(raw_order_id, limit_price)

    def cancel_order(self, order_id: int) -> None:
        self._broker_call()
        result = self.exchange.cancel(int(order_id))
        if result != 0:
            raise RuntimeError(f"QMT 撤单指令失败: {result}")
