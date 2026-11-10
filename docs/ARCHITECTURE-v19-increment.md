# 家庭保险整理工具 — v1.9 增量架构设计：访问密码

> **版本号**：`app/__init__.py` → **1.9.0**
> `docker-compose.yml` / `docker-compose.build.yml` 的 `image:`、`Dockerfile` 的 `LABEL`、
> `tools/offline-image-build/step3~5` 同步。
>
> **一句话**：给 `/api/*` 加一道**可选**的口令门；默认关、忘了能进、零新增依赖、零数据迁移。

---

## 1. 需求与范围

用户原话：「增加一个访问密码功能，版本号小变动就行。」

### 1.1 三条硬约束（先定，再写代码）

| 约束 | 为什么 |
|---|---|
| **默认关闭** | 升级即开启 = 用户打开网址被「请输入密码」堵住，而他不知道有这回事 |
| **必须有后门** | 单用户工具、密码全家共用；没有后门 = 忘一次就重装 |
| **必须服务端校验** | 前端校验是假锁：任何 `curl` 都能绕过 |

### 1.2 明确不做

- 多用户 / 角色 / 审计日志（给单人家用设计的，不做账号体系）；
- 找回密码的邮件 / 短信（没有邮件通道，也不该把口令托管到外部）；
- 会话表 / Redis / JWT 库（无状态签名足够，见 §3.3）；
- 对静态文件设防（HTML/JS/CSS 里没有数据，登录页本身就是它的一部分）。

---

## 2. 数据模型

### 2.1 复用 `app_settings`，不建新表

| key | 值 | 说明 |
|---|---|---|
| `auth.enabled` | `"true"` / `"false"` | 缺键 = `false` |
| `auth.password` | `pbkdf2_sha256$200000$<salt-hex>$<hash-hex>` | 缺键 = 未设置 |
| `auth.secret` | 64 位 hex | HMAC 签名密钥，**首次签发 token 时惰性生成** |

「缺键即默认值」是 v1.8 就立下的规矩（见 `ARCHITECTURE-v18-increment.md` §2.1），
本次继续沿用：**不新增表、不新增列、不登记 `_ADDITIVE_COLUMNS`、不回填**。

### 2.2 配置项（`app/config.py`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUTH_SESSION_DAYS` | `30` | 登录态有效期（天），Cookie 与 token 同时生效 |
| `AUTH_DISABLED` | `false` | **兜底开关**：`true` 时 `is_enabled()` 恒 `False` |

---

## 3. 口令与登录态

### 3.1 密码哈希

```
hash = pbkdf2_sha256(password, salt=16B 随机, iterations=200_000)
存储 = "pbkdf2_sha256$200000$<salt>$<hash>"
```

- 迭代数**写进字符串**，校验时按存储值重算 → 将来调大迭代数，老密码不会失效；
- 200,000 次在 NAS 上约 100–200 ms：人手输入无感，脚本爆破不可行；
- 用 `hmac.compare_digest` 比较，避免时序侧信道。

### 3.2 密码规则

长度 6–64；**不允许首尾空格**（否则「看着一样却登不上」，且难以自查）。
不做「必须含大小写数字」这类复杂度要求——家庭场景记不住，反而会被写在便签上。

### 3.3 登录态：无状态签名 token

```
token = "<过期时间戳>.<密码版本>.<HMAC-SHA256(secret, "过期时间戳.密码版本").hex>"
密码版本 = sha256(存储的密码哈希)[:16]
```

| 性质 | 怎么来的 |
|---|---|
| 服务重启不掉线 | 密钥持久化在 `auth.secret`，不靠内存会话 |
| 不建会话表 | 全部信息都在 token 里，验签即可 |
| 改密码 = 退出所有设备 | 密码哈希变 → 密码版本变 → 旧 token 全部失效 |
| 恢复旧备份也不认旧 token | 备份里的 `auth.secret` 与当前不同 → 验签失败 |

