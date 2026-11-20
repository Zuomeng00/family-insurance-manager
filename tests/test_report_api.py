"""保单托管报告 API 测试（v1.10）。

覆盖三层：

1. **接口契约**——``GET /api/report`` 的字段齐全、类型正确（金额是 number、日期是 ISO 串）；
2. **统计口径**——在保才计入、医疗不参与保额加总、保费按投保人归属、
   ``Σ byMonth == annualPremium``、``Σ paymentPlan.items == paymentPlan.total``；
3. **动态性**——新增 / 退保 / 到期后再次请求，结果必须立刻变（没有任何快照缓存）。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.enums import (
    CoverageCategory,
    CoverageTermType,
    Gender,
    InsuranceType,
    PaymentFrequency,
    PaymentTermType,
    PolicyStatus,
    Relation,
)
from app.models import PolicyCoverage

TODAY = date.today()


def _years_ago(years: int) -> date:
    return date(TODAY.year - years, TODAY.month, TODAY.day)


@pytest.fixture()
def family(member_factory, policy_factory):
    """一家三口 + 三张在保保单（重疾 / 医疗 / 寿险）。"""
    member_factory(
        id="m_me", name="张伟", relation=Relation.self_,
        birth_date=_years_ago(35), gender=Gender.male,
    )
    member_factory(
        id="m_wife", name="李娜", relation=Relation.spouse,
        birth_date=_years_ago(34), gender=Gender.female,
    )
    member_factory(
        id="m_kid", name="张小宝", relation=Relation.child,
        birth_date=_years_ago(6), gender=Gender.male,
    )

    policy_factory(
        id="p_critical", insurance_type=InsuranceType.critical_illness,
        policyholder_id="m_me", insured_id="m_me", effective_date=_years_ago(5),
        coverage_term_type=CoverageTermType.whole_life, coverage_term_value=None,
        payment_term_type=PaymentTermType.fixed_years, payment_term_value=20,
        payment_frequency=PaymentFrequency.yearly, premium_per_period="12000",
        insured_amount="500000", product_name="终身重疾A",
    )
    policy_factory(
        id="p_medical", insurance_type=InsuranceType.medical,
        policyholder_id="m_me", insured_id="m_me", effective_date=_years_ago(0),
        coverage_term_type=CoverageTermType.fixed_years, coverage_term_value=1,
        payment_term_type=PaymentTermType.fixed_years, payment_term_value=1,
        payment_frequency=PaymentFrequency.yearly, premium_per_period="800",
        insured_amount="2000000", product_name="百万医疗B",
    )
    policy_factory(
        id="p_life", insurance_type=InsuranceType.life,
        policyholder_id="m_me", insured_id="m_me",
        insured_ids=["m_me", "m_wife"],
        effective_date=_years_ago(2),
        coverage_term_type=CoverageTermType.fixed_years, coverage_term_value=30,
        payment_term_type=PaymentTermType.fixed_years, payment_term_value=30,
        payment_frequency=PaymentFrequency.monthly, premium_per_period="300",
        insured_amount="1000000", product_name="定期寿险C",
    )
    return {"me": "m_me", "wife": "m_wife", "kid": "m_kid"}


def _get(client) -> dict:
    response = client.get("/api/report")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 接口契约
# ---------------------------------------------------------------------------
def test_report_empty_database(client):
    """空库也要出报告，不能 500：只能提示「去录数据」。"""
    data = _get(client)
    assert data["overview"]["policyCount"] == 0
    assert data["overview"]["memberCount"] == 0
    assert data["members"] == []
    assert data["policies"] == []
    assert data["warnings"], "空库应提示补数据/补参数"


def test_report_payload_shape(client, family):
    data = _get(client)
    for key in (
        "meta", "params", "overview", "matrixCategories", "members",
        "premium", "policies", "paymentPlan", "expiring", "expired",
        "gaps", "actions", "warnings",
    ):
        assert key in data, key

    meta = data["meta"]
    assert meta["familyName"] == "张伟家庭"
    assert meta["reportNo"].startswith("RP")
    assert meta["version"]
    assert "T" in meta["generatedAt"] or " " in meta["generatedAt"]

    assert data["matrixCategories"] == ["重大疾病", "医疗", "意外", "身故全残", "年金"]

    row = next(r for r in data["policies"] if r["id"] == "p_critical")
    assert isinstance(row["annualizedPremium"], float)
    assert isinstance(row["insuredAmount"], float)
    assert row["effectiveDate"] == _years_ago(5).isoformat()

    # 金额必须是 JSON number（不是 Decimal 字符串）
    assert isinstance(data["overview"]["annualPremium"], float)


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------
def test_premium_and_sum_insured_scope(client, family):
    """医疗险计入保费，但**不**计入「总保额」；保费按投保人归属。"""
    data = _get(client)
    overview = data["overview"]

    assert overview["policyCount"] == 3
    assert overview["inForceCount"] == 3
    # 12000 + 800 + 300×12
    assert overview["annualPremium"] == pytest.approx(16400.0)
    # 重疾 50 万 + 身故 100 万（医疗 200 万不计）
    assert overview["totalSumInsured"] == pytest.approx(1_500_000.0)

    me = next(m for m in data["members"] if m["name"] == "张伟")
    assert me["sumByCategory"]["重大疾病"] == pytest.approx(500_000.0)
    assert me["sumByCategory"]["医疗"] == pytest.approx(2_000_000.0)
    assert me["sumByCategory"]["身故全残"] == pytest.approx(1_000_000.0)
    assert me["annualPremium"] == pytest.approx(16400.0)

    # 三张单的投保人都是本人 → 保费全部归到本人，配偶/子女为 0
    assert [row["name"] for row in data["premium"]["byMember"]] == ["张伟"]
    assert sum(row["amount"] for row in data["premium"]["byType"]) == pytest.approx(16400.0)


def test_premium_by_month_sums_to_annual(client, family):
    """月份分布不能漏钱：Σ byMonth 必须等于年化合计。"""
    data = _get(client)
    total = sum(row["amount"] for row in data["premium"]["byMonth"])
    assert total == pytest.approx(data["overview"]["annualPremium"])
    assert len(data["premium"]["byMonth"]) == 12


def test_payment_plan_within_horizon(client, family):
    data = _get(client)
    plan = data["paymentPlan"]
    assert plan["horizonDays"] == 365
    assert plan["items"], "月缴寿险应有待缴期次"
    assert sum(row["amount"] for row in plan["items"]) == pytest.approx(plan["total"])
    horizon = TODAY + timedelta(days=365)
    for row in plan["items"]:
        due = date.fromisoformat(row["dueDate"])
        assert TODAY <= due <= horizon


def test_multi_insured_policy_flags_shared_member(client, family):
    """夫妻共保的寿险：保额归主被保人，配偶记一条「请核对份额」而不是凭空缺口。"""
    data = _get(client)
    wife = next(m for m in data["members"] if m["name"] == "李娜")
    assert wife["sharedPolicyCount"] == 1
    assert wife["sharedNotes"], "共保成员应有核对提示"
    assert not any(gap["item"] == "寿险" for gap in wife["amountGaps"])
    assert not any(
        gap["memberName"] == "李娜" and gap["item"] == "寿险" for gap in data["gaps"]["amount"]
    )


def test_coverages_override_type_fallback(client, family, db_session):
    """录了责任表就按明细算：中症/轻症不进重疾，身故按比例折算。"""
    db_session.add_all(
        [
            PolicyCoverage(
                policy_id="p_critical", category=CoverageCategory.critical,
                name="重大疾病保险金", ratio=100, sort_order=0,
            ),
            PolicyCoverage(
                policy_id="p_critical", category=CoverageCategory.medium,
                name="中症疾病保险金", ratio=60, times=2, sort_order=1,
            ),
            PolicyCoverage(
                policy_id="p_critical", category=CoverageCategory.medical,
                name="住院医疗", amount=Decimal("300000"), sort_order=2,
            ),
            PolicyCoverage(
                policy_id="p_critical", category=CoverageCategory.medical,
                name="门诊医疗", amount=Decimal("200000"), sort_order=3,
            ),
        ]
    )
    db_session.commit()

    data = _get(client)
    me = next(m for m in data["members"] if m["name"] == "张伟")
    assert me["sumByCategory"]["重大疾病"] == pytest.approx(500_000.0)
    # 中症单独成项，不混入重疾
    assert me["sumByCategory"]["中症"] == pytest.approx(300_000.0)
    # 医疗按类别取最大值（限额不能叠加），且与另一张医疗险取最大
    assert me["sumByCategory"]["医疗"] == pytest.approx(2_000_000.0)
    assert data["policies"][0]["coverageCount"] >= 0  # 契约字段存在

    row = next(r for r in data["policies"] if r["id"] == "p_critical")
    assert row["coverageCount"] == 4

    member_policy = me["policies"][0]
    assert member_policy["recorded"] is True
    assert {item["category"] for item in member_policy["items"]} == {"重大疾病", "中症", "医疗"}


# ---------------------------------------------------------------------------
# 动态性：保单变了，报告立刻变
# ---------------------------------------------------------------------------
def test_new_policy_appears_immediately(client, family, policy_factory):
    before = _get(client)["overview"]
    assert before["policyCount"] == 3

    policy_factory(
        id="p_new", insurance_type=InsuranceType.accident,
        policyholder_id="m_me", insured_id="m_kid", effective_date=_years_ago(0),
        coverage_term_type=CoverageTermType.fixed_years, coverage_term_value=1,
        payment_term_type=PaymentTermType.fixed_years, payment_term_value=1,
        payment_frequency=PaymentFrequency.yearly, premium_per_period="300",
        insured_amount="500000", product_name="新增意外D",
    )

    after = _get(client)["overview"]
    assert after["policyCount"] == 4
    assert after["inForceCount"] == 4
    assert after["annualPremium"] == pytest.approx(before["annualPremium"] + 300.0)
    assert after["totalSumInsured"] == pytest.approx(before["totalSumInsured"] + 500_000.0)


def test_surrendered_policy_drops_out(client, family, db_session):
    before = _get(client)["overview"]

    from app.models import Policy

    policy = db_session.get(Policy, "p_critical")
    policy.status = PolicyStatus.surrendered
    db_session.commit()

    after = _get(client)["overview"]
    assert after["endedCount"] == 1
    assert after["inForceCount"] == 2
    assert after["annualPremium"] == pytest.approx(before["annualPremium"] - 12000.0)
    assert after["totalSumInsured"] == pytest.approx(before["totalSumInsured"] - 500_000.0)

    data = _get(client)
    assert any("已退保" in w for w in data["warnings"])


def test_expired_policy_excluded_and_warned(client, family, policy_factory):
    """1 年期保单过期但状态仍「生效中」：不计入合计，且必须报警。"""
    policy_factory(
        id="p_expired", insurance_type=InsuranceType.accident,
        policyholder_id="m_me", insured_id="m_kid",
        effective_date=TODAY - timedelta(days=400),
        coverage_term_type=CoverageTermType.fixed_years, coverage_term_value=1,
        payment_term_type=PaymentTermType.fixed_years, payment_term_value=1,
        payment_frequency=PaymentFrequency.yearly, premium_per_period="300",
        insured_amount="200000", product_name="过期意外E",
    )

    data = _get(client)
    assert data["overview"]["expiredCount"] == 1
    assert [row["productName"] for row in data["expired"]] == ["过期意外E"]
    assert data["overview"]["annualPremium"] == pytest.approx(16400.0)
    assert not next(r for r in data["policies"] if r["id"] == "p_expired")["counted"]
    assert any("已过保障期" in w for w in data["warnings"])
    assert any("到期" in action["title"] for action in data["actions"])


# ---------------------------------------------------------------------------
# 测算参数
# ---------------------------------------------------------------------------
def test_params_partial_update_and_clear(client, family):
    response = client.put("/api/report/params", json={"annualIncome": 300000})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "annualIncome": 300000.0,
        "totalDebt": None,
        "annualExpense": None,
        "filled": True,
    }

    # 只改负债，年收入不能丢
    client.put("/api/report/params", json={"totalDebt": 800000})
    data = _get(client)["params"]
    assert data["annualIncome"] == pytest.approx(300000.0)
    assert data["totalDebt"] == pytest.approx(800000.0)

    # 传 null = 清空
    client.put("/api/report/params", json={"totalDebt": None})
    data = _get(client)["params"]
    assert data["totalDebt"] is None
    assert data["annualIncome"] == pytest.approx(300000.0)


def test_params_reject_negative(client):
    response = client.put("/api/report/params", json={"annualIncome": -1})
    assert response.status_code == 422


def test_amount_gap_requires_income(client, family):
    """没填年收入不算额度缺口，只做结构缺口。"""
    data = _get(client)
    assert data["params"]["filled"] is False
    assert data["gaps"]["amount"] == []
    assert data["gaps"]["structure"], "结构缺口与参数无关，应照常给出"
    assert any("未填写家庭年收入" in w for w in data["warnings"])


def test_amount_gap_math(client, family):
    """重疾 = max(3×收入, 30 万)；寿险 = 负债 + 5×年支出；意外 = max(5×收入, 50 万)。"""
    client.put(
        "/api/report/params",
        json={"annualIncome": 300000, "totalDebt": 800000, "annualExpense": 150000},
    )
    data = _get(client)
    me = next(m for m in data["members"] if m["name"] == "张伟")
    gaps = {gap["item"]: gap for gap in me["amountGaps"]}

    # 重疾：3×30 万 = 90 万，现有 50 万 → 缺 40 万
    assert gaps["重大疾病"]["suggested"] == pytest.approx(900_000.0)
    assert gaps["重大疾病"]["gap"] == pytest.approx(400_000.0)
    # 寿险：80 万 + 5×15 万 = 155 万，现有 100 万 → 缺 55 万
    assert gaps["寿险"]["suggested"] == pytest.approx(1_550_000.0)
    assert gaps["寿险"]["gap"] == pytest.approx(550_000.0)
    # 意外：5×30 万 = 150 万，现有 0 → 缺 150 万
    assert gaps["意外保障"]["suggested"] == pytest.approx(1_500_000.0)
    assert gaps["意外保障"]["gap"] == pytest.approx(1_500_000.0)


def test_child_gap_uses_regulatory_limit(client, family):
    """未成年人：重疾按 50 万成本估算，意外按身故监管上限（10 岁以下 20 万）。"""
    client.put("/api/report/params", json={"annualIncome": 300000})
    data = _get(client)
    kid = next(m for m in data["members"] if m["name"] == "张小宝")
    gaps = {gap["item"]: gap for gap in kid["amountGaps"]}
    assert gaps["重大疾病"]["suggested"] == pytest.approx(500_000.0)
    assert gaps["意外保障"]["suggested"] == pytest.approx(200_000.0)
    assert gaps["意外保障"]["note"], "应说明未成年人身故保额受监管上限约束"
    assert "寿险" not in gaps, "未成年人不算寿险缺口"


# ---------------------------------------------------------------------------
# Word 导出
# ---------------------------------------------------------------------------
def test_export_docx_matches_current_data(client, family):
    """导出的是**当前**数据，且文档能正常打开（不是坏包）。"""
    from io import BytesIO

    from docx import Document

    response = client.get("/api/report/export.docx")
    assert response.status_code == 200
    assert "wordprocessingml" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    body = response.content
    assert body[:2] == b"PK", "docx 必须是 zip 容器"

    document = Document(BytesIO(body))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "家庭保障检视报告" in text
    assert "保障总览" in text
    assert "行动清单" in text
    assert len(document.tables) >= 6

    # 导出的数必须与接口一致：年化保费出现在文档表格里
    report = _get(client)
    annual = f"¥{report['overview']['annualPremium']:,.2f}"
    cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    assert any(annual == value for value in cells), annual


def test_export_docx_reflects_new_policy(client, family, policy_factory):
    """导出不是快照：新增保单后再导出，文档里应出现新产品名。"""
    from io import BytesIO

    from docx import Document

    policy_factory(
        id="p_export", insurance_type=InsuranceType.accident,
        policyholder_id="m_me", insured_id="m_kid", effective_date=_years_ago(0),
        coverage_term_type=CoverageTermType.fixed_years, coverage_term_value=1,
        payment_term_type=PaymentTermType.fixed_years, payment_term_value=1,
        payment_frequency=PaymentFrequency.yearly, premium_per_period="300",
        insured_amount="500000", product_name="导出验证意外X",
    )

    body = client.get("/api/report/export.docx").content
    document = Document(BytesIO(body))
    cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    assert any("导出验证意外X" in value for value in cells)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
def test_report_requires_password(client, family, db_session):
    """设了访问密码且未登录 → 报告接口必须 401（不能靠前端遮罩）。"""
    from app.services import auth_service

    auth_service.set_password(db_session, "secret123")
    auth_service.set_enabled(db_session, True)
    db_session.commit()

    response = client.get("/api/report")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"

    assert client.get("/api/report/export.docx").status_code == 401
