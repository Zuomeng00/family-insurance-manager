"""QA 独立验证 · v1.2 后端（第 1 组）。

覆盖（**独立于工程师测试**，含自建参考实现交叉比对）：
1. 批量编辑「三态语义」——2000 组随机请求 × 7 字段 × N 保单的逐字段交叉比对；
2. 「只填顾问姓名」时其余 6 字段逐保单保持原值；
3. 批量编辑校验边界（ids 空 / values 空 / 白名单外 / 超长 / 长度恰好到界）；
4. missingIds 记账恒等式 requested == updated + len(missingIds)；
5. 幂等性；
6. 常用值聚合（去重 / 排序 / limit / companyPhone 排除 / 路由不被吞 / 空历史）；
7. 向后兼容（旧保单 NULL 售后字段 / PolicyOut 形状 / health / stats）；
8. N+1：附件计数走一次 GROUP BY。

本文件由 QA（Edward）编写，**不** import 工程师的测试文件，**不**修改 app/ 业务代码。
"""

from __future__ import annotations

import random

from sqlalchemy import event, select

from app.models import Member, Policy, PolicyAttachment

#: 7 个售后字段（camelCase）
AFTER_SALES_FIELDS = [
    "advisorName",
    "advisorPhone",
    "advisorWechat",
    "advisorEmail",
    "companyPhone",
    "channelName",
    "channelPhone",
]

_FIELD_TO_ATTR = {
    "advisorName": Policy.advisor_name,
    "advisorPhone": Policy.advisor_phone,
    "advisorWechat": Policy.advisor_wechat,
    "advisorEmail": Policy.advisor_email,
    "companyPhone": Policy.company_phone,
    "channelName": Policy.channel_name,
    "channelPhone": Policy.channel_phone,
}

#: camelCase → ORM 属性名（字符串，供 setattr 使用）
_FIELD_TO_NAME = {
    "advisorName": "advisor_name",
    "advisorPhone": "advisor_phone",
    "advisorWechat": "advisor_wechat",
    "advisorEmail": "advisor_email",
    "companyPhone": "company_phone",
    "channelName": "channel_name",
    "channelPhone": "channel_phone",
}

