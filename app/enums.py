"""业务枚举定义（唯一真源）。

约定：枚举成员名用英文（便于 Python 侧书写），**枚举值（value）为中文字符串**，
即数据库与 API 中传输的字符串。数据库列使用
``Enum(Xxx, native_enum=False, length=..., values_callable=...)``，
实际存储枚举的 ``value``（中文）。

注意（v1.3 修正）：SQLAlchemy 2.x 的 ``native_enum=False`` 下 ``create_constraint``
**默认 False**，故列实际为纯 ``VARCHAR``，**不附带 CHECK 约束**；新增枚举值因此
**零迁移**（无需重建既有表）。
"""

from __future__ import annotations

from enum import Enum


def _values(enum_cls: type[Enum]) -> list[str]:
    """返回枚举成员的中文值列表，供 SQLAlchemy ``values_callable`` 使用。"""
    return [member.value for member in enum_cls]


class InsuranceType(str, Enum):
    """险种。"""

    critical_illness = "重疾险"
    medical = "医疗险"
    accident = "意外险"
    life = "寿险"
    annuity = "年金险"
    dividend = "分红险"  # v1.3 新增（插在「年金险」之后，共 10 值）
    education = "教育金"
    auto = "车险"
    property = "财产险"
    other = "其他"


class PolicyStatus(str, Enum):
    """保单状态。"""

    active = "生效中"
    paid_off = "已缴清"
    surrendered = "已退保"
    lapsed = "已失效"


class CoverageTermType(str, Enum):
    """保障期限类型（保多久）。"""

    whole_life = "终身"
    fixed_years = "固定年限"
    to_age = "至某年龄"


class PaymentTermType(str, Enum):
    """缴费期限类型（交多久）。"""

    single = "趸交"
    fixed_years = "固定年限"
    to_age = "交至某年龄"


class PaymentFrequency(str, Enum):
    """缴费方式。"""

    monthly = "月缴"
    yearly = "年缴"


class Relation(str, Enum):
    """与本人关系。"""

    self_ = "本人"
    spouse = "配偶"
    child = "子女"
    father = "父亲"
    mother = "母亲"
    other = "其他"


class Gender(str, Enum):
    """性别。"""

    male = "男"
    female = "女"
    unknown = "未知"


class CoverageCategory(str, Enum):
    """保障责任类别（v1.10）。

    这是「条款级责任拆解」的落点：一张保单的保额往往不是单一数字，
    重疾赔 100%、中症赔 60%×2、轻症赔 30%×3，混在一起会把缺口算错。
    因此每条责任单独记类别，汇总时**只把同类可加总的项相加**。
    """

    critical = "重大疾病"
    medium = "中症"
    minor = "轻症"
    death = "身故全残"
    medical = "医疗"
    accident = "意外"
    annuity = "年金"
    waiver = "保费豁免"
    other = "其他"


#: 可**加总**为「给付型保额」的责任类别（重疾 / 寿险 / 意外伤残）。
#: 医疗险是报销型，限额不能叠加，只能取最大值——混在一起算会得出荒谬的总额。
SUMMABLE_CATEGORIES: tuple[str, ...] = (
    CoverageCategory.critical.value,
    CoverageCategory.death.value,
    CoverageCategory.accident.value,
)


# 关系展示排序权重（本人 0 → 其他 5）
RELATION_ORDER: dict[str, int] = {
    Relation.self_.value: 0,
    Relation.spouse.value: 1,
    Relation.child.value: 2,
    Relation.father.value: 3,
    Relation.mother.value: 4,
    Relation.other.value: 5,
}


__all__ = [
    "InsuranceType",
    "PolicyStatus",
    "CoverageTermType",
    "PaymentTermType",
    "PaymentFrequency",
    "Relation",
    "Gender",
    "CoverageCategory",
    "SUMMABLE_CATEGORIES",
    "RELATION_ORDER",
    "_values",
]
