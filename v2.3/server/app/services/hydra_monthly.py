"""Monthly data inbox and plan publication. No QMT I/O or order creation here."""

import base64
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import HydraMonthlyCycle, HydraExecutionPlan, InstanceState
from app.schemas.hydra_relay import HydraTargetRequest, hydra_basket_hash
from app.services.ledger_transaction import begin_ledger_transaction

RESEARCH_COMMIT = "aa6b60deef44b244764385e7b6bd681429b9b362"
SYMBOLS = frozenset(
    {
        "159915.SZ",
        "159930.SZ",
        "159981.SZ",
        "159985.SZ",
        "510300.SH",
        "511260.SH",
        "513100.SH",
        "513500.SH",
        "518880.SH",
    }
)
CHINA = ZoneInfo("Asia/Shanghai")


def conflict(message):
    raise APIError(ErrorCode.BAD_REQUEST, message, http_status=409)


def month_end_next(calendar, day):
    datetime.strptime(day, "%Y%m%d")
    dates = sorted(set(calendar.trade_date.astype(str).str.replace("-", "", regex=False)))
    if day not in dates:
        raise ValueError("研究日期不是冻结日历中的交易日")
    future = [date for date in dates if date > day]
    if not future:
        raise ValueError("冻结日历缺下一交易日，不能判断月末")
    if future[0][:6] == day[:6]:
        raise ValueError("仅月末最后交易日可生成月度目标")
    return future[0]


def validate_inputs(relay, hashes, day):
    loaded = {key: relay.data_store.load("hydra_" + key, digest) for key, digest in hashes.items()}
    if any(manifest.as_of_date != day for _, manifest in loaded.values()):
        raise ValueError("输入批次日期不一致")
    model, mm = loaded["model_hfq"]
    raw, rm = loaded["execution_raw"]
    actions, _ = loaded["corporate_actions"]
    calendar, _ = loaded["trading_calendar"]
    relay._validate_data_universe(model, mm, raw, rm, actions)
    relay._require_as_of_coverage(model, day, SYMBOLS, "model_hfq")
    relay._require_as_of_coverage(raw, day, SYMBOLS, "execution_raw")
    return month_end_next(calendar, day)


def receive_snapshot(relay, settings, req, *, now=None):
    now = now or datetime.now(CHINA)
    today = now.astimezone(CHINA).strftime("%Y%m%d")
    if (req.instance_id, req.account_alias) != (
        settings.hydra_monthly_instance_id,
        settings.hydra_monthly_account_alias,
    ):
        raise APIError(
            ErrorCode.AUTH_FAILED, "研究数据账户/策略与服务器月度配置不一致", http_status=403
        )
    if req.as_of_date < settings.hydra_monthly_start_date or req.as_of_date > today:
        conflict("研究日期超出该自动任务启用范围；不会重放历史月度订单")
    if req.as_of_date == today and now.astimezone(CHINA).hour < 15:
        conflict("当日研究数据只能在收盘后冻结")
    identity = f"live/{req.account_alias}/{req.instance_id}/{req.as_of_date}"
    cycle_id = "hm_" + hashlib.sha256(identity.encode()).hexdigest()
    hashes = {key: item.manifest.file_sha256 for key, item in req.streams.items()}
    with relay.session_factory() as session:
        state = session.get(InstanceState, req.instance_id)
        if state is None or (state.execution_domain, state.account_alias, state.ledger_mode) != (
            "live",
            req.account_alias,
            "attributed",
        ):
            conflict("该策略尚无匹配的独立实盘账本")
    # Verify all byte digests before installation; register only a complete,
    # validated bundle. A lost HTTP response can retry the identical package.
    bodies = {}
    for key, item in req.streams.items():
        body = base64.b64decode(item.parquet_base64, validate=True)
        if hashlib.sha256(body).hexdigest() != item.manifest.file_sha256:
            raise ValueError("parquet 字节摘要不匹配")
        bodies[key] = body
    for key, body in bodies.items():
        item = req.streams[key]
        existing = (
            relay.data_store.root
            / item.manifest.stream
            / item.manifest.file_sha256
            / "manifest.json"
        )
        if existing.exists():
            _, old = relay.data_store.load(item.manifest.stream, item.manifest.file_sha256)
            if old.as_of_date != req.as_of_date or old.adjustment != item.manifest.adjustment:
                conflict("相同内容地址的日期/复权口径冲突")
        else:
            relay.data_store.install(body, item.manifest)
    next_date = validate_inputs(relay, hashes, req.as_of_date)
    with relay.session_factory() as session:
        begin_ledger_transaction(session, "live", req.account_alias)
        prior = session.get(HydraMonthlyCycle, cycle_id)
        if prior:
            if prior.input_hashes != hashes:
                conflict("该月度已有不同冻结输入；请审阅修订，不静默换掉已有计划")
            return {
                "status": "RESEARCH_SNAPSHOT_STORED",
                "cycle_id": cycle_id,
                "as_of_date": req.as_of_date,
                "already_stored": True,
                "cycle_status": prior.status,
            }
        session.add(
            HydraMonthlyCycle(
                cycle_id=cycle_id,
                execution_domain="live",
                account_alias=req.account_alias,
                instance_id=req.instance_id,
                as_of_date=req.as_of_date,
                input_hashes=hashes,
                status="RECEIVED",
                result={"next_trading_date": next_date},
                created_at=now.isoformat(),
            )
        )
        session.commit()
    return {
        "status": "RESEARCH_SNAPSHOT_STORED",
        "cycle_id": cycle_id,
        "as_of_date": req.as_of_date,
        "already_stored": False,
        "cycle_status": "RECEIVED",
    }


