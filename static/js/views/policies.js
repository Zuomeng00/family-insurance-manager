/**
 * 保单列表视图（T11 / v1.2 增强）。
 *
 * - 顶部筛选栏：被保人 / 险种 / 状态 / 缴费方式下拉 + 关键词搜索；
 *   筛选与搜索为**前端本地过滤**（数据量小，减少请求，见 ARCHITECTURE.md A2）；
 * - 表格列完整：含「保多久 / 交多久」（缴费方式并入「交多久」）；
 *   「缴费进度」列直接展示「已缴 x/y 期」（手动记录的加「手动」标记），
 *   「状态」列在存在逾期时附加「逾期 N 期」徽章；
 * - **v1.2 新增**：
 *   ① 首列勾选框 + 表头**全选**（用 `Set` 维护选中 id，跨重渲染保留）；
 *   ② 工具栏「批量编辑 (N)」按钮（N = 0 时禁用）；
 *   ③ 行内「联系方式」快捷查看（复用 `modal.js`，未填字段显示「—」）；
 *   ④ 删除二次确认文案带**附件数**（B-22）；
 * - 底部汇总：筛选结果的年化保费合计与累计已缴合计；
 * - 新增 / 编辑走 `policyForm`（携带常用值），删除走二次确认，批量走 `batchEditModal`。
 */

import * as api from '../api.js';
import * as store from '../store.js';
import { fmtMoney, fmtDate, escapeHtml, fmtInsuredNames, insuredTitle } from '../format.js';
import { openModal, confirmModal } from '../components/modal.js';
import { openCoverageEditor } from '../components/coverageModal.js';
import { createPolicyForm } from '../components/policyForm.js';
import { openBatchEdit } from '../components/batchEditModal.js';
import { createAttachmentList } from '../components/attachmentList.js';
import { openAttachmentPicker } from '../components/attachmentPicker.js';

/**
 * 生成枚举 `<option>` 列表（含「全部」占位项）。
 * @param {string[]} values
 * @param {string} placeholder
 * @returns {string}
 */
function enumOptionsHtml(values, placeholder) {
  const head = placeholder ? `<option value="">${escapeHtml(placeholder)}</option>` : '';
  return head + (values || []).map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
}

/**
 * 生成成员 `<option>` 列表（值为成员 id）。
 * @param {Array} members
 * @returns {string}
 */
