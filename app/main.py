"""FastAPI 应用入口。

职责：
1. 创建 FastAPI 实例并注册全部 ``/api`` 路由；
2. 启动时确保数据目录存在、自动建表并执行轻量增量迁移
   （``Base.metadata.create_all`` + ``_run_additive_migrations``）；
3. 注册统一异常处理器（业务异常 / 校验异常 / HTTP 异常 / 兜底）；
4. **最后**挂载 ``StaticFiles`` 到根路径，托管无构建的前端静态资源。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from . import models  # noqa: F401  # 确保 ORM 模型在 create_all 之前被导入注册
from .config import settings
from .database import Base, engine
from .errors import AppError
from .routers import attachments, members, meta, notifications, policies, stats
from .services import notification_service

logger = logging.getLogger(__name__)

# 各 HTTP 状态码到业务错误码的兜底映射
_HTTP_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}

# 轻量增量迁移清单：``表名 -> {列名: DDL 类型}``。
# ``create_all`` 只建缺失的表，不会给既有表补列，因此新增列需在此登记，
# 以便升级既有数据库时无需手工 ALTER。
# 说明：DDL 类型可为完整片段（含约束 / 默认值）。SQLite 的
# ``ALTER TABLE ... ADD COLUMN c INTEGER NOT NULL DEFAULT 1`` 中，
# **常量默认值**是合法的（不允许非常量表达式），故 v1.5 的
# ``count_in_stats`` 用 ``INTEGER NOT NULL DEFAULT 1`` 让存量行默认「计入」。
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "policies": {
        "paid_periods_override": "INTEGER",
        "advisor_name": "VARCHAR(20)",
        "advisor_phone": "VARCHAR(30)",
        "advisor_wechat": "VARCHAR(50)",
        "advisor_email": "VARCHAR(80)",
        "company_phone": "VARCHAR(50)",
        "channel_name": "VARCHAR(30)",
        "channel_phone": "VARCHAR(30)",
    },
    # v1.5：成员「是否计入家庭保费统计」，存量数据默认 1（计入）。
    "members": {
        "count_in_stats": "INTEGER NOT NULL DEFAULT 1",
    },
}


def _ensure_data_dir() -> None:
    """确保 SQLite 数据文件所在目录存在。"""
    directory = os.path.dirname(os.path.abspath(settings.db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)


def _ensure_attachments_dir() -> None:
    """确保电子保单附件落盘目录存在（v1.2）。"""
    os.makedirs(settings.attachments_dir, exist_ok=True)


def _run_additive_migrations() -> None:
    """为既有表补齐新增的可空列（幂等，失败不阻塞启动）。

    仅做 ``ADD COLUMN``，不涉及数据搬迁；列已存在时跳过。
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue  # 由 create_all 按最新模型建表，无需迁移
            present = {col["name"] for col in inspector.get_columns(table)}
            for column, ddl_type in columns.items():
                if column in present:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
                    logger.info("已为表 %s 补充列 %s %s", table, column, ddl_type)
                except Exception:  # noqa: BLE001 - 迁移失败不应阻塞服务启动
                    logger.warning("为表 %s 补充列 %s 失败，已跳过", table, column, exc_info=True)


def _backfill_policy_insureds() -> None:
    """把存量保单的主被保人 ``insured_id`` **幂等回填**进关联表 ``policy_insureds``（v1.3）。

    语义：``INSERT OR IGNORE ... SELECT id, insured_id FROM policies``——
    复合主键冲突自动忽略，重复启动不产生重复行。新表由 ``Base.metadata.create_all``
    自动创建；本函数仅负责数据搬迁。

    **失败仅 WARNING，不阻塞启动**（NAS 真实数据，红线）。
    """
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "policy_insureds" not in tables or "policies" not in tables:
            return  # 表缺失（异常库）→ 安全跳过
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO policy_insureds (policy_id, member_id) "
                    "SELECT id, insured_id FROM policies"
                )
            )
    except Exception:  # noqa: BLE001 - 回填失败不应阻塞服务启动
        logger.warning("回填 policy_insureds 失败，已跳过", exc_info=True)


#: 通知轮询间隔（秒）：每分钟判断一次「是否已到发送时间」，当天最多发一条
_NOTIFY_POLL_SECONDS = 60
#: 启动后延迟首次检查：避开建表 / 迁移窗口，也避免服务刚起就发消息
_NOTIFY_STARTUP_DELAY = 15


async def _notification_loop() -> None:
    """常驻后台任务：到点则发送每日汇总提醒。

    设计约束：

    - 所有业务 / 网络异常都在 ``run_check_safe`` 内部吞掉，**循环本身不退出**；
    - 同步的 DB 与 HTTP 调用经 ``asyncio.to_thread`` 丢到线程池，不阻塞事件循环；
    - 是否真的发送由 ``run_daily_check`` 判定（启用开关 / 发送时间 / 当日去重），
      因此「每分钟无脑轮询」不会造成重复推送。
    """
    await asyncio.sleep(_NOTIFY_STARTUP_DELAY)
    while True:
        try:
            await asyncio.to_thread(notification_service.run_check_safe)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 兜底：任何意外都不允许中断循环
            logger.warning("通知后台任务异常（已忽略）", exc_info=True)
        await asyncio.sleep(_NOTIFY_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    """应用生命周期：启动时准备目录、自动建表、执行增量迁移并启动通知任务。"""
    _ensure_data_dir()
    _ensure_attachments_dir()
    Base.metadata.create_all(bind=engine)
    _run_additive_migrations()
    _backfill_policy_insureds()

    task: asyncio.Task[None] | None = None
    if settings.notification_scheduler:
        task = asyncio.create_task(_notification_loop(), name="notification-daily-check")
        logger.info("通知后台任务已启动（每 %s 秒检查一次）", _NOTIFY_POLL_SECONDS)

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("通知后台任务已停止")


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:  # noqa: ARG001
    """业务异常 → 统一错误格式。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "detail": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(
    request: Request, exc: RequestValidationError  # noqa: ARG001
) -> JSONResponse:
    """Pydantic 请求校验失败 → 422 统一错误格式。"""
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "detail": jsonable_encoder(exc.errors()),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _handle_http_error(
    request: Request, exc: StarletteHTTPException  # noqa: ARG001
) -> JSONResponse:
    """HTTP 异常（如 404/405）→ 统一错误格式。"""
    code = _HTTP_CODE_MAP.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": code, "message": str(exc.detail), "detail": None},
    )


@app.exception_handler(Exception)
async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """兜底异常处理 → 500 统一错误格式。"""
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL_ERROR", "message": "服务器内部错误", "detail": str(exc)},
    )


# ---- 注册 API 路由（顺序不影响功能，但必须早于静态挂载） ----
app.include_router(meta.router)
app.include_router(members.router)
app.include_router(policies.router)
app.include_router(attachments.router)
app.include_router(stats.router)
app.include_router(notifications.router)

# ---- 静态资源挂载（务必放在所有 /api 路由之后，避免吞掉 API 请求） ----
# check_dir=False：目录暂不存在时也不阻塞启动（前端下一批次实现）。
app.mount(
    "/",
    StaticFiles(directory=settings.static_dir, html=True, check_dir=False),
    name="static",
)


__all__ = ["app"]
