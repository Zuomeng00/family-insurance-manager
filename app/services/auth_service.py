"""访问密码（v1.9）。

设计要点
--------
1. **默认关闭**。升级后不会把人关在门外：``auth.enabled`` 缺键即 ``false``，
   开启前必须先设置密码，否则拒绝开启（``422``）。
2. 密码用标准库 ``hashlib.pbkdf2_hmac`` 加盐哈希，**零新增依赖**；
   库里只存 ``pbkdf2_sha256$迭代次数$盐$哈希``，**永不留明文**。
3. 登录态是 **HMAC 签名的无状态 token**（``过期时间.密码版本.签名``），
   放 HttpOnly Cookie：服务重启不掉线；改密码后「密码版本」变化 →
   旧的 token 立刻全部失效，不需要维护会话表。
4. 兜底后门：环境变量 ``AUTH_DISABLED=true`` 强制忽略密码。
   忘了密码时改这一行重启即可进入，**不用动数据库**。
5. 登录失败限速：同一来源 5 分钟内错 5 次即 429，挡住脚本爆破。

为什么不用 ``itsdangerous`` / ``passlib`` / ``jwt``
--------------------------------------------------
``hashlib`` + ``hmac`` + ``secrets`` 都是标准库，功能完全够用，
而「多一个依赖」意味着离线镜像的 wheel 包、依赖层、审计清单都要跟着动。
（v1.8 的企业微信通知也是同一取舍。）
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..errors import AppError
from ..models import AppSetting

KEY_PREFIX = "auth."

#: 登录态 Cookie 名。不含敏感信息，只是签名后的过期时间串。
COOKIE_NAME = "fam_auth"

#: 密码长度区间（按字符计，非字节）
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 64

#: PBKDF2 迭代次数。取 20 万：NAS 上单次校验约 100–200ms，
#: 人手输密码无感，脚本爆破却近乎不可行。
PBKDF2_ITERATIONS = 200_000

#: 限速参数：窗口内失败达到上限即锁定到窗口结束
FAIL_LIMIT = 5
FAIL_WINDOW_SECONDS = 300

DEFAULTS: dict[str, str] = {
    "enabled": "false",
    "password": "",
    "secret": "",
}


# ---------------------------------------------------------------------------
# 键值读写（沿用 v1.8 通知的「缺键即默认」模式，存量库零迁移）
# ---------------------------------------------------------------------------
def _raw_map(db: Session) -> dict[str, str]:
    rows = db.query(AppSetting).filter(AppSetting.key.like(f"{KEY_PREFIX}%")).all()
    return {row.key[len(KEY_PREFIX) :]: row.value for row in rows}


def _get(db: Session, name: str) -> str:
    row = db.get(AppSetting, f"{KEY_PREFIX}{name}")
    return row.value if row is not None else DEFAULTS.get(name, "")


def _put(db: Session, changes: dict[str, str], commit: bool = True) -> None:
    for name, value in changes.items():
        key = f"{KEY_PREFIX}{name}"
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    if commit:
        db.commit()


def _as_bool(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def is_forced_disabled() -> bool:
    """环境变量 ``AUTH_DISABLED=true`` 时强制忽略密码（忘记密码的兜底通道）。"""
    return bool(settings.auth_disabled)


def is_password_set(db: Session) -> bool:
    """是否已设置过密码（与「是否启用」无关：关掉开关后密码仍保留）。"""
    return bool((_get(db, "password") or "").strip())


def is_enabled(db: Session) -> bool:
    """是否真的需要输入密码。环境变量强制关闭时恒 ``False``。"""
    if is_forced_disabled():
        return False
    return _as_bool(_get(db, "enabled")) and is_password_set(db)


def session_days() -> int:
    return max(1, int(settings.auth_session_days))


def status(db: Session, token: Optional[str] = None) -> dict[str, object]:
    """给前端的完整状态。

    ``authenticated`` 的语义是「这个会话现在能不能用」，因此未启用密码时
    恒为 ``True``——前端只需判断 ``enabled && !authenticated`` 就够。
    """
    enabled = is_enabled(db)
    return {
        "enabled": enabled,
        "password_set": is_password_set(db),
        "authenticated": (not enabled) or verify_token(db, token),
        "forced_disabled": is_forced_disabled(),
        "session_days": session_days(),
    }


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------
def _validate_password(password: str) -> str:
    """校验并返回原始密码（不做 strip，避免「看起来对但差一个空格」）。"""
    if not isinstance(password, str):
        raise AppError(422, "VALIDATION_ERROR", "密码格式不正确", {"field": "newPassword"})
    if password != password.strip():
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "密码不能以空格开头或结尾",
            {"field": "newPassword"},
        )
    length = len(password)
    if length < PASSWORD_MIN_LENGTH:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            f"密码至少 {PASSWORD_MIN_LENGTH} 位",
            {"field": "newPassword"},
        )
    if length > PASSWORD_MAX_LENGTH:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            f"密码最多 {PASSWORD_MAX_LENGTH} 位",
            {"field": "newPassword"},
        )
    return password


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _check_hash(stored: str, password: str) -> bool:
    """按存储里的迭代次数重算——将来调大迭代数不会让老密码失效。"""
    try:
        algorithm, iterations_text, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def verify_password(db: Session, password: str) -> bool:
    stored = (_get(db, "password") or "").strip()
    if not stored or not password:
        return False
    return _check_hash(stored, password)


# ---------------------------------------------------------------------------
# 登录态 token（无状态签名，不落库）
# ---------------------------------------------------------------------------
def _password_version(db: Session) -> str:
    """密码指纹：改密码 → 指纹变化 → 已签发的 token 全部作废。"""
    stored = (_get(db, "password") or "").strip()
    if not stored:
        return "none"
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:16]


def _read_secret(db: Session) -> str:
    return (_get(db, "secret") or "").strip()


def _ensure_secret(db: Session) -> str:
    """首次签 token 时惰性生成签名密钥并持久化（重启后 token 依然有效）。"""
    secret = _read_secret(db)
    if secret:
        return secret
    secret = secrets.token_hex(32)
    _put(db, {"secret": secret})
    return secret


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(db: Session) -> str:
    """签发登录 token：``过期时间戳.密码版本.签名``。"""
    secret = _ensure_secret(db)
    expires_at = int(time.time()) + session_days() * 86400
    payload = f"{expires_at}.{_password_version(db)}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(db: Session, token: Optional[str]) -> bool:
    """校验 token：签名对、未过期、且密码没改过。"""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expires_text, version, signature = parts
    try:
        expires_at = int(expires_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False

    secret = _read_secret(db)
    if not secret:
        return False  # 从未签发过 token（密钥未生成）→ 一律不认
    if not hmac.compare_digest(_sign(secret, f"{expires_text}.{version}"), signature):
        return False
    return hmac.compare_digest(version, _password_version(db))


# ---------------------------------------------------------------------------
# 变更操作
# ---------------------------------------------------------------------------
def set_password(db: Session, new_password: str, current_password: Optional[str] = None) -> None:
    """设置 / 修改密码。

    已启用访问限制时必须提供当前密码——**路由层已经要求登录**，这里再核一次，
    防的是「浏览器没锁、人走开了」的场景。
    """
    validated = _validate_password(new_password)
    if is_enabled(db):
        if not verify_password(db, current_password or ""):
            raise AppError(401, "INVALID_PASSWORD", "当前密码不正确", {"field": "currentPassword"})
    _put(db, {"password": _hash_password(validated)})


def set_enabled(db: Session, enabled: bool) -> None:
    """开启 / 关闭访问限制。开启前必须已设置密码。"""
    if enabled and not is_password_set(db):
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "请先设置访问密码，再开启访问限制",
            {"field": "password"},
        )
    _put(db, {"enabled": "true" if enabled else "false"})


def clear_password(db: Session) -> None:
    """清除密码并关闭访问限制（「彻底忘记」时的代码级兜底，当前无 UI 入口）。"""
    _put(db, {"password": "", "enabled": "false"})


# ---------------------------------------------------------------------------
# 登录失败限速（进程内，够用：单实例家用部署）
# ---------------------------------------------------------------------------
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def reset_failures() -> None:
    """清空限速状态（测试用）。"""
    with _lock:
        _failures.clear()


def _recent_failures(key: str, now: float) -> list[float]:
    stamps = [stamp for stamp in _failures.get(key, []) if now - stamp < FAIL_WINDOW_SECONDS]
    if stamps:
        _failures[key] = stamps
    else:
        _failures.pop(key, None)
    return stamps


def ensure_not_locked(key: str) -> None:
    """失败次数达上限则拒绝（``429``），并告知还需等多久。"""
    now = time.monotonic()
    with _lock:
        stamps = _recent_failures(key, now)
        if len(stamps) >= FAIL_LIMIT:
            wait = int(FAIL_WINDOW_SECONDS - (now - min(stamps))) + 1
            raise AppError(
                429,
                "TOO_MANY_ATTEMPTS",
                f"密码错误次数过多，请 {max(1, wait // 60 + 1)} 分钟后再试",
                {"retryAfter": max(1, wait)},
            )


def record_failure(key: str) -> None:
    with _lock:
        _failures.setdefault(key, []).append(time.monotonic())


def clear_failures(key: str) -> None:
    with _lock:
        _failures.pop(key, None)


__all__ = [
    "COOKIE_NAME",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "clear_password",
    "ensure_not_locked",
    "is_enabled",
    "is_forced_disabled",
    "is_password_set",
    "issue_token",
    "record_failure",
    "clear_failures",
    "reset_failures",
    "session_days",
    "set_enabled",
    "set_password",
    "status",
    "verify_password",
    "verify_token",
]