function memberOptionsHtml(members) {
  const head = '<option value="">全部被保人</option>';
  return (
    head +
    (members || [])
      .map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`)
      .join('')
  );
}

/**
 * 状态徽章（含逾期标记）。
 * @param {object} p 保单
 * @returns {string}
 */
function statusBadge(p) {
  const status = p.status;
  const cls = status === '生效中' ? 'active' : status === '已缴清' ? 'paid' : 'off';
  const overdue = Number(p.overduePeriods) || 0;
  const flag = overdue > 0 ? `<span class="badge warn">逾期 ${overdue} 期</span>` : '';
  return `<span class="badge ${cls}">${escapeHtml(status)}</span>${flag}`;
}

/**
 * 「险种」单元格（v1.5）。
 *
 * 若该保单的**投保人**被设为「不计入家庭保费统计」，则在险种旁附一个低调徽章，
 * 解释「为何这张保单的年化保费不计入统计」。沿用已有 `.badge` 类，不新增 CSS。
 * @param {object} p 保单（含 insuranceType / countedInStats）
 * @returns {string}
 */
function typeCell(p) {
  const flag = p.countedInStats === false ? ' <span class="badge off">不计入</span>' : '';
  return `${escapeHtml(p.insuranceType)}${flag}`;
}

/**
 * 「保多久」列文本（保障期限）。
 * - 终身 → `终身`
 * - 固定年限 N → `保 N 年`
 * - 至某年龄 A → `保至 A 岁`
 * @param {object} p
 * @returns {string}
 */
function coverageTermText(p) {
  const type = p.coverageTermType;
  const value = p.coverageTermValue;
  if (type === '终身') return '终身';
  if (type === '固定年限') return value != null ? `保 ${value} 年` : '—';
  return value != null ? `保至 ${value} 岁` : '—'; // 至某年龄
}

/**
 * 「交多久」列文本（缴费期限，并入缴费方式以控制表宽）。
 * - 趸交 → `趸交`（单次缴纳，不显示缴费方式）
 * - 固定年限 N → `交 N 年 · 月缴/年缴`
 * - 交至某年龄 A → `交至 A 岁 · 月缴/年缴`
 * @param {object} p
 * @returns {string}
 */
function paymentTermText(p) {
  const type = p.paymentTermType;
  const value = p.paymentTermValue;
  if (type === '趸交') return '趸交';
  const freq = p.paymentFrequency || '';
  const suffix = freq ? ` · ${freq}` : '';
  if (type === '固定年限') return value != null ? `交 ${value} 年${suffix}` : '—';
  return value != null ? `交至 ${value} 岁${suffix}` : '—'; // 交至某年龄
}

/**
 * 进度条单元格。
 *
 * 直接展示「已缴 x/y 期」，让「今年交没交」一眼可见；若该期数来自手动记录，
 * 额外加「手动」标记，便于区分「按生效日期自动推算」与「用户自己填的」。
 * @param {object} p 保单（含 progressPercent / paidPeriods / totalPeriods / paidPeriodsOverride）
 * @returns {string}
 */
function progressCell(p) {
  const percent = Math.max(0, Math.min(100, Number(p.progressPercent) || 0));
  const paid = Number(p.paidPeriods) || 0;
  const total = Number(p.totalPeriods) || 0;
  const manual = p.paidPeriodsOverride !== null && p.paidPeriodsOverride !== undefined;
  const title = manual
    ? `手动记录：已缴 ${paid}/${total} 期`
    : `按生效日期自动推算：已缴 ${paid}/${total} 期`;
  const mark = manual ? '<span class="badge manual">手动</span>' : '';
  return `<div class="progress-cell" title="${escapeHtml(title)}">
    <div class="progress"><span style="width:${percent}%"></span></div>
    <span class="progress-text">已缴 ${paid}/${total} 期</span>
    ${mark}
  </div>`;
}

/**
 * 「电子保单」单元格（v1.3）：0 个显示「—」，否则显示可点击的 📎N 按钮。
 * @param {object} p 保单（含 attachmentCount）
 * @returns {string}
 */
function attachmentCell(p) {
  const count = Number(p.attachmentCount) || 0;
  if (count === 0) return '<span class="muted">—</span>';
  return `<button class="btn small attachment-link" type="button" data-action="attachments" data-id="${escapeHtml(p.id)}" title="查看电子保单">📎 ${count}</button>`;
}

/**
 * 渲染保单表格 + 汇总。
 * @param {Array} rows 过滤后的保单
 * @param {number} annualSum
 * @param {number} paidSum
 * @param {Set<string>} selected 已勾选的保单 id（用于回显勾选态）
 * @returns {string}
 */
function tableHtml(rows, annualSum, paidSum, selected) {
  const body = rows
    .map(
      (p) => `<tr>
        <td class="checkbox-col"><input type="checkbox" class="row-select" data-id="${escapeHtml(p.id)}" aria-label="选择该保单"${selected.has(p.id) ? ' checked' : ''}></td>
        <td>${escapeHtml(p.company)}</td>
        <td>${typeCell(p)}</td>
        <td>${escapeHtml(p.productName)}</td>
        <td>${escapeHtml(p.policyholderName)}</td>
        <td title="${escapeHtml(insuredTitle(p.insuredNames))}">${escapeHtml(fmtInsuredNames(p.insuredNames))}</td>
        <td>${fmtDate(p.effectiveDate)}</td>
        <td>${escapeHtml(coverageTermText(p))}</td>
        <td>${escapeHtml(paymentTermText(p))}</td>
        <td class="num">${fmtMoney(p.premiumPerPeriod)}</td>
        <td class="num">${fmtMoney(p.annualizedPremium)}</td>
        <td>${progressCell(p)}</td>
        <td>${statusBadge(p)}</td>
        <td>${attachmentCell(p)}</td>
        <td>
          <div class="row-actions">
            <button class="btn small" type="button" data-action="edit" data-id="${escapeHtml(p.id)}">编辑</button>
            <button class="btn small" type="button" data-action="coverage" data-id="${escapeHtml(p.id)}" title="逐条录入条款责任，托管报告才够准">责任</button>
            <button class="btn small" type="button" data-action="contact" data-id="${escapeHtml(p.id)}">联系方式</button>
            <button class="btn small danger" type="button" data-action="delete" data-id="${escapeHtml(p.id)}">删除</button>
          </div>
        </td>
      </tr>`
    )
    .join('');

  return `<div class="panel table-panel">
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th class="checkbox-col"><input type="checkbox" id="select-all" aria-label="全选当前列表"></th>
            <th>保险公司</th><th>险种</th><th>产品名称</th><th>投保人</th><th>被保人</th>
            <th>生效日期</th><th>保多久</th><th>交多久</th>
            <th class="num">每期保费</th><th class="num">年化保费</th>
            <th>缴费进度</th><th>状态</th><th>电子保单</th><th>操作</th>
          </tr>
        </thead>
        <tbody>${body}</tbody>
        <tfoot>
          <tr>
            <td colspan="9">合计（${rows.length} 份）<span class="sel-count" id="sel-count"></span></td>
            <td class="num"></td>
            <td class="num">${fmtMoney(annualSum)}</td>
            <td colspan="4">累计已缴 ${fmtMoney(paidSum)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  </div>`;
}

/**
 * 渲染视图。
 * @param {HTMLElement} container
 */
export async function render(container) {
  container.innerHTML = '<div class="loading">加载中…</div>';
  let enums;
  try {
    enums = await store.ensureEnums();
    await store.refreshAll();
  } catch (error) {
    container.innerHTML = `<div class="empty-state">加载失败：${escapeHtml(error.message)}</div>`;
    return;
  }

  const members = store.get('members') || [];
  const filters = { insuredId: '', insuranceType: '', status: '', paymentFrequency: '', keyword: '' };
  /** 已勾选的保单 id（跨重渲染保留）。 @type {Set<string>} */
  const selected = new Set();

  container.innerHTML = `
    <div class="view-header">
      <div>
        <h2 class="view-title">保单列表</h2>
        <p class="view-sub" id="policy-summary"></p>
      </div>
      <div class="header-actions">
        <button class="btn" type="button" id="btn-batch-edit" disabled>批量编辑 (0)</button>
        <button class="btn primary" type="button" id="btn-add-policy">+ 新增保单</button>
      </div>
    </div>
    <div class="filter-bar">
      <select id="f-insured">${memberOptionsHtml(members)}</select>
      <select id="f-type">${enumOptionsHtml(enums.insuranceType, '全部险种')}</select>
      <select id="f-status">${enumOptionsHtml(enums.policyStatus, '全部状态')}</select>
      <select id="f-freq">${enumOptionsHtml(enums.paymentFrequency, '全部缴费方式')}</select>
      <input type="search" id="f-keyword" placeholder="搜索保险公司 / 产品名称">
    </div>
    <div id="policy-table-wrap"></div>
  `;

  const wrap = container.querySelector('#policy-table-wrap');
  const summaryEl = container.querySelector('#policy-summary');

  /**
   * 按当前筛选条件过滤保单。
   * @returns {Array}
   */
  function applyFilters() {
    const all = store.get('policies') || [];
    const keyword = filters.keyword.trim().toLowerCase();
    return all.filter((p) => {
      if (filters.insuredId) {
        // v1.3：any-of（被保人集合含该成员）
        const ids = Array.isArray(p.insuredIds) ? p.insuredIds : p.insuredId ? [p.insuredId] : [];
        if (!ids.includes(filters.insuredId)) return false;
      }
      if (filters.insuranceType && p.insuranceType !== filters.insuranceType) return false;
      if (filters.status && p.status !== filters.status) return false;
      if (filters.paymentFrequency && p.paymentFrequency !== filters.paymentFrequency) return false;
      if (keyword) {
        const haystack = `${p.company} ${p.productName}`.toLowerCase();
        if (!haystack.includes(keyword)) return false;
      }
      return true;
    });
  }

  /** 清理选中集合中「已被删除」的保单 id。 */
  function pruneSelection() {
    const ids = new Set((store.get('policies') || []).map((p) => p.id));
    Array.from(selected).forEach((id) => {
      if (!ids.has(id)) selected.delete(id);
    });
  }

  /** 同步「批量编辑 (N)」按钮与底部「已勾选 N 份」计数。 */
  function syncBatchButton() {
    const btn = container.querySelector('#btn-batch-edit');
    if (btn) {
      btn.textContent = `批量编辑 (${selected.size})`;
      btn.disabled = selected.size === 0;
    }
    const selCount = wrap.querySelector('#sel-count');
    if (selCount) selCount.textContent = selected.size > 0 ? `　已勾选 ${selected.size} 份` : '';
  }

  /**
   * 同步表头「全选」勾选态（含 indeterminate）。
   * @param {Array} rows 当前可见（过滤后）的保单
   */
  function syncSelectAll(rows) {
    const allCb = wrap.querySelector('#select-all');
    if (!allCb) return;
    const total = rows.length;
    const picked = rows.reduce((n, p) => n + (selected.has(p.id) ? 1 : 0), 0);
    allCb.checked = total > 0 && picked === total;
    allCb.indeterminate = picked > 0 && picked < total;
  }

  /**
   * 绑定首列勾选框与表头全选（每次重渲染后重新绑定）。
   * @param {Array} rows 当前可见（过滤后）的保单
   */
  function bindSelection(rows) {
    wrap.querySelectorAll('.row-select').forEach((cb) => {
      cb.addEventListener('change', () => {
        if (cb.checked) selected.add(cb.dataset.id);
        else selected.delete(cb.dataset.id);
        syncBatchButton();
        syncSelectAll(rows);
      });
    });
    const allCb = wrap.querySelector('#select-all');
    if (allCb) {
      allCb.addEventListener('change', () => {
        if (allCb.checked) rows.forEach((p) => selected.add(p.id));
        else rows.forEach((p) => selected.delete(p.id));
        wrap.querySelectorAll('.row-select').forEach((cb) => {
          cb.checked = selected.has(cb.dataset.id);
        });
        syncBatchButton();
        syncSelectAll(rows);
      });
    }
  }

  /** 绑定行内编辑 / 联系方式 / 删除按钮。 */
  function bindRowActions() {
    wrap.querySelectorAll('[data-action]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const { action, id } = btn.dataset;
        const policy = (store.get('policies') || []).find((p) => p.id === id);
        if (!policy) return;
        if (action === 'edit') openEdit(enums, members, policy);
        else if (action === 'coverage') openCoverageEditor(policy);
        else if (action === 'contact') openContact(policy);
        else if (action === 'attachments') openAttachmentPicker(policy);
        else if (action === 'delete') removePolicy(policy);
      });
    });
  }

  /** 重新渲染表格与汇总。 */
  function paint() {
    pruneSelection();
    const all = store.get('policies') || [];
    const rows = applyFilters();
    const annualSum = rows.reduce(
      (sum, p) => sum + (p.isPaidOff ? 0 : Number(p.annualizedPremium) || 0),
      0
    );
    const paidSum = rows.reduce((sum, p) => sum + (Number(p.cumulativePaid) || 0), 0);

    summaryEl.textContent =
      rows.length === all.length
        ? `共 ${all.length} 份保单`
        : `筛选出 ${rows.length} / ${all.length} 份保单`;

    wrap.innerHTML = rows.length
      ? tableHtml(rows, annualSum, paidSum, selected)
      : '<div class="empty-state">没有符合条件的保单。</div>';

    bindRowActions();
    bindSelection(rows);
    syncBatchButton();
    syncSelectAll(rows);
  }

  container.querySelector('#f-insured').addEventListener('change', (e) => {
    filters.insuredId = e.target.value;
    paint();
  });
  container.querySelector('#f-type').addEventListener('change', (e) => {
    filters.insuranceType = e.target.value;
    paint();
  });
  container.querySelector('#f-status').addEventListener('change', (e) => {
    filters.status = e.target.value;
    paint();
  });
  container.querySelector('#f-freq').addEventListener('change', (e) => {
    filters.paymentFrequency = e.target.value;
    paint();
  });
  container.querySelector('#f-keyword').addEventListener('input', (e) => {
    filters.keyword = e.target.value;
    paint();
  });
  container.querySelector('#btn-add-policy').addEventListener('click', () => openCreate(enums, members));

  container.querySelector('#btn-batch-edit').addEventListener('click', async () => {
    if (selected.size === 0) {
      store.notify('请先勾选要修改的保单', 'info');
      return;
    }
    const policies = (store.get('policies') || []).filter((p) => selected.has(p.id));
    const suggestions = await store.ensureSuggestions();
    openBatchEdit(policies, suggestions, () => {
      selected.clear();
      store.requestRefresh();
    });
  });

  paint();
}

/**
 * 打开新增保单弹窗。
 * @param {object} enums
 * @param {Array} members
 */
async function openCreate(enums, members) {
  if (!members || !members.length) {
    store.notify('请先添加家庭成员，再新增保单', 'error');
    return;
  }
  const suggestions = await store.ensureSuggestions();
  const form = createPolicyForm(enums, members, null, suggestions);
  openModal({
    title: '新增保单',
    body: form.element,
    confirmText: '保存',
    onConfirm: async () => {
      const result = form.collect();
      if (!result.ok) return false;
      try {
        await api.createPolicy(result.payload);
        store.notify('保单已新增', 'success');
        store.requestRefresh();
        return true;
      } catch (error) {
        form.showError(error.message);
        return false;
      }
    },
  });
}

/**
 * 打开编辑保单弹窗。
 * @param {object} enums
 * @param {Array} members
 * @param {object} policy
 */
async function openEdit(enums, members, policy) {
  const suggestions = await store.ensureSuggestions();
  const form = createPolicyForm(enums, members, policy, suggestions);

  // 附件区**仅编辑模式**挂载（A10：新增保单尚无 id，无附件可管理）
  const attachments = createAttachmentList(policy.id, {
    onCountChange: (n) => {
      const list = store.get('policies') || [];
      const target = list.find((p) => p.id === policy.id);
      if (target) target.attachmentCount = n;
    },
  });
  const attSection = document.createElement('div');
  attSection.className = 'attachment-section';
  attSection.innerHTML = '<h4 class="attachment-title">电子保单附件</h4>';
  attSection.appendChild(attachments.element);
  form.element.appendChild(attSection);

  openModal({
    title: `编辑保单 · ${policy.productName}`,
    body: form.element,
    confirmText: '保存',
    onConfirm: async () => {
      const result = form.collect();
      if (!result.ok) return false;
      try {
        await api.updatePolicy(policy.id, result.payload);
        store.notify('保单已更新', 'success');
        store.requestRefresh();
        return true;
      } catch (error) {
        form.showError(error.message);
        return false;
      }
    },
  });
}

/** 「联系方式」快捷查看弹层的字段顺序（与保单表单售后区块一致）。 */
const CONTACT_FIELDS = [
  ['服务顾问 · 姓名', 'advisorName'],
  ['服务顾问 · 电话', 'advisorPhone'],
  ['服务顾问 · 微信号', 'advisorWechat'],
  ['服务顾问 · 邮箱', 'advisorEmail'],
  ['保险公司 · 公司名', 'company'],
  ['保险公司 · 联系电话', 'companyPhone'],
  ['投保渠道 · 渠道名称', 'channelName'],
  ['投保渠道 · 渠道电话', 'channelPhone'],
];

/**
 * 打开「联系方式」快捷查看弹层（只读，未填字段显示「—」）。
 * @param {object} policy
 */
function openContact(policy) {
  const rows = CONTACT_FIELDS.map(([label, key]) => {
    const raw = policy[key];
    const text = raw == null || String(raw).trim() === '' ? '—' : String(raw);
    return `<div class="contact-row"><span class="contact-label">${escapeHtml(label)}</span><span class="contact-value">${escapeHtml(text)}</span></div>`;
  }).join('');

  const modal = openModal({
    title: `联系方式 · ${policy.company} · ${policy.productName}`,
    body: `<div class="contact-grid contact-grid-view">${rows}</div>
      <div class="modal-inline-actions"><button type="button" class="btn ghost" data-role="close">关闭</button></div>`,
    showFooter: false,
  });
  modal.bodyEl.querySelector('[data-role="close"]').addEventListener('click', modal.close);
}

/**
 * 删除保单（二次确认，文案带附件数）。
 * @param {object} policy
 */
function removePolicy(policy) {
  const attCount = Number(policy.attachmentCount) || 0;
  const attText = attCount > 0 ? `将同时删除 ${attCount} 个附件。` : '';
  confirmModal(
    `确定要删除保单「${policy.company} · ${policy.productName}」吗？${attText}删除后不可恢复。`,
    async () => {
      try {
        await api.deletePolicy(policy.id);
        store.notify('保单已删除', 'success');
        store.requestRefresh();
      } catch (error) {
        store.notify(error.message, 'error');
      }
      return true;
    },
    { confirmText: '删除' }
  );
}
