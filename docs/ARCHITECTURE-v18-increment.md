# 家庭保险整理工具 — v1.8 增量架构设计：企业微信到期提醒

> **基线**：v1.7.0，全量测试 **404 passed**。
> **本文范围**：仅描述 v1.8 增量；未提及处一律沿用 `docs/ARCHITECTURE.md`（§0 ~ §5，含 §5.5 / §5.6 口径与算例）与 `docs/ARCHITECTURE-v13-increment.md`、`docs/ARCHITECTURE-v17-increment.md`。
> **业务口径唯一真源**：`docs/ARCHITECTURE.md` §5。本文**不重定义**任何既有指标。
> **版本号**：`app/__init__.py` → **1.8.0**；`docker-compose.yml` 的 `image:` 同步 1.8.0。
> **硬约束**：前端零 CDN / 无构建；**零新增 Python 依赖**（只用标准库 `urllib`）；不硬编码枚举；金额 DB 存分、计算 `Decimal`、API 输出 JSON number；不新增错误码（复用 `VALIDATION_ERROR` / `NOTIFY_FAILED`）；**既有接口与统计数字一律不变**。

---

## 1. 需求与范围

| | 需求原话 | 落地 |
|---|---|---|
| 主需求 | 「保险快到期了自动提醒我，通过企业微信机器人通知」 | 每日一条汇总消息，含**两类**提醒 |
| 参考图 | 其他软件的提醒设置页（启用开关 / 提前 N 天 / 提醒时间） | 语义参考，不照抄版式 |

### 1.1 「到期」有两个，必须分开

| 到期对象 | 字段 | 语义 | 用户动作 | v1.8 前 |
|---|---|---|---|---|
| 保障到期 | `coverage_end_date` | 这份保险快保不住了 | 续保 / 换产品 | **不存在**（v1.8 新增） |
| 缴费到期 | `next_due_date` | 这一期该交钱了 | 交钱 / 标记已缴 | v1.1 已有 |

只做后者会漏掉「一年期意外险该续保」，只做前者会漏掉「长期险每年要交的钱」。
→ **两类独立开关 + 独立提前天数**。

### 1.2 明确不做

- 短信 / 邮件 / 钉钉 / 浏览器推送（只在服务层留扩展点，不实现）；
- 逐单推送（会让人静音机器人）；
- 提醒历史记录表（只记 `last_sent_date`，够去重即可）；
- 企业微信「应用消息」（需 corpid/secret，个人用户拿不到，只用**群机器人 Webhook**）。

---

## 2. 数据模型

### 2.1 新表 `app_settings`（键值配置）

```sql
CREATE TABLE app_settings (
    key        VARCHAR(64)  NOT NULL,   -- 主键，形如 'notify.enabled'
    value      VARCHAR(512) NOT NULL,   -- 字符串化：bool → 'true'/'false'
    updated_at DATETIME     NOT NULL,
    PRIMARY KEY (key)
);
```

**为什么是键值表**：配置项会随迭代增加（以后可能加邮件、加静默时段），
专用表每加一项就要改 DDL + 迁移；键值表**加一项只是代码里 `DEFAULTS` 多一行**。

**为什么无需迁移**：新表由 `Base.metadata.create_all` 自动创建；
配置读取一律 `DEFAULTS` 兜底，**缺键即默认值**，所以
**不需要登记 `_ADDITIVE_COLUMNS`、不需要回填、不需要版本判断**。

**安全**：`AppSetting.__repr__` 故意不输出 `value`，避免 Webhook key 被 `logger.debug(obj)` 带进日志。

### 2.2 配置项

| key（前缀 `notify.`） | 默认 | 取值 |
|---|---|---|
| `enabled` | `false` | 总开关 |
| `webhook` | `""` | 完整地址（含 key），**仅存库，不出网** |
| `expiry_enabled` | `true` | 保障到期提醒开关 |
| `expiry_days` | `30` | 1–365 |
| `due_enabled` | `true` | 缴费提醒开关 |
| `due_days` | `7` | 1–365 |
| `send_time` | `09:00` | `HH:MM`，服务器本地时间 |
| `last_sent_date` | `""` | 去重标记 `YYYY-MM-DD` |

### 2.3 `Schedule` 新字段（派生，不落库）

```python
@dataclass(frozen=True)
class Schedule:
    ...
    coverage_end_date: Optional[date]   # None = 终身险
```

计算口径（`calc.calc_schedule`，与既有 `in_coverage` **同源**）：

| `coverage_term_type` | `coverage_end_date` |
|---|---|
| 终身 | `None` |
| 固定年限 N | `add_years(effective_date, N)` |
| 至某年龄 N | `add_years(insured.birth_date, N)` |

经 `PolicyOut.coverageEndDate` 透出（新字段**必须**加入 `@field_serializer` 的检查清单——
本次是 `Optional[date]`，不涉及金额序列化，但仍需确认不在 money 序列化范围内）。

---

## 3. API 契约

