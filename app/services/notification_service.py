"""企业微信机器人通知（v1.8）。

设计要点
--------
1. 配置存 ``app_settings`` 键值表，**缺键即取默认值**——存量库升级无需回填。
2. 提醒分两类，各自可开关：
   - **保障即将到期**（续保提醒）：距离 ``coverage_end_date`` 不足 N 天；
   - **缴费临近/已逾期**：``next_due_date`` 在 N 天内或已过期。
3. 每天最多发送**一条汇总消息**（``notify.last_sent_date`` 去重），不逐单轰炸。
4. 发送使用标准库 ``urllib``，**零新增依赖**；消息类型固定 ``text``——
   ``markdown`` 在微信端不显示（只显示「暂不支持此消息类型」）。
5. Webhook 只接受企业微信官方地址，避免配置成任意 URL 造成 SSRF。
6. 任何网络/配置异常都只记录日志、不抛出到启动流程——**通知失败不能影响应用可用性**。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..calc import calc_schedule
from ..enums import PolicyStatus
from ..errors import AppError
from ..models import AppSetting, Member, Policy

logger = logging.getLogger(__name__)

KEY_PREFIX = "notify."
WEBHOOK_HOST = "qyapi.weixin.qq.com"
WEBHOOK_PATH = "/cgi-bin/webhook/send"

#: 企业微信 ``text`` 消息 ``content`` 上限 2048 **字节**（UTF-8，一个汉字 3 字节）。
#: 注意 markdown 是 4096，但 markdown 在微信端不显示（见 ``build_text`` 的说明）。
MAX_BYTES = 2000
#: 预留给「未列出」提示与页脚，避免折叠提示本身把消息顶出上限。
RESERVED_BYTES = 140

DEFAULTS: dict[str, str] = {
    "enabled": "false",
    "webhook": "",
    "expiry_enabled": "true",
    "expiry_days": "30",
    "due_enabled": "true",
    "due_days": "7",
    "send_time": "09:00",
    "last_sent_date": "",
}

#: 业务上仍需要提醒的保单状态（已退保/已失效不再提醒）
NOTIFIABLE_STATUSES = {PolicyStatus.active, PolicyStatus.paid_off}

#: 企业微信常见错误的**人话映射**（原始 errcode 仍放进 ``detail`` 便于排查）
_ERROR_HINTS: dict[int, str] = {
    93000: "Webhook 地址无效或机器人已被移除，请重新复制群机器人地址",
    45009: "发送过于频繁，请稍后再试",
    40058: "请求参数不合法，请检查 Webhook 地址是否完整（需以 ?key= 结尾）",
}


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NotifySettings:
    enabled: bool
    webhook: str
    expiry_enabled: bool
    expiry_days: int
    due_enabled: bool
    due_days: int
    send_time: str
    last_sent_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        """对外输出（camelCase 由 schema 层处理，这里保持内部命名）。"""
        return {
            "enabled": self.enabled,
            "webhook": _mask_webhook(self.webhook),
            "webhook_configured": bool(self.webhook),
            "expiry_enabled": self.expiry_enabled,
            "expiry_days": self.expiry_days,
            "due_enabled": self.due_enabled,
            "due_days": self.due_days,
            "send_time": self.send_time,
            "last_sent_date": self.last_sent_date or None,
            "today": date.today(),
        }


def _raw_map(db: Session) -> dict[str, str]:
    rows = db.query(AppSetting).filter(AppSetting.key.like(f"{KEY_PREFIX}%")).all()
    return {row.key[len(KEY_PREFIX) :]: row.value for row in rows}


def _as_bool(value: str, default: bool) -> bool:
    text = (value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: str, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def get_settings(db: Session) -> NotifySettings:
    """读取配置；缺失项一律回落到默认值。"""
    raw = {**DEFAULTS, **_raw_map(db)}
    return NotifySettings(
        enabled=_as_bool(raw["enabled"], False),
        webhook=raw["webhook"].strip(),
        expiry_enabled=_as_bool(raw["expiry_enabled"], True),
        expiry_days=_as_int(raw["expiry_days"], 30, 1, 365),
        due_enabled=_as_bool(raw["due_enabled"], True),
        due_days=_as_int(raw["due_days"], 7, 1, 365),
        send_time=_normalize_time(raw["send_time"]),
        last_sent_date=(raw.get("last_sent_date") or "").strip(),
    )


def _normalize_time(value: str) -> str:
    """``H:MM`` / ``HH:MM`` → ``HH:MM``；非法回落 09:00。"""
    try:
        hour_text, minute_text = (value or "").strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        return "09:00"
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "09:00"
    return f"{hour:02d}:{minute:02d}"


def _mask_webhook(url: str) -> str:
    """返回给前端时隐藏 key，只保留前缀 + 末 4 位。"""
    if not url:
        return ""
    marker = "key="
    if marker not in url:
        return url
    head, _, key = url.partition(marker)
    tail = key[-4:] if len(key) > 4 else ""
    return f"{head}{marker}****{tail}"


def validate_webhook(url: str) -> str:
    """只允许企业微信官方 Webhook；空串合法（= 清除配置）。"""
    url = (url or "").strip()
    if not url:
        return ""
    if "****" in url or "••••" in url:
        # 前端回填的是脱敏值（``key=****abcd``）；直接保存会把真实 key 覆盖成星号
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "检测到已脱敏的 Webhook 地址，请重新粘贴完整地址（留空表示不修改）",
            {"field": "webhook"},
        )
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != WEBHOOK_HOST
        or parsed.path != WEBHOOK_PATH
    ):
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "Webhook 地址必须是企业微信机器人地址（https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...）",
            {"field": "webhook"},
        )
    if "key=" not in (parsed.query or ""):
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "Webhook 地址缺少 key 参数",
            {"field": "webhook"},
        )
    return url


def update_settings(db: Session, payload: dict[str, Any]) -> NotifySettings:
    """部分更新；只写显式传入的键。

    ``None`` 视同「未提供」——这样前端可以只提交改动的字段，
    不必为「不修改」的项编造值。
    """
    payload = {key: value for key, value in payload.items() if value is not None}
    current = get_settings(db)
    changes: dict[str, str] = {}

    if "enabled" in payload:
        enabled = bool(payload["enabled"])
        # 开启时必须已有（或同时提供）Webhook，否则配置无效
        webhook = (
            validate_webhook(str(payload.get("webhook", "")))
            if "webhook" in payload and str(payload.get("webhook") or "").strip()
            else current.webhook
        )
        if enabled and not webhook:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "启用通知前请先填写企业微信 Webhook 地址",
                {"field": "webhook"},
            )
        changes["enabled"] = "true" if enabled else "false"

    if "webhook" in payload:
        changes["webhook"] = validate_webhook(str(payload["webhook"]))
        if not changes["webhook"] and current.enabled and "enabled" not in payload:
            # 清空 webhook 等同于无法发送，直接关掉开关，避免「已启用但发不出」
            changes["enabled"] = "false"

    if "expiry_enabled" in payload:
        changes["expiry_enabled"] = "true" if payload["expiry_enabled"] else "false"
    if "expiry_days" in payload:
        changes["expiry_days"] = str(_as_int(str(payload["expiry_days"]), 30, 1, 365))
    if "due_enabled" in payload:
        changes["due_enabled"] = "true" if payload["due_enabled"] else "false"
    if "due_days" in payload:
        changes["due_days"] = str(_as_int(str(payload["due_days"]), 7, 1, 365))
    if "send_time" in payload:
        changes["send_time"] = _normalize_time(str(payload["send_time"]))

    # 「刚打开开关」≠「服务错过发送时间」，不该立刻补发当天消息。
    # 否则用户在 13:00 配好、点过「测试」，60 秒内后台任务又推一条同样内容 → 看起来像重发。
    # 补发语义（服务宕机后补当天）针对的是「本来就开着」，这里用 current.enabled 区分。
    if changes.get("enabled") == "true" and not current.enabled:
        effective_send_time = changes.get("send_time", current.send_time)
        now = datetime.now()
        if now.strftime("%H:%M") >= effective_send_time and "last_sent_date" not in changes:
            changes["last_sent_date"] = now.date().isoformat()

    _write(db, changes)
    db.commit()
    return get_settings(db)


def _write(db: Session, changes: dict[str, str]) -> None:
    for name, value in changes.items():
        key = f"{KEY_PREFIX}{name}"
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value


def _get_raw(db: Session, name: str) -> str:
    row = db.get(AppSetting, f"{KEY_PREFIX}{name}")
    return row.value if row is not None else DEFAULTS.get(name, "")


# ---------------------------------------------------------------------------
# 提醒项收集
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExpiryItem:
    product_name: str
    company: str
    insured_name: str
    coverage_end: date
    days_left: int  # 负数 = 已过期


@dataclass(frozen=True)
class DueItemView:
    product_name: str
    insured_name: str
    due_date: date
    days_left: int  # 负数 = 已逾期
    amount: Decimal


def _policy_rows(db: Session) -> list[tuple[Policy, Member]]:
    """一次性取出可提醒的保单及其主被保人，避免 N+1。"""
    policies = (
        db.query(Policy)
        .filter(Policy.status.in_([s.value for s in NOTIFIABLE_STATUSES]))
        .all()
    )
    if not policies:
        return []
    member_ids = {p.insured_id for p in policies} | {p.policyholder_id for p in policies}
    members = {m.id: m for m in db.query(Member).filter(Member.id.in_(member_ids)).all()}
    rows: list[tuple[Policy, Member]] = []
    for policy in policies:
        insured = members.get(policy.insured_id)
        if insured is not None:
            rows.append((policy, insured))
    return rows


def collect_expiring(db: Session, today: date, days: int) -> list[ExpiryItem]:
    """保障将在 ``days`` 天内到期，或已过期但保单状态仍为生效中。

    已过期超过 30 天的不再重复提醒（多半是用户忘了改状态，噪音大于价值）。
    """
    items: list[ExpiryItem] = []
    for policy, insured in _policy_rows(db):
        if policy.status != PolicyStatus.active:
            continue  # 已缴清的长期险仍在保障期，但到期续保通常针对生效中保单
        schedule = calc_schedule(policy, insured, today)
        end = schedule.coverage_end_date
        if end is None:  # 终身险
            continue
        days_left = (end - today).days
        if days_left > days or days_left < -30:
            continue
        items.append(
            ExpiryItem(
                product_name=policy.product_name,
                company=policy.company,
                insured_name=insured.name,
                coverage_end=end,
                days_left=days_left,
            )
        )
    items.sort(key=lambda item: item.coverage_end)
    return items


def collect_due(db: Session, today: date, days: int) -> list[DueItemView]:
    """下次缴费日在 ``days`` 天内，或已逾期（超过 60 天不再提醒）。"""
    items: list[DueItemView] = []
    for policy, insured in _policy_rows(db):
        if policy.status != PolicyStatus.active:
            continue
        schedule = calc_schedule(policy, insured, today)
        if schedule.is_paid_off or schedule.next_due_date is None:
            continue
        days_left = (schedule.next_due_date - today).days
        if days_left > days or days_left < -60:
            continue
        items.append(
            DueItemView(
                product_name=policy.product_name,
                insured_name=insured.name,
                due_date=schedule.next_due_date,
                days_left=days_left,
                amount=policy.premium_per_period,
            )
        )
    items.sort(key=lambda item: item.due_date)
    return items


# ---------------------------------------------------------------------------
# 消息组装与发送
# ---------------------------------------------------------------------------
def _money(value: Decimal) -> str:
    return f"¥{Decimal(value):,.2f}"


def _due_phrase(days_left: int) -> str:
    """天数的人话描述（纯文本，**不带任何标记**）。"""
    if days_left < 0:
        return f"已逾期 {-days_left} 天"
    if days_left == 0:
        return "今天到期"
    return f"剩余 {days_left} 天"


def _expiry_phrase(days_left: int) -> str:
    """保障到期的天数描述（已过期不说「逾期」，那是缴费的说法）。"""
    if days_left < 0:
        return f"已过期 {-days_left} 天"
    if days_left == 0:
        return "今天到期"
    return f"剩余 {days_left} 天"


def _expiry_block(item: ExpiryItem) -> str:
    who = f"{item.insured_name} · " if item.insured_name else ""
    return (
        f"· {who}{item.product_name}（{item.company}）\n"
        f"  保障至 {item.coverage_end.isoformat()}（{_expiry_phrase(item.days_left)}）"
    )


def _due_block(item: DueItemView) -> str:
    who = f"{item.insured_name} · " if item.insured_name else ""
    return (
        f"· {who}{item.product_name}\n"
        f"  {item.due_date.isoformat()} 应缴 {_money(item.amount)}（{_due_phrase(item.days_left)}）"
    )


def build_text(
    expiring: list[ExpiryItem],
    dues: list[DueItemView],
    today: date,
) -> str:
    """组装一条**纯文本**消息（``msgtype=text``）。

    为什么不用 markdown
    -------------------
    企业微信群机器人的 ``markdown`` 消息**在微信里不显示**——如果这个群开了
    「微信互通」，微信端只会看到一行「暂不支持此消息类型，点击前往企业微信查看」，
    等于提醒白发了。``text`` 是唯一在**微信 / 企业微信两端都能正常显示**的类型。

    代价是丢掉加粗与红色高亮，因此改用「【】分段 + 缩进」表达层级，
    并对逾期 / 当天到期用文字强调（如「已逾期 3 天」）。

    长度限制
    --------
    企业微信 ``text`` 的 ``content`` 上限 **2048 字节**（UTF-8，一个汉字 3 字节），
    比 markdown 的 4096 更紧。因此按**字节**预算逐条累加，超出后折叠为
    「……另有 N 条未列出」，保证消息不被接口直接拒绝。
    """
    lines: list[str] = [f"家庭保单提醒（{today.isoformat()}）", ""]
    dropped = 0

    def used_bytes() -> int:
        # +1：每行末尾的换行符
        return sum(len(line.encode("utf-8")) + 1 for line in lines)

    def append_block(block: str) -> bool:
        """按字节预算追加；放不下返回 False（由调用方计入「未列出」）。"""
        if used_bytes() + len(block.encode("utf-8")) + 1 > MAX_BYTES - RESERVED_BYTES:
            return False
        lines.append(block)
        return True

    if expiring:
        append_block(f"【保障即将到期】共 {len(expiring)} 份")
        for item in expiring:
            if not append_block(_expiry_block(item)):
                dropped += 1
        lines.append("")

    if dues:
        total = sum((item.amount for item in dues), Decimal("0"))
        append_block(f"【近期应缴费】共 {len(dues)} 笔，合计 {_money(total)}")
        for item in dues:
            if not append_block(_due_block(item)):
                dropped += 1
        lines.append("")

    if dropped:
        lines.append(f"……另有 {dropped} 条未列出，请打开工具查看")
    lines.append("（由「家庭保险整理工具」自动发送）")
    return "\n".join(lines)


def send_wechat(webhook: str, content: str, timeout: float = 10.0) -> None:
    """向企业微信机器人发送**纯文本**；失败抛 ``AppError``（由调用方决定是否吞掉）。"""
    url = validate_webhook(webhook)
    if not url:
        raise AppError(422, "VALIDATION_ERROR", "未配置 Webhook 地址", {"field": "webhook"})

    payload = json.dumps(
        {"msgtype": "text", "text": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AppError(502, "NOTIFY_FAILED", f"企业微信返回 HTTP {exc.code}", None) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AppError(502, "NOTIFY_FAILED", f"连接企业微信失败：{exc}", None) from exc
    except json.JSONDecodeError as exc:
        raise AppError(502, "NOTIFY_FAILED", "企业微信返回内容无法解析", None) from exc

    if int(body.get("errcode", -1)) != 0:
        errcode = body.get("errcode")
        errmsg = str(body.get("errmsg", ""))[:200]
        raise AppError(
            502,
            "NOTIFY_FAILED",
            _ERROR_HINTS.get(errcode) or f"企业微信返回错误：{errcode} {errmsg}",
            {"errcode": errcode, "errmsg": errmsg},
        )


def run_daily_check(db: Session, now: Optional[datetime] = None) -> dict[str, Any]:
    """执行一次每日检查。返回结构化结果，便于测试与日志。"""
    now = now or datetime.now()
    settings = get_settings(db)

    if not settings.enabled or not settings.webhook:
        return {"sent": False, "reason": "disabled"}
    if now.strftime("%H:%M") < settings.send_time:
        return {"sent": False, "reason": "too_early"}
    if _get_raw(db, "last_sent_date") == now.date().isoformat():
        return {"sent": False, "reason": "already_sent"}

    expiring = (
        collect_expiring(db, now.date(), settings.expiry_days)
        if settings.expiry_enabled
        else []
    )
    dues = collect_due(db, now.date(), settings.due_days) if settings.due_enabled else []
    if not expiring and not dues:
        return {"sent": False, "reason": "nothing_to_send"}

    send_wechat(settings.webhook, build_text(expiring, dues, now.date()))
    _write(db, {"last_sent_date": now.date().isoformat()})
    db.commit()
    return {
        "sent": True,
        "expiring": len(expiring),
        "due": len(dues),
    }


def send_test(db: Session) -> dict[str, Any]:
    """立即发送一条测试消息（不受 send_time / 去重限制）。"""
    settings = get_settings(db)
    if not settings.webhook:
        raise AppError(422, "VALIDATION_ERROR", "未配置 Webhook 地址", {"field": "webhook"})

    today = date.today()
    expiring = collect_expiring(db, today, settings.expiry_days)
    dues = collect_due(db, today, settings.due_days)
    if expiring or dues:
        content = build_text(expiring, dues, today)
    else:
        content = (
            f"## 家庭保单提醒（测试）\n\n"
            f"> 通知已配置成功。当前没有临近到期或待缴的保单。\n\n"
            f"> 由「家庭保险整理工具」发送"
        )
    send_wechat(settings.webhook, content)
    return {"sent": True, "expiring": len(expiring), "due": len(dues)}


def run_check_safe() -> None:
    """供后台任务调用：自行开 session、吞掉所有异常。"""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        result = run_daily_check(db)
        if result.get("sent"):
            logger.info("通知已发送：到期 %s 份 / 待缴 %s 笔", result.get("expiring"), result.get("due"))
    except Exception:  # noqa: BLE001 - 通知失败不能影响应用
        logger.warning("每日通知检查失败（已忽略）", exc_info=True)
    finally:
        db.close()
