"""Pydantic v2 Schema 定义。

约定：
- 所有字段使用 **camelCase** JSON 别名（``alias_generator=to_camel`` +
  ``populate_by_name=True``）；
- 金额 / 百分比响应字段通过 ``@field_serializer`` 输出 **JSON number**（float），
  规避 Pydantic v2 默认把 ``Decimal`` 序列化为字符串的行为；
- 保单的「保障期限 / 缴费期限」条件必填用 ``@model_validator(mode="after")`` 校验。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
from pydantic.alias_generators import to_camel

from .calc import money
from .enums import (
    CoverageTermType,
    Gender,
    InsuranceType,
    PaymentFrequency,
    PaymentTermType,
    PolicyStatus,
    Relation,
)


class CamelModel(BaseModel):
    """所有 Schema 的基类：camelCase 别名 + 支持按字段名赋值 + 支持 ORM 对象。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


def _serialize_decimal(value: Optional[Decimal]) -> Optional[float]:
    """Decimal → float（保留 None）。"""
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# v1.3：成员「已有保障类型」统计
# ---------------------------------------------------------------------------
class CoverageTypeStatOut(CamelModel):
    """成员「已有保障类型」的一行（v1.3）。

    - ``count`` / ``activeCount``：该成员在该险种下的保单张数 / 生效中张数
      （含「已缴清」，即状态 ∈ {生效中, 已缴清}）；
    - ``annualPremium``：**分摊后**年化保费，仅计入「生效中且未缴清」保单
      （与成员 ``annualPremium`` 口径一致）；经 ``@field_serializer`` → JSON number。
    """

    insurance_type: InsuranceType
    count: int
    active_count: int
    annual_premium: Decimal

    @field_serializer("annual_premium")
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


