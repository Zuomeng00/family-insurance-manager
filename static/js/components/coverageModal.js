/**
 * 保障责任录入弹窗（v1.10）。
 *
 * 为什么值得单独做一个弹窗：一张保单的「保额」往往不是一个数字——
 * 重疾赔 100%、中症 60% 赔 2 次、轻症 30% 赔 3 次。只有把这些拆开，
 * 托管报告里的缺口分析才算得准（重疾缺口只认「重大疾病保险金」）。
 *
 * 交互约定：
 * - **按模板填充** = 把该险种的常见责任项追加进来（已有的同名项不重复加），
 *   然后用户改数字、删没用到的行；
 * - 金额一律填「元」，比例填百分数（0–500）；
 * - 底部实时显示「给付型合计」：重疾/身故/意外三类可加总，
 *   医疗险限额不参与（报销型，叠加没有意义）。
 */

import * as api from '../api.js';
import * as store from '../store.js';
import { escapeHtml, fmtMoney } from '../format.js';
import { openModal } from './modal.js';

/** 可加总的责任类别（与后端 `SUMMABLE_CATEGORIES` 保持一致）。 */
const SUMMABLE = ['重大疾病', '身故全残', '意外'];

/** 空行模板。 */
function emptyRow() {
  return {
    name: '',
    category: '重大疾病',
    ratio: null,
    amount: null,
    times: null,
    deductible: null,
    note: '',
  };
}

/**
 * 一条责任的有效金额（元）。
 * @param {object} row
 * @param {number} insuredAmount 保单基本保额（元）
 * @returns {number|null}
 */
function effectiveAmount(row, insuredAmount) {
  const amount = Number(row.amount);
  if (Number.isFinite(amount) && amount > 0) return amount;
  const ratio = Number(row.ratio);
  if (Number.isFinite(ratio) && ratio > 0 && insuredAmount > 0) {
    return (insuredAmount * ratio) / 100;
  }
  return null;
}

/**
 * 打开责任录入弹窗。
 * @param {object} policy 保单对象（用到 id / productName / insuredAmount / insuranceType）
 */
