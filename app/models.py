"""ORM 模型：``Member`` 与 ``Policy``。

- 主键由应用生成：成员 ``"m_" + uuid4().hex[:12]``，保单 ``"p_" + uuid4().hex[:12]``；
- 枚举列以 ``VARCHAR + CHECK`` 存储中文枚举值；
- 金额列使用自定义 ``Money`` 类型（底层整数分）；
- 两个外键均 ``ON DELETE RESTRICT``，配合 ``PRAGMA foreign_keys=ON`` 防止误删。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .enums import (
    CoverageTermType,
    Gender,
    InsuranceType,
    PaymentFrequency,
    PaymentTermType,
    PolicyStatus,
    Relation,
    _values,
)
from .types import Money


def new_member_id() -> str:
    """生成成员主键：``m_`` + 12 位十六进制。"""
    return "m_" + uuid.uuid4().hex[:12]


def new_policy_id() -> str:
    """生成保单主键：``p_`` + 12 位十六进制。"""
    return "p_" + uuid.uuid4().hex[:12]


def new_attachment_id() -> str:
    """生成附件主键：``a_`` + 12 位十六进制。"""
    return "a_" + uuid.uuid4().hex[:12]


def _now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


class Member(Base):
    """家庭成员。"""

    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_member_id)
    name: Mapped[str] = mapped_column(String(20), nullable=False)
    relation: Mapped[Relation] = mapped_column(
        SAEnum(Relation, native_enum=False, length=4, values_callable=_values),
        nullable=False,
    )
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[Gender] = mapped_column(
        SAEnum(Gender, native_enum=False, length=4, values_callable=_values),
        nullable=False,
        default=Gender.unknown,
    )
    remark: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default=None)
    # ---- v1.5：是否计入家庭保费统计（按投保人粒度）----
    # True（默认）= 该成员名下保单的年化保费计入统计；False = 从「年化保费」口径中排除。
    # 仅影响年化保费口径（计数 / 待缴 / 已缴实际均不受影响）。
    # ``server_default=1``：保证存量库经 ALTER 补列后旧行即为「计入」，
    # 且允许不指定该列的裸 SQL INSERT（NOT NULL 列需要 SQL 默认值）。
    count_in_stats: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Member id={self.id!r} name={self.name!r} relation={self.relation!r}>"


class Policy(Base):
    """保单。"""

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_policy_id)
    company: Mapped[str] = mapped_column(String(50), nullable=False)
    insurance_type: Mapped[InsuranceType] = mapped_column(
        SAEnum(InsuranceType, native_enum=False, length=8, values_callable=_values),
        nullable=False,
    )
    product_name: Mapped[str] = mapped_column(String(80), nullable=False)
    policyholder_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("members.id", ondelete="RESTRICT"),
        nullable=False,
    )
    insured_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("members.id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_term_type: Mapped[CoverageTermType] = mapped_column(
        SAEnum(CoverageTermType, native_enum=False, length=8, values_callable=_values),
        nullable=False,
    )
    coverage_term_value: Mapped[Optional[int]] = mapped_column(nullable=True, default=None)
    payment_term_type: Mapped[PaymentTermType] = mapped_column(
        SAEnum(PaymentTermType, native_enum=False, length=10, values_callable=_values),
        nullable=False,
    )
    payment_term_value: Mapped[Optional[int]] = mapped_column(nullable=True, default=None)
    payment_frequency: Mapped[PaymentFrequency] = mapped_column(
        SAEnum(PaymentFrequency, native_enum=False, length=4, values_callable=_values),
        nullable=False,
    )
    premium_per_period: Mapped[object] = mapped_column(Money, nullable=False)
    insured_amount: Mapped[Optional[object]] = mapped_column(Money, nullable=True, default=None)
    status: Mapped[PolicyStatus] = mapped_column(
        SAEnum(PolicyStatus, native_enum=False, length=8, values_callable=_values),
        nullable=False,
        default=PolicyStatus.active,
    )
    remark: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, default=None)
    # 手动指定的已缴期数（缴费记录）。
    # ``None`` = 按生效日自动推算（默认，兼容既有数据）；
    # 非 ``None`` = 以用户记录为准，用于纠正漏缴 / 提前缴 / 补录历史。
    paid_periods_override: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=None
    )
    # ---- v1.2 售后信息（全部选填；NULL = 未填）----
    advisor_name: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default=None)
    advisor_phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default=None)
    advisor_wechat: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)
    advisor_email: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, default=None)
    company_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)
    channel_name: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default=None)
    channel_phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (
        Index("idx_policies_type", "insurance_type"),
        Index("idx_policies_holder", "policyholder_id"),
        Index("idx_policies_insured", "insured_id"),
        Index("idx_policies_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Policy id={self.id!r} product={self.product_name!r} status={self.status!r}>"


class PolicyInsured(Base):
    """保单 ↔ 被保人 关联表（v1.3）。

    该表是**被保人集合的唯一真源**，**含主被保人本身**（避免"主被保人特判"分叉）：

    - **复合主键 ``(policy_id, member_id)``**：天然保证「同一保单同一成员不重复」；
    - ``policy_id`` 侧外键 ``ON DELETE CASCADE``：删保单自动连带删关联行
      （与既有 ``policy_attachments`` 同策略）；
    - ``member_id`` 侧外键 ``ON DELETE RESTRICT``：被引用时禁止删成员
      （与既有 ``policies.insured_id`` / ``policyholder_id`` 同策略）；
    - ``member_id`` 建索引：服务于「筛选 any-of」与「按被保人分组」。
    """

    __tablename__ = "policy_insureds"

    policy_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("policies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    member_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("members.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    __table_args__ = (Index("idx_policy_insureds_member", "member_id"),)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<PolicyInsured policy_id={self.policy_id!r} member_id={self.member_id!r}>"


class PolicyAttachment(Base):
    """保单电子附件（PDF）。

    - ``file_name`` 为客户端**原始文件名**，仅用于展示 / 下载；
    - ``stored_name`` 为**服务端生成**的落盘文件名（``uuid4().hex + ".pdf"``），
      绝不由客户端文件名参与拼路径，防止路径穿越；
    - 外键 ``ON DELETE CASCADE``：删除保单时附件记录由数据库自动连带删除。
    """

    __tablename__ = "policy_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_attachment_id)
    policy_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/pdf"
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        Index("idx_attachments_policy", "policy_id"),
        UniqueConstraint("stored_name", name="uq_attachments_stored"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<PolicyAttachment id={self.id!r} policy_id={self.policy_id!r} "
            f"file_name={self.file_name!r}>"
        )


class StatsExclusion(Base):
    """统计排除配置（v1.7）。

    复合主键 ``(kind, ref_id)``；v1.7 **只写入** ``kind='insurance_type'``
    （``ref_id`` = 险种中文枚举值，如 ``"车险"``）。

    投保人维度**沿用** ``members.count_in_stats``（见 ARCHITECTURE-v17-increment §1.5），
    本表仅承载「按险种」等**集合**语义的排除项，为未来维度预留 ``kind`` 列。

    迁移：新表由 ``Base.metadata.create_all`` 自动创建，**无需**登记
    ``_ADDITIVE_COLUMNS``、**无需**数据回填（表为空 = 全部险种计入）。
    """

    __tablename__ = "stats_exclusions"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    ref_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<StatsExclusion kind={self.kind!r} ref_id={self.ref_id!r}>"


class AppSetting(Base):
    """应用级键值配置（v1.8）。

    仅承载少量**非业务**配置：通知开关、企业微信 Webhook、提醒参数等。
    不存任何保单/成员数据。

    值为字符串（布尔用 ``"true"/"false"``，整数按其十进制字符串），
    由 ``notification_service`` 统一解析并提供默认值，避免调用方各自兜底。

    迁移：新表由 ``Base.metadata.create_all`` 自动创建，**无需**登记
    ``_ADDITIVE_COLUMNS``、**无需**回填（缺键即取默认值）。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        # 注意：不打印 value，避免 Webhook key 出现在日志里
        return f"<AppSetting key={self.key!r}>"


__all__ = [
    "Member",
    "Policy",
    "PolicyInsured",
    "PolicyAttachment",
    "StatsExclusion",
    "AppSetting",
    "new_member_id",
    "new_policy_id",
    "new_attachment_id",
]