# ---------------------------------------------------------------------------
# 家庭成员
# ---------------------------------------------------------------------------
class MemberCreate(CamelModel):
    """新增成员请求体。"""

    name: str = Field(min_length=1, max_length=20)
    relation: Relation
    birth_date: date
    gender: Gender = Gender.unknown
    remark: Optional[str] = Field(default=None, max_length=100)
    # v1.5：是否计入家庭保费统计（默认 True = 计入）；对外 camelCase → ``countInStats``
    count_in_stats: bool = True

    @field_validator("birth_date")
    @classmethod
    def _validate_birth_date(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("出生日期不能晚于今天")
        return value


class MemberUpdate(MemberCreate):
    """编辑成员请求体（字段与新增一致）。"""


class MemberOut(CamelModel):
    """成员响应体（含派生字段）。"""

    id: str
    name: str
    relation: Relation
    birth_date: date
    gender: Gender
    remark: Optional[str] = None
    # v1.5：是否计入家庭保费统计（默认 True）
    count_in_stats: bool = True
    created_at: datetime
    updated_at: datetime

    # 派生字段
    policy_count: int = 0
    active_policy_count: int = 0
    annual_premium: Decimal = Decimal("0.00")
    # v1.3：成员「已有保障类型」（险种 + 张数 + 分摊年化）；无保障时为空数组
    coverage_types: list[CoverageTypeStatOut] = []

    @field_serializer("annual_premium")
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


# ---------------------------------------------------------------------------
# 保单
# ---------------------------------------------------------------------------
class PolicyBase(CamelModel):
    """保单公共字段与校验。"""

    company: str = Field(min_length=1, max_length=50)
    insurance_type: InsuranceType
    product_name: str = Field(min_length=1, max_length=80)
    policyholder_id: str = Field(min_length=1)
    # ---- v1.3 多被保人 ----
    insured_ids: Optional[list[str]] = None
    primary_insured_id: Optional[str] = None
    # 兼容字段：**仅请求侧**接受（旧脚本 / 旧前端发单值 ``insuredId`` → 降级为单选）
    insured_id: Optional[str] = Field(default=None, min_length=1)
    effective_date: date
    coverage_term_type: CoverageTermType
    coverage_term_value: Optional[int] = None
    payment_term_type: PaymentTermType
    payment_term_value: Optional[int] = None
    payment_frequency: PaymentFrequency
    premium_per_period: Decimal
    insured_amount: Optional[Decimal] = None
    status: PolicyStatus = PolicyStatus.active
    remark: Optional[str] = Field(default=None, max_length=200)
    # 手动记录的已缴期数；``None`` = 按生效日自动推算（默认）
    paid_periods_override: Optional[int] = None

    # ---- v1.2 售后信息（全部选填；空串归一为 ``None``）----
    advisor_name: Optional[str] = Field(default=None, max_length=20)
    advisor_phone: Optional[str] = Field(default=None, max_length=30)
    advisor_wechat: Optional[str] = Field(default=None, max_length=50)
    advisor_email: Optional[str] = Field(default=None, max_length=80)
    company_phone: Optional[str] = Field(default=None, max_length=50)
    channel_name: Optional[str] = Field(default=None, max_length=30)
    channel_phone: Optional[str] = Field(default=None, max_length=30)

    @field_validator(
        "advisor_name",
        "advisor_phone",
        "advisor_wechat",
        "advisor_email",
        "company_phone",
        "channel_name",
        "channel_phone",
        mode="before",
    )
    @classmethod
    def _normalize_after_sales(cls, value: Optional[str]) -> Optional[str]:
        """售后字段：首尾去空格；空串归一为 ``None``。"""
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("premium_per_period")
    @classmethod
    def _validate_premium(cls, value: Decimal) -> Decimal:
        value = money(value)
        if value <= 0:
            raise ValueError("每期保费必须大于 0")
        return value

    @field_validator("paid_periods_override")
    @classmethod
    def _validate_paid_periods_override(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if value < 0:
            raise ValueError("已缴期数不能为负")
        if value > 1200:
            raise ValueError("已缴期数过大（上限 1200 期）")
        return value

    @field_validator("insured_amount")
    @classmethod
    def _validate_insured_amount(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        if value is None:
            return None
        value = money(value)
        if value < 0:
            raise ValueError("保额不能为负")
        return value

    @model_validator(mode="after")
    def _validate_insureds(self) -> "PolicyBase":
        """被保人多选校验（**纯逻辑，不查库**）。

        规则（见 ARCHITECTURE-v13-increment §3.5.1）：

        1. 取 ``insured_ids``；缺省时降级为 ``[insured_id]``（兼容旧请求）；
        2. 去重（保序）；空 → 422「请至少选择 1 名被保人」；
        3. 主被保人缺省 = ``insured_id`` 或 ``insured_ids[0]``；须 ∈ ``insured_ids``；
        4. 归一化：``insured_id`` 回写为主被保人（兼容字段）。

        成员**存在性**校验在 Service 层（需查库）。
        """
        raw = (
            self.insured_ids
            if self.insured_ids is not None
            else ([self.insured_id] if self.insured_id else [])
        )
        ids = list(dict.fromkeys([i for i in raw if i]))
        if not ids:
            raise ValueError("请至少选择 1 名被保人")
        primary = self.primary_insured_id or self.insured_id or ids[0]
        if primary not in ids:
            raise ValueError("主被保人必须是已选被保人之一")
        self.insured_ids = ids
        self.primary_insured_id = primary
        self.insured_id = primary  # 归一化兼容字段
        return self

    @model_validator(mode="after")
    def _validate_terms(self) -> "PolicyBase":
        """保障期限 / 缴费期限的条件必填与范围校验。"""
        # ---- 保障期限 ----
        ctt, ctv = self.coverage_term_type, self.coverage_term_value
        if ctt == CoverageTermType.whole_life:
            if ctv is not None:
                raise ValueError("保障期限为「终身」时，保障期限数值必须为空")
        elif ctt == CoverageTermType.fixed_years:
            if ctv is None:
                raise ValueError("保障期限为「固定年限」时，保障期限数值必填")
            if not 1 <= ctv <= 100:
                raise ValueError("保障期限「固定年限」数值须在 1–100 之间")
        else:  # 至某年龄
            if ctv is None:
                raise ValueError("保障期限为「至某年龄」时，保障期限数值必填")
            if not 1 <= ctv <= 120:
                raise ValueError("保障期限「至某年龄」数值须在 1–120 之间")

        # ---- 缴费期限 ----
        ptt, ptv = self.payment_term_type, self.payment_term_value
        if ptt == PaymentTermType.single:
            if ptv is not None:
                raise ValueError("缴费期限为「趸交」时，缴费期限数值必须为空")
        elif ptt == PaymentTermType.fixed_years:
            if ptv is None:
                raise ValueError("缴费期限为「固定年限」时，缴费期限数值必填")
            if not 1 <= ptv <= 100:
                raise ValueError("缴费期限「固定年限」数值须在 1–100 之间")
        else:  # 交至某年龄
            if ptv is None:
                raise ValueError("缴费期限为「交至某年龄」时，缴费期限数值必填")
            if not 1 <= ptv <= 120:
                raise ValueError("缴费期限「交至某年龄」数值须在 1–120 之间")

        # ---- 已缴期数（缴费记录）----
        paid = self.paid_periods_override
        if paid is not None:
            per_year = 12 if self.payment_frequency == PaymentFrequency.monthly else 1
            if ptt == PaymentTermType.single:
                max_periods: Optional[int] = 1
            elif ptt == PaymentTermType.fixed_years:
                max_periods = ptv * per_year
            else:
                # 「交至某年龄」的总期数取决于被保人出生日期，此处无法判定；
                # 由 calc_schedule 做防御性钳制。
                max_periods = None
            if max_periods is not None and paid > max_periods:
                raise ValueError(f"已缴期数不能超过总缴费期数（{max_periods} 期）")

        return self


class PolicyCreate(PolicyBase):
    """新增保单请求体。"""


class PolicyUpdate(PolicyBase):
    """编辑保单请求体。"""


class PolicyOut(CamelModel):
    """保单响应体（含全部派生字段）。"""

    id: str
    company: str
    insurance_type: InsuranceType
    product_name: str
    policyholder_id: str
    insured_id: str  # 兼容字段 = primary_insured_id
    # ---- v1.3 多被保人（规范序）----
    primary_insured_id: str
    insured_ids: list[str] = []
    insured_names: list[str] = []
    effective_date: date
    coverage_term_type: CoverageTermType
    coverage_term_value: Optional[int] = None
    payment_term_type: PaymentTermType
    payment_term_value: Optional[int] = None
    payment_frequency: PaymentFrequency
    premium_per_period: Decimal
    insured_amount: Optional[Decimal] = None
    status: PolicyStatus
    remark: Optional[str] = None
    paid_periods_override: Optional[int] = None
    # ---- v1.2 售后信息（全部选填）----
    advisor_name: Optional[str] = None
    advisor_phone: Optional[str] = None
    advisor_wechat: Optional[str] = None
    advisor_email: Optional[str] = None
    company_phone: Optional[str] = None
    channel_name: Optional[str] = None
    channel_phone: Optional[str] = None
    # ---- v1.2 派生字段：已挂载附件数（明细走单独接口，避免 N+1）----
    attachment_count: int = 0
    created_at: datetime
    updated_at: datetime

    # ---- 派生字段（实时计算，不落库） ----
    annualized_premium: Decimal
    #: v1.5：该保单的**投保人**是否计入家庭保费统计（``False`` = 本单年化不计入统计口径）。
    #: 仅作展示标识用，``annualizedPremium`` 本身**不过滤**（保单事实数据）。
    counted_in_stats: bool = True
    total_periods: int
    paid_periods: int
    cumulative_paid: Decimal
    remaining_periods: int
    is_paid_off: bool
    progress_percent: Decimal
    in_coverage: bool
    #: 保障结束日；``None`` = 终身险，不存在续保到期。
    coverage_end_date: Optional[date] = None
    next_due_date: Optional[date] = None
    #: 仅按时间推算的应缴期数（不含手动记录）
    expected_periods_to_date: int = 0
    #: 逾期期数 = max(0, expected_periods_to_date - paid_periods)
    overdue_periods: int = 0
    insured_name: str
    policyholder_name: str

    @field_serializer("premium_per_period", "insured_amount", "annualized_premium", "cumulative_paid")
    def _ser_money(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal(value)

    @field_serializer("progress_percent")
    def _ser_pct(self, value: Decimal) -> float:
        return float(value)


# ---------------------------------------------------------------------------
# v1.2：批量编辑 / 常用值聚合
# ---------------------------------------------------------------------------
#: 批量编辑允许的字段白名单（camelCase）与其长度上限
BATCH_UPDATE_FIELD_LIMITS: dict[str, int] = {
    "advisorName": 20,
    "advisorPhone": 30,
    "advisorWechat": 50,
    "advisorEmail": 80,
    "companyPhone": 50,
    "channelName": 30,
    "channelPhone": 30,
}


class PolicyBatchUpdateIn(CamelModel):
    """批量编辑请求体。

    ``values`` 使用 ``dict[str, Optional[str]]`` 以保留**三态**：
    键不存在 = 不改；键为字符串 = 设值；键为 ``null`` = 显式清空。
    """

    ids: list[str]
    values: dict[str, Optional[str]]


class PolicyBatchUpdateOut(CamelModel):
    """批量编辑响应体。"""

    requested: int
    updated: int
    updated_ids: list[str]
    missing_ids: list[str]


class FieldSuggestionItem(CamelModel):
    """常用值聚合中的一项。"""

    value: str
    count: int


class FieldSuggestionsOut(CamelModel):
    """常用值聚合响应（6 个字段；``companyPhone`` 不参与聚合）。"""

    advisor_name: list[FieldSuggestionItem] = []
    advisor_phone: list[FieldSuggestionItem] = []
    advisor_wechat: list[FieldSuggestionItem] = []
    advisor_email: list[FieldSuggestionItem] = []
    channel_name: list[FieldSuggestionItem] = []
    channel_phone: list[FieldSuggestionItem] = []


# ---------------------------------------------------------------------------
# v1.2：电子保单附件
# ---------------------------------------------------------------------------
class AttachmentOut(CamelModel):
    """单个附件响应体。"""

    id: str
    file_name: str
    file_size: int
    content_type: str
    uploaded_at: datetime


class AttachmentUploadOut(CamelModel):
    """附件上传响应体。"""

    uploaded: list[AttachmentOut]
    attachment_count: int


class AttachmentListOut(CamelModel):
    """附件列表响应体。"""

    policy_id: str
    attachment_count: int
    attachments: list[AttachmentOut]


# ---------------------------------------------------------------------------
# 统计看板
# ---------------------------------------------------------------------------
class OverviewOut(CamelModel):
    """看板核心指标（v1.4 保费双口径）。

    - ``annualTotalPremium``（**当年应缴总保费**）：``status ∈ {生效中, 已缴清}``
      保单的年化保费合计（含已缴清、含已缴完的生效中保单）；
    - ``unpaidAnnualPremium``（**当年未缴费金额**）：``status == 生效中`` 且
      ``is_paid_off == False`` 的年化保费合计（= v1.3 旧「年度总保费」，逐值不变）；
    - 两者之差即「已缴部分」：``annualTotalPremium - unpaidAnnualPremium == 已缴部分``。
    """

    annual_total_premium: Decimal
    unpaid_annual_premium: Decimal = Decimal(0)
    # ★ v1.7 新增：本日历年到期且未标记已缴的期次金额合计（现金流口径，不受排除筛选影响）
    year_unpaid_premium: Decimal = Decimal(0)
    upcoming60_premium: Decimal
    upcoming60_count: int
    policy_total: int
    active_policy_count: int
    insured_person_count: int
    #: 存在逾期未缴期次的保单数量
    overdue_policy_count: int = 0

    @field_serializer(
        "annual_total_premium",
        "unpaid_annual_premium",
        "year_unpaid_premium",  # ★ 必须加入，否则 Pydantic v2 输出字符串
        "upcoming60_premium",
    )
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


class StatsExclusionsIn(CamelModel):
    """写入统计排除配置（``PUT /api/stats/exclusions`` 请求体，**全量替换**语义）。

    - ``excludedPolicyholderIds``：被排除的**投保人**成员 id（落到 ``members.count_in_stats``）；
    - ``excludedInsuranceTypes``：被排除的**险种**中文枚举值（非法值由 ``InsuranceType``
      枚举校验 → 422 ``VALIDATION_ERROR``）。
    """

    excluded_policyholder_ids: list[str] = []
    excluded_insurance_types: list[InsuranceType] = []


class StatsExclusionsOut(CamelModel):
    """读取统计排除配置（``GET`` 响应 / ``PUT`` 回显）。

    两个列表均为**排除项**（被排除的投保人 id / 险种中文值）。
    """

    excluded_policyholder_ids: list[str] = []
    excluded_insurance_types: list[str] = []


class MemberStatOut(CamelModel):
    """按成员聚合的一行。"""

    member_id: str
    name: str
    relation: Relation
    policy_count: int
    active_policy_count: int
    annual_premium: Decimal
    upcoming60_premium: Decimal
    paid_to_date_premium: Decimal

    @field_serializer("annual_premium", "upcoming60_premium", "paid_to_date_premium")
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


class ByMemberOut(CamelModel):
    """按成员分布（双维度）。"""

    by_insured: list[MemberStatOut]
    by_policyholder: list[MemberStatOut]


class TypeStatOut(CamelModel):
    """按险种聚合的一行。"""

    insurance_type: InsuranceType
    policy_count: int
    annual_premium: Decimal
    upcoming60_premium: Decimal
    paid_to_date_premium: Decimal

    @field_serializer("annual_premium", "upcoming60_premium", "paid_to_date_premium")
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


class UpcomingItemOut(CamelModel):
    """近 60 天待缴清单的一行。"""

    policy_id: str
    company: str
    product_name: str
    insured_id: str
    insured_name: str
    insured_ids: list[str] = []
    insured_names: list[str] = []
    premium_per_period: Decimal
    due_date: date
    period_index: int
    total_periods: int

    @field_serializer("premium_per_period")
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


class OverdueItemOut(CamelModel):
    """逾期未缴清单的一行（已过缴费日但未标记已缴）。"""

    policy_id: str
    company: str
    product_name: str
    insured_id: str
    insured_name: str
    insured_ids: list[str] = []
    insured_names: list[str] = []
    premium_per_period: Decimal
    #: 逾期期数
    overdue_periods: int
    #: 最早未缴期次的应缴日（已过期）
    earliest_due_date: date
    #: 逾期金额合计 = 逾期期数 × 每期保费
    amount: Decimal

    @field_serializer("premium_per_period", "amount")
    def _ser_money(self, value: Decimal) -> float:
        return float(value)


class DashboardOut(CamelModel):
    """看板完整响应。"""

    as_of_date: date
    overview: OverviewOut
    by_member: ByMemberOut
    by_type: list[TypeStatOut]
    upcoming: list[UpcomingItemOut]
    overdue: list[OverdueItemOut] = []


# ---------------------------------------------------------------------------
# v1.8：消息通知（企业微信机器人）
# ---------------------------------------------------------------------------
class NotifySettingsIn(CamelModel):
    """通知配置**部分更新**请求体。

    所有字段可缺省：路由层用 ``model_dump(exclude_unset=True)`` 保证
    **未传的键一律不改**（避免前端表单漏带字段把配置清空）。

    - ``webhook`` 传空串 = 清除已保存地址；
    - ``webhook`` 传 ``null`` = 不改动（便于「只改天数」等局部提交）。
    """

    enabled: Optional[bool] = None
    webhook: Optional[str] = None
    expiry_enabled: Optional[bool] = None
    expiry_days: Optional[int] = Field(default=None, ge=1, le=365)
    due_enabled: Optional[bool] = None
    due_days: Optional[int] = Field(default=None, ge=1, le=365)
    send_time: Optional[str] = None

    @field_validator("send_time")
    @classmethod
    def _validate_send_time(cls, value: Optional[str]) -> Optional[str]:
        """``H:MM`` / ``HH:MM`` → 归一为 ``HH:MM``；非法值直接 422。"""
        if value is None:
            return None
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError("发送时间格式应为 HH:MM")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError("发送时间格式应为 HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("发送时间须在 00:00–23:59 之间")
        return f"{hour:02d}:{minute:02d}"


class NotifySettingsOut(CamelModel):
    """通知配置响应体。

    ``webhook`` 为**脱敏值**（``key=****abcd``），真实 key 永不出网；
    前端回填时留空即表示「不修改」。
    """

    enabled: bool
    webhook: str = ""
    webhook_configured: bool = False
    expiry_enabled: bool
    expiry_days: int
    due_enabled: bool
    due_days: int
    send_time: str
    #: 最近一次成功发送的日期（``YYYY-MM-DD``，空 = 从未发送）
    last_sent_date: Optional[str] = None
    #: 服务端当天日期，便于前端判断「今日是否已发送」
    today: Optional[date] = None


class NotifyTestOut(CamelModel):
    """「立即发送测试」响应体。"""

    sent: bool
    expiring: int = 0
    due: int = 0


__all__ = [
    "CamelModel",
    "MemberCreate",
    "MemberUpdate",
    "MemberOut",
    "CoverageTypeStatOut",
    "PolicyCreate",
    "PolicyUpdate",
    "PolicyOut",
    "BATCH_UPDATE_FIELD_LIMITS",
    "PolicyBatchUpdateIn",
    "PolicyBatchUpdateOut",
    "FieldSuggestionItem",
    "FieldSuggestionsOut",
    "AttachmentOut",
    "AttachmentUploadOut",
    "AttachmentListOut",
    "OverviewOut",
    "StatsExclusionsIn",
    "StatsExclusionsOut",
    "MemberStatOut",
    "ByMemberOut",
    "TypeStatOut",
    "UpcomingItemOut",
    "OverdueItemOut",
    "DashboardOut",
    "NotifySettingsIn",
    "NotifySettingsOut",
    "NotifyTestOut",
]
