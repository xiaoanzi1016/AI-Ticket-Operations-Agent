# -*- coding: utf-8 -*-
"""接口鉴权：Bearer Token（PHASE 2 补上的安全短板）。

【为什么补这个】
这个服务能读到客户姓名、订单号、金额，还能提交/取消任务、翻全量任务列表，
而原实现**完全没有鉴权**：任何能访问到端口的人都能提交工单、把历史任务
一条条拉走，CORS 还默认允许 `*`。对自称"企业级"、主打"审计可追责"的系统，
这是站不住的 —— 审计的前提是先能确定"谁在操作"。

【为什么用「配了才校验」而不是默认全开】
- 本地/离线演示（AGENT_FORCE_MOCK=true）不该被鉴权挡住；
- 但"默认不鉴权"必须留下明显的痕迹：启动时打 WARNING，
  并且 `token_required()` 对外可查询，方便运维自检；
- 生产只要在 .env 里设 `API_AUTH_TOKEN` 即自动开启，无需改代码。

【为什么放在 tasks 路由上做依赖，而不是全局中间件】
`/health` 要给 docker healthcheck / K8s 探针用，`/metrics` 给监控用，
这两个必须免鉴权。按路由挂依赖能精确控制作用域，比在中间件里维护
一串白名单路径更不容易漏。
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.settings import api_settings

_log = logging.getLogger(__name__)

# auto_error=False：让"没带 Token"由我们自己决定要不要拦（未开启鉴权时要放行），
# 而不是让框架直接抛 403。
_scheme = HTTPBearer(auto_error=False, description="Bearer <API_AUTH_TOKEN>")

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def token_required() -> bool:
    """当前是否强制鉴权（取决于有没有配置 API_AUTH_TOKEN）。"""
    return bool((api_settings.api_auth_token or "").strip())


def verify_token(cred: Optional[HTTPAuthorizationCredentials] = Depends(_scheme)) -> None:
    """校验 Bearer Token。未配置 API_AUTH_TOKEN 时直接放行（并已打过 WARNING）。

    技术细节：用 `hmac.compare_digest` 而不是 `==` 比较字符串 ——
    后者在比较过程中会短路，理论上可通过响应时间差逐字节猜出 Token。
    """
    expected = (api_settings.api_auth_token or "").strip()
    if not expected:
        return

    def _reject(code: str, message: str) -> HTTPException:
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail={"code": code, "message": message},
                             headers=_UNAUTHORIZED_HEADERS)

    if cred is None or (cred.scheme or "").lower() != "bearer":
        raise _reject("missing_token", "缺少 Bearer Token")
    if not hmac.compare_digest((cred.credentials or "").strip(), expected):
        raise _reject("invalid_token", "Token 无效")


def warn_if_auth_disabled() -> None:
    """启动时自检：没配 Token 就大声说出来，别让"裸奔"变成默认无声状态。"""
    if not token_required():
        _log.warning(
            "安全：未配置 API_AUTH_TOKEN，任务接口当前**不鉴权**"
            "（任何人都能提交任务并读取全部工单数据）。"
            "生产环境请在 .env 中设置 API_AUTH_TOKEN。"
        )
