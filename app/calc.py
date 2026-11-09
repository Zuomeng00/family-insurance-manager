"""核心计算模块（纯函数，无 IO、无副作用）。

实现 ARCHITECTURE.md 第 5 节的算法：
- ``age_years`` / ``add_years`` / ``add_months`` / ``money`` / ``pct`` 工具函数；
- ``compute_k_max`` 期次计算（含 0.4 的 k_max 修正）；
- ``calc_schedule`` 保单派生量计算；
- ``enumerate_upcoming`` 近 N 天待缴期次枚举。

约定：内部期次 ``k`` 为 **0 基**；对外展示期次统一为 ``k + 1``（1 基）。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Optional

# 每年期数：月缴 12，年缴 1
MONTHLY = "月缴"
YEARLY = "年缴"

# 缴费期限 / 保障期限类型的中文枚举值
SINGLE = "趸交"
FIXED_YEARS = "固定年限"
TO_AGE_PAY = "交至某年龄"
WHOLE_LIFE = "终身"
TO_AGE_COVER = "至某年龄"

STATUS_ACTIVE = "生效中"
# 「在保」状态集合（生效中 / 已缴清）——「当年未缴费金额」枚举仅纳入这两类；
# 已退保 / 已失效 → 无需缴费，直接返回空。
INSURED_STATUSES = ("生效中", "已缴清")


def enum_value(value: Any) -> Any:
    """将（可能为 Enum 的）值归一化为其底层字符串值。"""
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True)
class Schedule:
    """单张保单的派生计算结果。"""

    annualized_premium: Decimal
    total_periods: int
    paid_periods: int
    cumulative_paid: Decimal
    remaining_periods: int
    is_paid_off: bool
    progress_percent: Decimal
    in_coverage: bool
    #: 保障结束日；``None`` = 终身（不会到期）。用于续保提醒。
    coverage_end_date: Optional[date]
    next_due_date: Optional[date]
    k_max: int
    # ---------- 缴费记录（手动覆盖）相关 ----------
    #: 用户手动记录的已缴期数；``None`` = 按生效日自动推算。
    paid_periods_override: Optional[int] = None
    #: 仅按时间推算的「应缴期数」（不含手动记录），用于对比得出逾期。
    expected_periods_to_date: int = 0
    #: 逾期期数 = ``max(0, expected_periods_to_date - paid_periods)``。
    overdue_periods: int = 0


@dataclass(frozen=True)
class DueItem:
    """一笔待缴期次。"""

    policy_id: str
    due_date: date
    period_index: int
    total_periods: int
    amount: Decimal


# ---------------------------------------------------------------------------
# 5.1 工具函数
# ---------------------------------------------------------------------------
def age_years(birth: date, on: date) -> int:
    """返回 ``on`` 时点的「已满周岁」。"""
    years = on.year - birth.year
    if (on.month, on.day) < (birth.month, birth.day):
        years -= 1
    return years


def add_years(d: date, n: int) -> date:
    """日期加 ``n`` 年；2/29 溢出时回退到 2/28。"""
    try:
        return date(d.year + n, d.month, d.day)
    except ValueError:
        return date(d.year + n, d.month, 28)


def add_months(d: date, n: int) -> date:
    """日期加 ``n`` 个月，处理月末溢出。

    当日序数超出目标月天数时，钳制到该月最后一天。
    例：``add_months(2020-01-31, 1) == 2020-02-29``；
        ``add_months(2021-01-31, 1) == 2021-02-28``。
    """
    total = d.year * 12 + (d.month - 1) + n
    year, month_index = divmod(total, 12)
    month = month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def money(x: Any) -> Decimal:
    """金额规整：转为 ``Decimal`` 并四舍五入保留 2 位小数。"""
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def pct(num: Any, den: Any) -> Decimal:
    """百分比（1 位小数）；分母为 0 时返回 100.0。"""
    if den == 0:
        return Decimal("100.0")
    return (Decimal(str(num)) / Decimal(str(den)) * 100).quantize(
        Decimal("0.1"), ROUND_HALF_UP
    )


def allocate_amounts(total: Any, n: int) -> list[Decimal]:
    """把 ``total`` 均摊到 ``n`` 份：保留 2 位小数，**尾差归最后一份**，保证 ``Σ = total``。

    约定（唯一真源，见 ARCHITECTURE-v13-increment §3.6）：

    - 全程 ``Decimal`` + ``ROUND_HALF_UP`` + 2 位小数（复用 ``money()`` 量化）；
    - ``base = money(total / n)``，前 ``n-1`` 份 = ``base``，
      **最后一份 = ``total - base * (n - 1)``**（尾差归最后一份）；
    - 调用方须按「规范排序」传入被保人，故最后一份即「排序最后一名被保人」；
    - 不变式：``Σ allocate_amounts(total, n) == money(total)``（对任意 ``n ≥ 1`` 成立）。

    Args:
        total: 待分摊总额（可为 ``Decimal`` / 数字 / 数字字符串）。
        n: 份数。

    Returns:
        长度 ``n`` 的 ``Decimal`` 列表；``n <= 0`` 返回 ``[]``；``n == 1`` 恒等返回 ``[money(total)]``。
    """
    total = money(total)
    if n <= 0:
        return []
    if n == 1:
        return [total]
    base = money(total / n)  # 每份基础额（ROUND_HALF_UP，2 位小数）
    shares = [base] * n
    shares[-1] = money(total - base * (n - 1))  # 尾差归最后一份
    return shares


# ---------------------------------------------------------------------------
# 5.2 k_max 计算（含 0.4 修正）
# ---------------------------------------------------------------------------
def compute_k_max(effective: date, freq: str, today: date) -> int:
    """返回满足 ``due(k) < today`` 的最大期次序号 ``k``（0 基），无则 ``-1``。

    缴费日定义（唯一真源）::

        due(k) = add_months(effective, k * period_months)

    其中月缴 ``period_months = 1``、年缴 ``period_months = 12``。

    语义（**严格小于**）::

        k_max = max{ k >= 0 : due(k) < today }

    即到期日恰为 ``today`` 的期次**不计入**已到期（视为当天待缴），
    从而 ``next_due_date`` 允许等于 ``today``。

    修正（0.4）：``effective >= today``（生效日不早于今天）时返回 ``-1``，
    从而 ``已到期期数 = max(0, min(k_max + 1, 总期数)) = 0``。

    实现说明：``due(k)`` 关于 ``k`` 单调不减，故先给出忽略月末钳制的估计值，
    再向前 / 向后校正到精确边界（钳制最多造成 1 期偏差，循环为 O(1)）。
    """
    if effective >= today:
        return -1

    period_months = 1 if freq == MONTHLY else 12

    # 初始估计（忽略月末钳制）：先取「月差」，再按周期长度换算为期次。
    month_span = (today.year - effective.year) * 12 + (today.month - effective.month)
    k = month_span // period_months
    if k < 0:
        k = 0

    # 校正上界：确保 due(k + 1) >= today（否则 k 还不是最大值）。
    while add_months(effective, (k + 1) * period_months) < today:
        k += 1
    # 校正下界：确保 due(k) < today（k = 0 时 due(0) = effective < today 恒成立）。
    while k >= 0 and add_months(effective, k * period_months) >= today:
        k -= 1
    return k


def due_date_for_period(effective: date, freq: str, period_index: int) -> date:
    """返回第 ``period_index``（**1 基**）期的应缴日。

    与 ``compute_k_max`` 同源，满足 ``due_date_for_period(eff, freq, k + 1) == due(k)``；
    趸交（``period_index == 1``）返回生效日本身。
    """
    period_months = 1 if freq == MONTHLY else 12
    return add_months(effective, (period_index - 1) * period_months)


# ---------------------------------------------------------------------------
# 5.3 calc_schedule
# ---------------------------------------------------------------------------
def calc_schedule(policy: Any, insured: Any, today: date) -> Schedule:
    """计算单张保单的全部派生量。

    Args:
        policy: 具备保单字段的对象（ORM 实例或同名属性的简单对象）。
        insured: 被保人对象（需含 ``birth_date``）。
        today: 计算基准日。

    Returns:
        ``Schedule`` 派生结果。
    """
    freq = enum_value(policy.payment_frequency)
    per_year = 12 if freq == MONTHLY else 1
    eff = policy.effective_date
    premium = Decimal(str(policy.premium_per_period))
    ptt = enum_value(policy.payment_term_type)
    ctt = enum_value(policy.coverage_term_type)

    # ---------- 1) 年化保费 ----------
    if ptt == SINGLE:
        annualized = Decimal("0")
    elif freq == YEARLY:
        annualized = premium
    else:  # 月缴
        annualized = premium * 12

    # ---------- 2) 总缴费期数 ----------
    if ptt == SINGLE:
        total_periods = 1
    elif ptt == FIXED_YEARS:
        total_periods = int(policy.payment_term_value) * per_year
    else:  # 交至某年龄 A
        age_at_eff = age_years(insured.birth_date, eff)
        total_periods = max(0, int(policy.payment_term_value) - age_at_eff) * per_year

    # ---------- 3) 期次：应缴 / 已缴 / 逾期 ----------
    # ``k_max`` 语义见 compute_k_max：严格 due(k) < today 才算已到期；
    # 趸交的唯一一期缴费日即生效日 due(0) = effective。
    if ptt == SINGLE:
        k_max = 0 if eff < today else -1
    else:
        k_max = compute_k_max(eff, freq, today)

    # 仅按时间推算的「应缴期数」（不含手动记录）
    expected_periods = max(0, min(k_max + 1, total_periods))

    # 已缴期数：手动记录优先（``None`` = 按时间自动推算，兼容既有数据）
    raw_override = getattr(policy, "paid_periods_override", None)
    override = int(raw_override) if raw_override is not None else None
    if override is None:
        paid_periods = expected_periods
    else:
        # 防御性钳制：手动值可能超出实际总期数（如「交至某年龄」依赖被保人生日）
        paid_periods = max(0, min(override, total_periods))

    # 逾期期数：应缴 > 已缴 的部分。手动记录少于应缴即说明这些期次过了缴费日却没交，
    # 必须单独暴露，否则用户漏缴了反而在界面上「看不见」。
    overdue_periods = max(0, expected_periods - paid_periods)

    # ---------- 4) 其余派生量 ----------
    remaining = max(0, total_periods - paid_periods)
    is_paid_off = remaining == 0
    progress = pct(paid_periods, total_periods)
    cumulative = money(Decimal(paid_periods) * premium)

    # ---------- 5) 是否在保障期内 / 保障结束日 ----------
    if ctt == WHOLE_LIFE:
        coverage_end: Optional[date] = None  # 终身：无到期日
        in_coverage = True
    else:
        if ctt == FIXED_YEARS:
            coverage_end = add_years(eff, int(policy.coverage_term_value))
        else:  # 至某年龄 A
            coverage_end = add_years(insured.birth_date, int(policy.coverage_term_value))
        in_coverage = today <= coverage_end

    # ---------- 6) 下次缴费日 ----------
    # 定义为「最早未缴期次的应缴日」。手动记录少于应缴时该日期会落在过去，
    # 此时配合 ``overdue_periods > 0`` 由前端呈现为「逾期」。
    if is_paid_off:
        next_due: Optional[date] = None
    else:
        next_due = due_date_for_period(eff, freq, paid_periods + 1)

    return Schedule(
        annualized_premium=money(annualized),
        total_periods=total_periods,
        paid_periods=paid_periods,
        cumulative_paid=cumulative,
        remaining_periods=remaining,
        is_paid_off=is_paid_off,
        progress_percent=progress,
        in_coverage=in_coverage,
        coverage_end_date=coverage_end,
        next_due_date=next_due,
        k_max=k_max,
        paid_periods_override=override,
        expected_periods_to_date=expected_periods,
        overdue_periods=overdue_periods,
    )


# ---------------------------------------------------------------------------
# 5.4 近 N 天待缴期次枚举（闭区间）
# ---------------------------------------------------------------------------
def enumerate_upcoming(
    policy: Any,
    insured: Any,
    today: date,
    window_days: int = 60,
) -> list[DueItem]:
    """枚举 ``[today, today + window_days]`` 闭区间内的待缴期次。

    规则：仅 ``status == 生效中`` 且未缴清的保单；不含已逾期期次（``k <= k_max``）。
    """
    if enum_value(policy.status) != STATUS_ACTIVE:
        return []

    sched = calc_schedule(policy, insured, today)
    if sched.is_paid_off:
        return []

    end = today + timedelta(days=window_days)
    freq = enum_value(policy.payment_frequency)
    period_months = 1 if freq == MONTHLY else 12
    eff = policy.effective_date
    premium = Decimal(str(policy.premium_per_period))

    items: list[DueItem] = []
    # 下一未缴期次起点：
    # - 自动推算 / 逾期：``paid_periods <= k_max + 1``，起点 = ``k_max + 1``
    #   （由 k_max 语义 due(k_max) < today <= due(k_max+1) 可知下界恰落在闭区间内；
    #   逾期期次 k <= k_max 被跳过，不进入待缴窗口）；
    # - 提前缴：``paid_periods > k_max + 1``，此时 k_max+1..paid_periods-1 均已缴，
    #   必须从 ``paid_periods`` 起算，否则会把**已缴期次**错误地列为待缴。
    k = max(sched.paid_periods, sched.k_max + 1)
    guard = 0
    while guard < 4:  # 60 天窗口最多容纳 3 期（月缴），guard 兜底
        if k >= sched.total_periods:
            break
        due = add_months(eff, k * period_months)
        if due > end:
            break
        if due >= today:  # 闭区间下界（必要检查：允许 due == today 当天待缴）
            items.append(
                DueItem(
                    policy_id=policy.id,
                    due_date=due,
                    period_index=k + 1,  # 展示期次 = k+1（1 基）
                    total_periods=sched.total_periods,
                    amount=money(premium),
                )
            )
        k += 1
        guard += 1
    return items


# ---------------------------------------------------------------------------
# 5.7 当年未缴期次枚举（v1.7，现金流口径）
# ---------------------------------------------------------------------------
def enumerate_year_periods(
    policy: Any,
    insured: Any,
    today: date,
    year: Optional[int] = None,
) -> list[DueItem]:
    """枚举「缴费日落在 ``year`` 年（默认 ``today.year``）且尚未标记已缴」的期次。

    口径（唯一真源，v1.7 定稿，见 ARCHITECTURE-v17-increment §1.1 / §1.2）：

    - 纳入：``status ∈ {生效中, 已缴清}``（已退保 / 已失效 → ``[]``）；**含已逾期**期次；
    - 期次 ``k``（0 基）满足：``due(k).year == year`` 且 ``k >= paid_periods``（未缴）
      且 ``0 <= k < total_periods``；
    - 金额 = 每期保费（``premium_per_period``），**不是**年化保费；
    - **含本年度未来期次**（定义即「缴费日落在本年度」，与是否已开始缴费无关）。

    实现要点：

    - **复用** ``due_date_for_period``（内含月末钳制）计算 ``due(k)``，
      **禁止**自造日历算术；
    - **不依赖** ``k_max``（§5.2 铁律不受影响）；
    - ``due(k)`` 关于 ``k`` 单调不减 → ``due(k).year`` 亦单调不减，可安全 ``break``；
    - 复杂度 ``O(total_periods)``（≤ ~1200）。

    Args:
        policy: 具备保单字段的对象（ORM 实例或同名属性的简单对象）。
        insured: 被保人对象（需含 ``birth_date``，供 ``calc_schedule`` 使用）。
        today: 计算基准日。
        year: 目标日历年；``None`` = ``today.year``。

    Returns:
        该年度未缴期次的 ``DueItem`` 列表（按 ``k`` 升序，即按 ``due_date`` 升序）。
    """
    year = today.year if year is None else year
    if enum_value(policy.status) not in INSURED_STATUSES:
        return []

    sched = calc_schedule(policy, insured, today)
    total = sched.total_periods
    if total <= 0:
        return []

    freq = enum_value(policy.payment_frequency)
    eff = policy.effective_date
    premium = money(Decimal(str(policy.premium_per_period)))
    paid = sched.paid_periods  # 已含 paid_periods_override 语义

    items: list[DueItem] = []
    for k in range(total):
        due = due_date_for_period(eff, freq, k + 1)  # ★ 复用既有函数（含月末钳制）
        if due.year > year:
            break  # due 单调不减 → 后续年份只会更大
        if due.year < year or k < paid:
            continue
        items.append(
            DueItem(
                policy_id=policy.id,
                due_date=due,
                period_index=k + 1,  # 展示期次 = k+1（1 基）
                total_periods=total,
                amount=premium,
            )
        )
    return items


__all__ = [
    "MONTHLY",
    "YEARLY",
    "INSURED_STATUSES",
    "Schedule",
    "DueItem",
    "enum_value",
    "age_years",
    "add_years",
    "add_months",
    "money",
    "pct",
    "allocate_amounts",
    "compute_k_max",
    "due_date_for_period",
    "calc_schedule",
    "enumerate_upcoming",
    "enumerate_year_periods",
]
