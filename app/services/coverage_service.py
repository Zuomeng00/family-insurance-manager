"""保障责任明细（v1.10）。

一条保单的「保额」往往不是一个数字：重疾赔 100%、中症 60% 赔 2 次、轻症 30% 赔 3 次、
身故另算。经纪公司的托管报告之所以值钱，就是把这堆条款拆成一张能加总的表。

本模块负责：

1. **按险种给模板**——录一张保单不用从零想「该有哪些责任」；
2. **全量替换式保存**——前端一次提交整张责任表，后端整体覆盖（比逐条 diff 简单可靠）；
3. **有效金额换算**——``ratio`` 按保单基本保额折算成钱，供报告汇总。

设计判断
--------
- **不做 PDF/条款自动提取**：家用场景下，正则从合同里抓「100%」「60%」的准确率
  不足以支撑理赔判断，逐条核对的时间比直接录入还长。宁可让人录，也不能给错数。
- **医疗险限额不参与加总**：报销型限额（200 万 / 400 万）叠加会得出荒谬的「总保额」，
  汇总时单列。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..enums import CoverageCategory, InsuranceType
from ..errors import AppError
from ..models import Policy, PolicyCoverage

#: 金额上限（元）：10 亿元。防手抖多打几个 0，也防脏数据把汇总撑爆。
MAX_AMOUNT = Decimal("1000000000")
#: 比例上限（%）：意外险有 200% 赔付的产品，留足余量。
MAX_RATIO = 500
#: 单张保单的责任项上限。
MAX_ITEMS = 40


def _yuan(value: str | int) -> Decimal:
    """构造金额字面量（项目约定：**API 与模型层一律用「元」，只有库里存「分」**）。"""
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# 模板：按险种预置常见责任项
# ---------------------------------------------------------------------------
# 说明：这里的默认值只是「填个样子」，落库后用户可任意改；
# 金额一律按常见产品形态给，不对应任何具体产品。
TEMPLATES: dict[str, list[dict[str, Any]]] = {
    InsuranceType.critical_illness.value: [
        {"name": "重大疾病保险金", "category": CoverageCategory.critical, "ratio": 100},
        {"name": "中症疾病保险金", "category": CoverageCategory.medium, "ratio": 60, "times": 2},
        {"name": "轻症疾病保险金", "category": CoverageCategory.minor, "ratio": 30, "times": 3},
        {"name": "身故保险金", "category": CoverageCategory.death, "ratio": 100},
        {"name": "全残保险金", "category": CoverageCategory.death, "ratio": 100},
        {"name": "疾病终末期保险金", "category": CoverageCategory.critical, "ratio": 100},
        {
            "name": "保费豁免",
            "category": CoverageCategory.waiver,
            "note": "确诊轻症及以上豁免后续保费",
        },
        {"name": "等待期", "category": CoverageCategory.other, "note": "90 天或 180 天，以条款为准"},
    ],
    InsuranceType.medical.value: [
        {
            "name": "一般医疗保险金",
            "category": CoverageCategory.medical,
            "amount": _yuan(2_000_000),
            "deductible": _yuan(10_000),
            "ratio": 100,
        },
        {
            "name": "重大疾病医疗保险金",
            "category": CoverageCategory.medical,
            "amount": _yuan(4_000_000),
            "deductible": 0,
            "ratio": 100,
        },
        {
            "name": "质子重离子医疗",
            "category": CoverageCategory.medical,
            "amount": _yuan(1_000_000),
            "deductible": 0,
            "ratio": 100,
        },
        {"name": "门诊手术医疗", "category": CoverageCategory.medical, "ratio": 100},
        {
            "name": "住院前后门急诊",
            "category": CoverageCategory.medical,
            "note": "住院前 7 天、出院后 30 天",
        },
        {"name": "保证续保", "category": CoverageCategory.other, "note": "如 6 年保证续保，以条款为准"},
    ],
    InsuranceType.accident.value: [
        {
            "name": "意外身故/伤残",
            "category": CoverageCategory.accident,
            "amount": _yuan(1_000_000),
        },
        {
            "name": "意外医疗",
            "category": CoverageCategory.medical,
            "amount": _yuan(50_000),
            "deductible": _yuan(100),
            "ratio": 100,
        },
        {
            "name": "交通意外额外给付",
            "category": CoverageCategory.accident,
            "note": "航空/轨道交通等额外赔付倍数以条款为准",
        },
        {"name": "意外住院津贴", "category": CoverageCategory.accident, "note": "如 150 元/天"},
        {"name": "猝死保障", "category": CoverageCategory.death, "note": "部分产品含，留意等待期"},
    ],
    InsuranceType.life.value: [
        {"name": "身故保险金", "category": CoverageCategory.death, "ratio": 100},
        {"name": "全残保险金", "category": CoverageCategory.death, "ratio": 100},
        {"name": "等待期", "category": CoverageCategory.other, "note": "通常 90 天"},
        {
            "name": "免责条款",
            "category": CoverageCategory.other,
            "note": "故意犯罪、两年内自杀等，以条款为准",
        },
    ],
    InsuranceType.annuity.value: [
        {"name": "生存金", "category": CoverageCategory.annuity, "note": "领取年龄与金额以条款为准"},
        {"name": "满期金", "category": CoverageCategory.annuity},
        {"name": "身故保险金", "category": CoverageCategory.death},
    ],
}
# 分红险 / 教育金 与年金险同构，共用一套模板
TEMPLATES[InsuranceType.dividend.value] = TEMPLATES[InsuranceType.annuity.value]
TEMPLATES[InsuranceType.education.value] = TEMPLATES[InsuranceType.annuity.value]


def template_for(insurance_type: str) -> list[dict[str, Any]]:
    """取某险种的模板；没有对应模板时返回空列表（如车险、财产险）。"""
    return [
        {
            "name": item["name"],
            "category": item["category"].value,
            "amount": item.get("amount"),
            "ratio": item.get("ratio"),
            "times": item.get("times"),
            "deductible": item.get("deductible"),
            "note": item.get("note"),
        }
        for item in TEMPLATES.get(insurance_type, [])
    ]


def all_templates() -> dict[str, list[dict[str, Any]]]:
    """全量模板字典（前端一次取全后缓存）。"""
    return {name: template_for(name) for name in TEMPLATES}


def categories() -> list[str]:
    """全部责任类别（前端下拉框）。"""
    return [item.value for item in CoverageCategory]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _clean_amount(value: Any, field: str) -> Optional[Decimal]:
    """金额统一按**元**接收（与保单保费的口径一致），落库时由 ``Money`` 转成分。"""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AppError(422, "VALIDATION_ERROR", f"{field}必须是数字", {"field": field})
    if amount < 0 or amount > MAX_AMOUNT:
        raise AppError(422, "VALIDATION_ERROR", f"{field}超出合理范围", {"field": field})
    return amount.quantize(Decimal("0.01"))


def _clean_ratio(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        ratio = int(value)
    except (TypeError, ValueError):
        raise AppError(
            422, "VALIDATION_ERROR", "赔付比例必须是整数百分比", {"field": "ratio"}
        )
    if ratio < 0 or ratio > MAX_RATIO:
        raise AppError(
            422, "VALIDATION_ERROR", f"赔付比例须在 0–{MAX_RATIO}% 之间", {"field": "ratio"}
        )
    return ratio


def _clean_times(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        times = int(value)
    except (TypeError, ValueError):
        raise AppError(422, "VALIDATION_ERROR", "赔付次数必须是整数", {"field": "times"})
    if times < 1 or times > 99:
        raise AppError(422, "VALIDATION_ERROR", "赔付次数须在 1–99 之间", {"field": "times"})
    return times


def _clean_category(value: Any) -> str:
    text = str(value or "").strip()
    if text not in categories():
        raise AppError(
            422,
            "VALIDATION_ERROR",
            f"责任类别不合法：{text or '（空）'}",
            {"field": "category"},
        )
    return text


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def require_policy(db: Session, policy_id: str) -> Policy:
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise AppError(404, "NOT_FOUND", "保单不存在", {"policyId": policy_id})
    return policy


def list_for_policy(db: Session, policy_id: str) -> list[PolicyCoverage]:
    """按录入顺序返回一张保单的责任项。"""
    return (
        db.query(PolicyCoverage)
        .filter(PolicyCoverage.policy_id == policy_id)
        .order_by(PolicyCoverage.sort_order, PolicyCoverage.id)
        .all()
    )


def replace_all(db: Session, policy_id: str, items: list[dict[str, Any]]) -> list[PolicyCoverage]:
    """**全量替换**一张保单的责任项。

    选全量替换而不是逐条增删改：前端就是一张可增删的表格，
    整体覆盖最不容易出现「删了没删掉、改了一半」的中间态。
    """
    require_policy(db, policy_id)
    if len(items) > MAX_ITEMS:
        raise AppError(
            422, "VALIDATION_ERROR", f"单张保单最多 {MAX_ITEMS} 条责任项", {"field": "items"}
        )

    cleaned: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        name = str(raw.get("name") or "").strip()
        if not name:
            raise AppError(
                422, "VALIDATION_ERROR", f"第 {index + 1} 行的责任名称不能为空", {"field": "name"}
            )
        if len(name) > 40:
            raise AppError(
                422, "VALIDATION_ERROR", f"第 {index + 1} 行的责任名称过长（≤40 字）", {"field": "name"}
            )
        note = raw.get("note")
        note = str(note).strip()[:120] if note else None
        cleaned.append(
            {
                "category": _clean_category(raw.get("category")),
                "name": name,
                "amount": _clean_amount(raw.get("amount"), "保障金额"),
                "ratio": _clean_ratio(raw.get("ratio")),
                "times": _clean_times(raw.get("times")),
                "deductible": _clean_amount(raw.get("deductible"), "免赔额"),
                "note": note or None,
                "sort_order": index,
            }
        )

    db.query(PolicyCoverage).filter(PolicyCoverage.policy_id == policy_id).delete(
        synchronize_session=False
    )
    for item in cleaned:
        db.add(PolicyCoverage(policy_id=policy_id, **item))
    db.commit()
    return list_for_policy(db, policy_id)


# ---------------------------------------------------------------------------
# 汇总辅助
# ---------------------------------------------------------------------------
def effective_amount(
    coverage: PolicyCoverage, insured_amount: Optional[Decimal]
) -> Optional[Decimal]:
    """一条责任的有效金额（**元**）。

    ``amount`` 优先（已折算好的固定金额）；否则用 ``ratio`` × 保单基本保额。
    两者都没有（如「保费豁免」「等待期」这类纯文本责任）返回 ``None``。
    """
    if coverage.amount:
        return coverage.amount
    if coverage.ratio and insured_amount:
        return (Decimal(insured_amount) * Decimal(coverage.ratio) / Decimal(100)).quantize(
            Decimal("0.01")
        )
    return None


__all__ = [
    "MAX_AMOUNT",
    "MAX_ITEMS",
    "MAX_RATIO",
    "TEMPLATES",
    "all_templates",
    "categories",
    "effective_amount",
    "list_for_policy",
    "require_policy",
    "replace_all",
    "template_for",
]
