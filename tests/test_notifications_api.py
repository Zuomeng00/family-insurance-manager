"""v1.8 消息通知：配置接口 + 提醒收集 + 消息组装测试。

覆盖点
------
1. ``GET/PUT /api/notifications/settings`` 的默认值、脱敏、部分更新与校验；
2. **脱敏值回写必须被拒**（否则真实 Webhook key 会被星号覆盖）；
3. 两类提醒的收集口径：保障到期（终身险跳过）/ 缴费临近与逾期；
4. ``build_text`` 的纯文本结构（**不能含 markdown / HTML 标记**，否则微信端不显示）；
5. ``run_daily_check`` 的「发送时间 + 当日去重 + 开关」三重判定。

测试全程 **不发起真实网络请求**：``send_wechat`` 一律被 monkeypatch。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.calc import add_years
from app.enums import (
    CoverageTermType,
    InsuranceType,
    PaymentFrequency,
    PaymentTermType,
    PolicyStatus,
    Relation,
)
from app.services import notification_service as ns

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456"
SETTINGS_URL = "/api/notifications/settings"


def _enabled_since_before(db, send_time: str = "00:00"):
    """把配置置为「早就开着、今天还没发过」。

    ``update_settings`` 对「由关到开且已过发送时间」会顺手把 ``last_sent_date`` 记为今天
    （避免刚开启就补发），所以那些测试要显式清掉这个标记，模拟「跨天延续的开启状态」。
    """
    ns.update_settings(db, {"webhook": WEBHOOK, "enabled": True, "send_time": send_time})
    ns._write(db, {"last_sent_date": ""})
    db.commit()


# ---------------------------------------------------------------------------
# 配置接口
# ---------------------------------------------------------------------------
def test_get_settings_defaults(client):
    """未配置时返回默认值（关闭 / 无 Webhook / 30 天 / 7 天 / 09:00）。"""
    resp = client.get(SETTINGS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["webhook"] == ""
    assert data["webhookConfigured"] is False
    assert data["expiryEnabled"] is True
    assert data["expiryDays"] == 30
    assert data["dueEnabled"] is True
    assert data["dueDays"] == 7
    assert data["sendTime"] == "09:00"
    assert data["lastSentDate"] is None
    assert data["today"] == date.today().isoformat()


def test_put_webhook_is_masked(client):
    """保存后回显必须是脱敏值，真实 key 不出网。"""
    resp = client.put(SETTINGS_URL, json={"webhook": WEBHOOK})
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhookConfigured"] is True
    assert data["webhook"].startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=****")
    assert data["webhook"].endswith("3456")
    assert "abcdef123456" not in resp.text


def test_put_masked_webhook_rejected(client):
    """把回显的脱敏值原样提交 → 422（避免覆盖真实 key）。"""
    client.put(SETTINGS_URL, json={"webhook": WEBHOOK})
    masked = client.get(SETTINGS_URL).json()["webhook"]
    resp = client.put(SETTINGS_URL, json={"webhook": masked})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
    # 真实值未被破坏
    assert client.get(SETTINGS_URL).json()["webhook"].endswith("3456")


def test_enable_requires_webhook(client):
    """无 Webhook 时直接开启 → 422，提示先填地址。"""
    resp = client.put(SETTINGS_URL, json={"enabled": True})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "Webhook" in body["message"]


def test_non_wechat_host_rejected(client):
    """只允许企业微信官方域名，防止被配成任意 URL（SSRF）。"""
    for url in (
        "https://evil.example.com/cgi-bin/webhook/send?key=abc",
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        "https://qyapi.weixin.qq.com/other/path?key=abc",
    ):
        resp = client.put(SETTINGS_URL, json={"webhook": url})
        assert resp.status_code == 422, url


def test_webhook_without_key_rejected(client):
    resp = client.put(SETTINGS_URL, json={"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"})
    assert resp.status_code == 422


def test_partial_update_keeps_other_fields(client):
    """只改提醒天数时，Webhook / 启用状态保持不变。"""
    client.put(SETTINGS_URL, json={"webhook": WEBHOOK, "enabled": True, "sendTime": "08:30"})
    resp = client.put(SETTINGS_URL, json={"dueDays": 15})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dueDays"] == 15
    assert data["expiryDays"] == 30
    assert data["enabled"] is True
    assert data["webhookConfigured"] is True
    assert data["sendTime"] == "08:30"


def test_send_time_normalized(client):
    """``7:5`` → ``07:05``。"""
    resp = client.put(SETTINGS_URL, json={"sendTime": "7:5"})
    assert resp.status_code == 200
    assert resp.json()["sendTime"] == "07:05"


@pytest.mark.parametrize("value", ["25:00", "12:60", "abc", "12", "12:00:00"])
def test_send_time_invalid(client, value):
    resp = client.put(SETTINGS_URL, json={"sendTime": value})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [0, 366, -1])
def test_days_out_of_range(client, value):
    assert client.put(SETTINGS_URL, json={"expiryDays": value}).status_code == 422
    assert client.put(SETTINGS_URL, json={"dueDays": value}).status_code == 422


def test_clear_webhook_disables_notification(client):
    """清空 Webhook → 自动关闭开关，杜绝「已启用但发不出」。"""
    client.put(SETTINGS_URL, json={"webhook": WEBHOOK, "enabled": True})
    resp = client.put(SETTINGS_URL, json={"webhook": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhookConfigured"] is False
    assert data["enabled"] is False


def test_test_endpoint_sends(client, monkeypatch):
    """「立即发送测试」不受启用开关限制。"""
    sent: list[str] = []
    monkeypatch.setattr(ns, "send_wechat", lambda url, content, timeout=10.0: sent.append(content))

    client.put(SETTINGS_URL, json={"webhook": WEBHOOK})  # 不开启 enabled
    resp = client.post("/api/notifications/test")
    assert resp.status_code == 200
    assert resp.json()["sent"] is True
    assert len(sent) == 1
    assert "家庭保单提醒" in sent[0]


def test_test_endpoint_without_webhook(client):
    resp = client.post("/api/notifications/test")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 提醒收集口径
# ---------------------------------------------------------------------------
@pytest.fixture()
def notified_env(db_session, member_factory, policy_factory):
    """一名成员 + 一张 30 天后保障到期的保单 + 一张 3 天后应缴的保单。"""
    member_factory(id="m1", name="张三", relation=Relation.self_, birth_date=date(1988, 6, 1))
    today = date.today()

    # 保障到期：1 年期，起保日 = 今天 + 10 天 - 1 年 → 到期日 = 今天 + 10 天
    policy_factory(
        id="p_expiring",
        insurance_type=InsuranceType.accident,
        policyholder_id="m1",
        insured_id="m1",
        effective_date=add_years(today + timedelta(days=10), -1),
        coverage_term_type=CoverageTermType.fixed_years,
        coverage_term_value=1,
        payment_term_type=PaymentTermType.single,
        payment_term_value=None,
        payment_frequency=PaymentFrequency.yearly,
        premium_per_period="300",
        status=PolicyStatus.active,
        company="平安",
        product_name="综合意外",
    )

    # 缴费临近：年缴，起保日 = 今天 - 1 年 + 3 天 → 第 2 期应缴日 = 今天 + 3 天
    policy_factory(
        id="p_due",
        insurance_type=InsuranceType.medical,
        policyholder_id="m1",
        insured_id="m1",
        effective_date=add_years(today + timedelta(days=3), -1),
        coverage_term_type=CoverageTermType.whole_life,
        coverage_term_value=None,
        payment_term_type=PaymentTermType.fixed_years,
        payment_term_value=10,
        payment_frequency=PaymentFrequency.yearly,
        premium_per_period="1200",
        status=PolicyStatus.active,
        company="人保",
        product_name="百万医疗",
    )
    db_session.commit()
    return db_session


def test_collect_expiring_picks_upcoming(notified_env):
    db = notified_env
    items = ns.collect_expiring(db, date.today(), 30)
    assert [item.product_name for item in items] == ["综合意外"]
    assert items[0].days_left == 10
    assert items[0].insured_name == "张三"


def test_collect_expiring_respects_window(notified_env):
    """窗口缩小到 9 天 → 不再命中。"""
    assert ns.collect_expiring(notified_env, date.today(), 9) == []


def test_collect_expiring_skips_whole_life(notified_env):
    """终身险没有到期日，不产生续保提醒。"""
    names = [item.product_name for item in ns.collect_expiring(notified_env, date.today(), 365)]
    assert "百万医疗" not in names


def test_collect_due_picks_upcoming(notified_env):
    items = ns.collect_due(notified_env, date.today(), 7)
    assert [item.product_name for item in items] == ["百万医疗"]
    assert items[0].days_left == 3
    assert items[0].amount == Decimal("1200")


def test_collect_due_respects_window(notified_env):
    assert ns.collect_due(notified_env, date.today(), 2) == []


def test_collect_skips_inactive_policy(db_session, member_factory, policy_factory):
    """已退保 / 已失效的保单不提醒。"""
    member_factory(id="m2", name="李四", relation=Relation.spouse, birth_date=date(1990, 3, 12))
    today = date.today()
    policy_factory(
        id="p_off",
        insurance_type=InsuranceType.accident,
        policyholder_id="m2",
        insured_id="m2",
        effective_date=add_years(today + timedelta(days=5), -1),
        coverage_term_type=CoverageTermType.fixed_years,
        coverage_term_value=1,
        payment_term_type=PaymentTermType.single,
        payment_term_value=None,
        payment_frequency=PaymentFrequency.yearly,
        premium_per_period="200",
        status=PolicyStatus.surrendered,
        product_name="意外险",
    )
    db_session.commit()
    assert ns.collect_expiring(db_session, today, 30) == []
    assert ns.collect_due(db_session, today, 7) == []


# ---------------------------------------------------------------------------
# 消息组装与发送判定
# ---------------------------------------------------------------------------
def test_build_text_structure(notified_env):
    """纯文本结构：分段标题 + 缩进明细，两端（微信 / 企业微信）都能显示。"""
    today = date.today()
    expiring = ns.collect_expiring(notified_env, today, 30)
    dues = ns.collect_due(notified_env, today, 7)
    content = ns.build_text(expiring, dues, today)

    assert content.startswith(f"家庭保单提醒（{today.isoformat()}）")
    assert "【保障即将到期】共 1 份" in content
    assert "综合意外" in content
    assert "【近期应缴费】共 1 笔，合计 ¥1,200.00" in content
    assert "百万医疗" in content
    assert "剩余 3 天" in content
    assert "（由「家庭保险整理工具」自动发送）" in content
    # 纯文本：不允许出现 markdown / HTML 标记（微信端会原样显示或直接不显示）
    for token in ("##", "> ", "<font", "**"):
        assert token not in content, token


def test_build_text_overdue_uses_plain_words():
    """逾期不用颜色标记，改用文字强调。"""
    today = date.today()
    item = ns.DueItemView(
        product_name="百万医疗",
        insured_name="张三",
        due_date=today - timedelta(days=3),
        days_left=-3,
        amount=Decimal("1200"),
    )
    content = ns.build_text([], [item], today)
    assert "已逾期 3 天" in content
    assert "<font" not in content


def test_build_text_truncates_by_byte_budget():
    """条目过多时按**字节**折叠，保证不超过企业微信 text 的 2048 字节上限。"""
    today = date.today()
    many = [
        ns.ExpiryItem(
            product_name=f"长期重疾保险产品第{i}号",
            company="某某人寿保险股份有限公司",
            insured_name="张三",
            coverage_end=today + timedelta(days=1),
            days_left=1,
        )
        for i in range(40)
    ]
    content = ns.build_text(many, [], today)
    assert len(content.encode("utf-8")) <= ns.MAX_BYTES
    assert "另有" in content and "条未列出" in content
    # 折叠后仍保留了开头几条（不是全丢）
    assert "第0号" in content


def test_send_wechat_uses_text_msgtype(monkeypatch):
    """必须发 ``msgtype=text``——markdown 在微信端不显示。"""
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"errcode": 0, "errmsg": "ok"}'

    def fake_urlopen(request, timeout=10.0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(ns.urllib.request, "urlopen", fake_urlopen)
    ns.send_wechat(WEBHOOK, "hello")
    assert captured["body"]["msgtype"] == "text"
    assert captured["body"]["text"]["content"] == "hello"


def test_enabling_after_send_time_does_not_immediately_resend(db_session):
    """刚打开开关且当天发送时间已过 → 不补发（避免与「测试」按钮重复）。

    补发语义只针对「本来开着、但服务当时没起」。这里把发送时间设成 00:00，
    即「今天的时间点早已过去」，开启后 ``last_sent_date`` 直接记为今天。
    """
    settings = ns.update_settings(
        db_session,
        {"webhook": WEBHOOK, "enabled": True, "send_time": "00:00"},
    )
    assert settings.enabled is True
    assert settings.last_sent_date == date.today().isoformat()


def test_enabling_before_send_time_still_sends_today(db_session):
    """发送时间还没到就开启 → 不写「已发送」，当天照常发。"""
    settings = ns.update_settings(
        db_session,
        {"webhook": WEBHOOK, "enabled": True, "send_time": "23:59"},
    )
    assert settings.last_sent_date == ""


def test_run_daily_check_disabled(notified_env):
    assert ns.run_daily_check(notified_env, datetime.combine(date.today(), time(12, 0)))["reason"] == "disabled"


def test_run_daily_check_too_early(notified_env, monkeypatch):
    db = notified_env
    _enabled_since_before(db, "09:00")
    monkeypatch.setattr(ns, "send_wechat", lambda *a, **k: pytest.fail("不应发送"))
    result = ns.run_daily_check(db, datetime.combine(date.today(), time(8, 59)))
    assert result == {"sent": False, "reason": "too_early"}


def test_run_daily_check_sends_once_per_day(notified_env, monkeypatch):
    db = notified_env
    _enabled_since_before(db, "09:00")
    sent: list[str] = []
    monkeypatch.setattr(ns, "send_wechat", lambda url, content, timeout=10.0: sent.append(content))

    now = datetime.combine(date.today(), time(9, 0))
    first = ns.run_daily_check(db, now)
    assert first["sent"] is True
    assert first["expiring"] == 1
    assert first["due"] == 1
    assert len(sent) == 1

    second = ns.run_daily_check(db, now)
    assert second == {"sent": False, "reason": "already_sent"}
    assert len(sent) == 1


def test_run_daily_check_nothing_to_send(db_session, member_factory, monkeypatch):
    """无可提醒项时不发空消息，也不写「已发送」标记。"""
    member_factory(id="m9", name="王五", relation=Relation.father, birth_date=date(1960, 2, 10))
    db_session.commit()
    _enabled_since_before(db_session, "00:00")
    monkeypatch.setattr(ns, "send_wechat", lambda *a, **k: pytest.fail("不应发送"))

    result = ns.run_daily_check(db_session, datetime.combine(date.today(), time(12, 0)))
    assert result == {"sent": False, "reason": "nothing_to_send"}
    assert ns.get_settings(db_session).last_sent_date == ""


def test_run_daily_check_respects_switches(notified_env, monkeypatch):
    """两类提醒都关掉 → 不发消息。"""
    db = notified_env
    ns.update_settings(
        db,
        {
            "enabled": True,
            "webhook": WEBHOOK,
            "send_time": "00:00",
            "expiry_enabled": False,
            "due_enabled": False,
        },
    )
    ns._write(db, {"last_sent_date": ""})
    db.commit()
    monkeypatch.setattr(ns, "send_wechat", lambda *a, **k: pytest.fail("不应发送"))
    result = ns.run_daily_check(db, datetime.combine(date.today(), time(12, 0)))
    assert result == {"sent": False, "reason": "nothing_to_send"}


def test_run_check_safe_swallows_errors(monkeypatch):
    """后台任务入口：异常必须被吞掉（通知失败不能影响服务可用性）。"""

    def boom(*args, **kwargs):
        raise RuntimeError("模拟数据库故障")

    monkeypatch.setattr(ns, "run_daily_check", boom)
    ns.run_check_safe()  # 不抛异常即通过


def test_send_wechat_rejects_bad_response(monkeypatch):
    """企业微信返回 errcode != 0 → 502 NOTIFY_FAILED。"""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"errcode": 93000, "errmsg": "invalid webhook url"}'

    monkeypatch.setattr(ns.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(Exception) as excinfo:
        ns.send_wechat(WEBHOOK, "hello")
    error = excinfo.value
    assert error.code == "NOTIFY_FAILED"
    # 常见错误码翻译成人话，原始 errmsg 仍保留在 detail 便于排查
    assert "Webhook 地址无效" in error.message
    assert error.detail["errcode"] == 93000
    assert "invalid webhook url" in error.detail["errmsg"]


def test_send_wechat_unknown_errcode_keeps_raw(monkeypatch):
    """未见过的错误码 → 保留原始 errcode / errmsg，不要吞掉线索。"""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"errcode": 40001, "errmsg": "invalid credential"}'

    monkeypatch.setattr(ns.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(Exception) as excinfo:
        ns.send_wechat(WEBHOOK, "hello")
    assert "40001" in excinfo.value.message
    assert "invalid credential" in excinfo.value.message
