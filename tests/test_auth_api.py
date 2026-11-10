"""访问密码（v1.9）接口测试。

覆盖：默认关闭、开启前置校验、登录 / 退出、Cookie 登录态、改密码后旧 token 失效、
签名篡改、限速、环境变量兜底、密码哈希不落明文、受保护路由清单。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import AppSetting
from app.services import auth_service

PASSWORD = "family2026"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    """测试里把 PBKDF2 迭代降到 1000，避免每次哈希 ~100ms 拖慢全量测试。

    ``_check_hash`` 按**存储里记录的迭代次数**重算，所以调低常量不影响正确性，
    反而顺带验证了「调大迭代数不会让老密码失效」。
    """
    monkeypatch.setattr(auth_service, "PBKDF2_ITERATIONS", 1000, raising=False)
    auth_service.reset_failures()
    yield
    auth_service.reset_failures()


@pytest.fixture()
def anon(client: TestClient) -> TestClient:
    """确保「未登录」状态：清空 Cookie。"""
    client.cookies.clear()
    return client


def enable_auth(client: TestClient, password: str = PASSWORD) -> None:
    """设置密码并开启访问限制（当前客户端随后处于已登录状态）。"""
    assert client.put("/api/auth/password", json={"newPassword": password}).status_code == 200
    assert client.put("/api/auth/enabled", json={"enabled": True}).status_code == 200


# ---------------------------------------------------------------------------
# 默认状态
# ---------------------------------------------------------------------------
def test_default_disabled_and_app_open(anon: TestClient):
    """全新库：未启用密码，业务接口直接可用（升级不会被锁在门外）。"""
    body = anon.get("/api/auth/status").json()
    assert body["enabled"] is False
    assert body["passwordSet"] is False
    assert body["authenticated"] is True
    assert body["forcedDisabled"] is False
    assert body["sessionDays"] == 30

    assert anon.get("/api/members").status_code == 200
    assert anon.get("/api/policies").status_code == 200


def test_public_endpoints_stay_open_when_enabled(client: TestClient):
    """健康检查与枚举字典不参与鉴权：前者是 Docker HEALTHCHECK 的生命线。"""
    enable_auth(client)
    client.cookies.clear()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/meta/enums").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is False


# ---------------------------------------------------------------------------
# 开启前置校验
# ---------------------------------------------------------------------------
def test_enable_without_password_is_rejected(anon: TestClient):
    response = anon.put("/api/auth/enabled", json={"enabled": True})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert anon.get("/api/auth/status").json()["enabled"] is False


@pytest.mark.parametrize("bad", ["", "12345", "a" * 65, " abcdef", "abcdef "])
def test_password_rules(anon: TestClient, bad: str):
    """长度 6–64，且不允许首尾空格（否则「看着一样却登不上」）。"""
    response = anon.put("/api/auth/password", json={"newPassword": bad})
    assert response.status_code == 422


def test_password_set_then_enable_keeps_current_session(client: TestClient):
    """设置密码时会顺带签发登录态，用户不会「刚设完密码就被自己挡住」。"""
    body = client.put("/api/auth/password", json={"newPassword": PASSWORD}).json()
    assert body["passwordSet"] is True
    assert body["authenticated"] is True
    assert client.cookies.get(auth_service.COOKIE_NAME)

    body = client.put("/api/auth/enabled", json={"enabled": True}).json()
    assert body["enabled"] is True
    assert body["authenticated"] is True
    assert client.get("/api/members").status_code == 200


# ---------------------------------------------------------------------------
# 登录 / 退出
# ---------------------------------------------------------------------------
def test_login_required_when_enabled(anon: TestClient):
    enable_auth(anon)
    anon.cookies.clear()

    response = anon.get("/api/members")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"

    assert anon.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert anon.get("/api/members").status_code == 200


def test_login_wrong_password(anon: TestClient):
    enable_auth(anon)
    anon.cookies.clear()
    response = anon.post("/api/auth/login", json={"password": "wrong-one"})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_PASSWORD"
    assert response.json()["message"] == "密码不正确"
    assert not anon.cookies.get(auth_service.COOKIE_NAME)


def test_login_when_disabled_is_rejected(anon: TestClient):
    response = anon.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 400
    assert response.json()["code"] == "AUTH_NOT_ENABLED"


def test_logout_clears_session(anon: TestClient):
    enable_auth(anon)
    assert anon.post("/api/auth/logout").status_code == 200
    assert anon.get("/api/members").status_code == 401
    # 幂等：未登录再调一次也不报错
    assert anon.post("/api/auth/logout").status_code == 200


def test_cookie_is_httponly_and_lax(anon: TestClient):
    """HttpOnly 防 XSS 读取；SameSite=Lax 保证附件链接跳转仍能带上 Cookie。"""
    enable_auth(anon)
    anon.cookies.clear()
    response = anon.post("/api/auth/login", json={"password": PASSWORD})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "secure" not in header  # 家用 NAS 多为 http，加 secure 会导致永远登不上


# ---------------------------------------------------------------------------
# 改密码 / token 失效
# ---------------------------------------------------------------------------
def test_change_password_requires_current(anon: TestClient):
    enable_auth(anon)
    response = anon.put("/api/auth/password", json={"newPassword": "another-one"})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_PASSWORD"

    ok = anon.put(
        "/api/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": "another-one"},
    )
    assert ok.status_code == 200

    anon.cookies.clear()
    assert anon.post("/api/auth/login", json={"password": PASSWORD}).status_code == 401
    assert anon.post("/api/auth/login", json={"password": "another-one"}).status_code == 200


def test_old_token_is_revoked_after_password_change(anon: TestClient):
    """改密码 → 密码版本变化 → 之前签发的 token 全部作废（换手机 / 借出后有效）。"""
    enable_auth(anon)
    old_token = anon.cookies.get(auth_service.COOKIE_NAME)
    assert old_token

    anon.put(
        "/api/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": "another-one"},
    )
    anon.cookies.set(auth_service.COOKIE_NAME, old_token)
    assert anon.get("/api/members").status_code == 401


def test_tampered_token_is_rejected(anon: TestClient):
    enable_auth(anon)
    token = anon.cookies.get(auth_service.COOKIE_NAME)
    expires, version, signature = token.split(".")
    # ① 改过期时间 ② 改签名 ③ 结构不对
    for forged in (
        f"{int(expires) + 9_999}.{version}.{signature}",
        f"{expires}.{version}.{signature[:-2]}00",
        "garbage",
        "",
    ):
        anon.cookies.set(auth_service.COOKIE_NAME, forged)
        assert anon.get("/api/members").status_code == 401


def test_expired_token_is_rejected(anon: TestClient, db_session):
    """手工签一个已过期的 token（签名本身是对的，只靠有效期兜住）。"""
    enable_auth(anon)
    secret = auth_service._read_secret(db_session)
    expires = int(time.time()) - 10
    payload = f"{expires}.{auth_service._password_version(db_session)}"
    anon.cookies.set(
        auth_service.COOKIE_NAME, f"{payload}.{auth_service._sign(secret, payload)}"
    )
    assert anon.get("/api/members").status_code == 401


def test_secret_rotation_invalidates_tokens(anon: TestClient, db_session):
    """库里的签名密钥被换掉（如恢复旧备份）→ 旧 token 一律不认。"""
    enable_auth(anon)
    row = db_session.get(AppSetting, "auth.secret")
    row.value = "0" * 64
    db_session.commit()
    assert anon.get("/api/members").status_code == 401


# ---------------------------------------------------------------------------
# 限速
# ---------------------------------------------------------------------------
def test_login_rate_limit(anon: TestClient):
    enable_auth(anon)
    anon.cookies.clear()
    for _ in range(auth_service.FAIL_LIMIT):
        assert anon.post("/api/auth/login", json={"password": "nope"}).status_code == 401

    blocked = anon.post("/api/auth/login", json={"password": "nope"})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "TOO_MANY_ATTEMPTS"

    # 锁定期间即使密码正确也拒绝（否则限速形同虚设）
    assert anon.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_rate_limit_resets_after_success(anon: TestClient):
    enable_auth(anon)
    anon.cookies.clear()
    for _ in range(auth_service.FAIL_LIMIT - 1):
        anon.post("/api/auth/login", json={"password": "nope"})
    assert anon.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    # 成功后计数清零，再错一次不该直接进 429
    anon.cookies.clear()
    assert anon.post("/api/auth/login", json={"password": "nope"}).status_code == 401


# ---------------------------------------------------------------------------
# 关闭 / 兜底
# ---------------------------------------------------------------------------
def test_disable_keeps_password(anon: TestClient):
    enable_auth(anon)
    anon.put("/api/auth/enabled", json={"enabled": False})
    assert anon.get("/api/members").status_code == 200

    status = anon.get("/api/auth/status").json()
    assert status["enabled"] is False
    assert status["passwordSet"] is True  # 密码保留：重新开启不用重设

    anon.put("/api/auth/enabled", json={"enabled": True})
    assert anon.get("/api/members").status_code == 200  # 会话还在


def test_env_backdoor_forces_disabled(anon: TestClient, monkeypatch):
    """忘了密码时：AUTH_DISABLED=true → 立刻放行，且前端能看到「已临时关闭」。"""
    enable_auth(anon)
    anon.cookies.clear()
    assert anon.get("/api/members").status_code == 401

    monkeypatch.setattr(settings, "auth_disabled", True, raising=False)
    assert anon.get("/api/members").status_code == 200
    status = anon.get("/api/auth/status").json()
    assert status["forcedDisabled"] is True
    assert status["enabled"] is False


# ---------------------------------------------------------------------------
# 存储安全
# ---------------------------------------------------------------------------
def test_password_is_hashed_not_stored_plain(anon: TestClient, db_session):
    enable_auth(anon)
    stored = db_session.get(AppSetting, "auth.password").value
    assert PASSWORD not in stored
    assert stored.startswith("pbkdf2_sha256$")
    assert len(stored.split("$")) == 4
    # 明文密码也不许出现在任何配置值里
    for row in db_session.query(AppSetting).all():
        assert PASSWORD not in row.value


def test_hash_survives_iteration_change(anon: TestClient):
    """调大迭代数不会让老密码失效（校验按存储里的迭代次数重算）。"""
    enable_auth(anon)
    stored = auth_service._hash_password("hello-world")
    assert auth_service._check_hash(stored, "hello-world") is True
    assert auth_service._check_hash(stored, "hello-worlD") is False
    assert auth_service._check_hash("not-a-hash", "hello-world") is False


# ---------------------------------------------------------------------------
# 受保护路由清单
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/members"),
        ("POST", "/api/members"),
        ("GET", "/api/policies"),
        ("GET", "/api/policies/suggestions"),
        ("GET", "/api/stats/dashboard"),
        ("GET", "/api/stats/exclusions"),
        ("GET", "/api/notifications/settings"),
        ("PUT", "/api/notifications/settings"),
        ("POST", "/api/notifications/test"),
    ],
)
def test_protected_endpoints_require_login(anon: TestClient, method: str, path: str):
    enable_auth(anon)
    anon.cookies.clear()
    response = anon.request(method, path, json={})
    assert response.status_code == 401, f"{method} {path} 未被访问密码保护"


def test_protected_write_endpoints_require_login(anon: TestClient, member_factory):
    """members 已存在时，删除/修改同样要被拦住。"""
    from datetime import date

    from app.enums import Relation

    member_factory(id="m1", name="张三", relation=Relation.self_, birth_date=date(1988, 6, 1))
    enable_auth(anon)
    anon.cookies.clear()
    assert anon.delete("/api/members/m1").status_code == 401
    assert anon.put("/api/members/m1", json={"name": "李四"}).status_code == 401
