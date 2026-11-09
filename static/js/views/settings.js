/**
 * 通知设置视图（v1.8）。
 *
 * 功能：
 * 1. 配置企业微信机器人 Webhook（**回显脱敏值**，输入框留空 = 不修改）；
 * 2. 两类提醒各自开关 + 提前天数：保障到期 / 缴费临近（含逾期）；
 * 3. 每日发送时间（`HH:MM`），到点后由后端后台任务发送**一条汇总消息**；
 * 4. 「立即发送测试」先保存当前表单，再发一条消息用于自检。
 *
 * 安全约束：真实 Webhook key 永不下发到前端，页面只显示 `key=****末4位`；
 * 因此保存时**不会**把脱敏值回写（后端也会拒收含 `****` 的地址）。
 */

import * as api from '../api.js';
import * as store from '../store.js';
import { escapeHtml, fmtDate } from '../format.js';
import { confirmModal } from '../components/modal.js';

/** Webhook 输入框为空时的提示语（区分「已配置」与「从未配置」）。 */
function webhookPlaceholder(configured) {
  return configured
    ? '已保存，留空表示不修改；如需更换请粘贴新的完整地址'
    : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx';
}

/** 从脱敏地址里取出 key 末 4 位，用于「当前已保存哪条机器人」的确认。 */
function webhookTail(masked) {
  const match = /key=\*+(\w{1,8})$/.exec(masked || '');
  return match ? match[1] : '';
}

/**
 * 顶部状态徽章：已启用 / 未启用 + 上次发送。
 * @param {object} data 服务端返回的配置
 * @returns {string}
 */
function statusHtml(data) {
  const on = data.enabled && data.webhookConfigured;
  const badge = on
    ? '<span class="badge active">已启用</span>'
    : '<span class="badge off">未启用</span>';
  let last = '尚未发送过提醒';
  if (data.lastSentDate) {
    last =
      data.lastSentDate === data.today
        ? `今日已发送（${fmtDate(data.lastSentDate)}）`
        : `上次发送：${fmtDate(data.lastSentDate)}`;
  }
  return `<div class="notify-status">${badge}<span>${escapeHtml(last)}</span></div>`;
}

/**
 * 渲染整页。
 * @param {HTMLElement} container
 */
export async function render(container) {
  container.innerHTML = '<div class="loading">加载中…</div>';

  let data;
  try {
    data = await api.getNotifySettings();
  } catch (error) {
    container.innerHTML = `<div class="empty-state">加载失败：${escapeHtml(error.message)}</div>`;
    return;
  }

  container.innerHTML = `
    <div class="view-header">
      <div>
        <h2 class="view-title">通知设置</h2>
        <p class="view-sub">保险快到期、该缴费时，通过企业微信群机器人每天提醒一次</p>
      </div>
      ${statusHtml(data)}
    </div>

    <section class="panel">
      <div class="panel-head">
        <h3>企业微信机器人</h3>
      </div>
      <div class="field">
        <label class="field-label" for="notify-webhook">Webhook 地址</label>
        <input
          type="text"
          id="notify-webhook"
          autocomplete="off"
          spellcheck="false"
          placeholder="${escapeHtml(webhookPlaceholder(data.webhookConfigured))}"
        >
        <p class="field-hint">
          获取方式：企业微信 → 进入要接收提醒的群 → 右上角「…」→ 群机器人 → 添加机器人 → 复制 Webhook 地址。
          地址只保存在本机数据库，不会外传。${
            data.webhookConfigured
              ? `当前已保存（key 末 4 位 <code>${escapeHtml(webhookTail(data.webhook))}</code>）。`
              : ''
          }
        </p>
      </div>
      <div class="notify-actions">
        <button type="button" class="btn small ghost" id="btn-clear-webhook"${
          data.webhookConfigured ? '' : ' disabled'
        }>清除已保存的地址</button>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>提醒规则</h3>
        <span class="panel-total">每天最多发送 <strong>1</strong> 条汇总消息</span>
      </div>
      <div class="setting-list">
        <div class="setting-row">
          <div class="setting-main">
            <div class="setting-title">启用每日提醒</div>
            <div class="setting-desc">关闭后不再自动发送；「立即发送测试」仍可用</div>
          </div>
          <label class="switch">
            <input type="checkbox" id="notify-enabled"${data.enabled ? ' checked' : ''}>
            <span class="switch-track"></span>
          </label>
        </div>

        <div class="setting-row">
          <div class="setting-main">
            <div class="setting-title">保障即将到期</div>
            <div class="setting-desc">保障期限为「终身」的保单不会到期，不参与此类提醒</div>
          </div>
          <div class="setting-control">
            <label class="switch">
              <input type="checkbox" id="notify-expiry-enabled"${data.expiryEnabled ? ' checked' : ''}>
              <span class="switch-track"></span>
            </label>
            <span>提前</span>
            <input class="setting-input" type="number" id="notify-expiry-days"
              min="1" max="365" step="1" value="${Number(data.expiryDays)}">
            <span>天提醒</span>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-main">
            <div class="setting-title">缴费临近 / 已逾期</div>
            <div class="setting-desc">已过缴费日仍未标记「已缴」的，会按逾期天数提醒</div>
          </div>
          <div class="setting-control">
            <label class="switch">
              <input type="checkbox" id="notify-due-enabled"${data.dueEnabled ? ' checked' : ''}>
              <span class="switch-track"></span>
            </label>
            <span>提前</span>
            <input class="setting-input" type="number" id="notify-due-days"
              min="1" max="365" step="1" value="${Number(data.dueDays)}">
            <span>天提醒</span>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-main">
            <div class="setting-title">每日发送时间</div>
            <div class="setting-desc">按服务器本地时间，到点后发送；当天只发一次，错过会补发</div>
          </div>
          <div class="setting-control">
            <input class="setting-time" type="time" id="notify-send-time" value="${escapeHtml(data.sendTime)}">
          </div>
        </div>
      </div>

      <div class="notify-actions">
        <button type="button" class="btn primary" id="btn-save-notify">保存设置</button>
        <button type="button" class="btn ghost" id="btn-test-notify">立即发送测试</button>
      </div>
    </section>
  `;

  bind(container, () => data);
}