Cookie 属性：`HttpOnly`（JS 读不到）、`SameSite=Lax`（附件 `<a href>` 跳转能带上）、
`path=/`、`max_age=30天`、**不加 `Secure`**。
理由：家用 NAS 多为 `http://192.168.x.x:8000`，加了 `Secure` 浏览器就不发送，
表现为「密码明明对，进去了又让你登」——极难排查。

---

## 4. API 契约

前缀 `/api/auth`。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/status` | 公开 | `{enabled, passwordSet, authenticated, forcedDisabled, sessionDays}` |
| POST | `/login` | 公开 | body `{password}`；成功写 Cookie，返回 status |
| POST | `/logout` | 公开 | 清 Cookie，幂等返回 `{ok:true}` |
| PUT | `/password` | 需登录 | body `{newPassword, currentPassword?}`；已启用时**必须**带当前密码 |
| PUT | `/enabled` | 需登录 | body `{enabled}`；开启前必须已设密码，否则 422 |

`authenticated` 的语义是「**这个会话现在能不能用**」：未启用密码时恒 `true`，
前端只需判断 `enabled && !authenticated` 决定是否弹登录页。

### 4.1 错误码

| HTTP | code | 场景 |
|---|---|---|
| 401 | `UNAUTHORIZED` | 业务接口未登录 / token 失效 |
| 401 | `INVALID_PASSWORD` | 登录密码错 / 改密码时当前密码错 |
| 400 | `AUTH_NOT_ENABLED` | 未启用密码却调登录 |
| 422 | `VALIDATION_ERROR` | 密码不合规 / 无密码就想开启 |
| 429 | `TOO_MANY_ATTEMPTS` | 5 分钟内错 5 次（`detail.retryAfter` 秒数） |

---

## 5. 守卫为什么挂在 `include_router` 上

```python
_PROTECTED = [Depends(require_auth)]
app.include_router(members.router, dependencies=_PROTECTED)
app.include_router(policies.router, dependencies=_PROTECTED)
app.include_router(attachments.router, dependencies=_PROTECTED)
app.include_router(stats.router, dependencies=_PROTECTED)
app.include_router(notifications.router, dependencies=_PROTECTED)
```

**不选中间件**，两个理由：

1. 中间件拿不到 `Depends(get_db)`，只能自己 `SessionLocal()`。测试里用内存库 + 依赖覆盖，
   守卫会读到**另一个数据库** → 静默失效（测试全绿但线上没锁）。
2. 放行名单要按路径字符串维护，新增接口容易漏。挂在 router 上，
   **新路由默认不带守卫**——加业务时被迫显式决定，不会默认裸奔。

公开的三个：`/api/health`（Docker HEALTHCHECK 的生命线）、`/api/meta/enums`（无业务数据）、
`/api/auth/*`（登录本身）。

---

## 6. 限速

| 项 | 值 |
|---|---|
| 维度 | 来源 IP（单实例家用部署，`request.client.host` 足够） |
| 窗口 | 300 秒 |
| 上限 | 5 次失败 |
| 处置 | `429 TOO_MANY_ATTEMPTS`，**锁定期间密码正确也拒绝** |
| 存储 | 进程内 `dict[str, list[float]]` + `threading.Lock`，重启即清零 |

> 已知局限：多 worker / 多副本会各算各的。当前 `Dockerfile` 固定单进程，不构成问题，
> 已在 `DEVELOPMENT-HISTORY.md` §14 待办里记着。

---

## 7. 前端

| 文件 | 改动 |
|---|---|
| `js/views/login.js`（新） | 全屏闸门：品牌 + 密码框 + 「进入」+ 忘记密码提示；`showLoginGate(onSuccess)` / `hideLoginGate()` / `isLoginGateVisible()` |
| `js/main.js` | `ensureAccess()` 先问 `/api/auth/status`；未登录只挂闸门，不加载任何视图；登录成功后 `bootApp()`；监听 `app:unauthorized`；`route()` 在闸门可见时直接返回 |
| `js/api.js` | 5 个 auth 方法；`request()` 里 `401 → 广播 app:unauthorized`（**排除 `/auth/login`**） |
| `js/views/settings.js` | 新增「访问密码」面板：开关、设置/修改密码弹窗、退出登录 |
| `index.html` | 新增 `#auth-gate` 容器；tab「通知设置」→「设置」 |
| `css/styles.css` | `.auth-gate` / `.auth-card` / `.auth-input` / `body.auth-locked` / `.field-stack` |

两个细节：

- `body.auth-locked` 会把顶栏与内容 `display:none`——否则键盘 Tab 仍能聚焦到被遮住的按钮；
- 401 广播**排除登录接口**：密码输错也是 401，若也广播，登录页会被重渲染，
  输入框清空 + 错误提示被冲掉，用户看到「点了没反应」。

---

## 8. 文件清单

**新增**
- `app/services/auth_service.py` — 哈希、token、限速、状态
- `app/routers/auth.py` — 5 个接口
- `static/js/views/login.js` — 登录闸门
- `tests/test_auth_api.py` — 35 例

**修改**
- `app/deps.py` — 新增 `require_auth`（路由级守卫）
- `app/config.py` — `auth_session_days` / `auth_disabled`
- `app/schemas.py` — `AuthStatusOut` / `AuthLoginIn` / `AuthPasswordIn` / `AuthEnabledIn`
- `app/main.py` — 注册 auth 路由；业务路由统一挂守卫
- `static/js/{api,main}.js`、`static/js/views/settings.js`、`static/index.html`、`static/css/styles.css`
- `docker-compose.yml`（附注释版的 `AUTH_DISABLED` 示例）、`docker-compose.build.yml`、`Dockerfile`、`app/__init__.py`
- `tools/offline-image-build/step3~5` — 版本号 / tar 名 / LABEL 断言
- `README.md`、`overview.md`、`docs/DEVELOPMENT-HISTORY.md`、`docs/DEPLOY-ZSPACE.md`

---

## 9. 验收标准

### 9.1 功能

- 全新库：`GET /api/auth/status` → `enabled=false, passwordSet=false, authenticated=true`，业务接口 200；
- 无密码时 `PUT /enabled {enabled:true}` → 422；
- 设密码 → 自动登录（响应 `authenticated=true`）→ 开开关 → 当前会话仍可用；
- 换一个"没登录"的客户端 → 业务接口 401，`/api/health` 与 `/api/meta/enums` 仍 200；
- 输错密码 → 401「密码不正确」；连错 5 次 → 429，且此时输对也 429；
- 改密码 → 旧 token 401，新 token 正常；
- 关开关 → 无需登录；密码仍保留，重新开启不用重设；
- `AUTH_DISABLED=true` → 立即放行，`status.forcedDisabled=true`。

### 9.2 安全

- 库里任何配置值都不含明文密码；
- Cookie 三属性：`HttpOnly` / `SameSite=Lax` / **无** `Secure`；
- 篡改过期时间或签名 → 401；签名正确但已过期 → 401；
- 9 个业务端点（members / policies / suggestions / dashboard / exclusions / notifications ×3）逐个断言 401。

### 9.3 回归声明

- 算例集 A/B/C 的统计口径**逐值不变**（本次不碰计算层）；
- 443 条既有测试全绿，新增 35 条 → **478 passed**；
- `requirements.txt` **未改**；依赖层 `diff_id` 与 v1.8.1 一致（`0da8e586…`）。

---

## 10. 兼容性与迁移

- 数据结构：**零变更**（只往 `app_settings` 加键，缺键即默认）；
- 行为：默认关闭 → 升级后**与 v1.8 完全一致**，不会打断任何人；
- 回滚：换回 v1.8.1 镜像即可，`app_settings` 里的 `auth.*` 键对旧版本无意义、无副作用。

## 11. 待明确事项

1. 要不要「连续登录失败 N 次后企微推送一条告警」？—— 与 v1.8 通知可复用，但会增加噪音，暂不做。
2. `sessionDays` 是否需要在页面上可配？—— 现在固定 30 天，用户没提。
