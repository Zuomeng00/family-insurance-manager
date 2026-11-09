"""v1.5 QA 独立验证（不采信工程师自测结论）。

覆盖靶点 A（口径正确性）与 D（回归 / 静态契约）：
- A1 默认全计入 → 算例 A/B 逐值不变（硬红线）；
- A2 ``byInsured`` 的过滤依据是**保单投保人**而非被保人（最易做错）；
- A3 排除某投保人后，**只有年化保费口径**变化，其余全不变（全字段深比对）；
- A4 跨页面一致：``/api/members`` 年化 == ``dashboard.byInsured`` 年化；
- A5 边界：全排除 / 双重身份 / 开关往返 / 新建默认。

本文件仅做**独立复算与断言**，不改任何产品代码。
"""

from __future__ import annotations

TODAY = "2025-05-20"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _by_name(rows) -> dict:
    return {r["name"]: r for r in rows}


def _dashboard(client, today: str = TODAY) -> dict:
    resp = client.get("/api/stats/dashboard", params={"today": today})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _member(client, name: str, relation: str, birth: str, gender: str = "男", count=None) -> dict:
    payload = {"name": name, "relation": relation, "birthDate": birth, "gender": gender}
    if count is not None:
        payload["countInStats"] = count
    resp = client.post("/api/members", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set_count(client, member_id: str, flag: bool) -> dict:
    members = client.get("/api/members").json()
    m = next(x for x in members if x["id"] == member_id)
    payload = {
        "name": m["name"],
        "relation": m["relation"],
        "birthDate": m["birthDate"],
        "gender": m["gender"],
        "remark": m["remark"],
        "countInStats": flag,
    }
    resp = client.put(f"/api/members/{member_id}", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _policy(client, **over) -> dict:
    base = {
        "company": "测试保险",
        "insuranceType": "重疾险",
        "productName": "测试产品",
        "policyholderId": "m1",
        "insuredIds": ["m1"],
        "primaryInsuredId": "m1",
        "effectiveDate": "2020-01-01",
        "coverageTermType": "终身",
        "coverageTermValue": None,
        "paymentTermType": "固定年限",
        "paymentTermValue": 10,
        "paymentFrequency": "年缴",
        "premiumPerPeriod": 1000,
    }
    base.update(over)
    resp = client.post("/api/policies", json=base)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _flatten_dashboard(d: dict) -> dict:
    """把看板展平为 ``路径 -> 值``；列表按稳定键归一，规避排序变化造成的假差异。"""
    flat: dict[str, object] = {}
    for k, v in d["overview"].items():
        flat[f"overview.{k}"] = v
    for r in d["byMember"]["byInsured"]:
        for k, v in r.items():
            if k in ("memberId", "name", "relation"):
                continue
            flat[f"byInsured[{r['name']}].{k}"] = v
    for r in d["byMember"]["byPolicyholder"]:
        for k, v in r.items():
            if k in ("memberId", "name", "relation"):
                continue
            flat[f"byPolicyholder[{r['name']}].{k}"] = v
    for r in d["byType"]:
        for k, v in r.items():
            if k == "insuranceType":
                continue
            flat[f"byType[{r['insuranceType']}].{k}"] = v
    flat["upcoming"] = d["upcoming"]
    flat["overdue"] = d["overdue"]
    return flat


def _is_annual_field(path: str) -> bool:
    """该字段是否属于「年化保费」口径（唯一允许被过滤的口径）。"""
    return path.endswith(".annualPremium") or path in (
        "overview.annualTotalPremium",
        "overview.unpaidAnnualPremium",
    )


def _cross_data(client) -> dict:
    """跨投保人/被保人的关键算例（用于 A2/A3/A4/A5）：

    - p_self ：投保人 m1、被保人 m1、年化 1000（重疾险）
    - p_cross：投保人 **m1**、被保人 **m2**、年化 2000（医疗险）★ 关键
    - p_m2self：投保人 m2、被保人 m2、年化 1500（意外险）
    """
    m1 = _member(client, "张三", "本人", "1988-06-01", "男")
    m2 = _member(client, "李四", "配偶", "1990-03-12", "女")
    _policy(client, productName="p_self", policyholderId=m1["id"], insuredIds=[m1["id"]],
            primaryInsuredId=m1["id"], premiumPerPeriod=1000, insuranceType="重疾险")
    _policy(client, productName="p_cross", policyholderId=m1["id"], insuredIds=[m2["id"]],
            primaryInsuredId=m2["id"], premiumPerPeriod=2000, insuranceType="医疗险")
    _policy(client, productName="p_m2self", policyholderId=m2["id"], insuredIds=[m2["id"]],
            primaryInsuredId=m2["id"], premiumPerPeriod=1500, insuranceType="意外险")
    return {"m1": m1, "m2": m2}


# ---------------------------------------------------------------------------
# A1：默认全计入 → 算例 A/B 逐值不变
# ---------------------------------------------------------------------------
def test_a1_example_a_all_counted_unchanged(client, seed_example_a) -> None:
    data = _dashboard(client)
    ov = data["overview"]
    assert ov["annualTotalPremium"] == 17000.00
    assert ov["unpaidAnnualPremium"] == 17000.00
    assert ov["upcoming60Premium"] == 4000.00
    assert ov["upcoming60Count"] == 3
    assert ov["policyTotal"] == 4
    assert ov["activePolicyCount"] == 3
    assert ov["insuredPersonCount"] == 3

    ins = _by_name(data["byMember"]["byInsured"])
    assert (ins["张三"]["policyCount"], ins["张三"]["annualPremium"], ins["张三"]["paidToDatePremium"]) == (2, 6000.00, 81500.00)
    assert (ins["李四"]["policyCount"], ins["李四"]["annualPremium"], ins["李四"]["paidToDatePremium"]) == (1, 8000.00, 32000.00)
    assert (ins["小张"]["policyCount"], ins["小张"]["annualPremium"], ins["小张"]["paidToDatePremium"]) == (1, 3000.00, 3000.00)
    # 不变式②：分摊守恒
    assert sum(r["annualPremium"] for r in data["byMember"]["byInsured"]) == ov["unpaidAnnualPremium"]

    by_type = {r["insuranceType"]: r for r in data["byType"]}
    assert by_type["医疗险"]["annualPremium"] == 8000.00
    assert by_type["重疾险"]["annualPremium"] == 6000.00
    assert by_type["意外险"]["annualPremium"] == 3000.00

    ph = _by_name(data["byMember"]["byPolicyholder"])
    assert ph["张三"]["policyCount"] == 4
    assert ph["张三"]["annualPremium"] == 17000.00

    # 待缴清单顺序
    assert [u["dueDate"] for u in data["upcoming"]] == ["2025-06-01", "2025-06-15", "2025-07-15"]


def test_a1_example_b_all_counted_unchanged(client, seed_example_b) -> None:
    data = _dashboard(client)
    ov = data["overview"]
    assert ov["annualTotalPremium"] == 14400.00
    assert ov["unpaidAnnualPremium"] == 2400.00
    assert ov["upcoming60Premium"] == 0.00
    assert ov["upcoming60Count"] == 0
    assert ov["policyTotal"] == 3
    assert ov["insuredPersonCount"] == 2

    ins = _by_name(data["byMember"]["byInsured"])
    assert (ins["张三"]["policyCount"], ins["张三"]["annualPremium"], ins["张三"]["paidToDatePremium"]) == (2, 2400.00, 50000.00)
    # v1.6：年化口径放宽为「生效中 + 已缴清」
    assert (ins["王五"]["policyCount"], ins["王五"]["annualPremium"], ins["王五"]["paidToDatePremium"]) == (1, 12000.00, 60000.00)


def test_a1_members_annual_matches_insured_default(client, seed_example_a) -> None:
    """默认全计入：成员列表年化 == 看板 byInsured 年化（算例 A）。"""
    members = {m["name"]: m["annualPremium"] for m in client.get("/api/members").json()}
    ins = {r["name"]: r["annualPremium"] for r in _dashboard(client)["byMember"]["byInsured"]}
    assert members == ins == {"张三": 6000.00, "李四": 8000.00, "小张": 3000.00}


# ---------------------------------------------------------------------------
# A2：byInsured 按「保单投保人」过滤（★ 最易做错）
# ---------------------------------------------------------------------------
def test_a2_by_insured_filters_by_policyholder_not_insured(client) -> None:
    ids = _cross_data(client)
    before = _dashboard(client)
    ins0 = _by_name(before["byMember"]["byInsured"])
    assert ins0["张三"]["annualPremium"] == 1000.00  # p_self
    assert ins0["李四"]["annualPremium"] == 3500.00  # p_cross(2000) + p_m2self(1500)

    _set_count(client, ids["m1"]["id"], False)
    after = _dashboard(client)
    ins1 = _by_name(after["byMember"]["byInsured"])

    # 张三名下两张保单年化全剔除 → 0
    assert ins1["张三"]["annualPremium"] == 0.00
    # ★ p_cross 的被保人是李四，但投保人是张三（被排除）→ 必须从李四那一行也消失
    assert ins1["李四"]["annualPremium"] == 1500.00, "p_cross 应因投保人张三被排除而从李四行剔除"

    # 反向确认：排除李四（p_m2self 的投保人）不影响张三行的 p_self
    _set_count(client, ids["m1"]["id"], True)
    _set_count(client, ids["m2"]["id"], False)
    ins2 = _by_name(_dashboard(client)["byMember"]["byInsured"])
    assert ins2["张三"]["annualPremium"] == 1000.00  # p_self 保留
    assert ins2["李四"]["annualPremium"] == 2000.00  # p_cross 保留（投保人张三），p_m2self 剔除


# ---------------------------------------------------------------------------
# A3：排除某投保人 → 只有年化口径变，其余全不变（全字段深比对）
# ---------------------------------------------------------------------------
def test_a3_only_annual_fields_change(client) -> None:
    ids = _cross_data(client)
    before = _flatten_dashboard(_dashboard(client))
    _set_count(client, ids["m1"]["id"], False)
    after = _flatten_dashboard(_dashboard(client))

    assert set(before) == set(after), "字段集合发生变化"
    changed = {k for k in before if before[k] != after[k]}
    # 每一个变化字段都必须属于「年化保费」口径
    for k in changed:
        assert _is_annual_field(k), f"非年化字段被改动：{k} ({before[k]} -> {after[k]})"
    # 且确实发生了变化（证明过滤生效，而非空跑）
    assert changed, "排除投保人后年化口径竟无任何变化"

    # 显式核对「被排除投保人那一行仍在，仅年化变 0」
    ph_before = _by_name(_dashboard(client)["byMember"]["byPolicyholder"])  # after
    assert "张三" in ph_before
    assert ph_before["张三"]["policyCount"] == 2  # 计数仍在
    assert ph_before["张三"]["annualPremium"] == 0.00
    assert ph_before["张三"]["activePolicyCount"] == 2


def test_a3_upcoming_overdue_paid_untouched(client) -> None:
    """待缴清单 / 逾期清单 / 累计已缴 完全不变。"""
    ids = _cross_data(client)
    # 造一张 m1 名下、月缴、0 期 → 逾期，且落在 60 天窗口
    _policy(client, productName="p_overdue", policyholderId=ids["m1"]["id"],
            insuredIds=[ids["m1"]["id"]], primaryInsuredId=ids["m1"]["id"],
            paymentFrequency="月缴", premiumPerPeriod=100, paidPeriodsOverride=0)

    before = _dashboard(client)
    _set_count(client, ids["m1"]["id"], False)
    after = _dashboard(client)

    assert after["upcoming"] == before["upcoming"]
    assert after["overdue"] == before["overdue"]
    assert after["overview"]["upcoming60Premium"] == before["overview"]["upcoming60Premium"]
    assert after["overview"]["upcoming60Count"] == before["overview"]["upcoming60Count"]
    assert after["overview"]["overduePolicyCount"] == before["overview"]["overduePolicyCount"]
    for name in ("张三", "李四"):
        b = _by_name(before["byMember"]["byInsured"])[name]
        a = _by_name(after["byMember"]["byInsured"])[name]
        assert a["paidToDatePremium"] == b["paidToDatePremium"]
        assert a["upcoming60Premium"] == b["upcoming60Premium"]
        assert a["policyCount"] == b["policyCount"]


def test_a3_policy_counted_in_stats_flag(client) -> None:
    ids = _cross_data(client)
    for p in client.get("/api/policies").json():
        assert p["countedInStats"] is True
    _set_count(client, ids["m1"]["id"], False)
    by_product = {p["productName"]: p for p in client.get("/api/policies").json()}
    assert by_product["p_self"]["countedInStats"] is False
    assert by_product["p_cross"]["countedInStats"] is False
    assert by_product["p_m2self"]["countedInStats"] is True
    # annualizedPremium 本身不过滤（保单事实数据）
    assert by_product["p_self"]["annualizedPremium"] == 1000.00
    assert by_product["p_cross"]["annualizedPremium"] == 2000.00


# ---------------------------------------------------------------------------
# A4：跨页面一致性（默认 + 排除后）
# ---------------------------------------------------------------------------
def _assert_members_equals_insured(client) -> None:
    members = {m["name"]: m["annualPremium"] for m in client.get("/api/members").json()}
    ins = {r["name"]: r["annualPremium"] for r in _dashboard(client)["byMember"]["byInsured"]}
    for name, value in ins.items():
        assert members.get(name) == value, f"{name}: /api/members={members.get(name)} vs byInsured={value}"


def test_a4_cross_page_consistency_default(client, seed_example_a) -> None:
    _assert_members_equals_insured(client)


def test_a4_cross_page_consistency_after_exclude(client) -> None:
    ids = _cross_data(client)
    _set_count(client, ids["m1"]["id"], False)
    _assert_members_equals_insured(client)


def test_a4_coverage_types_annual_filtered(client) -> None:
    ids = _cross_data(client)
    _set_count(client, ids["m1"]["id"], False)
    li = next(m for m in client.get("/api/members").json() if m["name"] == "李四")
    annual = {c["insuranceType"]: c["annualPremium"] for c in li["coverageTypes"]}
    counts = {c["insuranceType"]: c["count"] for c in li["coverageTypes"]}
    assert annual.get("医疗险") == 0.00  # p_cross 投保人张三被排除
    assert annual.get("意外险") == 1500.00  # p_m2self 投保人李四计入
    assert counts.get("医疗险") == 1  # 计数不受影响


# ---------------------------------------------------------------------------
# A5：边界
# ---------------------------------------------------------------------------
def test_a5_exclude_all_holders_zeroes_annual_keeps_counts(client) -> None:
    ids = _cross_data(client)
    before = _dashboard(client)
    for mid in (ids["m1"]["id"], ids["m2"]["id"]):
        _set_count(client, mid, False)
    after = _dashboard(client)

    assert after["overview"]["annualTotalPremium"] == 0.00
    assert after["overview"]["unpaidAnnualPremium"] == 0.00
    for r in after["byMember"]["byInsured"]:
        assert r["annualPremium"] == 0.00
    for r in after["byType"]:
        assert r["annualPremium"] == 0.00
    for r in after["byMember"]["byPolicyholder"]:
        assert r["annualPremium"] == 0.00

    # 计数 / 待缴 / 已缴 全不变
    for key in ("policyTotal", "activePolicyCount", "insuredPersonCount",
                "upcoming60Premium", "upcoming60Count", "overduePolicyCount"):
        assert after["overview"][key] == before["overview"][key], f"overview.{key} 不应变"
    assert after["upcoming"] == before["upcoming"]
    assert after["overdue"] == before["overdue"]
    for name in ("张三", "李四"):
        b = _by_name(before["byMember"]["byInsured"])[name]
        a = _by_name(after["byMember"]["byInsured"])[name]
        assert a["policyCount"] == b["policyCount"]
        assert a["paidToDatePremium"] == b["paidToDatePremium"]
        assert a["upcoming60Premium"] == b["upcoming60Premium"]


def test_a5_toggle_off_then_on_restores_values(client) -> None:
    ids = _cross_data(client)
    before = _dashboard(client)
    _set_count(client, ids["m1"]["id"], False)
    mid = _dashboard(client)
    assert mid["overview"]["annualTotalPremium"] != before["overview"]["annualTotalPremium"]
    _set_count(client, ids["m1"]["id"], True)
    after = _dashboard(client)
    assert after == before, "开关关掉再打开后，看板数字未回到原值"


def test_a5_new_member_defaults_counted(client) -> None:
    m = _member(client, "赵六", "母亲", "1962-08-08", "女")
    assert m["countInStats"] is True
    listed = next(x for x in client.get("/api/members").json() if x["id"] == m["id"])
    assert listed["countInStats"] is True


def test_a5_dual_role_member_own_and_others(client) -> None:
    """某人既是投保人又是被保人：排除其投保人身份 → 其名下保单年化全剔除，
    但其作为**他人保单被保人**的部分按「他人是否被排除」决定。"""
    m1 = _member(client, "张三", "本人", "1988-06-01", "男")
    m2 = _member(client, "李四", "配偶", "1990-03-12", "女")
    # m1 自购（holder m1 / insured m1）
    _policy(client, productName="p_own", policyholderId=m1["id"], insuredIds=[m1["id"]],
            primaryInsuredId=m1["id"], premiumPerPeriod=1000, insuranceType="重疾险")
    # m2 给 m1 买（holder m2 / insured m1）
    _policy(client, productName="p_gift", policyholderId=m2["id"], insuredIds=[m1["id"]],
            primaryInsuredId=m1["id"], premiumPerPeriod=2000, insuranceType="医疗险")

    _set_count(client, m1["id"], False)  # 只排除 m1 的投保人身份
    ins = _by_name(_dashboard(client)["byMember"]["byInsured"])
    # p_own 因投保人 m1 被排除 → 剔除；p_gift 投保人 m2 未排除 → 保留
    assert ins["张三"]["annualPremium"] == 2000.00
    ph = _by_name(_dashboard(client)["byMember"]["byPolicyholder"])
    assert ph["张三"]["annualPremium"] == 0.00  # 其名下 p_own 被剔除
    assert ph["张三"]["policyCount"] == 1


# ---------------------------------------------------------------------------
# D：静态契约
# ---------------------------------------------------------------------------
def test_d_version_is_1_7_0() -> None:
    import app

    assert app.__version__ == "1.8.0"


def test_d_money_fields_are_json_numbers(client, seed_example_a) -> None:
    """金额字段必须是 JSON number（原始 JSON 中不带引号）。"""
    raw = client.get("/api/stats/dashboard", params={"today": TODAY}).text
    for field in ("annualTotalPremium", "unpaidAnnualPremium"):
        idx = raw.index(f'"{field}"')
        # 跳过 `"field":` 三个字符（两个引号 + 一个冒号）
        tail = raw[idx + len(field) + 3:].lstrip()
        assert tail[0] != '"', f"{field} 被序列化为字符串：{tail[:20]}"
        assert tail[0].isdigit() or tail[0] == "-", f"{field} 不是数字：{tail[:20]}"