def publish_monthly_plan(relay, cycle_id, computed):
    """One transaction publishes weights and a durable plan, never an order.

    Evening advance uses fresh QMT facts and prices to size the orders. A crash
    before this transaction can recompute; a crash after cannot duplicate it.
    """
    with relay.session_factory() as session:
        cycle = session.get(HydraMonthlyCycle, cycle_id)
        if cycle is None:
            raise ValueError("月度输入不存在")
        account = cycle.account_alias
    with relay.session_factory() as session:
        begin_ledger_transaction(session, "live", account)
        cycle = session.get(HydraMonthlyCycle, cycle_id)
        if cycle.status == "PLANNED":
            return cycle.result
        weights = computed["weights"]
        if (
            computed["research_commit"] != RESEARCH_COMMIT
            or computed["as_of_date"] != cycle.as_of_date
            or computed["input_hashes"] != cycle.input_hashes
        ):
            conflict("研究输出与该月度固定源码/输入不一致")
        if set(weights) != SYMBOLS:
            conflict("研究权重标的集合发生变化")
        payload = dict(
            execution_domain="live",
            account_alias=account,
            instance_id=cycle.instance_id,
            strategy_version="v48.1-RB",
            publisher_source_commit=RESEARCH_COMMIT,
            decision_date=cycle.as_of_date,
            as_of_date=cycle.as_of_date,
            execution_date=cycle.result["next_trading_date"],
            input_hashes=cycle.input_hashes,
            research_input_hashes=cycle.input_hashes,
            weights=[
                {"code": symbol, "weight": weight} for symbol, weight in sorted(weights.items())
            ],
            cash_buffer_weight=0.0,
            buy_price_offset_bps=50,
            sell_price_offset_bps=50,
        )
        payload["basket_sha256"] = hydra_basket_hash(payload)
        req = HydraTargetRequest(**payload)
        relay._gate_request(req)
        pending = session.scalar(
            select(HydraExecutionPlan).where(
                HydraExecutionPlan.execution_domain == "live",
                HydraExecutionPlan.account_alias == account,
                HydraExecutionPlan.instance_id == cycle.instance_id,
                HydraExecutionPlan.status != "STAGED",
            )
        )
        if pending:
            conflict("已有尚未执行的策略目标；保留两期研究结果，需明确是否替换旧目标")
        plan_id = "hp_monthly_" + cycle_id[3:]
        result = {
            "status": "MONTHLY_PLAN_READY",
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "as_of_date": cycle.as_of_date,
            "weights": weights,
            "order_count": 0,
        }
        session.add(
            HydraExecutionPlan(
                plan_id=plan_id,
                execution_domain="live",
                account_alias=account,
                instance_id=cycle.instance_id,
                request_payload=req.model_dump(mode="json"),
                status="WAITING_EXECUTION_DATA",
                response_payload=result,
                created_at=datetime.now(CHINA).isoformat(),
            )
        )
        cycle.status = "PLANNED"
        cycle.result = result
        session.commit()
        return result
