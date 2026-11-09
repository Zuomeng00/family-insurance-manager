"""消息通知路由（v1.8）。

- ``GET  /api/notifications/settings``  读取通知配置（Webhook 已脱敏）；
- ``PUT  /api/notifications/settings``  部分更新配置（只写传了的键）；
- ``POST /api/notifications/test``      立即发送一条测试消息。

约定
----
1. 配置存入 ``app_settings`` 键值表，**缺失即取默认值**，存量库升级无需迁移；
2. 真实 Webhook key **永不出网**：``GET`` / ``PUT`` 都只回传 ``key=****末4位``；
3. 「测试发送」不受启用开关与发送时间去重限制，用于配置后自检。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import NotifySettingsIn, NotifySettingsOut, NotifyTestOut
from ..services import notification_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _current(db: Session) -> NotifySettingsOut:
    """读取配置并转为响应体（含脱敏 Webhook 与当天日期）。"""
    return NotifySettingsOut(**notification_service.get_settings(db).as_dict())


@router.get("/settings", summary="读取通知配置")
def read_settings(db: Session = Depends(get_db)) -> NotifySettingsOut:
    """返回当前通知配置；Webhook 以 ``key=****abcd`` 形式脱敏。"""
    return _current(db)


@router.put("/settings", summary="更新通知配置（部分更新）")
def save_settings(
    payload: NotifySettingsIn,
    db: Session = Depends(get_db),
) -> NotifySettingsOut:
    """只更新请求体中显式给出的字段。

    - 未传的字段保持原值；
    - ``webhook`` 传空串 = 清除地址（同时自动关闭启用开关）；
    - 启用通知时若仍无有效 Webhook → 422，避免「开了但发不出」。
    """
    data: dict[str, Any] = payload.model_dump(exclude_unset=True)
    notification_service.update_settings(db, data)
    return _current(db)


@router.post("/test", summary="立即发送一条测试消息")
def test_notification(db: Session = Depends(get_db)) -> NotifyTestOut:
    """立即向已配置的企业微信机器人发送一条消息。

    不受「启用开关 / 发送时间 / 每日去重」限制：配好 Webhook 就能点一下试试。
    发送失败会返回 502 ``NOTIFY_FAILED``（如 key 失效、机器人被移出群）。
    """
    return NotifyTestOut(**notification_service.send_test(db))


__all__ = ["router"]