前缀 `/api/notifications`，与既有接口一致：成功直接返回对象，错误走统一
`{code, message, detail}`。

### 3.1 `GET /api/notifications/settings`

```json
{
  "enabled": true,
  "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=****1234",
  "webhookConfigured": true,
  "expiryEnabled": true,
  "expiryDays": 30,
  "dueEnabled": true,
  "dueDays": 7,
  "sendTime": "09:00",
  "lastSentDate": "2026-11-09",
  "today": "2026-11-09"
}
```

- `webhook` **永远是脱敏值**（`key=****末4位`）；`webhookConfigured` 才是「是否已配置」的真值。
- `lastSentDate` / `today` 供前端显示「今日已发送 / 上次发送」。

### 3.2 `PUT /api/notifications/settings`（部分更新）

请求体所有字段可省；**未传的键不改**（`model_dump(exclude_unset=True)` + 服务层过滤 `None`）。

| 场景 | 结果 |
|---|---|
| `{"dueDays": 15}` | 只改缴费提前天数，其余不动 |
| `{"webhook": ""}` | 清除地址，**同时自动把 `enabled` 置 false** |
| `{"enabled": true}` 且库中无 webhook | **422**「启用通知前请先填写企业微信 Webhook 地址」 |
| `{"webhook": "...key=****1234"}` | **422**「检测到已脱敏的 Webhook 地址，请重新粘贴完整地址」 |
| `{"sendTime": "7:5"}` | 归一为 `07:05` |
| `{"expiryDays": 0}` | 422（`Field(ge=1, le=365)`） |

### 3.3 `POST /api/notifications/test`

立即发送一条消息，**不受** `enabled` / `send_time` / `last_sent_date` 限制。
无 webhook → 422；企业微信报错 → 502 `NOTIFY_FAILED`。

```json
{"sent": true, "expiring": 2, "due": 1}
```

---

## 4. 调度设计

```
lifespan 启动
  └─ asyncio.create_task(_notification_loop())
        ├─ sleep(15)                        # 避开建表 / 迁移窗口
        └─ while True:
              await asyncio.to_thread(run_check_safe)   # 同步 DB + HTTP 丢线程池
              sleep(60)
```

`run_check_safe()` → `run_daily_check()` 的判定顺序（**短路**）：

| 顺序 | 条件 | 返回 reason |
|---|---|---|
| 1 | `not enabled or not webhook` | `disabled` |
| 2 | `now.strftime("%H:%M") < send_time` | `too_early` |
| 3 | `last_sent_date == today` | `already_sent` |
| 4 | 两类收集结果都为空 | `nothing_to_send` |
| 5 | 发送成功并写 `last_sent_date` | `sent: true` |

**关键取舍**

| 决定 | 理由 |
|---|---|
| 60 秒轮询而非 cron / APScheduler | 判断逻辑本就幂等，轮询最简单；**零新增依赖** |
| `last_sent_date` 落库而非内存 | 进程重启不重发；NAS 上容器随时会被重启 |
| `now >= send_time` 即发送（补发） | 09:00 该发而服务 11:00 才起，**晚发远好于漏发** |
| 异常全部吞掉（只 WARNING） | 通知失败的最坏结果是「今天没提醒」，不能是「工具打不开」 |
| 环境变量 `NOTIFICATION_SCHEDULER=false` 可关 | 排障时不希望后台任务干扰 |

> 时间口径 = **服务器本地时间**。容器已固定 `TZ=Asia/Shanghai`，与用户所在时区一致。

---

## 5. 提醒收集口径

### 5.1 候选集

只取 `status ∈ {生效中, 已缴清}` 的保单（已退保 / 已失效跳过）；
成员一次性批量载入（避免 N+1）。

### 5.2 保障即将到期（`collect_expiring`）

- 只针对 `status == 生效中`（已缴清的长期险通常不再续保）；
- `coverage_end_date is None`（终身险）→ 跳过；
- `-30 <= (end - today).days <= expiry_days` 才收：
  - 上界 = 用户配置的提前天数；
  - **下界 = -30**：已过期超过 30 天不再提醒（多半是用户忘了改状态，噪音大于价值）。

### 5.3 缴费临近 / 已逾期（`collect_due`）

- `is_paid_off` 或 `next_due_date is None` → 跳过；
- `-60 <= (due - today).days <= due_days` 才收（逾期超 60 天不再提醒）。

### 5.4 消息结构（单条 markdown）

```
## 家庭保单提醒（2026-11-09）

### 保障即将到期（2 份）
> 张三 · 综合意外（平安）
> 保障至 2026-11-20，剩余 11 天

### 近期应缴费（1 笔，合计 ¥1,200.00）
> 张三 · 百万医疗
> 2026-11-12 应缴 ¥1,200.00，剩余 3 天

> 由「家庭保险整理工具」自动发送
```

- 单类超过 `MAX_LINES = 20` 折叠为「另有 N 条未列出」（企业微信 markdown 有长度上限，宁可折叠不要截断）；
- 逾期 / 今天到日用 `<font color="warning">` 高亮；
- **不含**证件号、手机号、保单号等敏感信息。