#: 各字段长度上限（PRD 9.4 / ARCHITECTURE 6.3.1）
_FIELD_LIMITS = {
    "advisorName": 20,
    "advisorPhone": 30,
    "advisorWechat": 50,
    "advisorEmail": 80,
    "companyPhone": 50,
    "channelName": 30,
    "channelPhone": 30,
}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _member(client, name: str = "张三") -> dict:
    resp = client.post(
        "/api/members",
        json={"name": name, "relation": "本人", "birthDate": "1990-01-01", "gender": "男"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _policy(client, holder_id: str, **overrides) -> dict:
    payload = {
        "company": "众安",
        "insuranceType": "医疗险",
        "productName": "测试产品",
        "policyholderId": holder_id,
        "insuredId": holder_id,
        "effectiveDate": "2024-01-01",
        "coverageTermType": "固定年限",
        "coverageTermValue": 30,
        "paymentTermType": "固定年限",
        "paymentTermValue": 10,
        "paymentFrequency": "年缴",
        "premiumPerPeriod": 8000,
    }
    payload.update(overrides)
    resp = client.post("/api/policies", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _batch(client, ids, values):
    return client.post("/api/policies/batch-update", json={"ids": ids, "values": values})


def _read_all_fields(db) -> dict[str, dict[str, object]]:
    """一次查询读回全部保单的 7 个售后字段（Core 列查询，绕开 ORM 身份映射缓存）。"""
    cols = [_FIELD_TO_ATTR[f] for f in AFTER_SALES_FIELDS]
    rows = db.execute(select(Policy.id, *cols)).all()
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        result[row[0]] = {f: row[i + 1] for i, f in enumerate(AFTER_SALES_FIELDS)}
    return result


def _ref_apply(state: dict, ids, values: dict) -> dict:
    """**独立参考实现**（不复用 policy_service）。

    三态语义：
    - 键不存在 → 不改；
    - 值为 ``None`` → 清空（写 NULL）；
    - 值为字符串 → ``strip()`` 后为空串则不改，否则写入 strip 后的值。
    """
    for pid in ids:
        if pid not in state:
            continue
        for key, value in values.items():
            if value is None:
                state[pid][key] = None
            else:
                trimmed = value.strip()
                if trimmed != "":
                    state[pid][key] = trimmed
    return state


# ---------------------------------------------------------------------------
# 1. 三态语义 · 随机交叉比对（核心）
# ---------------------------------------------------------------------------
def test_batch_three_state_random_crosscheck(client, db_session):
    """2000 组随机批量请求 × 逐字段比对真实 API 与独立参考实现。"""
    rng = random.Random(20260607)
    holder = _member(client)
    n_policies = 10
    policies = [_policy(client, holder["id"]) for _ in range(n_policies)]
    ids = [p["id"] for p in policies]

    # 随机初始值（含 None / 普通字符串 / 首尾空格 / 数字 / 邮箱 / 中文）
    initial_pool = [
        None,
        None,
        "王小明",
        "李丽",
        "13800000000",
        "wx_wang",
        "a@b.com",
        "1010-9955/952299",
        "支付宝蚂蚁保",
        "顾问甲",
        " 张伟 ",
    ]
    for pid in ids:
        row = db_session.get(Policy, pid)
        for field in AFTER_SALES_FIELDS:
            setattr(row, _FIELD_TO_NAME[field], rng.choice(initial_pool))
    db_session.commit()

    state = _read_all_fields(db_session)

    value_pool = [
        "新顾问",
        " 新渠道 ",
        "13900000000",
        "new@x.com",
        "z",
        "1010-9955",
        "王小明",
        "支付宝蚂蚁保",
    ]
    iterations = 2000
    diffs: list[tuple] = []
    cell_checks = 0

    for it in range(iterations):
        k = rng.randint(1, n_policies)
        selected = rng.sample(ids, k)
        if rng.random() < 0.3:
            selected = selected + [f"p_ghost{rng.randint(0, 9)}"]

        values: dict[str, object] = {}
        for field in AFTER_SALES_FIELDS:
            roll = rng.random()
            if roll < 0.40:
                continue  # 不发键 = 不改
            elif roll < 0.55:
                values[field] = None  # 显式清空
            elif roll < 0.70:
                values[field] = "   "  # 全空格 = 不改（关键防误清空）
            else:
                values[field] = rng.choice(value_pool)
        if not values:
            continue

        resp = _batch(client, selected, values)
        assert resp.status_code == 200, f"iter={it} body={resp.text}"
        body = resp.json()
        assert body["requested"] == body["updated"] + len(body["missingIds"]), body

        _ref_apply(state, selected, values)

        actual_map = _read_all_fields(db_session)
        for pid in ids:
            for field in AFTER_SALES_FIELDS:
                cell_checks += 1
                actual = actual_map[pid][field]
                expected = state[pid][field]
                if (actual or None) != (expected or None):
                    diffs.append((it, pid, field, actual, expected))

    assert diffs == [], f"发现 {len(diffs)} 处差异，前 10 条：{diffs[:10]}"
    assert cell_checks >= 2000
    # 便于报告：把统计量写进断言消息上下文
    print(f"[QA] 随机交叉比对：{iterations} 组请求，{cell_checks} 个字段单元，差异 {len(diffs)}")


def test_batch_only_advisor_name_keeps_other_six_per_policy(client, db_session):
    """只填「顾问姓名」时，其余 6 个字段**逐保单**保持原值（用户最怕的数据破坏场景）。"""
    holder = _member(client)
    distinct = [
        dict(
            advisorName="顾问A",
            advisorPhone="111",
            advisorWechat="wx_a",
            advisorEmail="a@a.com",
            companyPhone="1001",
            channelName="渠道A",
            channelPhone="2001",
        ),
        dict(
            advisorName="顾问B",
            advisorPhone="222",
            advisorWechat="wx_b",
            advisorEmail="b@b.com",
            companyPhone="1002",
            channelName="渠道B",
            channelPhone="2002",
        ),
        dict(
            advisorName=None,
            advisorPhone=None,
            advisorWechat="wx_c",
            advisorEmail=None,
            companyPhone="1003",
            channelName=None,
            channelPhone="2003",
        ),
    ]
    policies = [_policy(client, holder["id"], **vals) for vals in distinct]
    ids = [p["id"] for p in policies]

    resp = _batch(client, ids, {"advisorName": "统一新顾问"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 3

    for p, orig in zip(policies, distinct):
        got = client.get(f"/api/policies/{p['id']}").json()
        assert got["advisorName"] == "统一新顾问"
        for field in AFTER_SALES_FIELDS:
            if field == "advisorName":
                continue
            assert got[field] == orig[field], f"{p['id']} 的 {field} 被意外改动"


# ---------------------------------------------------------------------------
# 2. 校验边界
# ---------------------------------------------------------------------------
def test_batch_validation_errors(client):
    holder = _member(client)
    p1 = _policy(client, holder["id"])

    r = _batch(client, [], {"advisorName": "x"})
    assert r.status_code == 422 and r.json()["code"] == "POLICY_IDS_EMPTY", r.text

    r = _batch(client, [p1["id"]], {})
    assert r.status_code == 422 and r.json()["code"] == "FIELD_VALUES_EMPTY", r.text

    r = _batch(client, [p1["id"]], {"bogusField": "x"})
    assert r.status_code == 422 and r.json()["code"] == "UNKNOWN_UPDATE_FIELD", r.text

    # 白名单外键（含 company 等既有字段）一律拒绝
    for bad_key in ["company", "productName", "id", "policyholderId", "paidPeriodsOverride"]:
        r = _batch(client, [p1["id"]], {bad_key: "x"})
        assert r.status_code == 422 and r.json()["code"] == "UNKNOWN_UPDATE_FIELD", (
            bad_key,
            r.text,
        )


def test_batch_length_boundary_per_field(client):
    """每个字段：恰好到界通过、超 1 字符 422。"""
    holder = _member(client)
    p1 = _policy(client, holder["id"])
    for field, limit in _FIELD_LIMITS.items():
        ok = _batch(client, [p1["id"]], {field: "字" * limit})
        assert ok.status_code == 200, (field, ok.text)
        too_long = _batch(client, [p1["id"]], {field: "字" * (limit + 1)})
        assert too_long.status_code == 422, (field, too_long.text)
        assert too_long.json()["code"] == "VALIDATION_ERROR", too_long.text


def test_batch_whitespace_longer_than_limit_is_rejected_as_observation(client):
    """观察项：校验按**未 trim** 的原始长度判定，故纯空格超长会被 422。

    这不是设计 Bug（前端提交前已 trim），但记录实际行为以免误判。
    """
    holder = _member(client)
    p1 = _policy(client, holder["id"])
    r = _batch(client, [p1["id"]], {"advisorName": " " * 21})
    assert r.status_code == 422


def test_batch_ids_all_missing_is_ok(client):
    """全部 id 不存在 → 200，updated=0，全部计入 missingIds，不报错。"""
    r = _batch(client, ["p_x1", "p_x2"], {"advisorName": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 0
    assert set(body["missingIds"]) == {"p_x1", "p_x2"}
    assert body["requested"] == 2


def test_batch_duplicate_ids_deduplicated(client):
    """重复 id 去重后计数（requested 为去重后数量）。"""
    holder = _member(client)
    p1 = _policy(client, holder["id"])
    r = _batch(client, [p1["id"], p1["id"], p1["id"]], {"advisorName": "去重"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 1 and body["updated"] == 1


def test_batch_missing_ids_accounting(client):
    holder = _member(client)
    p1 = _policy(client, holder["id"])
    p2 = _policy(client, holder["id"])
    r = _batch(client, [p1["id"], "p_gone", p2["id"]], {"advisorName": "有效值"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missingIds"] == ["p_gone"]
    assert set(body["updatedIds"]) == {p1["id"], p2["id"]}
    assert body["requested"] == body["updated"] + len(body["missingIds"]) == 3


def test_batch_idempotent(client):
    holder = _member(client)
    p1 = _policy(client, holder["id"], advisorName="原1", channelName="原渠道")
    p2 = _policy(client, holder["id"], advisorName="原2", channelName="原渠道2")
    ids = [p1["id"], p2["id"]]
    values = {"advisorName": "幂等", "advisorPhone": None}

    first = _batch(client, ids, values)
    second = _batch(client, ids, values)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    a = client.get(f"/api/policies/{p1['id']}").json()
    assert a["advisorName"] == "幂等" and a["advisorPhone"] is None


def test_batch_does_not_touch_non_whitelist_columns(client):
    """批量编辑绝不改动白名单外的列（company / productName / status ...）。"""
    holder = _member(client)
    p1 = _policy(client, holder["id"], company="原公司", productName="原产品")
    r = _batch(client, [p1["id"]], {"advisorName": "新"})
    assert r.status_code == 200
    got = client.get(f"/api/policies/{p1['id']}").json()
    assert got["company"] == "原公司"
    assert got["productName"] == "原产品"
    assert got["status"] == "生效中"


# ---------------------------------------------------------------------------
# 3. 常用值聚合
# ---------------------------------------------------------------------------
def _seed_suggestions(db_session, holder_id: str) -> None:
    """直接经 ORM 造数据（含空白 / 空串 / None，用于验证过滤）。"""
    import uuid

    from app.enums import (
        CoverageTermType,
        InsuranceType,
        PaymentFrequency,
        PaymentTermType,
        PolicyStatus,
    )

    from datetime import date

    def make(advisor_name=None, advisor_email=None, channel_name=None, company_phone=None):
        pid = "p_" + uuid.uuid4().hex[:12]
        db_session.add(
            Policy(
                id=pid,
                company="众安",
                insurance_type=InsuranceType.medical,
                product_name="x",
                policyholder_id=holder_id,
                insured_id=holder_id,
                effective_date=date(2024, 1, 1),
                coverage_term_type=CoverageTermType.fixed_years,
                coverage_term_value=30,
                payment_term_type=PaymentTermType.fixed_years,
                payment_term_value=10,
                payment_frequency=PaymentFrequency.yearly,
                premium_per_period=1000,
                status=PolicyStatus.active,
                advisor_name=advisor_name,
                advisor_email=advisor_email,
                channel_name=channel_name,
                company_phone=company_phone,
            )
        )

    for _ in range(3):
        make(advisor_name="Wang")
    for _ in range(2):
        make(advisor_name="Li")
    make(advisor_name="Zhang")
    make(advisor_name="An")
    # 空白 / 空串 / None —— 均应被排除
    make(advisor_name="   ")
    make(advisor_name="")
    make(advisor_name=None)
    # advisorEmail
    for _ in range(2):
        make(advisor_email="a@x.com")
    make(advisor_email="b@x.com")
    # channelName 与 companyPhone（后者必须不出现）
    make(channel_name="蚂蚁保")
    make(company_phone="1010-9955")
    make(company_phone="1010-9955")
    db_session.commit()


def test_suggestions_ordering_dedup_and_filtering(client, db_session):
    holder = _member(client)
    _seed_suggestions(db_session, holder["id"])

    resp = client.get("/api/policies/suggestions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 路由未被 {policy_id} 吞掉：返回 6 键聚合结构
    assert set(body.keys()) == {
        "advisorName",
        "advisorPhone",
        "advisorWechat",
        "advisorEmail",
        "channelName",
        "channelPhone",
    }

    names = body["advisorName"]
    assert names == [
        {"value": "Wang", "count": 3},
        {"value": "Li", "count": 2},
        {"value": "An", "count": 1},
        {"value": "Zhang", "count": 1},
    ], names
    # 空白 / 空串 / None 已排除（只出现 4 个值）
    assert all(item["value"].strip() != "" for item in names)

    assert body["advisorEmail"] == [
        {"value": "a@x.com", "count": 2},
        {"value": "b@x.com", "count": 1},
    ]
    assert body["channelName"] == [{"value": "蚂蚁保", "count": 1}]
    # companyPhone 刻意不参与聚合
    assert body["advisorPhone"] == []
    assert body["advisorWechat"] == []
    assert body["channelPhone"] == []


def test_suggestions_company_phone_never_present(client, db_session):
    """即便有 companyPhone 历史值，响应里也不得出现其值。"""
    holder = _member(client)
    _seed_suggestions(db_session, holder["id"])
    resp = client.get("/api/policies/suggestions")
    raw = resp.text
    assert "1010-9955" not in raw


def test_suggestions_limit_bounds(client, db_session):
    holder = _member(client)
    _seed_suggestions(db_session, holder["id"])

    r1 = client.get("/api/policies/suggestions", params={"limit": 1})
    assert r1.status_code == 200
    assert len(r1.json()["advisorName"]) == 1

    r50 = client.get("/api/policies/suggestions", params={"limit": 50})
    assert r50.status_code == 200

    r0 = client.get("/api/policies/suggestions", params={"limit": 0})
    assert r0.status_code == 422

    r51 = client.get("/api/policies/suggestions", params={"limit": 51})
    assert r51.status_code == 422


def test_suggestions_empty_history_returns_empty_arrays(client):
    _member(client)
    resp = client.get("/api/policies/suggestions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key, items in body.items():
        assert items == [], (key, items)


def test_suggestions_route_not_shadowed_by_policy_id(client, db_session):
    """确保 /suggestions 不被 /{policy_id} 吞掉。"""
    holder = _member(client)
    _seed_suggestions(db_session, holder["id"])
    resp = client.get("/api/policies/suggestions")
    assert resp.status_code == 200
    body = resp.json()
    # 不是保单对象（无 id/company），是聚合结构
    assert "id" not in body and "company" not in body
    assert "advisorName" in body


# ---------------------------------------------------------------------------
# 4. 向后兼容
# ---------------------------------------------------------------------------
def test_backward_compat_legacy_policy_all_null(client):
    """旧保单（7 字段全 NULL）：列表 / 详情 / 更新 / 删除照常。"""
    holder = _member(client)
    p = _policy(client, holder["id"])
    pid = p["id"]
    for field in AFTER_SALES_FIELDS:
        assert p[field] is None
    assert p["attachmentCount"] == 0

    listing = client.get("/api/policies")
    assert listing.status_code == 200
    assert any(x["id"] == pid for x in listing.json())

    got = client.get(f"/api/policies/{pid}")
    assert got.status_code == 200
    for field in AFTER_SALES_FIELDS:
        assert got.json()[field] is None

    upd = client.put(
        f"/api/policies/{pid}",
        json={
            "company": "众安",
            "insuranceType": "医疗险",
            "productName": "改名产品",
            "policyholderId": holder["id"],
            "insuredId": holder["id"],
            "effectiveDate": "2024-01-01",
            "coverageTermType": "固定年限",
            "coverageTermValue": 30,
            "paymentTermType": "固定年限",
            "paymentTermValue": 10,
            "paymentFrequency": "年缴",
            "premiumPerPeriod": 8000,
        },
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["productName"] == "改名产品"

    deleted = client.delete(f"/api/policies/{pid}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["deletedAttachments"] == 0


def test_policy_out_shape_no_attachment_array(client):
    """PolicyOut 只多 7 售后字段 + attachmentCount，**不含**附件明细数组。"""
    holder = _member(client)
    p = _policy(client, holder["id"])
    got = client.get(f"/api/policies/{p['id']}").json()
    assert "attachmentCount" in got
    assert "attachments" not in got
    for field in AFTER_SALES_FIELDS:
        assert field in got


def test_health_version_and_stats(client):
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["version"] == "1.8.1"

    holder = _member(client)
    _policy(client, holder["id"])
    dash = client.get("/api/stats/dashboard")
    assert dash.status_code == 200, dash.text
    assert "overview" in dash.json()


# ---------------------------------------------------------------------------
# 5. N+1
# ---------------------------------------------------------------------------
def test_attachment_count_uses_single_group_by_not_n_plus_1(client, db_session):
    """200 张保单 + 附件：列表接口附件计数必须是一次 GROUP BY，而非逐张查询。"""
    holder = _member(client)
    expected: dict[str, int] = {}
    for i in range(200):
        p = _policy(client, holder["id"], productName=f"产品{i}")
        expected[p["id"]] = i % 3
        for j in range(i % 3):
            db_session.add(
                PolicyAttachment(
                    id=f"a_{i}_{j}",
                    policy_id=p["id"],
                    file_name=f"f{i}_{j}.pdf",
                    stored_name=f"{i}_{j}.pdf",
                    file_size=100,
                    content_type="application/pdf",
                )
            )
    db_session.commit()

    engine = db_session.get_bind()
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        resp = client.get("/api/policies")
    finally:
        event.remove(engine, "before_cursor_execute", _before)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 200

    attach_queries = [s for s in statements if "policy_attachments" in s.lower()]
    assert len(attach_queries) == 1, (
        f"附件计数应仅 1 条 SQL，实际 {len(attach_queries)} 条：{attach_queries[:5]}"
    )
    assert "group by" in attach_queries[0].lower()
    # 总 SQL 条数应为常数级（远小于 200）
    assert len(statements) <= 8, f"列表接口 SQL 条数过多：{len(statements)}"

    # 计数正确性：逐张核对
    got = {x["id"]: x["attachmentCount"] for x in resp.json()}
    for pid, count in expected.items():
        assert got[pid] == count, f"{pid} 期望 {count}，实际 {got[pid]}"
    assert all(isinstance(v, int) for v in got.values())
