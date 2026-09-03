"""持仓对账 schemas：把 Windows client 拉的 QMT 真实持仓推给 server 做 diff。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.execution import ExecutionDomain


class QmtPositionSnapshot(BaseModel):
    """Client 推上来的 QMT 实时账户快照。"""

    instance_id: str = Field(description="对应 strategies.yaml 里的 instance")
    execution_domain: ExecutionDomain = "paper"
    account_alias: str | None = None
    qmt_account_id: str = Field(description="QMT 账户号，仅作记录")
    qmt_cash: float = Field(description="账户可用资金（人民币元）")
    qmt_positions: dict[str, int] = Field(
        description="{symbol: 持仓量}，symbol 为 QMT 标准格式（如 600519.SH）",
    )
    snapshot_time: str = Field(description="Client 端 dump 时间，ISO 格式")
    dry_run: bool = Field(
        default=True,
        description="True=仅返回 diff 报告不改 server；False=apply 修改 instance_state",
    )
    force: bool = Field(
        default=False,
        description="apply 时跳过大批量 close 护栏（确认快照完整后才用）",
    )


class PositionDiff(BaseModel):
    """单只票的服务器 vs QMT 差异。"""

    symbol: str
    server_qty: int
    qmt_qty: int
    diff: int  # qmt_qty - server_qty，正数代表 QMT 比 server 多


class ReconcileResult(BaseModel):
    instance_id: str
    snapshot_time: str
    dry_run: bool
    applied: bool = False

    # Cash 对比
    server_cash: float
    qmt_cash: float
    cash_diff: float  # qmt - server

    # Position 对比汇总
    n_server_positions: int
    n_qmt_positions: int
    n_matched: int          # 数量完全一致的票数
    n_mismatched: int       # 数量不一致但两边都持有
    n_server_only: int      # server 有但 QMT 没有（最可疑：可能是幽灵持仓）
    n_qmt_only: int         # QMT 有但 server 没有（最可疑：可能漏推 trade_result）

    # 差异详情（如果 dry_run 返回所有 diffs；apply 模式返回为空避免日志爆炸）
    diffs: list[PositionDiff] = Field(default_factory=list)


class TotalReconcileResult(BaseModel):
    """Portfolio 级总量对账结果：Σ_instances virtual_positions[X] vs QMT[X]。"""

    snapshot_time: str
    n_symbols: int
    n_matched: int
    n_mismatched: int
    n_external_positions: int = 0
    external_positions: dict[str, int] = Field(default_factory=dict)
    # [{symbol, qmt, ledger_sum, diff, per_instance:{iid:qty}}]
    mismatches: list[dict] = Field(default_factory=list)
    cash_ok: bool
    ledger_cash_total: float
    qmt_cash: float
