"""SQLAlchemy ORM models 集中导出。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# 触发 model 注册到 Base.metadata
from app.models.instance_state import InstanceState  # noqa: E402, F401
from app.models.raw_signal import RawSignal  # noqa: E402, F401
from app.models.order import Order  # noqa: E402, F401
from app.models.order_signal_map import OrderSignalMap  # noqa: E402, F401
from app.models.trade import Trade  # noqa: E402, F401
from app.models.perf_snapshot import PerfSnapshot  # noqa: E402, F401
from app.models.cash_flow import CashFlowJournal  # noqa: E402, F401
from app.models.daily_risk_snapshot import DailyRiskSnapshot  # noqa: E402, F401
from app.models.execution_quality import ExecutionQualityObservation  # noqa: E402, F401
from app.models.hydra_execution import (  # noqa: E402, F401
    HydraExecutionAttempt,
    HydraRebalance,
    HydraTarget,
)
from app.models.risk_blacklist import RiskBlacklistEntry  # noqa: E402, F401
from app.models.shadow import (  # noqa: E402, F401
    ShadowInstanceState,
    ShadowNavSnapshot,
    ShadowTarget,
)
from app.models.architecture_review import (  # noqa: E402, F401
    ArchitectureReviewComment,
    ArchitectureReviewDecision,
)

__all__ = [
    "Base",
    "InstanceState",
    "RawSignal",
    "Order",
    "OrderSignalMap",
    "Trade",
    "PerfSnapshot",
    "CashFlowJournal",
    "DailyRiskSnapshot",
    "ExecutionQualityObservation",
    "HydraTarget",
    "HydraRebalance",
    "HydraExecutionAttempt",
    "RiskBlacklistEntry",
    "ShadowInstanceState",
    "ShadowNavSnapshot",
    "ShadowTarget",
    "ArchitectureReviewComment",
    "ArchitectureReviewDecision",
]
