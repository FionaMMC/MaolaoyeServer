"""示例策略：当某只股票当日 close 跌破最近 5 日均线 5% 时买入 100 股。"""
from __future__ import annotations

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context


class BuyOnDipExample(Strategy):
    name = "buy_on_dip_example"
    data_dir = None
    data_files = []

    LOOKBACK = 5
    DROP_THRESHOLD = 0.95
    LIMIT_OFFSET = 0.005

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        signals: list[RawSignal] = []
        for symbol in ctx.universe("stocks"):
            df = ctx.market(symbol, fields=["close"])
            if len(df) < self.LOOKBACK:
                continue
            recent = df.tail(self.LOOKBACK)
            today_close = float(recent["close"].iloc[-1])
            ma = float(recent["close"].mean())
            if today_close < ma * self.DROP_THRESHOLD and ctx.position(symbol) == 0:
                cost = today_close * 100 * (1 + self.LIMIT_OFFSET)
                if cost <= ctx.cash():
                    signals.append(RawSignal(
                        symbol=symbol, direction="BUY", quantity=100,
                        reference_price=today_close,
                        price_offset=self.LIMIT_OFFSET,
                    ))
        return signals
