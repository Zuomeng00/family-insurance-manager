"""访问密码路由（v1.9）。

- ``GET  /api/auth/status``    读取状态（**公开**，登录页自己要先能取状态）；
- ``POST /api/auth/login``     校验密码并写入登录 Cookie（**公开**）；
- ``POST /api/auth/logout``    清除登录 Cookie（**公开**，幂等）；
- ``PUT  /api/auth/password``  设置 / 修改密码（需已登录）；
- ``PUT  /api/auth/enabled``   开启 / 关闭访问限制（需已登录）。

约定
----
1. 登录态放 **HttpOnly Cookie**，前端 JS 读不到 —— 由浏览器自动携带，
   附件预览 / 下载这类 ``<a href>`` 跳转也能通过鉴权；
2. 未启用密码时 ``PUT`` 两个接口等价于「初始化」：先设密码、再开开关；
3. 登录失败限速按来源 IP 计，5 分钟内错 5 次返回 ``429``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..deps import get_db, require_auth
from ..errors import AppError
from ..schemas import AuthEnabledIn, AuthLoginIn, AuthPasswordIn, AuthStatusOut
from ..services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_key(request: Request) -> str:
    """限速用的来源标识（单实例家用部署，取 IP 足够）。"""
    client = request.client
    return client.host if client and client.host else "unknown"


def _set_cookie(response: Response, token: str) -> None:
    """写入 HttpOnly 登录 Cookie。

    **不设 ``secure``**：家用 NAS 多是 ``http://`` 直连，加了会导致 Cookie
    根本不下发（只在 ``https`` 下发送），表现为「密码对但一直要重新登录」。
    """
    response.set_cookie(
        key=auth_service.COOKIE_NAME,
        value=token,
        max_age=auth_service.session_days() * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _current(db: Session, request: Request) -> AuthStatusOut:
    token = request.cookies.get(auth_service.COOKIE_NAME)
    return AuthStatusOut(**auth_service.status(db, token))


def _authenticated(db: Session, token: str) -> AuthStatusOut:
    """用**刚签发**的 token 组装状态。

    不能复用 ``_current``：请求里的 Cookie 还是旧的（本次响应才写入新值），
    直接读会得到 ``authenticated=false``，前端据此又弹一次登录页。
    """
    return AuthStatusOut(**auth_service.status(db, token))


@router.get("/status", summary="读取访问密码状态")
def read_status(request: Request, db: Session = Depends(get_db)) -> AuthStatusOut:
    """返回是否启用、是否已设密码、当前会话是否已登录。"""
    return _current(db, request)


@router.post("/login", summary="用访问密码登录")
def login(
    payload: AuthLoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthStatusOut:
    """校验密码，成功后写入登录 Cookie（有效期见 ``sessionDays``）。"""
    if not auth_service.is_enabled(db):
        raise AppError(
            400,
            "AUTH_NOT_ENABLED",
            "访问密码未启用，无需登录",
            {"authenticated": True},
        )

    key = _client_key(request)
    auth_service.ensure_not_locked(key)
    if not auth_service.verify_password(db, payload.password):
        auth_service.record_failure(key)
        raise AppError(401, "INVALID_PASSWORD", "密码不正确", {"authenticated": False})

    auth_service.clear_failures(key)
    token = auth_service.issue_token(db)
    _set_cookie(response, token)
    return _authenticated(db, token)


@router.post("/logout", summary="退出登录（清除登录状态）")
def logout(response: Response) -> dict[str, bool]:
    """清除登录 Cookie。未登录时调用也返回成功（幂等）。"""
    response.delete_cookie(auth_service.COOKIE_NAME, path="/")
    return {"ok": True}


@router.put(
    "/password",
    summary="设置 / 修改访问密码",
    dependencies=[Depends(require_auth)],
)
def update_password(
    payload: AuthPasswordIn,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthStatusOut:
    """设置或修改密码。

    - 已启用访问限制：必须提供 ``currentPassword``；
    - 设置成功后**顺带签发登录态**——否则用户刚设完密码就被自己挡在门外。
    """
    auth_service.set_password(db, payload.new_password, payload.current_password)
    token = auth_service.issue_token(db)
    _set_cookie(response, token)
    return _authenticated(db, token)


@router.put(
    "/enabled",
    summary="开启 / 关闭访问限制",
    dependencies=[Depends(require_auth)],
)
def update_enabled(
    payload: AuthEnabledIn,
    request: Request,
    db: Session = Depends(get_db),
) -> AuthStatusOut:
    """开启前必须已设置密码；关闭后已保存的密码仍保留（重新开启无需重设）。"""
    auth_service.set_enabled(db, payload.enabled)
    return _current(db, request)


__all__ = ["router"]