---

## 6. Webhook 安全（四道闸）

| # | 威胁 | 对策 |
|---|---|---|
| 1 | 前端拿到完整 key | 接口只回传 `key=****末4位` |
| 2 | 把脱敏值存回去，覆盖真实 key | `validate_webhook()` 见 `****` → 422 |
| 3 | 配成任意 URL（SSRF 打内网） | scheme / host / path **三者全等**：`https` + `qyapi.weixin.qq.com` + `/cgi-bin/webhook/send` |
| 4 | 消息内容泄露隐私 | 只含产品名 / 公司 / 被保人姓名 / 金额 / 日期 |

另：`request` 用 `urllib`（不走代理），超时 10 秒；`errcode != 0` 一律视为失败，
常见码翻译成人话（`93000` / `45009` / `40058`），原始 `errcode` + `errmsg` 放 `detail` 便于排查。

---

## 7. 文件清单

**后端（新增 2）**
- `app/services/notification_service.py` — 配置读写 / 收集 / 组装 / 发送 / 调度入口
- `app/routers/notifications.py` — 3 个接口

**后端（修改 6）**
- `app/models.py`（`AppSetting`）、`app/calc.py`（`Schedule.coverage_end_date`）
- `app/schemas.py`（`NotifySettingsIn/Out`、`NotifyTestOut`、`PolicyOut.coverage_end_date`）
- `app/services/policy_service.py`（`_to_out` 透出 `coverage_end_date`）
- `app/schemas.py` / `app/config.py`（`notification_scheduler`）
- `app/main.py`（注册路由 + `_notification_loop` + lifespan 启停）
- `app/routers/__init__.py`、`app/services/__init__.py`（`__all__`）

**前端（新增 1，修改 4）**
- 新增 `static/js/views/settings.js`
- 修改 `static/index.html`（导航 + 容器）、`static/js/main.js`（路由 + 隐藏「+新增保单」）、
  `static/js/api.js`（3 个函数）、`static/css/styles.css`（`.setting-*` / `.switch`）

**测试（新增 1）**
- `tests/test_notifications_api.py` — 35 例

**文档 / 交付**
- 新增 `docs/ARCHITECTURE-v18-increment.md`（本文）、`docs/screenshots/demo/settings.png`
- 修改 `README.md`、`overview.md`、`docs/DEVELOPMENT-HISTORY.md`

---

## 8. 验收标准

### 8.1 功能

1. 未配置 Webhook 时开启开关 → 422，页面提示先填地址；
2. 配置后点「立即发送测试」→ 群里收到消息，页面提示「已发送」；
3. 保存后重新加载页面 → Webhook 显示 `key=****末4位`，输入框留空；
4. 只改「缴费提前天数」保存 → 其余配置不变；
5. 清空 Webhook → 开关自动变灰（关闭）；
6. 把脱敏值粘贴进输入框保存 → 422，库中真实 key 未变。

### 8.2 口径

| 用例 | 期望 |
|---|---|
| 终身险 | 不出现在「保障即将到期」 |
| 1 年期保单到期日 +10 天，`expiryDays=30` | 命中，`daysLeft=10` |
| 同上但 `expiryDays=9` | 不命中 |
| 已退保 / 已失效 | 两类都不命中 |
| 已缴清保单 | 不出现在「缴费提醒」 |
| 当天已发过 | 第二次调用返回 `already_sent`，不重复发送 |
| 无待提醒项 | `nothing_to_send`，**不发空消息**，不写 `last_sent_date` |

### 8.3 回归声明

- **既有接口响应字段只增不减**（`PolicyOut` 多一个 `coverageEndDate`，其余逐值不变）；
- **算例集 A/B/C 所有既有断言逐值不变**（本次不触碰任何统计口径）；
- 全量测试：**439 passed**（v1.7 基线 404 + 新增 35）。

---

## 9. 兼容性与迁移

| 项 | 结论 |
|---|---|
| 存量数据库 | 只需 `create_all` 建 `app_settings` 空表，**无 ALTER、无回填** |
| 升级失败风险 | 通知模块异常不影响启动（lifespan 中 try/finally 收尾，任务异常只记日志） |
| 关闭通知 | 页面关掉开关，或 `NOTIFICATION_SCHEDULER=false` |
| 回滚 | 换回 v1.7 镜像即可；`app_settings` 表被旧版本忽略，**不影响任何既有功能** |
| 依赖 | `requirements.txt` 未改（只增代码、不加包）。传递依赖版本随时间前移，依赖层 `diff_id` 与 v1.7 不同，属预期 |

---

## 10. 待明确事项

1. 提醒内容是否需要带「还剩 N 期未交」——用户未提，暂不加。
2. 是否需要「周末不推送」/ 静默时段——未提，暂不做。
3. 多收件人（同一个人多个群 / 多个机器人）——当前只支持一个 Webhook。
