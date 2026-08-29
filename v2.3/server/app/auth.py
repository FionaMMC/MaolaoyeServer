"""Bearer token 鉴权 dependency。"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from app.execution import ExecutionDomain
from app.exceptions import APIError, ErrorCode
from app.settings import Settings, get_settings


@dataclass(frozen=True)
class AuthContext:
    """由 token 唯一确定的执行域，业务 API 不接受客户端自行切换。"""

    execution_domain: ExecutionDomain
    client_id: str
    allowed_account_aliases: tuple[str, ...]

    def allows_account(self, account_alias: str | None) -> bool:
        # legacy paper key 保持兼容：空 allowlist 只在 paper 域表示不限账户别名。
        if self.execution_domain == "paper" and not self.allowed_account_aliases:
            return True
        return account_alias is not None and account_alias in self.allowed_account_aliases


def _aliases(csv_value: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in csv_value.split(",") if value.strip())
    if len(values) != len(set(values)):
        raise APIError(ErrorCode.AUTH_FAILED, "account alias allowlist 重复", http_status=401)
    return values


async def verify_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    """检查 Bearer token，并把身份绑定到 paper 或 live 执行域。"""
    paper_key = settings.paper_api_key or settings.api_key
    live_key = settings.live_api_key
    trigger_key = settings.live_trigger_api_key
    backup_key = settings.live_data_backup_api_key
    if not paper_key and not live_key and not trigger_key and not backup_key:
        raise APIError(ErrorCode.AUTH_FAILED, "server API key 未配置", http_status=401)

    if not authorization or not authorization.startswith("Bearer "):
        raise APIError(ErrorCode.AUTH_FAILED, "缺少 Bearer token", http_status=401)

    provided = authorization[len("Bearer "):]
    if trigger_key and hmac.compare_digest(provided, trigger_key):
        if request.url.path != "/hydra/live/trigger":
            raise APIError(ErrorCode.AUTH_FAILED, "live trigger token 无权访问该路由", http_status=403)
        return AuthContext(
            execution_domain="live",
            client_id="live-trigger",
            allowed_account_aliases=(),
        )
    if backup_key and hmac.compare_digest(provided, backup_key):
        if request.url.path != "/live-qmt-backups/market-data":
            raise APIError(ErrorCode.AUTH_FAILED, "backup token 无权访问该路由", http_status=403)
        sources = _aliases(settings.live_data_backup_source_ids_csv)
        if not sources:
            raise APIError(ErrorCode.AUTH_FAILED, "backup source scope 未配置", http_status=401)
        return AuthContext(execution_domain="live", client_id="live-qmt-backup", allowed_account_aliases=sources)
    if paper_key and hmac.compare_digest(provided, paper_key):
        context = AuthContext(
            execution_domain="paper",
            client_id=settings.paper_client_id,
            allowed_account_aliases=_aliases(settings.paper_account_aliases_csv),
        )
        return context
    if live_key and hmac.compare_digest(provided, live_key):
        aliases = _aliases(settings.live_account_aliases_csv)
        if not settings.live_client_id or not aliases:
            raise APIError(
                ErrorCode.AUTH_FAILED,
                "live client_id/account alias scope 未配置",
                http_status=401,
            )
        context = AuthContext(
            execution_domain="live",
            client_id=settings.live_client_id,
            allowed_account_aliases=aliases,
        )
        # live 凭据只允许访问已完成 domain/account 强制校验的最小 API 面。
        # 新增路由默认拒绝，必须在完成隔离审计后显式加入。
        path = request.url.path
        live_exact_paths = {
            "/orders",
            "/trade-result",
            "/cash-flows",
            "/accounts/initialize-from-qmt",
            "/admin/reconcile-positions",
            "/hydra/targets/stage",
            "/hydra/rebalances/retry",
            "/hydra/attempts/close",
            "/hydra/canary/stage",
        }
        if path not in live_exact_paths:
            raise APIError(
                ErrorCode.AUTH_FAILED,
                "live token 无权访问未完成分域审计的路由",
                http_status=403,
            )
        return context
    raise APIError(ErrorCode.AUTH_FAILED, "API key 不匹配", http_status=401)
