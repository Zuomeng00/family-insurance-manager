"""保单托管报告（v1.10）。

把库里散落的保单数据，变成一份「能给别人看」的家庭保障检视报告。

**每次请求现算，不落快照**。用户新增一张保单、旧保单到期、起保日改了、
责任表补录了，下一次打开报告就是新的数——这是本模块的核心设计约束：

- 不写任何报告缓存表，不做「生成后存档」；
- 所有派生量都走 ``calc_schedule``（与看板同源），不另起一套日历算术；
- ``meta.dataUpdatedAt`` 取保单/责任/成员三张表 ``max(updated_at)``，
  前端据此判断「数据变了、该重新拉一次」。

结构对标明亚/大童的《家庭保障分析报告》，但**只保留我们真的有数据的部分**：

| 它们的章节 | 我们做不做 | 原因 |
|---|---|---|
| 家庭保障总览 | ✅ | 成员 × 保障类别矩阵，核心页 |
| 家庭保费分布 | ✅ | 按人 / 按险种 / 按月 |
| 成员保障分析 | ✅ | 逐人：有什么、缺什么 |
| 保障缺口分析 | ✅ | 结构缺口自动 + 额度缺口（需参数） |
| 保单基础信息汇总 | ✅ | 全字段明细表 |
| 交费计划明细 | ✅ | 未来 12 个月 |
| 产品详情解析 | ⚠️ 部分 | 靠用户录入的「保障责任」，不做条款自动拆解 |
| 公司介绍 / 经纪人介绍 | ❌ | 中介的销售物料，与我们无关 |
| 生存金 / 现金价值 / 绿通权益 | ❌ | 库里没有这些数据，不编 |

**统计口径**（四条，报告页脚要写出来）：

1. **只有「在保」的保单计入保障与保费**：状态 ∈ {生效中, 已缴清} **且** 仍在保障期内。
   已退保、已失效、保障已到期的保单不进矩阵、不进保费合计——否则报告会虚高。
2. **多被保人保单的保额归主被保人**。一张保单保夫妻两人时，各自保额通常在条款里分列，
   我们无法从「保额」一个字段还原，归给主被保人并单独提示，好过虚高。
3. **没录责任的保单按险种粗算**：重疾险的保额记作重疾保障、医疗险记作「有医疗」……
   精度低但不会把客户说成「裸奔」，同时在报告里提示补录。
4. **医疗险是报销型限额，不参与「总保额」加总**，矩阵里单列。

缺口口径（**行业通行经验值，非监管标准**，报告里必须标注）：

- 重疾：``max(3 × 年收入, 30 万)``；
- 寿险：``负债余额 + 5 × 年生活支出``（只对家庭经济支柱：本人 / 配偶）；
- 意外：``max(5 × 年收入, 50 万)``；
- 医疗：只判有无，不判额度（报销型，够不够取决于就医习惯，算不准）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import __version__
from ..calc import (
    MONTHLY,
    STATUS_ACTIVE,
    add_months,
    age_years,
    allocate_amounts,
    calc_schedule,
    enum_value,
    money,
)
from ..enums import (
    RELATION_ORDER,
    SUMMABLE_CATEGORIES,
    CoverageCategory,
    InsuranceType,
    Relation,
)
from ..models import AppSetting, Member, Policy, PolicyCoverage, PolicyInsured
from . import coverage_service

KEY_PREFIX = "report."

DEFAULTS: dict[str, str] = {
    "annual_income": "",
    "total_debt": "",
    "annual_expense": "",
}

#: 计入保障与保费的保单状态（已退保 / 已失效不计）
INSURED_STATUSES = ("生效中", "已缴清")

#: 报告矩阵与汇总用的保障类别（按这个顺序展示）
MATRIX_CATEGORIES: tuple[str, ...] = (
    CoverageCategory.critical.value,
    CoverageCategory.medical.value,
    CoverageCategory.accident.value,
    CoverageCategory.death.value,
    CoverageCategory.annuity.value,
)

#: 给付型类别（可加总成「总保额」）；医疗是报销限额，不参与。
#: 与 ``enums.SUMMABLE_CATEGORIES`` 同源，只在这里转成 set 便于成员判断。
SUMMABLE: frozenset[str] = frozenset(SUMMABLE_CATEGORIES)

#: 险种 → 粗算类别（没录责任时的兜底映射）
TYPE_FALLBACK: dict[str, str] = {
    InsuranceType.critical_illness.value: CoverageCategory.critical.value,
    InsuranceType.medical.value: CoverageCategory.medical.value,
    InsuranceType.accident.value: CoverageCategory.accident.value,
    InsuranceType.life.value: CoverageCategory.death.value,
    InsuranceType.annuity.value: CoverageCategory.annuity.value,
    InsuranceType.dividend.value: CoverageCategory.annuity.value,
    InsuranceType.education.value: CoverageCategory.annuity.value,
}

#: 参考建议（元）
MIN_CRITICAL = Decimal("300000")
MIN_ACCIDENT = Decimal("500000")
CRITICAL_INCOME_MULTIPLE = Decimal("3")
ACCIDENT_INCOME_MULTIPLE = Decimal("5")
LIFE_EXPENSE_MULTIPLE = Decimal("5")
#: 未成年人重疾参考保额：按治疗 + 康复成本估算，不套用收入倍数
CHILD_CRITICAL = Decimal("500000")
#: 未成年人身故保险金额监管上限：10 周岁以下 20 万，10–17 周岁 50 万
CHILD_DEATH_LIMIT_YOUNG = Decimal("200000")
CHILD_DEATH_LIMIT_OLD = Decimal("500000")

#: 只对家庭经济支柱算寿险缺口
BREADWINNER_RELATIONS = {Relation.self_.value, Relation.spouse.value}

ADULT_AGE = 18
#: 交费计划 / 缺口测算的时间窗（天）
PLAN_HORIZON_DAYS = 365
#: 「即将到期」预警窗（天）
EXPIRY_SOON_DAYS = 30

ZERO = Decimal("0")


def _num(value: Optional[Decimal]) -> Optional[float]:
    """Decimal → JSON number。"""
    return None if value is None else float(value)


def _yuan_text(value: Decimal) -> str:
    """Decimal → 库里存的字符串（不用科学计数法）。"""
    return format(value.quantize(Decimal("0.01")), "f")


# ---------------------------------------------------------------------------
# 参数（家庭年收入 / 负债 / 年支出）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReportParams:
    annual_income: Optional[Decimal]
    total_debt: Optional[Decimal]
    annual_expense: Optional[Decimal]

    @property
    def filled(self) -> bool:
        """只要填了年收入，就能给出有意义的额度建议。"""
        return self.annual_income is not None and self.annual_income > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "annualIncome": _num(self.annual_income),
            "totalDebt": _num(self.total_debt),
            "annualExpense": _num(self.annual_expense),
            "filled": self.filled,
        }


def _read_amount(raw: str) -> Optional[Decimal]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 - 脏值按未填处理，不阻塞报告
        return None


def get_params(db: Session) -> ReportParams:
    rows = db.query(AppSetting).filter(AppSetting.key.like(f"{KEY_PREFIX}%")).all()
    raw = {row.key[len(KEY_PREFIX) :]: row.value for row in rows}
    return ReportParams(
        annual_income=_read_amount(raw.get("annual_income", "")),
        total_debt=_read_amount(raw.get("total_debt", "")),
        annual_expense=_read_amount(raw.get("annual_expense", "")),
    )


def save_params(db: Session, values: dict[str, Optional[Decimal]]) -> ReportParams:
    """保存参数。

    ``values`` 的语义：

    - **键缺省** = 不改（前端只提交改过的字段时不会被清空）；
    - **值为 ``None`` / 空** = 清空该键（用户把输入框删空了）；
    - 有值 = 覆盖。

    只写这三个白名单键，避免 ``report.`` 前缀被塞进任意配置。
    """
    for name, value in values.items():
        if name not in DEFAULTS:
            continue
        key = f"{KEY_PREFIX}{name}"
        text = "" if value is None else _yuan_text(Decimal(value))
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=text))
        else:
            row.value = text
    db.commit()
    return get_params(db)


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------
def _members(db: Session) -> list[Member]:
    """成员（按关系权重 + 姓名排序，与看板/成员页一致）。"""
    rows = db.query(Member).all()
    return sorted(
        rows,
        key=lambda m: (RELATION_ORDER.get(enum_value(m.relation), 99), m.name),
    )


def _policies(db: Session) -> list[Policy]:
    return db.query(Policy).order_by(Policy.effective_date, Policy.id).all()


def _coverage_map(db: Session) -> dict[str, list[PolicyCoverage]]:
    """policy_id → 责任项列表（一次查全，避免 N+1）。"""
    grouped: dict[str, list[PolicyCoverage]] = {}
    for item in db.query(PolicyCoverage).order_by(
        PolicyCoverage.policy_id, PolicyCoverage.sort_order
    ):
        grouped.setdefault(item.policy_id, []).append(item)
    return grouped


def _insured_links(db: Session) -> dict[str, list[str]]:
    """member_id → 该成员作为被保人的 policy_id 列表（含主/次被保人）。"""
    links: dict[str, list[str]] = {}
    for row in db.query(PolicyInsured).all():
        links.setdefault(row.member_id, []).append(row.policy_id)
    return links


def _invert(links: dict[str, list[str]]) -> dict[str, list[str]]:
    """``member_id → [policy_id]`` 反转为 ``policy_id → [member_id]``。"""
    inverted: dict[str, list[str]] = {}
    for member_id, policy_ids in links.items():
        for policy_id in policy_ids:
            inverted.setdefault(policy_id, []).append(member_id)
    return inverted


def _data_updated_at(db: Session) -> Optional[datetime]:
    """保单 / 责任 / 成员三张表的最新修改时间，供前端判断数据是否已变。"""
    stamps = [
        db.query(func.max(Policy.updated_at)).scalar(),
        db.query(func.max(PolicyCoverage.updated_at)).scalar(),
        db.query(func.max(Member.updated_at)).scalar(),
    ]
    present = [stamp for stamp in stamps if stamp is not None]
    return max(present) if present else None


# ---------------------------------------------------------------------------
# 单张保单 → 各责任类别的金额
# ---------------------------------------------------------------------------
def _policy_category_amounts(
    policy: Policy, coverages: list[PolicyCoverage]
) -> dict[str, Decimal]:
    """一张保单在各责任类别上的金额。

    - 有责任明细：按 ``effective_amount`` 汇总（``amount`` 优先，否则 ``ratio`` × 基本保额）；
    - 没有：按险种 + 保额粗算（医疗只记「有」，金额为 0）。

    医疗按**取最大值**处理（报销限额不能叠加）；其余类别累加。
    """
    result: dict[str, Decimal] = {}
    if coverages:
        for item in coverages:
            category = enum_value(item.category)
            amount = coverage_service.effective_amount(item, policy.insured_amount) or ZERO
            if category == CoverageCategory.medical.value:
                result[category] = max(result.get(category, ZERO), amount)
            else:
                result[category] = result.get(category, ZERO) + amount
        return result

    category = TYPE_FALLBACK.get(enum_value(policy.insurance_type))
    if category is None:
        return result
    # 医疗险的「保额」本身就是报销限额，直接拿来展示；没填保额才是 0（有保障、额度未知）
    result[category] = Decimal(policy.insured_amount or ZERO)
    return result


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_report(db: Session, today: Optional[date] = None) -> dict[str, Any]:
    """聚合一份完整报告。**纯读**，不写库、不缓存，每次调用都反映当前数据。"""
    today = today or date.today()
    params = get_params(db)

    members = _members(db)
    member_map = {m.id: m for m in members}
    policies = _policies(db)
    coverages = _coverage_map(db)
    insured_links = _insured_links(db)
    insured_by_policy = _invert(insured_links)

    # ---- 逐单派生量（与看板同源；一张单只算一次） ----
    schedules: dict[str, Any] = {}
    for policy in policies:
        insured = member_map.get(policy.insured_id)
        if insured is not None:
            schedules[policy.id] = calc_schedule(policy, insured, today)

    per_member: dict[str, dict[str, Decimal]] = {m.id: {} for m in members}
    detail: dict[str, list[dict[str, Any]]] = {m.id: [] for m in members}
    policy_rows: list[dict[str, Any]] = []
    premium_by_holder: dict[str, Decimal] = {m.id: ZERO for m in members}
    premium_by_type: dict[str, Decimal] = {}
    premium_by_month: dict[int, Decimal] = {month: ZERO for month in range(1, 13)}
    upcoming: list[dict[str, Any]] = []
    expiring: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    #: policy_id → 该保单实际覆盖的类别（只统计在保保单，供「共保」判断）
    policy_categories: dict[str, set[str]] = {}
    recorded = 0
    missing = 0
    unpriced = 0
    total_annual = ZERO

    horizon = today + timedelta(days=PLAN_HORIZON_DAYS)

    for policy in policies:
        items = coverages.get(policy.id, [])
        schedule = schedules.get(policy.id)
        status = enum_value(policy.status)
        # 「在保」= 状态正常 且 仍在保障期内。已退保 / 已失效 / 已到期一律不计。
        in_force = status in INSURED_STATUSES and (schedule is None or schedule.in_coverage)
        is_expired = (
            status in INSURED_STATUSES
            and schedule is not None
            and schedule.coverage_end_date is not None
            and schedule.coverage_end_date < today
        )

        # 责任录入进度只统计在保保单：已退保的保单没录责任不算「待办」
        if in_force:
            if items:
                recorded += 1
            else:
                missing += 1

        annual = schedule.annualized_premium if (in_force and schedule) else ZERO
        total_annual += annual

        holder = member_map.get(policy.policyholder_id)
        if holder is not None and in_force:
            premium_by_holder[holder.id] = premium_by_holder.get(holder.id, ZERO) + annual
            type_name = enum_value(policy.insurance_type)
            premium_by_type[type_name] = premium_by_type.get(type_name, ZERO) + annual
            # 全年保费分布：月缴平摊到 12 个月，年缴 / 趸交落在生效月（Σ 恒等于年化合计）
            freq = enum_value(policy.payment_frequency)
            if freq == MONTHLY:
                for month, share in enumerate(allocate_amounts(annual, 12), start=1):
                    premium_by_month[month] += share
            else:
                premium_by_month[policy.effective_date.month] += annual

        # ---- 未来 12 个月待缴（严格按未缴期次推，兼容提前缴 / 逾期） ----
        if in_force and schedule is not None and not schedule.is_paid_off:
            freq = enum_value(policy.payment_frequency)
            period_months = 1 if freq == MONTHLY else 12
            premium = money(Decimal(policy.premium_per_period))
            k = max(schedule.paid_periods, schedule.k_max + 1)
            while k < schedule.total_periods:
                due = add_months(policy.effective_date, k * period_months)
                if due > horizon:
                    break
                if due >= today:
                    upcoming.append(
                        {
                            "policyId": policy.id,
                            "productName": policy.product_name,
                            "company": policy.company,
                            "dueDate": due,
                            "periodIndex": k + 1,
                            "amount": _num(premium),
                        }
                    )
                k += 1

        # ---- 到期预警 / 已过期 ----
        if schedule is not None and schedule.coverage_end_date is not None:
            remain = (schedule.coverage_end_date - today).days
            if in_force and 0 <= remain <= EXPIRY_SOON_DAYS:
                expiring.append(
                    {
                        "policyId": policy.id,
                        "productName": policy.product_name,
                        "company": policy.company,
                        "coverageEndDate": schedule.coverage_end_date,
                        "daysLeft": remain,
                    }
                )
            elif is_expired:
                expired.append(
                    {
                        "policyId": policy.id,
                        "productName": policy.product_name,
                        "company": policy.company,
                        "coverageEndDate": schedule.coverage_end_date,
                        "daysAgo": -remain,
                        "status": status,
                    }
                )

        # ---- 保障金额：只统计在保保单，归主被保人 ----
        if in_force:
            # 只对「金额有意义」的类别统计未定价责任；豁免/等待期这类纯文本责任不该报警
            for item in items:
                if enum_value(item.category) not in MATRIX_CATEGORIES:
                    continue
                if coverage_service.effective_amount(item, policy.insured_amount) is None:
                    unpriced += 1
            target = policy.insured_id
            amounts = _policy_category_amounts(policy, items)
            policy_categories[policy.id] = set(amounts)
            for category, amount in amounts.items():
                bucket = per_member.setdefault(target, {})
                if category == CoverageCategory.medical.value:
                    bucket[category] = max(bucket.get(category, ZERO), amount)
                else:
                    bucket[category] = bucket.get(category, ZERO) + amount

            if target in detail:
                detail[target].append(
                    {
                        "policyId": policy.id,
                        "productName": policy.product_name,
                        "company": policy.company,
                        "insuranceType": enum_value(policy.insurance_type),
                        "status": status,
                        "recorded": bool(items),
                        "items": [
                            {
                                "name": item.name,
                                "category": enum_value(item.category),
                                "amount": _num(item.amount),
                                "ratio": item.ratio,
                                "times": item.times,
                                "deductible": _num(item.deductible),
                                "note": item.note,
                                "effectiveAmount": _num(
                                    coverage_service.effective_amount(
                                        item, policy.insured_amount
                                    )
                                ),
                            }
                            for item in items
                        ],
                    }
                )

        insured_names = [
            member_map[mid].name if mid in member_map else "（已删除）"
            for mid in insured_by_policy.get(policy.id, [])
        ] or ([member_map[policy.insured_id].name] if policy.insured_id in member_map else [])

        policy_rows.append(
            {
                "id": policy.id,
                "productName": policy.product_name,
                "company": policy.company,
                "insuranceType": enum_value(policy.insurance_type),
                "status": status,
                "policyholderName": holder.name if holder else "—",
                "insuredNames": insured_names,
                "effectiveDate": policy.effective_date,
                "coverageEndDate": schedule.coverage_end_date if schedule else None,
                "annualizedPremium": _num(schedule.annualized_premium) if schedule else 0.0,
                "insuredAmount": _num(policy.insured_amount),
                "paidPeriods": schedule.paid_periods if schedule else 0,
                "totalPeriods": schedule.total_periods if schedule else 0,
                "nextDueDate": schedule.next_due_date if schedule else None,
                "coverageCount": len(items),
                "counted": in_force,
                "expired": is_expired,
            }
        )

    # ---- 概览 ----
    total_sum_insured = ZERO
    for bucket in per_member.values():
        for category, amount in bucket.items():
            if category in SUMMABLE:
                total_sum_insured += amount

    # ---- 共保类别：某成员的保障记在「别人当主被保人」的保单上 ----
    shared_categories: dict[str, set[str]] = {}
    shared_policy_count: dict[str, int] = {}
    for policy in policies:
        categories = policy_categories.get(policy.id)
        if not categories:
            continue
        for member_id in insured_by_policy.get(policy.id, []):
            if member_id == policy.insured_id:
                continue
            shared_categories.setdefault(member_id, set()).update(categories)
            shared_policy_count[member_id] = shared_policy_count.get(member_id, 0) + 1

    # ---- 成员块（含缺口）----
    member_blocks: list[dict[str, Any]] = []
    structure_gaps: list[dict[str, Any]] = []
    amount_gaps: list[dict[str, Any]] = []

    for member in members:
        bucket = per_member.get(member.id, {})
        age = age_years(member.birth_date, today) if member.birth_date else None
        is_adult = age is not None and age >= ADULT_AGE
        is_minor = age is not None and age < ADULT_AGE
        relation = enum_value(member.relation)
        is_breadwinner = relation in BREADWINNER_RELATIONS and is_adult
        shared = shared_categories.get(member.id, set())

        cells = [
            {
                "category": category,
                "amount": _num(bucket.get(category)),
                "has": category in bucket,
            }
            for category in MATRIX_CATEGORIES
        ]

        member_structure: list[str] = []
        shared_notes: list[str] = []
        for category, label in (
            (CoverageCategory.critical.value, "重疾保障"),
            (CoverageCategory.medical.value, "医疗险"),
            (CoverageCategory.accident.value, "意外保障"),
        ):
            if category in bucket:
                continue
            if category in shared:
                shared_notes.append(
                    f"{label}记在多被保人保单上，保额归主被保人，请对照条款核对本人份额"
                )
            else:
                member_structure.append(f"未配置{label}")
        if is_breadwinner and CoverageCategory.death.value not in bucket:
            if CoverageCategory.death.value in shared:
                shared_notes.append(
                    "寿险为多被保人共保，保额归主被保人，请对照条款核对本人份额"
                )
            else:
                member_structure.append("家庭经济支柱未配置寿险")

        member_amount: list[dict[str, Any]] = []
        if params.filled:
            income = params.annual_income or ZERO
            # 重疾：成年人 3 倍年收入（不低于 30 万）；未成年人按治疗+康复成本 50 万
            if is_minor:
                suggested_critical = CHILD_CRITICAL
                critical_note = "未成年人不按收入测算，按治疗与康复成本估算"
            else:
                suggested_critical = max(CRITICAL_INCOME_MULTIPLE * income, MIN_CRITICAL)
                critical_note = None
            current_critical = bucket.get(CoverageCategory.critical.value, ZERO)
            if (
                CoverageCategory.critical.value not in shared
                and current_critical < suggested_critical
            ):
                member_amount.append(
                    _gap_item("重大疾病", current_critical, suggested_critical, critical_note)
                )

            # 意外：成年人 5 倍年收入（不低于 50 万）；
            # 未成年人身故给付受监管上限约束（10 周岁以下 20 万、10–17 周岁 50 万）
            if is_minor:
                suggested_accident = CHILD_DEATH_LIMIT_YOUNG if (age or 0) < 10 else CHILD_DEATH_LIMIT_OLD
                accident_note = "按未成年人身故保险金额监管上限测算，意外伤残/医疗不受此限"
            else:
                suggested_accident = max(ACCIDENT_INCOME_MULTIPLE * income, MIN_ACCIDENT)
                accident_note = None
            current_accident = bucket.get(CoverageCategory.accident.value, ZERO)
            if (
                CoverageCategory.accident.value not in shared
                and current_accident < suggested_accident
            ):
                member_amount.append(
                    _gap_item("意外保障", current_accident, suggested_accident, accident_note)
                )

            if is_breadwinner:
                suggested_life = (params.total_debt or ZERO) + (
                    LIFE_EXPENSE_MULTIPLE * (params.annual_expense or ZERO)
                )
                if suggested_life > 0:
                    current_life = bucket.get(CoverageCategory.death.value, ZERO)
                    if (
                        CoverageCategory.death.value not in shared
                        and current_life < suggested_life
                    ):
                        member_amount.append(_gap_item("寿险", current_life, suggested_life))

        for message in member_structure:
            structure_gaps.append(
                {
                    "memberName": member.name,
                    "message": message,
                    "severity": "high" if "支柱" in message else "medium",
                }
            )
        for item in member_amount:
            amount_gaps.append({"memberName": member.name, **item})

        owned = detail.get(member.id, [])
        member_blocks.append(
            {
                "memberId": member.id,
                "name": member.name,
                "relation": relation,
                "age": age,
                "policyCount": len(owned),
                # 作为被保人但保额记在主被保人名下的**在保**保单数
                "sharedPolicyCount": shared_policy_count.get(member.id, 0),
                "annualPremium": _num(premium_by_holder.get(member.id, ZERO)),
                "sumByCategory": {key: _num(value) for key, value in bucket.items()},
                "cells": cells,
                "structureGaps": member_structure,
                "sharedNotes": shared_notes,
                "amountGaps": member_amount,
                "policies": owned,
            }
        )

    actions = _build_actions(
        today=today,
        policies=policies,
        schedules=schedules,
        structure_gaps=structure_gaps,
        amount_gaps=amount_gaps,
        upcoming=upcoming,
        expiring=expiring,
        expired=expired,
        missing=missing,
    )

    # 保费负担率：年化保费 / 家庭年收入。托管报告里最常被问的一个数，
    # 没填收入就算不出来，明确返回 null 而不是 0。
    premium_ratio = (
        float(total_annual / params.annual_income)
        if params.filled and params.annual_income
        else None
    )
    diagnosis = _build_diagnosis(
        member_blocks=member_blocks,
        params=params,
        premium_ratio=premium_ratio,
        total_sum_insured=total_sum_insured,
        missing=missing,
        expired=expired,
        total_annual=total_annual,
    )

    warnings: list[str] = []
    if missing:
        warnings.append(
            f"{missing} 张在保保单尚未录入「保障责任」，已按险种与保额粗算，缺口结论仅供参考。"
        )
    if unpriced:
        warnings.append(
            f"{unpriced} 条保障责任既没填金额、也没填比例或保单保额，这部分金额按 0 计，"
            "合计可能偏低。"
        )
    if not params.filled:
        warnings.append("未填写家庭年收入，额度缺口未计算（只做结构缺口判断）。")
    if expired:
        warnings.append(
            f"{len(expired)} 张保单已过保障期但状态仍为「生效中」，未计入保障与保费合计："
            + "、".join(row["productName"] for row in expired[:5])
            + ("…" if len(expired) > 5 else "")
        )
    ended = [p for p in policies if enum_value(p.status) not in INSURED_STATUSES]
    if ended:
        warnings.append(
            f"{len(ended)} 张保单已退保 / 已失效，未计入保障与保费合计："
            + "、".join(p.product_name for p in ended[:5])
            + ("…" if len(ended) > 5 else "")
        )
    multi_insured = [
        p.product_name for p in policies if len(insured_by_policy.get(p.id, [])) > 1
    ]
    if multi_insured:
        warnings.append(
            "多被保人保单的保额统一归到主被保人名下："
            + "、".join(multi_insured[:5])
            + ("…" if len(multi_insured) > 5 else "")
        )

    data_updated = _data_updated_at(db)
    return {
        "meta": {
            "reportNo": _report_no(today),
            "generatedAt": datetime.now().replace(microsecond=0),
            "dataUpdatedAt": data_updated.replace(microsecond=0)
            if data_updated is not None
            else None,
            "version": __version__,
            "familyName": _family_name(members),
        },
        "params": params.as_dict(),
        "overview": {
            "memberCount": len(members),
            "policyCount": len(policies),
            "activeCount": sum(1 for p in policies if enum_value(p.status) == STATUS_ACTIVE),
            "inForceCount": sum(1 for row in policy_rows if row["counted"]),
            "expiredCount": len(expired),
            "endedCount": len(ended),
            "annualPremium": _num(total_annual),
            "totalSumInsured": _num(total_sum_insured),
            #: 保费负担率 = 年化保费 / 家庭年收入（未填收入为 null）
            "premiumRatio": premium_ratio,
            "coverageRecorded": recorded,
            "coverageMissing": missing,
        },
        "matrixCategories": list(MATRIX_CATEGORIES),
        "diagnosis": diagnosis,
        "members": member_blocks,
        "premium": {
            "byMember": _ratio_rows(
                [(member_map[mid].name, amount) for mid, amount in premium_by_holder.items()],
                total_annual,
            ),
            "byType": _ratio_rows(list(premium_by_type.items()), total_annual),
            "byMonth": [
                {"month": month, "amount": _num(amount)}
                for month, amount in sorted(premium_by_month.items())
            ],
        },
        "policies": sorted(
            policy_rows,
            key=lambda row: (row["coverageEndDate"] is None, row["effectiveDate"]),
        ),
        "paymentPlan": {
            "items": sorted(upcoming, key=lambda row: (row["dueDate"], row["policyId"])),
            "total": (_num(sum((Decimal(str(row["amount"])) for row in upcoming), ZERO))),
            "horizonDays": PLAN_HORIZON_DAYS,
        },
        "expiring": expiring,
        "expired": expired,
        "gaps": {"structure": structure_gaps, "amount": amount_gaps},
        "actions": actions,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _gap_item(
    name: str, current: Decimal, suggested: Decimal, note: Optional[str] = None
) -> dict[str, Any]:
    gap = suggested - current
    ratio = float(gap / suggested) if suggested else 0.0
    return {
        "item": name,
        "current": _num(current),
        "suggested": _num(suggested),
        "gap": _num(gap),
        "severity": "high" if ratio >= 0.5 else "medium",
        "note": note,
    }


def _ratio_rows(pairs: list[tuple[str, Decimal]], total: Decimal) -> list[dict[str, Any]]:
    rows = [
        {
            "name": name,
            "amount": _num(amount),
            "ratio": float(amount / total) if total else 0.0,
        }
        for name, amount in pairs
        if amount and amount > 0
    ]
    return sorted(rows, key=lambda row: row["amount"], reverse=True)


def _family_name(members: list[Member]) -> str:
    """用「本人」的姓名拼一个中性名字，如「张伟家庭」。"""
    for member in members:
        if enum_value(member.relation) == Relation.self_.value:
            return f"{member.name}家庭"
    return "我的家庭"


#: 保费负担率的经验区间（占家庭年收入）。5%–15% 是行业里常被引用的合理带，
#: 低于 5% 往往意味着保障买得不够，高于 15% 则可能挤压日常现金流。
RATIO_LOW = 0.05
RATIO_HIGH = 0.15


def _names(items: list[str], limit: int = 4) -> str:
    """「张三、李四、王五 等 6 人」这种列举文本。"""
    head = "、".join(items[:limit])
    return head + (f" 等 {len(items)} 人" if len(items) > limit else "")


def _build_diagnosis(
    *,
    member_blocks: list[dict[str, Any]],
    params: ReportParams,
    premium_ratio: Optional[float],
    total_sum_insured: Decimal,
    missing: int,
    expired: list[dict[str, Any]],
    total_annual: Decimal,
) -> list[dict[str, Any]]:
    """把汇总数字翻译成几句能读的话。

    原则：**只说数据支持的结论**。没有收入就不评价保费贵不贵，
    没录责任就不假装缺口很准——报告的可信度靠这条守住。

    ``tone``：``warn``（要处理）/ ``good``（没问题）/ ``info``（说明性）。
    """
    items: list[dict[str, Any]] = []
    blocks = [b for b in member_blocks]
    if not blocks:
        return [
            {
                "tone": "info",
                "title": "还没有可分析的数据",
                "text": "先在「家庭成员」里添加成员、在「保单列表」里录入保单，报告才有内容。",
            }
        ]

    # ① 保障覆盖：谁完全没有重疾 / 医疗
    no_critical = [b["name"] for b in blocks if CoverageCategory.critical.value not in b["sumByCategory"]]
    no_medical = [b["name"] for b in blocks if CoverageCategory.medical.value not in b["sumByCategory"]]
    covered = len(blocks) - len(no_critical)
    if no_critical:
        items.append(
            {
                "tone": "warn",
                "title": "重疾保障没有覆盖到每个人",
                "text": (
                    f"{len(blocks)} 名家庭成员中 {covered} 人有重疾保障，"
                    f"{_names(no_critical)} 目前没有。重疾险是家庭保障的地基，"
                    "一次大病动辄几十万支出，缺口比保费更难扛。"
                ),
            }
        )
    else:
        items.append(
            {
                "tone": "good",
                "title": "重疾保障已覆盖全部家庭成员",
                "text": f"{len(blocks)} 人都有重疾保障，接下来重点看额度是否够（见第二章）。",
            }
        )
    if no_medical:
        items.append(
            {
                "tone": "warn",
                "title": "有人没有医疗险",
                "text": (
                    f"{_names(no_medical)} 没有医疗险。百万医疗一年几百元、能报大额住院费，"
                    "是性价比最高的一层，建议优先补齐。"
                ),
            }
        )

    # ② 保费负担率
    if premium_ratio is None:
        items.append(
            {
                "tone": "info",
                "title": "保费负担暂无法评估",
                "text": "填写家庭年收入后，可以看出保费占收入的比例是否合理（经验区间 5%–15%）。",
            }
        )
    else:
        percent = premium_ratio * 100
        annual_text = f"年化保费 {_wan(float(total_annual))}，约占家庭年收入的 {percent:.1f}%"
        if premium_ratio > RATIO_HIGH:
            items.append(
                {
                    "tone": "warn",
                    "title": "保费负担偏重",
                    "text": (
                        f"{annual_text}，高于 15% 的经验上限。"
                        "若已感到缴费吃力，可优先保核心（重疾 + 医疗 + 定寿），"
                        "压缩返还型、储蓄型产品的比例。"
                    ),
                }
            )
        elif premium_ratio < RATIO_LOW:
            items.append(
                {
                    "tone": "info",
                    "title": "保费支出偏低",
                    "text": (
                        f"{annual_text}，低于 5%。"
                        "如果保障额度也没达标，说明还有加保空间；如果额度已够，说明买得划算。"
                    ),
                }
            )
        else:
            items.append(
                {
                    "tone": "good",
                    "title": "保费负担在合理区间",
                    "text": f"{annual_text}，落在 5%–15% 的经验区间内。",
                }
            )

    # ③ 身故责任是否覆盖负债
    debt = params.total_debt or ZERO
    if debt > 0:
        spouse_life = sum(
            (
                Decimal(str(b["sumByCategory"].get(CoverageCategory.death.value) or 0))
                for b in blocks
                if b["relation"] in BREADWINNER_RELATIONS
            ),
            ZERO,
        )
        if spouse_life >= debt:
            items.append(
                {
                    "tone": "good",
                    "title": "身故责任能覆盖当前负债",
                    "text": (
                        f"经济支柱的身故 / 全残保额合计 {_wan(float(spouse_life))}，"
                        f"高于负债 {_wan(float(debt))}，即使最坏情况也不至于把房贷留给孩子。"
                    ),
                }
            )
        else:
            items.append(
                {
                    "tone": "warn",
                    "title": "身故责任不足以覆盖负债",
                    "text": (
                        f"负债 {_wan(float(debt))}，经济支柱身故 / 全残保额只有 "
                        f"{_wan(float(spouse_life))}。定期寿险保费低，是最直接的补法。"
                    ),
                }
            )

    # ④ 数据完整性
    if missing:
        items.append(
            {
                "tone": "info",
                "title": "责任明细还有缺口",
                "text": (
                    f"{missing} 张保单没录保障责任，报告里只能按险种和保额粗算。"
                    "把条款里的赔付比例、次数填进去，缺口结论才站得住。"
                ),
            }
        )
    if expired:
        items.append(
            {
                "tone": "warn",
                "title": "有保障已经断了",
                "text": (
                    f"{_names([row['productName'] for row in expired])} 已过保障期但状态仍是「生效中」，"
                    "这几天先确认是否续保，别等出事才发现没保。"
                ),
            }
        )

    # ⑤ 给付型保额总览（一句话定调）
    if total_sum_insured > 0:
        items.append(
            {
                "tone": "info",
                "title": "家庭给付型保额合计",
                "text": (
                    f"{_wan(float(total_sum_insured))}（重疾 + 身故全残 + 意外，不含医疗报销限额）。"
                    "这个数字要和「负债 + 未来 5 年支出」对照着看，单看大小没有意义。"
                ),
            }
        )

    return items


def _report_no(today: date) -> str:
    return f"RP{today.strftime('%Y%m%d')}-{time.strftime('%H%M%S')}"


def _wan(value: float) -> str:
    """元 → 口语化金额：不足 1 万说「元」，其余说「万元」。"""
    if not value:
        return "0 元"
    if abs(value) < 10000:
        return f"{value:,.0f} 元"
    return f"{round(value / 10000, 1)} 万元"


def _build_actions(    *,
    today: date,
    policies: list[Policy],
    schedules: dict[str, Any],
    structure_gaps: list[dict[str, Any]],
    amount_gaps: list[dict[str, Any]],
    upcoming: list[dict[str, Any]],
    expiring: list[dict[str, Any]],
    expired: list[dict[str, Any]],
    missing: int,
) -> list[dict[str, Any]]:
    """把「数据现状」翻译成能照着做的动作，按优先级排序。"""
    actions: list[dict[str, Any]] = []

    # ① 已过期（最要命：以为有保障，其实没有）
    for row in expired:
        actions.append(
            {
                "priority": "high",
                "title": f"{row['productName']} 保障已于 {row['coverageEndDate']} 到期",
                "detail": (
                    f"已过期 {row['daysAgo']} 天但仍标记为「{row['status']}」，"
                    "未计入保障合计。确认是否已续保，或把状态改为「已失效」。"
                ),
            }
        )

    # ② 30 天内到期
    for row in expiring:
        actions.append(
            {
                "priority": "high",
                "title": (
                    f"{row['productName']} 将于 {row['coverageEndDate']} 到期"
                    f"（还剩 {row['daysLeft']} 天）"
                ),
                "detail": "短期险需续保，长期险确认是否已按约延续；到期后保障中断。",
            }
        )

    # ③ 逾期未缴
    for policy in policies:
        schedule = schedules.get(policy.id)
        if schedule is None or schedule.overdue_periods <= 0:
            continue
        if enum_value(policy.status) != STATUS_ACTIVE:
            continue
        actions.append(
            {
                "priority": "high",
                "title": f"{policy.product_name} 有 {schedule.overdue_periods} 期保费未登记",
                "detail": "若已缴费请到保单名下标记「已缴」，否则核对银行卡扣款，避免宽限期后失效。",
            }
        )

    # ④ 结构缺口
    for gap in structure_gaps:
        actions.append(
            {
                "priority": "high" if gap["severity"] == "high" else "medium",
                "title": f"{gap['memberName']}：{gap['message']}",
                "detail": "结构缺口靠加保解决。一般顺序是先医疗与意外（低保费、高杠杆），再补重疾与寿险。",
            }
        )

    # ⑤ 额度缺口（只取最大的三条，避免清单变噪音）
    for gap in sorted(amount_gaps, key=lambda item: item["gap"], reverse=True)[:3]:
        actions.append(
            {
                "priority": "medium",
                "title": f"{gap['memberName']} 的{gap['item']}缺口约 {_wan(gap['gap'])}",
                "detail": (
                    f"现有 {_wan(gap['current'])}，参考建议 {_wan(gap['suggested'])}。"
                    "参考值按行业通行经验测算，需结合家庭预算与实际体况调整。"
                ),
            }
        )

    # ⑥ 补录责任
    if missing:
        actions.append(
            {
                "priority": "low",
                "title": f"{missing} 张保单还没录保障责任",
                "detail": "在保单列表点「责任」，按模板把条款里的赔付比例/次数填进去，下次报告才准。",
            }
        )

    # ⑦ 未来 30 天待缴
    soon = [row for row in upcoming if (row["dueDate"] - today).days <= 30]
    if soon:
        total_soon = sum((float(row["amount"]) for row in soon), 0.0)
        actions.append(
            {
                "priority": "medium",
                "title": f"未来 30 天有 {len(soon)} 笔保费待缴",
                "detail": f"合计约 {_wan(total_soon)}，提前确认扣款账户余额。",
            }
        )

    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(actions, key=lambda item: order.get(item["priority"], 3))


__all__ = [
    "DEFAULTS",
    "MATRIX_CATEGORIES",
    "PLAN_HORIZON_DAYS",
    "ReportParams",
    "SUMMABLE",
    "build_report",
    "get_params",
    "save_params",
]
