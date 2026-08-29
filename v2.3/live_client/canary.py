"""One-lot, real-MiniQMT canary with an immutable two-phase approval plan.

This deliberately does not call the Hydra server.  A sliced formal Hydra order
would create a false partial batch and contaminate the production ledger.  The
canary proves the real broker path only; any fill must be included in the next
server account initialization/reconciliation before a formal target is staged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from live_client.config import LiveClientConfig
from live_client.gateway import (
    BrokerOrderSnapshot,
    MarketQuote,
    XtQMTGateway,
    classify_qmt_settlement_status,
)
from live_client.http_client import LiveServerClient

SCHEMA = "hydra-miniqmt-live-canary/v1"
CANARY_SYMBOLS = frozenset({"510300.SH", "159915.SZ"})
CANARY_QUANTITY = 100
HARD_MAX_NOTIONAL_CNY = 2_000.0
MAX_SPREAD_BPS = 15.0
MAX_PREMIUM_BPS = 30.0
MAX_QUOTE_AGE_SECONDS = 5.0
MAX_REQUOTE_DRIFT_BPS = 20.0
PLAN_TTL_SECONDS = 120
PLAN_CONFIRM_ENV = "HYDRA_LIVE_CANARY_CONFIRM_SHA256"
CANCEL_CONFIRM_ENV = "HYDRA_LIVE_CANARY_CANCEL_CONFIRM_SHA256"
CANARY_ENABLED_ENV = "HYDRA_LIVE_CANARY_ENABLED"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _env_enabled(name: str) -> bool:
    value = os.environ.get(name, "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def _now_shanghai() -> datetime:
    return datetime.now(timezone.utc).astimezone(SHANGHAI)


def _require_canary_window(now: datetime) -> None:
    local = now.astimezone(SHANGHAI)
    if local.weekday() >= 5:
        raise RuntimeError("MiniQMT canary 仅允许中国交易日盘中运行")
    clock = local.time().replace(tzinfo=None)
    allowed = (
        time(9, 35) <= clock <= time(11, 25)
        or time(13, 5) <= clock <= time(14, 50)
    )
    if not allowed:
        raise RuntimeError("MiniQMT canary 仅允许 09:35-11:25 / 13:05-14:50 运行")


def _require_real_mode(cfg: LiveClientConfig) -> None:
    if cfg.mode != "live" or cfg.execution_domain != "live":
        raise RuntimeError("MiniQMT canary 只允许 mode=live 且 execution_domain=live")


def _positions_hash(positions: dict[str, int]) -> str:
    return hashlib.sha256(_canonical(dict(sorted(positions.items())))).hexdigest()


def _quote_iopv(
    quote: MarketQuote,
    supplied_iopv: float | None,
    supplied_source: str | None,
) -> tuple[float, str]:
    if supplied_iopv is not None and (
        not math.isfinite(supplied_iopv) or supplied_iopv <= 0
    ):
        raise ValueError("--iopv 必须是正有限数")
    if quote.iopv is not None:
        if supplied_iopv is not None:
            divergence = abs(supplied_iopv / quote.iopv - 1) * 10_000
            if divergence > 20:
                raise RuntimeError("QMT tick IOPV 与人工 IOPV 偏差超过 20bps")
        return quote.iopv, "xtdata.get_full_tick"
    if supplied_iopv is None or not (supplied_source or "").strip():
        raise RuntimeError(
            "QMT tick 未提供 IOPV；必须同时提供 --iopv 和 --iopv-source"
        )
    return supplied_iopv, supplied_source.strip()


def _validate_quote(
    quote: MarketQuote,
    *,
    now: datetime,
    iopv: float,
    available_cash: float,
) -> dict:
    _require_canary_window(now)
    if quote.symbol not in CANARY_SYMBOLS:
        raise RuntimeError(f"canary 标的不在专用白名单: {quote.symbol}")
    if not quote.is_trading:
        raise RuntimeError(f"QMT 合约当前不可交易: {quote.symbol}")
    source = quote.source_time.astimezone(SHANGHAI)
    current = now.astimezone(SHANGHAI)
    age = (current - source).total_seconds()
    if source.date() != current.date() or not -2 <= age <= MAX_QUOTE_AGE_SECONDS:
        raise RuntimeError(f"QMT 行情不新鲜: age_seconds={age:.3f}")
    numbers = (
        quote.last_price, quote.bid1, quote.ask1, quote.price_tick,
        quote.up_limit, quote.down_limit, iopv, available_cash,
    )
    if any(not math.isfinite(value) or value <= 0 for value in numbers):
        raise RuntimeError("QMT 行情、IOPV 或可用资金含非法值")
    if quote.bid1 > quote.ask1:
        raise RuntimeError("QMT 一档买价高于卖价")
    if not quote.down_limit <= quote.ask1 <= quote.up_limit:
        raise RuntimeError("QMT 卖一价超出涨跌停范围")
    ticks = quote.ask1 / quote.price_tick
    if abs(ticks - round(ticks)) > 1e-6:
        raise RuntimeError("QMT 卖一价不在最小价格档位上")
    midpoint = (quote.bid1 + quote.ask1) / 2
    spread_bps = (quote.ask1 - quote.bid1) / midpoint * 10_000
    if spread_bps > MAX_SPREAD_BPS:
        raise RuntimeError(f"盘口价差超过 canary 上限: {spread_bps:.3f}bps")
    premium_bps = (quote.ask1 / iopv - 1) * 10_000
    if premium_bps > MAX_PREMIUM_BPS:
        raise RuntimeError(f"ETF 买入溢价超过 canary 上限: {premium_bps:.3f}bps")
    notional = quote.ask1 * CANARY_QUANTITY
    if notional > HARD_MAX_NOTIONAL_CNY:
        raise RuntimeError(f"canary 名义金额超过硬上限: CNY {notional:.2f}")
    estimated_commission = max(5.0, notional * 0.0003)
    if available_cash < notional + estimated_commission + 5.0:
        raise RuntimeError("QMT 可用资金不足以覆盖 canary、最低佣金和安全余量")
    return {
        "quote_age_seconds": round(age, 6),
        "spread_bps": round(spread_bps, 6),
        "premium_bps": round(premium_bps, 6),
        "notional_cny": round(notional, 3),
        "estimated_commission_cny": round(estimated_commission, 3),
    }


def _write_exclusive(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # fdopen owns the descriptor after it succeeds.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _append_event(plan_path: Path, plan_sha256: str, event: dict) -> dict:
    event_path = Path(f"{plan_path}.events.jsonl")
    previous = "0" * 64
    if event_path.exists():
        lines = [line for line in event_path.read_text(encoding="utf-8").splitlines() if line]
        if lines:
            previous = json.loads(lines[-1])["event_sha256"]
    payload = {
        "schema": SCHEMA,
        "plan_sha256": plan_sha256,
        "previous_event_sha256": previous,
        **event,
    }
    payload["event_sha256"] = _sha256(payload)
    descriptor = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(_canonical(payload).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def _load_plan(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise RuntimeError("canary plan schema 不匹配")
    expected = str(payload.get("plan_sha256", ""))
    unsigned = {key: value for key, value in payload.items() if key != "plan_sha256"}
    if not expected or _sha256(unsigned) != expected:
        raise RuntimeError("canary plan SHA-256 校验失败")
    return payload


def _lock_path(plan_path: Path) -> Path:
    return Path(f"{plan_path}.submit-lock.json")


def _require_evidence_path(cfg: LiveClientConfig, path: Path) -> Path:
    resolved = Path(path).resolve()
    root = cfg.log_dir.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError("canary plan/evidence 必须位于 live client log_dir 内")
    return resolved


def _snapshot_payload(order: BrokerOrderSnapshot) -> dict:
    payload = asdict(order)
    account_id = payload.pop("account_id")
    payload["account_fingerprint_sha256"] = hashlib.sha256(
        account_id.encode(),
    ).hexdigest()
    return payload


def _verify_broker_order(
    order: BrokerOrderSnapshot, plan: dict, cfg: LiveClientConfig,
) -> None:
    if order.account_id != cfg.account_id:
        raise RuntimeError("QMT canary 委托账户不匹配")
    if order.stock_code != plan["order"]["symbol"]:
        raise RuntimeError("QMT canary 委托代码不匹配")
    if order.order_volume != CANARY_QUANTITY:
        raise RuntimeError("QMT canary 委托数量不匹配")
    if abs(order.price - float(plan["order"]["limit_price"])) > 1e-6:
        raise RuntimeError("QMT canary 委托限价不匹配")
    if order.strategy_name != "hydra_canary":
        raise RuntimeError("QMT canary strategy_name 不匹配")
    if order.order_remark != plan["order"]["remark"]:
        raise RuntimeError("QMT canary remark 不匹配")


def _broker_state(order: BrokerOrderSnapshot, constants) -> dict:
    try:
        status = classify_qmt_settlement_status(
            constants,
            order.order_status,
            order.traded_volume,
            order.order_volume,
        )
        terminal = True
    except RuntimeError as exc:
        status = "ACTIVE_OR_UNKNOWN"
        terminal = False
        classification_error = str(exc)
    result = {
        "terminal": terminal,
        "status": status,
        "order": _snapshot_payload(order),
    }
    if not terminal:
        result["classification_error"] = classification_error
    return result


def plan_canary(
    cfg: LiveClientConfig,
    *,
    symbol: str,
    output: Path,
    supplied_iopv: float | None = None,
    supplied_iopv_source: str | None = None,
    gateway_factory: Callable = XtQMTGateway,
    now: datetime | None = None,
) -> dict:
    _require_real_mode(cfg)
    if cfg.trading_enabled or _env_enabled(CANARY_ENABLED_ENV):
        raise RuntimeError("生成 plan 时交易总开关和 canary 开关必须同时关闭")
    if symbol not in CANARY_SYMBOLS or symbol not in cfg.allowed_symbols:
        raise RuntimeError(f"canary 仅允许 {sorted(CANARY_SYMBOLS)}")
    output = _require_evidence_path(cfg, output)
    current = now or _now_shanghai()
    _require_canary_window(current)
    gateway = gateway_factory(cfg)
    gateway.connect()
    try:
        account = gateway.account_snapshot()
        if account.account_id != cfg.account_id:
            raise RuntimeError("QMT account_id 二次校验失败")
        active = gateway.cancelable_orders()
        if active:
            raise RuntimeError("QMT 账户存在可撤委托，拒绝生成 canary plan")
        if gateway.canary_orders():
            raise RuntimeError("QMT 账户今天已经存在 canary 委托")
        quote = gateway.market_quote(symbol)
        iopv, iopv_source = _quote_iopv(
            quote, supplied_iopv, supplied_iopv_source,
        )
        checks = _validate_quote(
            quote, now=current, iopv=iopv, available_cash=account.available_cash,
        )
    finally:
        gateway.close()
    created = current.astimezone(SHANGHAI)
    plan = {
        "schema": SCHEMA,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
        "trade_date": created.strftime("%Y%m%d"),
        "account": {
            "account_fingerprint_sha256": cfg.expected_account_sha256,
            "account_alias": cfg.account_alias,
            "instance_id": cfg.instance_id,
            "available_cash": round(account.available_cash, 6),
            "total_asset": round(account.total_asset, 6),
            "positions_sha256": _positions_hash(account.positions),
        },
        "order": {
            "direction": "BUY",
            "symbol": symbol,
            "quantity": CANARY_QUANTITY,
            "limit_price": round(quote.ask1, 6),
            "remark": f"HC|{secrets.token_hex(10)}",
        },
        "quote": {
            "last_price": round(quote.last_price, 6),
            "bid1": round(quote.bid1, 6),
            "ask1": round(quote.ask1, 6),
            "source_time": quote.source_time.isoformat(),
            "captured_at": quote.captured_at.isoformat(),
            "price_tick": quote.price_tick,
            "up_limit": quote.up_limit,
            "down_limit": quote.down_limit,
            "iopv": round(iopv, 8),
            "iopv_source": iopv_source,
        },
        "checks": checks,
        "hard_limits": {
            "max_notional_cny": HARD_MAX_NOTIONAL_CNY,
            "max_spread_bps": MAX_SPREAD_BPS,
            "max_premium_bps": MAX_PREMIUM_BPS,
            "max_quote_age_seconds": MAX_QUOTE_AGE_SECONDS,
            "max_requote_drift_bps": MAX_REQUOTE_DRIFT_BPS,
            "plan_ttl_seconds": PLAN_TTL_SECONDS,
        },
        "scope": "REAL_MINIQMT_ONLY_NO_HYDRA_SERVER_LEDGER",
    }
    plan["plan_sha256"] = _sha256(plan)
    _write_exclusive(output, plan)
    return plan


def _validate_plan_for_action(
    cfg: LiveClientConfig, plan: dict, current: datetime, *, require_fresh: bool,
) -> None:
    _require_real_mode(cfg)
    if plan["account"]["account_fingerprint_sha256"] != cfg.expected_account_sha256:
        raise RuntimeError("canary plan 账户指纹与当前配置不一致")
    if plan["account"]["account_alias"] != cfg.account_alias:
        raise RuntimeError("canary plan account_alias 与当前配置不一致")
    if plan["account"]["instance_id"] != cfg.instance_id:
        raise RuntimeError("canary plan instance_id 与当前配置不一致")
    local = current.astimezone(SHANGHAI)
    if require_fresh and plan["trade_date"] != local.strftime("%Y%m%d"):
        raise RuntimeError("canary plan 不是当前中国交易日")
    if require_fresh:
        _require_canary_window(local)
        expires = datetime.fromisoformat(plan["expires_at"])
        if local > expires:
            raise RuntimeError("canary plan 已过 120 秒有效期")


def _fresh_submit_checks(
    plan: dict,
    quote: MarketQuote,
    *,
    now: datetime,
    available_cash: float,
) -> dict:
    iopv = quote.iopv or float(plan["quote"]["iopv"])
    checks = _validate_quote(
        quote, now=now, iopv=iopv, available_cash=available_cash,
    )
    planned_ask = float(plan["quote"]["ask1"])
    drift_bps = abs(quote.ask1 / planned_ask - 1) * 10_000
    if drift_bps > MAX_REQUOTE_DRIFT_BPS:
        raise RuntimeError(f"二次盘口相对 plan 漂移超过上限: {drift_bps:.3f}bps")
    if quote.ask1 > float(plan["order"]["limit_price"]):
        raise RuntimeError("当前卖一高于已批准限价，拒绝追价")
    return {**checks, "requote_drift_bps": round(drift_bps, 6)}


def submit_canary(
    cfg: LiveClientConfig,
    *,
    plan_path: Path,
    gateway_factory: Callable = XtQMTGateway,
    now: datetime | None = None,
) -> dict:
    plan_path = _require_evidence_path(cfg, plan_path)
    plan = _load_plan(plan_path)
    current = now or _now_shanghai()
    _validate_plan_for_action(cfg, plan, current, require_fresh=True)
    cfg.require_submission_enabled()
    if not _env_enabled(CANARY_ENABLED_ENV):
        raise RuntimeError(f"{CANARY_ENABLED_ENV}=false，canary 独立闸门关闭")
    if os.environ.get(PLAN_CONFIRM_ENV, "").strip() != plan["plan_sha256"]:
        raise RuntimeError(f"{PLAN_CONFIRM_ENV} 与 plan_sha256 不一致")
    lock_path = _lock_path(plan_path)
    if lock_path.exists():
        raise RuntimeError("canary submit lock 已存在；禁止重复下单")
    gateway = gateway_factory(cfg)
    gateway.connect()
    try:
        account = gateway.account_snapshot()
        if account.account_id != cfg.account_id:
            raise RuntimeError("QMT account_id 二次校验失败")
        if _positions_hash(account.positions) != plan["account"]["positions_sha256"]:
            raise RuntimeError("QMT 持仓自 plan 后发生变化")
        planned_cash = float(plan["account"]["available_cash"])
        if abs(account.available_cash - planned_cash) > 1.0:
            raise RuntimeError("QMT 可用资金自 plan 后变化超过 CNY 1")
        if gateway.cancelable_orders():
            raise RuntimeError("QMT 账户存在其他可撤委托，拒绝 canary 下单")
        if gateway.canary_orders():
            raise RuntimeError("QMT 账户今天已经存在 canary 委托")
        if gateway.orders_by_remark(plan["order"]["remark"]):
            raise RuntimeError("QMT 已存在相同 canary remark，禁止重复下单")
        quote = gateway.market_quote(plan["order"]["symbol"])
        checks = _fresh_submit_checks(
            plan,
            quote,
            now=current,
            available_cash=account.available_cash,
        )
        _write_exclusive(lock_path, {
            "schema": SCHEMA,
            "plan_sha256": plan["plan_sha256"],
            "created_at": current.isoformat(),
            "state": "SUBMITTING_OR_SUBMITTED_DO_NOT_RETRY",
        })
        _append_event(plan_path, plan["plan_sha256"], {
            "event": "SUBMIT_INTENT_DURABLE",
            "recorded_at": current.isoformat(),
            "checks": checks,
        })
        order_id = gateway.submit_canary(
            symbol=plan["order"]["symbol"],
            quantity=CANARY_QUANTITY,
            limit_price=float(plan["order"]["limit_price"]),
            remark=plan["order"]["remark"],
        )
        order = gateway.query_order(order_id)
        event = {
            "event": "ORDER_STOCK_RETURNED",
            "recorded_at": _now_shanghai().isoformat(),
            "qmt_order_id": order_id,
            "broker_order": _snapshot_payload(order) if order else None,
        }
        _append_event(plan_path, plan["plan_sha256"], event)
        if order is None:
            return {
                "status": "SUBMITTED_UNCONFIRMED_DO_NOT_RETRY",
                "qmt_order_id": order_id,
                "plan_sha256": plan["plan_sha256"],
            }
        _verify_broker_order(order, plan, cfg)
        return {
            "status": "BROKER_ORDER_OBSERVED",
            "plan_sha256": plan["plan_sha256"],
            "broker_state": _broker_state(order, gateway.xtconstant),
        }
    finally:
        gateway.close()


def status_canary(
    cfg: LiveClientConfig,
    *,
    plan_path: Path,
    gateway_factory: Callable = XtQMTGateway,
    now: datetime | None = None,
) -> dict:
    plan_path = _require_evidence_path(cfg, plan_path)
    plan = _load_plan(plan_path)
    current = now or _now_shanghai()
    _validate_plan_for_action(cfg, plan, current, require_fresh=False)
    gateway = gateway_factory(cfg)
    gateway.connect()
    try:
        matches = gateway.orders_by_remark(plan["order"]["remark"])
        if len(matches) != 1:
            result = {
                "status": "ORDER_NOT_UNIQUELY_OBSERVED",
                "match_count": len(matches),
                "submit_lock_exists": _lock_path(plan_path).exists(),
            }
        else:
            _verify_broker_order(matches[0], plan, cfg)
            result = _broker_state(matches[0], gateway.xtconstant)
        _append_event(plan_path, plan["plan_sha256"], {
            "event": "STATUS_OBSERVED",
            "recorded_at": current.isoformat(),
            "result": result,
        })
        return {"plan_sha256": plan["plan_sha256"], **result}
    finally:
        gateway.close()


def stage_server_canary(
    cfg: LiveClientConfig, *, plan_path: Path, execution_date: str,
) -> dict:
    """Stage a frozen plan for a later trading date; no QMT action."""
    plan_path = _require_evidence_path(cfg, plan_path)
    plan = _load_plan(plan_path)
    # Formal Hydra orders are deliberately staged before T+1.  Unlike the
    # direct broker-only canary, server staging never chases/reprices a plan.
    _validate_plan_for_action(cfg, plan, _now_shanghai(), require_fresh=False)
    if not execution_date.isdigit() or len(execution_date) != 8:
        raise ValueError("execution_date 必须为 YYYYMMDD")
    order, quote = plan["order"], plan["quote"]
    return LiveServerClient(cfg.server_base_url, cfg.api_key, execution_domain="live").stage_canary({
        "execution_domain": "live", "account_alias": cfg.account_alias,
        "trade_date": execution_date, "plan_sha256": plan["plan_sha256"],
        "symbol": order["symbol"], "quantity": order["quantity"],
        "reference_price": quote["ask1"], "limit_price": order["limit_price"],
    })


def prepare_weekend_plan(cfg: LiveClientConfig, *, symbol: str, execution_date: str,
                         reference_price: float, limit_price: float, output: Path) -> dict:
    """Create an immutable T+1 canary intent without QMT access or submission."""
    _require_real_mode(cfg)
    if symbol not in CANARY_SYMBOLS or symbol not in cfg.allowed_symbols:
        raise ValueError("canary symbol not allowed")
    if not execution_date.isdigit() or len(execution_date) != 8:
        raise ValueError("execution_date 必须为 YYYYMMDD")
    if reference_price <= 0 or limit_price <= 0 or limit_price * 100 > HARD_MAX_NOTIONAL_CNY:
        raise ValueError("canary price/notional invalid")
    if abs(limit_price / reference_price - 1) * 10_000 > 50.01:
        raise ValueError("canary price offset exceeds 50bps")
    output = _require_evidence_path(cfg, output)
    plan = {"schema": SCHEMA, "created_at": _now_shanghai().isoformat(),
        "expires_at": None, "trade_date": execution_date,
        "account": {"account_fingerprint_sha256": cfg.expected_account_sha256,
                    "account_alias": cfg.account_alias, "instance_id": cfg.instance_id},
        "order": {"direction": "BUY", "symbol": symbol, "quantity": 100,
                  "limit_price": round(limit_price, 6), "remark": f"HC|{secrets.token_hex(10)}"},
        "quote": {"ask1": round(reference_price, 6)},
        "checks": {"notional_cny": round(limit_price * 100, 3)},
        "scope": "HYDRA_TPLUS1_SERVER_CANARY"}
    plan["plan_sha256"] = _sha256(plan)
    _write_exclusive(output, plan)
    return plan


def cancel_canary(
    cfg: LiveClientConfig,
    *,
    plan_path: Path,
    gateway_factory: Callable = XtQMTGateway,
    now: datetime | None = None,
) -> dict:
    plan_path = _require_evidence_path(cfg, plan_path)
    plan = _load_plan(plan_path)
    current = now or _now_shanghai()
    _validate_plan_for_action(cfg, plan, current, require_fresh=False)
    if cfg.trading_enabled:
        raise RuntimeError("撤单前必须先关闭 HYDRA_LIVE_TRADING_ENABLED")
    if os.environ.get(CANCEL_CONFIRM_ENV, "").strip() != plan["plan_sha256"]:
        raise RuntimeError(f"{CANCEL_CONFIRM_ENV} 与 plan_sha256 不一致")
    if not _lock_path(plan_path).exists():
        raise RuntimeError("canary submit lock 不存在，拒绝猜测性撤单")
    gateway = gateway_factory(cfg)
    gateway.connect()
    try:
        matches = gateway.orders_by_remark(plan["order"]["remark"])
        if len(matches) != 1:
            raise RuntimeError(f"QMT canary remark 匹配 {len(matches)} 笔，拒绝撤单")
        order = matches[0]
        _verify_broker_order(order, plan, cfg)
        state = _broker_state(order, gateway.xtconstant)
        if state["terminal"]:
            result = {"cancel_sent": False, "reason": "ALREADY_TERMINAL", **state}
        else:
            cancelable_ids = {item.order_id for item in gateway.cancelable_orders()}
            if order.order_id not in cancelable_ids:
                raise RuntimeError("QMT 未把该 canary 委托列为可撤，拒绝猜测性撤单")
            gateway.cancel_order(order.order_id)
            result = {
                "cancel_sent": True,
                "reason": "QMT_ACCEPTED_CANCEL_INSTRUCTION_NOT_FINAL_STATUS",
                **state,
            }
        _append_event(plan_path, plan["plan_sha256"], {
            "event": "CANCEL_ACTION",
            "recorded_at": current.isoformat(),
            "result": result,
        })
        return {"plan_sha256": plan["plan_sha256"], **result}
    finally:
        gateway.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hydra one-lot real MiniQMT canary (not a server order)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-tplus1", help="offline T+1 canary intent")
    prepare.add_argument("--symbol", required=True, choices=sorted(CANARY_SYMBOLS))
    prepare.add_argument("--execution-date", required=True)
    prepare.add_argument("--reference-price", required=True, type=float)
    prepare.add_argument("--limit-price", required=True, type=float)
    prepare.add_argument("--output", required=True, type=Path)
    plan = sub.add_parser("plan", help="read-only plan; trading switches must be off")
    plan.add_argument("--symbol", required=True, choices=sorted(CANARY_SYMBOLS))
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--iopv", type=float)
    plan.add_argument("--iopv-source")
    for command in ("stage-server", "submit", "status", "cancel"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--plan", required=True, type=Path)
        if command == "stage-server":
            command_parser.add_argument("--execution-date", required=True)
    args = parser.parse_args()
    cfg = LiveClientConfig.from_env()
    if args.command == "prepare-tplus1":
        result = prepare_weekend_plan(cfg, symbol=args.symbol, execution_date=args.execution_date,
                                      reference_price=args.reference_price, limit_price=args.limit_price, output=args.output)
    elif args.command == "plan":
        result = plan_canary(
            cfg,
            symbol=args.symbol,
            output=args.output,
            supplied_iopv=args.iopv,
            supplied_iopv_source=args.iopv_source,
        )
    elif args.command == "stage-server":
        result = stage_server_canary(
            cfg, plan_path=args.plan, execution_date=args.execution_date,
        )
    elif args.command == "submit":
        result = submit_canary(cfg, plan_path=args.plan)
    elif args.command == "status":
        result = status_canary(cfg, plan_path=args.plan)
    else:
        result = cancel_canary(cfg, plan_path=args.plan)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
