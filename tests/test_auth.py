# -*- coding: utf-8 -*-
"""鉴权单元测试（src/api/auth.py）。

为什么单独一个文件：接口测试（test_api.py）用的是模块级单例 client，
不方便反复切换"有没有配 Token"两种状态；这里直接测依赖函数更干净，
也能覆盖"没配 Token 时要放行"这个分支。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.api.auth import token_required, verify_token
from src.api.settings import api_settings


def _cred(token: str, scheme: str = "Bearer") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


def test_disabled_when_no_token_configured(monkeypatch):
    """没配 API_AUTH_TOKEN：放行（本地演示不该被挡住）。"""
    monkeypatch.setattr(api_settings, "api_auth_token", "")
    assert token_required() is False
    # 不带 Token、带错 Token 都放行
    assert verify_token(None) is None
    assert verify_token(_cred("whatever")) is None


def test_correct_token_passes(monkeypatch):
    monkeypatch.setattr(api_settings, "api_auth_token", "s3cret")
    assert token_required() is True
    assert verify_token(_cred("s3cret")) is None


def test_missing_token_rejected(monkeypatch):
    monkeypatch.setattr(api_settings, "api_auth_token", "s3cret")
    with pytest.raises(HTTPException) as exc:
        verify_token(None)
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "missing_token"


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setattr(api_settings, "api_auth_token", "s3cret")
    with pytest.raises(HTTPException) as exc:
        verify_token(_cred("s3cret-but-typo"))
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "invalid_token"


def test_non_bearer_scheme_rejected(monkeypatch):
    monkeypatch.setattr(api_settings, "api_auth_token", "s3cret")
    with pytest.raises(HTTPException) as exc:
        verify_token(_cred("s3cret", scheme="Basic"))
    assert exc.value.status_code == 401


def test_error_shape_matches_global_handler(monkeypatch):
    """401 的响应体必须是统一的 {"error": {"code", "message"}} 形状。

    全局异常处理器只对 dict 形态的 detail 保留 code，
    这里守住这个约定，前端的错误处理才不用写两套。
    """
    monkeypatch.setattr(api_settings, "api_auth_token", "s3cret")
    with pytest.raises(HTTPException) as exc:
        verify_token(None)
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert set(detail) == {"code", "message"}