/**
 * 绑定交互。
 * @param {HTMLElement} container
 * @param {() => object} getData 读取当前（最近一次保存的）配置
 */
function bind(container, getData) {
  const el = (selector) => container.querySelector(selector);

  /** 未勾选的提醒项 → 天数输入框置灰。 */
  function syncDisabled() {
    const pairs = [
      ['#notify-expiry-enabled', '#notify-expiry-days'],
      ['#notify-due-enabled', '#notify-due-days'],
    ];
    pairs.forEach(([toggle, input]) => {
      const box = el(toggle);
      const field = el(input);
      if (box && field) field.disabled = !box.checked;
    });
  }
  syncDisabled();
  el('#notify-expiry-enabled').addEventListener('change', syncDisabled);
  el('#notify-due-enabled').addEventListener('change', syncDisabled);

  /**
   * 收集表单 → 请求体。
   * @returns {{ok: true, payload: object} | {ok: false, message: string}}
   */
  function collect() {
    const current = getData();
    const enabled = el('#notify-enabled').checked;
    const typed = el('#notify-webhook').value.trim();

    if (enabled && !typed && !current.webhookConfigured) {
      return { ok: false, message: '请先填写企业微信 Webhook 地址' };
    }

    const expiryDays = Number(el('#notify-expiry-days').value);
    const dueDays = Number(el('#notify-due-days').value);
    if (!Number.isInteger(expiryDays) || expiryDays < 1 || expiryDays > 365) {
      return { ok: false, message: '「保障到期」提前天数须为 1–365 的整数' };
    }
    if (!Number.isInteger(dueDays) || dueDays < 1 || dueDays > 365) {
      return { ok: false, message: '「缴费提醒」提前天数须为 1–365 的整数' };
    }

    const sendTime = el('#notify-send-time').value;
    if (!/^\d{2}:\d{2}$/.test(sendTime)) {
      return { ok: false, message: '请选择有效的发送时间' };
    }

    const payload = {
      enabled,
      expiryEnabled: el('#notify-expiry-enabled').checked,
      expiryDays,
      dueEnabled: el('#notify-due-enabled').checked,
      dueDays,
      sendTime,
    };
    // 只在用户真的输入了新地址时才提交 webhook；留空 = 不修改
    if (typed) payload.webhook = typed;
    return { ok: true, payload };
  }

  /** 保存（供按钮与「测试」复用）。 */
  async function save(silent) {
    const result = collect();
    if (!result.ok) {
      store.notify(result.message, 'error');
      return false;
    }
    try {
      await api.saveNotifySettings(result.payload);
      if (!silent) store.notify('通知设置已保存', 'success');
      return true;
    } catch (error) {
      store.notify(error.message, 'error');
      return false;
    }
  }

  el('#btn-save-notify').addEventListener('click', async () => {
    const button = el('#btn-save-notify');
    button.disabled = true;
    const ok = await save(false);
    button.disabled = false;
    if (ok) store.requestRefresh();
  });

  el('#btn-test-notify').addEventListener('click', async () => {
    const button = el('#btn-test-notify');
    button.disabled = true;
    button.textContent = '发送中…';
    try {
      // 先落盘当前表单，避免「测试用新地址、实际保存用旧地址」的错位
      if (!(await save(true))) return;
      const result = await api.sendNotifyTest();
      store.notify(
        `测试消息已发送（到期 ${result.expiring} 份 / 待缴 ${result.due} 笔），请到企业微信群查看`,
        'success'
      );
      store.requestRefresh();
    } catch (error) {
      store.notify(error.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = '立即发送测试';
    }
  });

  el('#btn-clear-webhook').addEventListener('click', () => {
    confirmModal(
      '清除后已保存的 Webhook 将失效，通知会自动关闭。确定清除吗？',
      async () => {
        try {
          await api.saveNotifySettings({ webhook: '' });
          store.notify('Webhook 已清除', 'success');
          store.requestRefresh();
        } catch (error) {
          store.notify(error.message, 'error');
        }
        return true;
      },
      { confirmText: '清除' }
    );
  });
}