export async function openCoverageEditor(policy) {
  let data;
  try {
    data = await api.getCoverages(policy.id);
  } catch (error) {
    store.notify(error.message, 'error');
    return;
  }

  const categories = data.categories || [];
  const templates = data.templates || [];
  const insuredAmount = Number(policy.insuredAmount) || 0;

  /** @type {Array<object>} 当前表格数据（与 DOM 双向同步） */
  let rows = (data.items || []).map((item) => ({
    name: item.name,
    category: item.category,
    ratio: item.ratio,
    amount: item.amount,
    times: item.times,
    deductible: item.deductible,
    note: item.note || '',
  }));

  const body = document.createElement('div');
  body.className = 'cvg-editor';
  body.innerHTML = `
    <p class="cvg-tip">
      把条款里的责任逐条拆开录入。<strong>重疾、身故、意外</strong>三类会相加成「给付型保额」，
      医疗限额单独看、不参与相加。留空的行保存时会被忽略。
    </p>
    <div class="cvg-table-wrap">
      <table class="cvg-table">
        <thead>
          <tr>
            <th class="cvg-col-name">责任项</th>
            <th class="cvg-col-cat">类别</th>
            <th class="cvg-col-num">比例%</th>
            <th class="cvg-col-num">金额(元)</th>
            <th class="cvg-col-num">次数</th>
            <th class="cvg-col-num">免赔额</th>
            <th class="cvg-col-note">备注</th>
            <th class="cvg-col-op"></th>
          </tr>
        </thead>
        <tbody id="cvg-body"></tbody>
      </table>
    </div>
    <div class="cvg-footer">
      <div class="cvg-actions">
        <button type="button" class="btn small ghost" id="cvg-fill"${
          templates.length ? '' : ' disabled'
        }>按模板填充${templates.length ? `（${templates.length} 项）` : ''}</button>
        <button type="button" class="btn small ghost" id="cvg-add">+ 添加一行</button>
      </div>
      <div class="cvg-total" id="cvg-total"></div>
    </div>
    <p class="cvg-hint">
      提示：比例按「基本保额<strong>${insuredAmount ? fmtMoney(insuredAmount) : '（未填）'}</strong>」折算；
      金额栏填了就优先用金额。免赔额只对医疗险有意义。
    </p>
  `;

  const tbody = body.querySelector('#cvg-body');
  const totalEl = body.querySelector('#cvg-total');

  /** 类别下拉选项。 */
  function categoryOptions(selected) {
    return categories
      .map(
        (name) =>
          `<option value="${escapeHtml(name)}"${name === selected ? ' selected' : ''}>${escapeHtml(
            name
          )}</option>`
      )
      .join('');
  }

  /** 渲染整张表（仅在行数变化时整体重绘）。 */
  function paintRows() {
    tbody.innerHTML = rows
      .map((row, index) => {
        const num = (value) => (value === null || value === undefined ? '' : value);
        return `<tr data-index="${index}">
          <td><input class="cvg-name" maxlength="40" value="${escapeHtml(row.name || '')}" placeholder="如 重大疾病保险金"></td>
          <td><select class="cvg-cat">${categoryOptions(row.category)}</select></td>
          <td><input class="cvg-ratio" type="number" min="0" max="500" step="1" value="${num(row.ratio)}"></td>
          <td><input class="cvg-amount" type="number" min="0" step="10000" value="${num(row.amount)}"></td>
          <td><input class="cvg-times" type="number" min="1" max="99" step="1" value="${num(row.times)}"></td>
          <td><input class="cvg-deductible" type="number" min="0" step="100" value="${num(row.deductible)}"></td>
          <td><input class="cvg-note" maxlength="120" value="${escapeHtml(row.note || '')}"></td>
          <td><button type="button" class="cvg-del" title="删除该行" aria-label="删除该行">×</button></td>
        </tr>`;
      })
      .join('');
    syncTotal();
  }

  /** DOM → rows（每次取值前先同步，避免"改了没存"）。 */
  function syncFromDom() {
    tbody.querySelectorAll('tr').forEach((tr) => {
      const index = Number(tr.dataset.index);
      if (!Number.isInteger(index) || !rows[index]) return;
      const row = rows[index];
      row.name = tr.querySelector('.cvg-name').value.trim();
      row.category = tr.querySelector('.cvg-cat').value;
      row.ratio = tr.querySelector('.cvg-ratio').value || null;
      row.amount = tr.querySelector('.cvg-amount').value || null;
      row.times = tr.querySelector('.cvg-times').value || null;
      row.deductible = tr.querySelector('.cvg-deductible').value || null;
      row.note = tr.querySelector('.cvg-note').value.trim();
    });
  }

  /** 实时刷新底部合计。 */
  function syncTotal() {
    syncFromDom();
    const byCategory = new Map();
    let sum = 0;
    rows.forEach((row) => {
      const amount = effectiveAmount(row, insuredAmount);
      if (amount === null) return;
      // 不乘赔付次数：报告里的「保额」口径就是单次可赔金额，
      // 中症 60%×2 次在报告里记 60%，乘上次数会和报告数字对不上。
      if (SUMMABLE.includes(row.category)) sum += amount;
      byCategory.set(row.category, (byCategory.get(row.category) || 0) + amount);
    });
    const parts = SUMMABLE.filter((name) => byCategory.has(name)).map(
      (name) => `${name} ${fmtMoney(byCategory.get(name))}`
    );
    totalEl.innerHTML = parts.length
      ? `<span class="cvg-total-label">给付型合计</span><strong>${fmtMoney(sum)}</strong><span class="cvg-total-detail">${parts.join(' · ')}</span>`
      : '<span class="cvg-total-hint">还没录入带金额的责任项</span>';
  }

  tbody.addEventListener('input', syncTotal);
  tbody.addEventListener('change', syncTotal);
  tbody.addEventListener('click', (event) => {
    const del = event.target.closest('.cvg-del');
    if (!del) return;
    const tr = del.closest('tr');
    const index = Number(tr.dataset.index);
    syncFromDom();
    rows.splice(index, 1);
    paintRows();
  });

  body.querySelector('#cvg-add').addEventListener('click', () => {
    syncFromDom();
    rows.push(emptyRow());
    paintRows();
    const last = tbody.querySelector('tr:last-child .cvg-name');
    if (last) last.focus();
  });

  body.querySelector('#cvg-fill').addEventListener('click', () => {
    syncFromDom();
    const existing = new Set(rows.map((row) => row.name).filter(Boolean));
    let added = 0;
    templates.forEach((tpl) => {
      if (existing.has(tpl.name)) return;
      rows.push({
        name: tpl.name,
        category: tpl.category,
        ratio: tpl.ratio,
        amount: tpl.amount,
        times: tpl.times,
        deductible: tpl.deductible,
        note: tpl.note || '',
      });
      existing.add(tpl.name);
      added += 1;
    });
    if (added === 0) {
      store.notify('模板里的责任项都已经有了', 'info');
      return;
    }
    paintRows();
    store.notify(`已按模板补入 ${added} 项，按实际条款改数字即可`, 'success');
  });

  if (!rows.length) rows = [emptyRow()];
  paintRows();

  const { bodyEl } = openModal({
    title: `保障责任 · ${policy.productName || ''}`,
    body,
    confirmText: '保存责任',
    onConfirm: async () => {
      syncFromDom();
      const items = rows
        .filter((row) => row.name)
        .map((row) => ({
          name: row.name,
          category: row.category,
          ratio: row.ratio === null ? null : Number(row.ratio),
          amount: row.amount === null ? null : Number(row.amount),
          times: row.times === null ? null : Number(row.times),
          deductible: row.deductible === null ? null : Number(row.deductible),
          note: row.note || null,
        }));
      try {
        await api.saveCoverages(policy.id, items);
        store.notify(items.length ? `已保存 ${items.length} 条保障责任` : '已清空保障责任', 'success');
        store.requestRefresh();
        return true;
      } catch (error) {
        store.notify(error.message, 'error');
        return false;
      }
    },
  });

  // 弹窗默认宽度放不下 8 列，单独加宽
  const dialog = bodyEl.closest('.modal');
  if (dialog) dialog.classList.add('modal-wide');
}
