"""依赖注入辅助。

职责：
1. 统一再导出数据库会话依赖，供各路由层引用，避免路由直接耦合 ``database`` 模块；
2. 提供 ``require_auth``——访问密码（v1.9）的**路由级**守卫。

为什么是依赖而不是中间件
------------------------
中间件拿不到 ``Depends(get_db)``，只能自己开 ``SessionLocal``，那样在测试里
（内存库 + 依赖覆盖）会读到**另一个数据库**，守卫就形同虚设。
挂在 ``include_router`` 上则与业务路由共用同一个会话，语义一致、可测。
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .database import get_db
from .errors import AppError
from .services import auth_service


def require_auth(request: Request, db: Session = Depends(get_db)) -> None:
    """校验访问密码登录态；未启用密码时直接放行。

    失败抛 ``401 UNAUTHORIZED``，前端据此弹出登录闸门。
    **不**用于 ``/api/health``（Docker HEALTHCHECK）与 ``/api/auth/*``。
    """
    if not auth_service.is_enabled(db):
        return
    token = request.cookies.get(auth_service.COOKIE_NAME)
    if auth_service.verify_token(db, token):
        return
    raise AppError(
        401,
        "UNAUTHORIZED",
        "请输入访问密码",
        {"authenticated": False},
    )


__all__ = ["get_db", "require_auth"]
