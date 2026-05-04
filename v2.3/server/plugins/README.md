# 策略插件目录

把你的策略 .py 文件丢到这个目录，server 启动时会自动扫描注册。

## 契约

每个文件定义一个或多个继承 `app.strategy.base.Strategy` 的子类：

```python
from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context


class MyStrategy(Strategy):
    name = "my_strategy"   # 必填，须与 strategies.yaml 的 strategy_id 对齐

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        return [RawSignal(symbol="600519.SH", direction="BUY",
                          quantity=100, reference_price=1500.0,
                          price_offset=0.005)]
```

## 注意事项

- `name` 必须唯一；冲突时 server 启动报错
- 单个 .py 加载失败时被跳过、记日志
- 单个策略 `run()` 抛异常时被捕获，该实例当日 0 信号，其他实例不受影响
- `_` 开头的文件也会被加载（如 `_example_buy_threshold.py`）
