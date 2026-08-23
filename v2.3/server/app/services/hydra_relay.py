"""把经验证的 Hydra 月度目标转成可审计订单和 residual attempts。"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select

from app.exceptions import APIError, ErrorCode
from app.models import (
    HydraExecutionAttempt,
    HydraRebalance,
    HydraTarget,
    InstanceState,
    Order,
    OrderSignalMap,
    RawSignal,
)
from app.schemas.hydra_relay import (
    HydraAttemptCloseRequest,
    HydraAttemptCloseResponseData,
    HydraRelayResponseData,
    HydraRetryRequest,
    HydraTargetRequest,
)
from app.services.hydra_data import HydraDataStore
from app.services.blacklist import BlacklistService


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _hash(prefix: str, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{prefix}_{hashlib.sha256(body).hexdigest()}"


def _tick_price(reference: float, direction: str, offset_bps: float) -> float:
    raw = reference * (
        1 + offset_bps / 10_000 if direction == "BUY" else 1 - offset_bps / 10_000
    )
    ticks = raw / 0.001
    rounded_ticks = math.floor(ticks + 1e-9) if direction == "BUY" else math.ceil(ticks - 1e-9)
    return round(rounded_ticks * 0.001, 3)


@dataclass(frozen=True)
class HydraRiskLimits:
    max_daily_orders: int
    max_single_order_notional: float
    max_daily_buy_notional: float
    max_daily_sell_notional: float
    max_daily_turnover_notional: float
    max_price_offset_bps: float

    def validate_live_ready(self) -> None:
        values = (
            self.max_daily_orders,
            self.max_single_order_notional,
            self.max_daily_buy_notional,
            self.max_daily_sell_notional,
            self.max_daily_turnover_notional,
            self.max_price_offset_bps,
        )
        if any(value <= 0 for value in values):
            raise APIError(
                ErrorCode.STRATEGY_PENDING,
                "Hydra live 风控限额未完整配置",
                http_status=423,
            )
        if self.max_price_offset_bps > 50:
            raise APIError(ErrorCode.BAD_REQUEST, "价格偏移上限不能超过 50bps")


class HydraRelayService:
    def __init__(
        self,
        session_factory,
        data_store: HydraDataStore,
        *,
        allowed_symbols: set[str],
        allowed_publisher_commits: set[str],
        live_enabled: bool,
        live_limits: HydraRiskLimits,
        blacklist_service: BlacklistService | None = None,
        lot_size: int = 100,
    ):
        self.session_factory = session_factory
        self.data_store = data_store
        self.allowed_symbols = allowed_symbols
        self.allowed_publisher_commits = allowed_publisher_commits
        self.live_enabled = live_enabled
        self.live_limits = live_limits
        self.blacklist_service = blacklist_service
        self.lot_size = lot_size

    def stage_initial(self, req: HydraTargetRequest) -> HydraRelayResponseData:
        self._gate_request(req)
        model, model_manifest = self.data_store.load(
            "hydra_model_hfq", req.input_hashes["model_hfq"]
        )
        raw, raw_manifest = self.data_store.load(
            "hydra_execution_raw", req.input_hashes["execution_raw"]
        )
        _, actions_manifest = self.data_store.load(
            "hydra_corporate_actions", req.input_hashes["corporate_actions"]
        )
        calendar, calendar_manifest = self.data_store.load(
            "hydra_trading_calendar", req.input_hashes["trading_calendar"]
        )
        for manifest in (
            model_manifest, raw_manifest, actions_manifest, calendar_manifest,
        ):
            if manifest.as_of_date != req.as_of_date:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    f"{manifest.stream} as_of_date 与 target 不一致",
                )
        calendar_dates = sorted(
            calendar["trade_date"].astype(str).str.replace("-", "", regex=False)
        )
        future_dates = [date for date in calendar_dates if date > req.decision_date]
        if req.decision_date not in set(calendar_dates):
            raise APIError(ErrorCode.BAD_REQUEST, "decision_date 不是交易日")
        if not future_dates or future_dates[0] != req.execution_date:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                "execution_date 不是 decision_date 后第一个交易日",
            )
        codes = {item.code for item in req.weights}
        self._require_as_of_coverage(model, req.as_of_date, codes, "model_hfq")
        raw_as_of = self._require_as_of_coverage(
            raw, req.as_of_date, codes, "execution_raw",
        )
        if (raw_as_of["suspendFlag"].astype(int) != 0).any():
            blocked = sorted(raw_as_of.loc[
                raw_as_of["suspendFlag"].astype(int) != 0, "symbol"
            ].tolist())
            raise APIError(ErrorCode.BAD_REQUEST, f"执行原价标记停牌: {blocked}")
        prices = raw_as_of.set_index("symbol")["close"].astype(float).to_dict()

        with self.session_factory() as session:
            existing_target = session.execute(
                select(HydraTarget).where(
                    HydraTarget.execution_domain == req.execution_domain,
                    HydraTarget.account_alias == req.account_alias,
                    HydraTarget.basket_sha256 == req.basket_sha256,
                )
            ).scalar_one_or_none()
            if existing_target is not None:
                attempt = session.execute(
                    select(HydraExecutionAttempt)
                    .join(
                        HydraRebalance,
                        HydraRebalance.rebalance_id == HydraExecutionAttempt.rebalance_id,
                    )
                    .where(HydraRebalance.target_id == existing_target.target_id)
                    .order_by(HydraExecutionAttempt.attempt_number)
                    .limit(1)
                ).scalar_one()
                return self._response(existing_target, attempt, idempotent=True)

            conflicting = session.execute(
                select(HydraTarget.target_id).where(
                    HydraTarget.execution_domain == req.execution_domain,
                    HydraTarget.account_alias == req.account_alias,
                    func.substr(HydraTarget.decision_date, 1, 6) == req.decision_date[:6],
                    HydraTarget.status.in_(("STAGED", "ACTIVE", "COMPLETED")),
                ).limit(1)
            ).first()
            if conflicting is not None:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    "同一月份已有不同 Hydra target，拒绝静默替换",
                    http_status=409,
                )
            state = self._validated_state(
                session, req.instance_id, req.execution_domain, req.account_alias,
            )
            self._assert_no_unresolved(session, req.instance_id, req.execution_domain)
            positions = self._validate_positions(dict(state.virtual_positions or {}))
            unexpected_positions = sorted(set(positions) - self.allowed_symbols)
            if unexpected_positions:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    f"专用 Hydra 账户含白名单外持仓: {unexpected_positions}",
                )
            cash = float(state.virtual_cash)
            all_codes = codes | set(positions)
            raw_as_of_all = self._require_as_of_coverage(
                raw, req.as_of_date, all_codes, "execution_raw",
            )
            if (raw_as_of_all["suspendFlag"].astype(int) != 0).any():
                blocked = sorted(raw_as_of_all.loc[
                    raw_as_of_all["suspendFlag"].astype(int) != 0, "symbol"
                ].tolist())
                raise APIError(ErrorCode.BAD_REQUEST, f"执行原价标记停牌: {blocked}")
            prices = raw_as_of_all.set_index("symbol")["close"].astype(float).to_dict()
            nav = cash + sum(qty * prices[code] for code, qty in positions.items())
            if not math.isfinite(nav) or nav <= 0:
                raise APIError(ErrorCode.BAD_REQUEST, "Hydra 调仓前 NAV 非法")
            investable = nav * (1 - req.cash_buffer_weight)
            target_shares = {
                item.code: int(
                    math.floor(investable * item.weight / prices[item.code] / self.lot_size)
                    * self.lot_size
                )
                for item in req.weights
            }
            for code in set(positions) - codes:
                target_shares[code] = 0

            target_id = _hash("ht", {"basket_sha256": req.basket_sha256})
            rebalance_id = _hash("hr", {
                "target_id": target_id,
                "cash": cash,
                "positions": positions,
                "target_shares": target_shares,
            })
            now = _now_iso()
            target = HydraTarget(
                target_id=target_id,
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                strategy_version=req.strategy_version,
                publisher_source_commit=req.publisher_source_commit,
                decision_date=req.decision_date,
                as_of_date=req.as_of_date,
                execution_date=req.execution_date,
                basket_sha256=req.basket_sha256,
                research_input_hashes=req.research_input_hashes,
                input_hashes=req.input_hashes,
                weights={item.code: item.weight for item in req.weights},
                cash_buffer_weight=req.cash_buffer_weight,
                status="STAGED",
                created_at=now,
            )
            rebalance = HydraRebalance(
                rebalance_id=rebalance_id,
                target_id=target_id,
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                baseline_cash=cash,
                baseline_positions=positions,
                target_shares=target_shares,
                status="OPEN",
                reconciliation_status="PRE_TRADE_OK",
                created_at=now,
            )
            session.add_all([target, rebalance])
            response = self._create_attempt(
                session=session,
                target=target,
                rebalance=rebalance,
                instance_id=req.instance_id,
                trade_date=req.execution_date,
                actual_cash=cash,
                actual_positions=positions,
                prices=prices,
                buy_offset=req.buy_price_offset_bps,
                sell_offset=req.sell_price_offset_bps,
                reconciliation_evidence_sha256=(
                    dict(state.strategy_state or {}).get(
                        "initialization_evidence_sha256"
                    )
                    or dict(state.strategy_state or {}).get(
                        "last_reconciliation_evidence_sha256"
                    )
                    or req.basket_sha256
                ),
            )
            session.commit()
            return response

    def stage_retry(self, req: HydraRetryRequest) -> HydraRelayResponseData:
        if req.execution_domain == "live":
            self._gate_live()
        raw, manifest = self.data_store.load(
            "hydra_execution_raw", req.execution_raw_sha256,
        )
        if manifest.as_of_date >= req.trade_date:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                "retry execution raw 必须来自 trade_date 之前的已冻结数据",
            )
        with self.session_factory() as session:
            rebalance = session.get(HydraRebalance, req.rebalance_id)
            if (
                rebalance is None
                or rebalance.execution_domain != req.execution_domain
                or rebalance.account_alias != req.account_alias
            ):
                raise APIError(ErrorCode.BAD_REQUEST, "rebalance 不存在或跨域", http_status=404)
            target = session.get(HydraTarget, rebalance.target_id)
            self._assert_rebalance_has_no_unresolved(session, rebalance.rebalance_id)
            previous = session.execute(
                select(HydraExecutionAttempt)
                .where(HydraExecutionAttempt.rebalance_id == rebalance.rebalance_id)
                .order_by(HydraExecutionAttempt.attempt_number.desc())
                .limit(1)
            ).scalar_one_or_none()
            if previous is None or previous.status != "RESIDUAL":
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    "上一 attempt 尚未完成 post-trade 对账或没有 residual",
                    http_status=409,
                )
            positions = self._validate_positions(req.actual_positions)
            unexpected_positions = sorted(set(positions) - self.allowed_symbols)
            if unexpected_positions:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    f"专用 Hydra 账户含白名单外持仓: {unexpected_positions}",
                )
            codes = set(rebalance.target_shares) | set(positions)
            price_rows = self._latest_before(raw, manifest.as_of_date, codes)
            prices = price_rows.set_index("symbol")["close"].astype(float).to_dict()
            instance_id = self._instance_id_for_rebalance(session, rebalance.rebalance_id)
            response = self._create_attempt(
                session=session,
                target=target,
                rebalance=rebalance,
                instance_id=instance_id,
                trade_date=req.trade_date,
                actual_cash=req.actual_cash,
                actual_positions=positions,
                prices=prices,
                buy_offset=50.0,
                sell_offset=50.0,
                reconciliation_evidence_sha256=req.reconciliation_evidence_sha256,
            )
            rebalance.reconciliation_status = "RETRY_PRE_TRADE_OK"
            session.commit()
            return response

    def close_attempt(
        self, req: HydraAttemptCloseRequest,
    ) -> HydraAttemptCloseResponseData:
        if req.execution_domain == "live":
            self._gate_live()
        with self.session_factory() as session:
            attempt = session.get(HydraExecutionAttempt, req.attempt_id)
            if (
                attempt is None
                or attempt.execution_domain != req.execution_domain
                or attempt.account_alias != req.account_alias
            ):
                raise APIError(ErrorCode.BAD_REQUEST, "attempt 不存在或跨域", http_status=404)
            if attempt.status in {"COMPLETE", "RESIDUAL"}:
                if attempt.posttrade_reconciliation_sha256 != req.reconciliation_evidence_sha256:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        "attempt 已结案且 reconciliation evidence 不同",
                        http_status=409,
                    )
                rebalance = session.get(HydraRebalance, attempt.rebalance_id)
                return HydraAttemptCloseResponseData(
                    target_id=rebalance.target_id,
                    rebalance_id=rebalance.rebalance_id,
                    attempt_id=attempt.attempt_id,
                    execution_domain=req.execution_domain,
                    status=attempt.status,
                    residual_after=dict(attempt.residual_after or {}),
                )
            self._assert_rebalance_has_no_unresolved(session, attempt.rebalance_id)
            rebalance = session.get(HydraRebalance, attempt.rebalance_id)
            target = session.get(HydraTarget, rebalance.target_id)
            positions = self._validate_positions(req.actual_positions)
            unexpected = sorted(set(positions) - self.allowed_symbols)
            if unexpected:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    f"专用 Hydra 账户含白名单外持仓: {unexpected}",
                )
            instance_id = self._instance_id_for_rebalance(session, rebalance.rebalance_id)
            state = self._validated_state(
                session, instance_id, req.execution_domain, req.account_alias,
            )
            if dict(state.virtual_positions or {}) != {
                code: qty for code, qty in positions.items() if qty > 0
            }:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    "post-trade QMT 持仓与虚拟账本不一致",
                    http_status=409,
                )
            if abs(float(state.virtual_cash) - req.actual_cash) > 1.0:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    "post-trade QMT 现金与虚拟账本不一致；先核对费用/分红/入出金",
                    http_status=409,
                )
            residual = {
                code: int(target_qty) - int(positions.get(code, 0))
                for code, target_qty in rebalance.target_shares.items()
                if int(target_qty) != int(positions.get(code, 0))
            }
            status = "RESIDUAL" if residual else "COMPLETE"
            now = _now_iso()
            attempt.residual_after = residual
            attempt.posttrade_reconciliation_sha256 = req.reconciliation_evidence_sha256
            attempt.reconciled_cash = req.actual_cash
            attempt.reconciled_positions = positions
            attempt.status = status
            attempt.closed_at = now
            rebalance.reconciliation_status = (
                "POST_TRADE_RESIDUAL" if residual else "POST_TRADE_OK"
            )
            if not residual:
                rebalance.status = "COMPLETED"
                rebalance.closed_at = now
                target.status = "COMPLETED"
            session.commit()
            return HydraAttemptCloseResponseData(
                target_id=target.target_id,
                rebalance_id=rebalance.rebalance_id,
                attempt_id=attempt.attempt_id,
                execution_domain=req.execution_domain,
                status=status,
                residual_after=residual,
            )

    def _create_attempt(
        self, *, session, target: HydraTarget, rebalance: HydraRebalance,
        instance_id: str, trade_date: str, actual_cash: float,
        actual_positions: dict[str, int], prices: dict[str, float],
        buy_offset: float, sell_offset: float,
        reconciliation_evidence_sha256: str,
    ) -> HydraRelayResponseData:
        prior_count = session.execute(
            select(HydraExecutionAttempt).where(
                HydraExecutionAttempt.rebalance_id == rebalance.rebalance_id
            )
        ).scalars().all()
        attempt_number = len(prior_count) + 1
        residual = {
            code: int(target_qty) - int(actual_positions.get(code, 0))
            for code, target_qty in rebalance.target_shares.items()
            if int(target_qty) != int(actual_positions.get(code, 0))
        }
        canonical_orders = []
        for code, delta in sorted(residual.items()):
            if code not in prices:
                raise APIError(ErrorCode.BAD_REQUEST, f"缺少 residual 原始价格: {code}")
            direction = "BUY" if delta > 0 else "SELL"
            offset = buy_offset if direction == "BUY" else sell_offset
            canonical_orders.append({
                "symbol": code,
                "direction": direction,
                "quantity": abs(delta),
                "reference_price": round(float(prices[code]), 6),
                "limit_price": _tick_price(float(prices[code]), direction, offset),
            })
        batch_payload = {
            "rebalance_id": rebalance.rebalance_id,
            "attempt_number": attempt_number,
            "trade_date": trade_date,
            "orders": canonical_orders,
        }
        batch_sha = hashlib.sha256(
            json.dumps(batch_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        batch_id = f"hb_{batch_sha}"
        attempt_id = _hash("ha", {
            "rebalance_id": rebalance.rebalance_id,
            "attempt_number": attempt_number,
            "trade_date": trade_date,
            "batch_sha256": batch_sha,
        })
        self._apply_risk_limits(
            target.execution_domain, canonical_orders, buy_offset, sell_offset,
        )
        buy_notional = sum(
            order["quantity"] * order["limit_price"]
            for order in canonical_orders if order["direction"] == "BUY"
        )
        sell_notional = sum(
            order["quantity"] * order["limit_price"]
            for order in canonical_orders if order["direction"] == "SELL"
        )
        # Server 侧保守资金闸门；client 仍必须用 QMT 实时可用资金二次检查。
        if buy_notional > float(actual_cash) + sell_notional * 0.999:
            raise APIError(ErrorCode.BAD_REQUEST, "Hydra 订单批次预计资金不足")
        now = _now_iso()
        attempt = HydraExecutionAttempt(
            attempt_id=attempt_id,
            rebalance_id=rebalance.rebalance_id,
            execution_domain=target.execution_domain,
            account_alias=target.account_alias,
            attempt_number=attempt_number,
            trade_date=trade_date,
            residual_before=residual,
            pretrade_reconciliation_sha256=reconciliation_evidence_sha256,
            batch_id=batch_id,
            batch_sha256=batch_sha,
            status="PENDING" if canonical_orders else "NOOP",
            created_at=now,
        )
        session.add(attempt)
        for index, item in enumerate(canonical_orders):
            order_id = _hash("ho", {"batch_sha256": batch_sha, "index": index})
            signal_id = uuid.uuid5(uuid.NAMESPACE_URL, order_id).hex
            session.add(RawSignal(
                signal_id=signal_id,
                execution_domain=target.execution_domain,
                instance_id=instance_id,
                symbol=item["symbol"],
                direction=item["direction"],
                quantity=item["quantity"],
                reference_price=item["reference_price"],
                price_offset=(
                    buy_offset / 10_000
                    if item["direction"] == "BUY"
                    else -sell_offset / 10_000
                ),
                limit_price=item["limit_price"],
                valid_date=trade_date,
                signal_time=now,
                precheck_status="PASS",
                precheck_reason="hydra_relay_validated",
            ))
            session.add(Order(
                order_id=order_id,
                execution_domain=target.execution_domain,
                qmt_account_alias=target.account_alias,
                target_id=target.target_id,
                rebalance_id=rebalance.rebalance_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                batch_id=batch_id,
                batch_sha256=batch_sha,
                target_hash=target.basket_sha256,
                execution_reference_price=item["reference_price"],
                account_group=target.account_alias,
                symbol=item["symbol"],
                direction=item["direction"],
                quantity=item["quantity"],
                limit_price=item["limit_price"],
                valid_date=trade_date,
                status="PENDING",
                created_at=now,
            ))
            session.add(OrderSignalMap(
                order_id=order_id,
                signal_id=signal_id,
                signal_quantity=item["quantity"],
            ))
        return self._response(target, attempt, idempotent=False)

    def _gate_request(self, req: HydraTargetRequest) -> None:
        unexpected = sorted({item.code for item in req.weights} - self.allowed_symbols)
        if unexpected:
            raise APIError(ErrorCode.BAD_REQUEST, f"Hydra target 超出 ETF 白名单: {unexpected}")
        if req.publisher_source_commit not in self.allowed_publisher_commits:
            raise APIError(ErrorCode.BAD_REQUEST, "Hydra publisher commit 未获批准")
        if self.blacklist_service is not None:
            blocked = sorted(
                {item.code for item in req.weights}
                & self.blacklist_service.compute(
                    execution_domain=req.execution_domain,
                )
            )
            if blocked:
                raise APIError(ErrorCode.BAD_REQUEST, f"Hydra target 命中风险黑名单: {blocked}")
        if req.execution_domain == "live":
            self._gate_live()
            if (
                req.buy_price_offset_bps > self.live_limits.max_price_offset_bps
                or req.sell_price_offset_bps > self.live_limits.max_price_offset_bps
            ):
                raise APIError(ErrorCode.BAD_REQUEST, "target 价格偏移超过 live 上限")

    def _gate_live(self) -> None:
        if not self.live_enabled:
            raise APIError(
                ErrorCode.STRATEGY_PENDING,
                "Hydra live 订单生成闸门关闭",
                http_status=423,
            )
        self.live_limits.validate_live_ready()

    @staticmethod
    def _require_as_of_coverage(
        frame: pd.DataFrame, as_of_date: str, codes: set[str], label: str,
    ) -> pd.DataFrame:
        dates = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
        rows = frame.loc[dates == as_of_date].copy()
        missing = sorted(codes - set(rows["symbol"]))
        if missing:
            raise APIError(ErrorCode.BAD_REQUEST, f"{label} 缺 as_of 标的: {missing}")
        return rows.loc[rows["symbol"].isin(codes)]

    @staticmethod
    def _latest_before(
        frame: pd.DataFrame, as_of_date: str, codes: set[str],
    ) -> pd.DataFrame:
        data = frame.copy()
        data["_date"] = data["trade_date"].astype(str).str.replace("-", "", regex=False)
        data = data[(data["_date"] <= as_of_date) & data["symbol"].isin(codes)]
        rows = data.sort_values("_date").groupby("symbol", as_index=False).tail(1)
        missing = sorted(codes - set(rows["symbol"]))
        if missing:
            raise APIError(ErrorCode.BAD_REQUEST, f"retry raw 缺标的: {missing}")
        if (rows["suspendFlag"].astype(int) != 0).any():
            raise APIError(ErrorCode.BAD_REQUEST, "retry raw 包含停牌标的")
        return rows

    @staticmethod
    def _validated_state(
        session, instance_id: str, domain: str, account_alias: str | None = None,
    ) -> InstanceState:
        state = session.get(InstanceState, instance_id)
        if state is None:
            raise APIError(ErrorCode.BAD_REQUEST, "Hydra instance 尚未只读初始化", http_status=409)
        if state.execution_domain != domain:
            raise APIError(ErrorCode.AUTH_FAILED, "Hydra instance 跨 execution_domain", http_status=403)
        if account_alias is not None and state.account_alias != account_alias:
            raise APIError(ErrorCode.AUTH_FAILED, "Hydra instance 跨 account_alias", http_status=403)
        status = dict(state.strategy_state or {}).get("reconciliation_status")
        if status not in {"ok", "reconciled"}:
            raise APIError(ErrorCode.BAD_REQUEST, "Hydra 调仓前未完成 QMT 对账", http_status=409)
        return state

    @staticmethod
    def _validate_positions(positions: dict[str, int]) -> dict[str, int]:
        result = {}
        for code, qty in positions.items():
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
                raise APIError(ErrorCode.BAD_REQUEST, f"非法实际持仓 {code}={qty}")
            result[str(code)] = qty
        return result

    @staticmethod
    def _assert_no_unresolved(session, instance_id: str, domain: str) -> None:
        unresolved = session.execute(
            select(Order.order_id)
            .join(OrderSignalMap, OrderSignalMap.order_id == Order.order_id)
            .join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id)
            .where(RawSignal.instance_id == instance_id)
            .where(Order.execution_domain == domain)
            .where(Order.status.in_(("PENDING", "PARTIAL")))
            .limit(1)
        ).first()
        if unresolved:
            raise APIError(ErrorCode.BAD_REQUEST, "存在未决订单，禁止新月调仓", http_status=409)

    @staticmethod
    def _assert_rebalance_has_no_unresolved(session, rebalance_id: str) -> None:
        unresolved = session.execute(
            select(Order.order_id)
            .where(Order.rebalance_id == rebalance_id)
            .where(Order.status.in_(("PENDING", "PARTIAL")))
            .limit(1)
        ).first()
        if unresolved:
            raise APIError(ErrorCode.BAD_REQUEST, "前一 attempt 尚有未决订单", http_status=409)

    @staticmethod
    def _instance_id_for_rebalance(session, rebalance_id: str) -> str:
        instance_id = session.execute(
            select(RawSignal.instance_id)
            .join(OrderSignalMap, RawSignal.signal_id == OrderSignalMap.signal_id)
            .join(Order, Order.order_id == OrderSignalMap.order_id)
            .where(Order.rebalance_id == rebalance_id)
            .limit(1)
        ).scalar_one_or_none()
        if instance_id is None:
            raise APIError(ErrorCode.BAD_REQUEST, "rebalance 无 instance 血缘")
        return instance_id

    def _apply_risk_limits(
        self, domain: str, orders: list[dict], buy_offset: float, sell_offset: float,
    ) -> None:
        if domain != "live":
            return
        limits = self.live_limits
        if len(orders) > limits.max_daily_orders:
            raise APIError(ErrorCode.BAD_REQUEST, "订单数超过 live 日上限")
        buy = sell = 0.0
        for order in orders:
            notional = order["quantity"] * order["limit_price"]
            if notional > limits.max_single_order_notional:
                raise APIError(ErrorCode.BAD_REQUEST, "单笔金额超过 live 上限")
            if order["direction"] == "BUY":
                buy += notional
            else:
                sell += notional
        if buy > limits.max_daily_buy_notional:
            raise APIError(ErrorCode.BAD_REQUEST, "买入金额超过 live 日上限")
        if sell > limits.max_daily_sell_notional:
            raise APIError(ErrorCode.BAD_REQUEST, "卖出金额超过 live 日上限")
        if buy + sell > limits.max_daily_turnover_notional:
            raise APIError(ErrorCode.BAD_REQUEST, "总成交额超过 live 日上限")
        if max(buy_offset, sell_offset) > limits.max_price_offset_bps:
            raise APIError(ErrorCode.BAD_REQUEST, "订单价格偏移超过 live 上限")

    @staticmethod
    def _response(
        target: HydraTarget, attempt: HydraExecutionAttempt, *, idempotent: bool,
    ) -> HydraRelayResponseData:
        return HydraRelayResponseData(
            target_id=target.target_id,
            rebalance_id=attempt.rebalance_id,
            attempt_id=attempt.attempt_id,
            batch_id=attempt.batch_id,
            batch_sha256=attempt.batch_sha256,
            execution_domain=target.execution_domain,
            trade_date=attempt.trade_date,
            order_count=len(attempt.residual_before),
            idempotent_replay=idempotent,
        )
