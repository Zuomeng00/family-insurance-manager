"""应用配置层。

从环境变量读取运行期配置（``DB_PATH`` / ``PORT``），并提供静态资源目录与
应用版本号等常量。使用 ``pydantic-settings`` 统一管理，便于本地与 Docker
两种运行方式共用同一套代码。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__


class Settings(BaseSettings):
    """应用运行配置。

    字段与环境变量的映射关系（大小写不敏感）：

    - ``db_path``     ← ``DB_PATH``    （默认 ``./data/insurance.db``）
    - ``port``        ← ``PORT``       （默认 ``8000``）
    - ``static_dir``  ← ``STATIC_DIR`` （默认 ``static``）
    - ``attachments_dir``             ← ``ATTACHMENTS_DIR``              （默认 ``./data/attachments``）
    - ``max_attachment_size``         ← ``MAX_ATTACHMENT_SIZE``          （默认 ``20971520``，即 20MB）
    - ``max_attachments_per_policy``  ← ``MAX_ATTACHMENTS_PER_POLICY``   （默认 ``10``）
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 应用元信息
    app_name: str = "家庭保险整理工具"
    app_version: str = __version__

    # SQLite 数据库文件路径（相对项目根目录或绝对路径）
    db_path: str = "./data/insurance.db"

    # uvicorn 监听端口
    port: int = 8000

    # 前端静态资源目录
    static_dir: str = "static"

    # ---- v1.2 电子保单附件 ----
    # 附件落盘目录（默认与 SQLite 同源落在 ./data/ 下，Docker 只需挂一个卷即可持久化）
    attachments_dir: str = "./data/attachments"
    # 单个附件大小上限（字节），默认 20MB
    max_attachment_size: int = 20 * 1024 * 1024
    # 单张保单的附件数量上限，默认 10
    max_attachments_per_policy: int = 10

    # ---- v1.8 消息通知 ----
    # 是否启用「每日提醒」后台任务（关掉后仍可手动点「发送测试」，便于排障）
    notification_scheduler: bool = True

    @property
    def database_url(self) -> str:
        """返回 SQLAlchemy 使用的数据库连接串。"""
        return f"sqlite:///{self.db_path}"


# 全局单例配置对象
settings = Settings()
