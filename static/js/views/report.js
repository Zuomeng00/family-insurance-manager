/**
 * 保单托管报告视图（v1.10）。
 *
 * 设计取向：**这是一份文档，不是一个看板**。
 *
 * - 进入页面即向 `GET /api/report` 请求**实时聚合**结果，不读任何前端缓存，
 *   也不落快照——保单新增、到期、退保、补录责任后，刷新（或重新进入页面）就是新数；
 * - 页面本身按 A4 打印排版，`打印 / 存为 PDF` 用的是浏览器原生打印，
 *   与页面所见一致，不需要额外的 PDF 库；
 * - `导出 Word` 走后端 `python-docx`，与页面共用同一份现算数据。
 *
 * 章节顺序对标经纪公司的托管报告：
 * 总览矩阵 → 缺口分析 → 成员逐人 → 保费分布 → 保单汇总 → 交费计划 → 行动清单 → 口径说明。
 */

import * as api from '../api.js';
import { fmtMoney, fmtInt, fmtDate, escapeHtml } from '../format.js';

/** 可加总的给付型类别（与后端 SUMMABLE 保持一致，医疗不在内）。 */
const SUMMABLE = ['重大疾病', '身故全残', '意外'];
const PRIORITY_TEXT = { high: '优先处理', medium: '建议安排', low: '有空再补' };
const PRIORITY_CLASS = { high: 'p-high', medium: 'p-medium', low: 'p-low' };

/**
 * 金额按「万」展示：保额动辄几十上百万，万元比元更好读；不足 1 万回落到元。
 * @param {number|null|undefined} value
 * @returns {string}
 */
function wan(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  if (n === 0) return '0';
  if (Math.abs(n) < 10000) return fmtMoney(n);
  const w = n / 10000;
  const text = w >= 100 ? String(Math.round(w)) : w.toFixed(1).replace(/\.0$/, '');
  return `${text} 万`;
}

/**
 * 紧凑金额（图表标签用）：`12345` → `"1.2万"`。
 * @param {number} value
 * @returns {string}
 */
function compactWan(value) {
  const n = Number(value) || 0;
  if (n === 0) return '0';
  if (Math.abs(n) < 10000) return String(Math.round(n));
  return `${(n / 10000).toFixed(1).replace(/\.0$/, '')}万`;
}

/**
 * ISO 日期时间 → `YYYY-MM-DD HH:MM`。
 * @param {string|null|undefined} value
 * @returns {string}
 */
function dateTimeText(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 16);
}

/**
 * 某成员的给付型保额合计（重疾 + 身故全残 + 意外，医疗与年金不计）。
 * @param {Array<{category: string, amount: number|null}>} cells
 * @returns {number}
 */
function protectionTotal(cells) {
  return (cells || []).reduce(
    (sum, cell) => (SUMMABLE.includes(cell.category) ? sum + (Number(cell.amount) || 0) : sum),
    0
  );
}

/**
 * 成员矩阵里的一格。
 * @param {{category: string, amount: number|null, has: boolean}} cell
 * @returns {string}
 */
function cellHtml(cell) {
  if (!cell.has) return '<td class="matrix-cell is-missing">—</td>';
  if (!cell.amount) return '<td class="matrix-cell" title="有该责任但金额未填">待补金额</td>';
  return `<td class="matrix-cell">${wan(cell.amount)}</td>`;
}

/* -------------------------------------------------------------------------- */
/* 各章节                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * 顶部工具条（打印时隐藏）。
 * @param {any} data
 * @returns {string}
 */
function toolbarHtml(data) {
  return `<div class="report-toolbar no-print">
    <div class="report-toolbar-note">
      <strong>报告按当前数据实时生成</strong>
      <span>数据更新于 ${escapeHtml(dateTimeText(data.meta.dataUpdatedAt))}；
      保单有新增、到期或退保，点「重新生成」即可刷新</span>
    </div>
    <div class="report-toolbar-actions">
      <button class="btn" type="button" data-action="refresh">重新生成</button>
      <button class="btn" type="button" data-action="export">导出 Word</button>
      <button class="btn primary" type="button" data-action="print">打印 / 存为 PDF</button>
    </div>
  </div>`;
}

/**
 * 警示条（口径提醒、数据缺口）。
 * @param {string[]} warnings
 * @returns {string}
 */
function warningsHtml(warnings) {
  if (!warnings || !warnings.length) return '';
  return `<div class="report-warnings">
    ${warnings.map((text) => `<p>${escapeHtml(text)}</p>`).join('')}
  </div>`;
}

/**
 * 封面头。
 * @param {any} data
 * @returns {string}
 */
function headHtml(data) {
  const o = data.overview;
  return `<header class="report-head">
    <div>
      <h1>${escapeHtml(data.meta.familyName)} · 家庭保障检视报告</h1>
      <p class="report-sub">
        报告编号 ${escapeHtml(data.meta.reportNo)} ·
        生成于 ${escapeHtml(dateTimeText(data.meta.generatedAt))} ·
        版本 v${escapeHtml(data.meta.version)}
      </p>
      <div class="print-only cover-summary">
        <div><span>家庭成员</span><strong>${fmtInt(o.memberCount)} 人</strong></div>
        <div><span>在保保单</span><strong>${fmtInt(o.inForceCount)} 张</strong></div>
        <div><span>年化保费</span><strong>${fmtMoney(o.annualPremium)}</strong></div>
        <div><span>给付型保额</span><strong>${wan(o.totalSumInsured)}</strong></div>
      </div>
      <p class="print-only cover-note">
        本报告依据本人录入的保单信息整理，用于自查保障结构与额度，不构成投保建议。
        报告不含现金价值、生存金与分红数据。
      </p>
    </div>
    <div class="report-stamp">
      <span class="report-stamp-main">现算</span>
      <span class="report-stamp-sub">每次打开重新汇总</span>
    </div>
  </header>`;
}

/**
 * 一、保障总览：指标 + 成员 × 类别矩阵。
 * @param {any} data
 * @returns {string}
 */
function overviewSection(data) {
  const o = data.overview;
  const recordedTotal = o.coverageRecorded + o.coverageMissing;
  const cards = [
    ['家庭成员', `${fmtInt(o.memberCount)} 人`, ''],
    ['保单总数', `${fmtInt(o.policyCount)} 张`, `在保 ${fmtInt(o.inForceCount)} 张`],
    ['年化保费', fmtMoney(o.annualPremium), '在保保单合计'],
    ['给付型保额', wan(o.totalSumInsured), '不含医疗报销限额'],
    [
      '责任已录',
      `${fmtInt(o.coverageRecorded)} / ${fmtInt(recordedTotal)}`,
      o.coverageMissing ? `${o.coverageMissing} 张按险种粗算` : '全部已录',
    ],
  ];

  const head = `<tr><th>成员</th>${(data.matrixCategories || [])
    .map((c) => `<th class="num">${escapeHtml(c)}</th>`)
    .join('')}<th class="num">给付合计</th></tr>`;

  const body = (data.members || [])
    .map(
      (m) => `<tr>
        <td class="matrix-name">${escapeHtml(m.name)}
          <span class="matrix-relation">${escapeHtml(m.relation)}${m.age != null ? ` · ${m.age} 岁` : ''}</span>
        </td>
        ${m.cells.map(cellHtml).join('')}
        <td class="matrix-cell matrix-total">${wan(protectionTotal(m.cells))}</td>
      </tr>`
    )
    .join('');

  return `<section class="report-section">
    <h2><span class="sec-no">一</span>保障总览</h2>
    ${warningsHtml(data.warnings)}
    <div class="metric-row">
      ${cards
        .map(
          ([label, value, sub]) => `<div class="metric-card">
            <div class="metric-label">${escapeHtml(label)}</div>
            <div class="metric-value">${escapeHtml(value)}</div>
            <div class="metric-sub">${escapeHtml(sub)}</div>
          </div>`
        )
        .join('')}
    </div>
    <div class="matrix-wrap">
      <table class="matrix-table">
        <thead>${head}</thead>
        <tbody>${body || '<tr><td colspan="99" class="matrix-empty">还没有家庭成员</td></tr>'}</tbody>
      </table>
      <p class="table-note">
        「—」= 没有这一类保障；「待补金额」= 有该责任但没填金额或比例。
        医疗是报销限额，单列展示、不并入给付合计；
        多被保人保单的保额统一记在主被保人名下。
      </p>
    </div>
  </section>`;
}

/**
 * 二、缺口分析：参数 + 额度缺口 + 结构缺口。
 * @param {any} data
 * @returns {string}
 */
function gapSection(data) {
  const params = data.params || {};
  const amountRows = (data.gaps && data.gaps.amount) || [];
  const structureRows = (data.gaps && data.gaps.structure) || [];

  const form = `<form class="param-form no-print" data-role="param-form">
    <label>家庭年收入（元）<input type="number" min="0" step="1000" name="annualIncome"
      value="${params.annualIncome ?? ''}" placeholder="如 300000"></label>
    <label>负债余额（元）<input type="number" min="0" step="1000" name="totalDebt"
      value="${params.totalDebt ?? ''}" placeholder="房贷 + 车贷等"></label>
    <label>年生活支出（元）<input type="number" min="0" step="1000" name="annualExpense"
      value="${params.annualExpense ?? ''}" placeholder="全家一年开销"></label>
    <button class="btn primary" type="submit">保存并重新测算</button>
  </form>
  <p class="param-inline print-only">
    测算参数：家庭年收入 ${params.annualIncome ? fmtMoney(params.annualIncome) : '未填'} ·
    负债余额 ${params.totalDebt ? fmtMoney(params.totalDebt) : '未填'} ·
    年生活支出 ${params.annualExpense ? fmtMoney(params.annualExpense) : '未填'}
  </p>
  <p class="param-hint">
    这三项只影响「额度缺口」的参考值，不影响已有的保障汇总。
    参考值按行业通行经验测算：重疾 3 倍年收入（不低于 30 万）、
    寿险「负债 + 5 倍年支出」、意外 5 倍年收入（不低于 50 万）；
    未成年人重疾按 50 万、意外按身故给付监管上限。
    这是<strong>经验值不是监管标准</strong>，最终要结合预算与体况定。
  </p>`;

  const amountTable = amountRows.length
    ? `<div class="table-scroll">
        <table class="data-table compact">
          <thead><tr>
            <th>成员</th><th>保障项目</th>
            <th class="num">现有</th><th class="num">参考建议</th>
            <th class="num">缺口</th><th>说明</th>
          </tr></thead>
          <tbody>
            ${amountRows
              .map(
                (row) => `<tr>
                  <td>${escapeHtml(row.memberName)}</td>
                  <td>${escapeHtml(row.item)}</td>
                  <td class="num">${wan(row.current)}</td>
                  <td class="num">${wan(row.suggested)}</td>
                  <td class="num gap-amount">${wan(row.gap)}</td>
                  <td class="cell-note">${escapeHtml(row.note || '')}</td>
                </tr>`
              )
              .join('')}
          </tbody>
        </table>
      </div>`
    : `<p class="empty-line">${
        params.filled ? '各项保障均达到参考值，暂无额度缺口。' : '填入家庭年收入后即可测算额度缺口。'
      }</p>`;

  const structureList = structureRows.length
    ? `<ul class="structure-list">
        ${structureRows
          .map(
            (row) =>
              `<li><span class="dot ${row.severity === 'high' ? 'dot-high' : 'dot-medium'}"></span>
               <strong>${escapeHtml(row.memberName)}</strong>　${escapeHtml(row.message)}</li>`
          )
          .join('')}
      </ul>`
    : '<p class="empty-line">结构上没有明显空白。</p>';

  return `<section class="report-section">
    <h2><span class="sec-no">二</span>保障缺口分析</h2>
    ${form}
    <h3 class="sub-title">2.1 额度缺口</h3>
    ${amountTable}
    <h3 class="sub-title">2.2 结构缺口</h3>
    ${structureList}
  </section>`;
}

/**
 * 单张保单在成员块里的责任明细。
 * @param {any} policy
 * @returns {string}
 */
function memberPolicyHtml(policy) {
  const items = policy.items || [];
  const rows = items.length
    ? `<table class="mini-table">
        <thead><tr><th>责任</th><th>类别</th><th class="num">比例</th><th class="num">金额</th><th class="num">次数</th></tr></thead>
        <tbody>
          ${items
            .map(
              (item) => `<tr>
                <td>${escapeHtml(item.name)}</td>
                <td>${escapeHtml(item.category)}</td>
                <td class="num">${item.ratio ? `${item.ratio}%` : '—'}</td>
                <td class="num">${wan(item.effectiveAmount)}</td>
                <td class="num">${item.times ? `${item.times} 次` : '—'}</td>
              </tr>`
            )
            .join('')}
        </tbody>
      </table>`
    : `<p class="empty-line">未录入保障责任，本报告按「${escapeHtml(policy.insuranceType)}」与保额粗算。</p>`;

  return `<div class="member-policy">
    <div class="member-policy-head">
      <span class="member-policy-name">${escapeHtml(policy.productName)}</span>
      <span class="member-policy-meta">${escapeHtml(policy.company)} · ${escapeHtml(policy.insuranceType)} · ${escapeHtml(policy.status)}</span>
    </div>
    ${rows}
  </div>`;
}

/**
 * 三、保障诊断小结：把数字翻译成几句人话。
 * @param {any} data
 * @returns {string}
 */
function diagnosisSection(data) {
  const items = data.diagnosis || [];
  if (!items.length) return '';
  const list = items
    .map(
      (item) => `<li class="diag-item diag-${escapeHtml(item.tone || 'info')}">
        <p class="diag-title">${escapeHtml(item.title)}</p>
        <p class="diag-text">${escapeHtml(item.text)}</p>
      </li>`
    )
    .join('');
  return `<section class="report-section">
    <h2><span class="sec-no">三</span>保障诊断小结</h2>
    <ul class="diag-list">${list}</ul>
    <p class="table-note">结论由当前数据自动生成，不涉及任何投资或投保建议。</p>
  </section>`;
}

/**
 * 四、成员逐人分析。
 * @param {any} data
 * @returns {string}
 */
function memberSection(data) {
  if (!data.members || !data.members.length) {
    return `<section class="report-section"><h2><span class="sec-no">四</span>成员保障分析</h2>
      <p class="empty-line">还没有家庭成员，先到「家庭成员」页添加。</p></section>`;
  }

  const blocks = data.members
    .map((m) => {
      const chips = m.cells
        .filter((cell) => cell.has)
        .map(
          (cell) =>
            `<span class="cover-chip">${escapeHtml(cell.category)}
             <strong>${cell.amount ? wan(cell.amount) : '待补金额'}</strong></span>`
        )
        .join('');

      const gaps = (m.amountGaps || [])
        .map(
          (gap) => `<li>
            <strong>${escapeHtml(gap.item)}</strong>　现有 ${wan(gap.current)}，参考 ${wan(gap.suggested)}，
            缺口约 <span class="gap-amount">${wan(gap.gap)}</span>
            ${gap.note ? `<span class="cell-note">（${escapeHtml(gap.note)}）</span>` : ''}
          </li>`
        )
        .join('');

      const structure = (m.structureGaps || [])
        .map((text) => `<li class="structure-warn">${escapeHtml(text)}</li>`)
        .join('');

      const shared = (m.sharedNotes || [])
        .map((text) => `<li class="structure-note">${escapeHtml(text)}</li>`)
        .join('');

      const policies = (m.policies || []).map(memberPolicyHtml).join('');

      return `<div class="member-block">
        <div class="member-head">
          <div>
            <span class="member-name">${escapeHtml(m.name)}</span>
            <span class="member-meta">${escapeHtml(m.relation)}${m.age != null ? ` · ${m.age} 岁` : ''}
              · ${fmtInt(m.policyCount)} 张保单${m.sharedPolicyCount ? `（另 ${fmtInt(m.sharedPolicyCount)} 张共保）` : ''}
              · 年化保费 ${fmtMoney(m.annualPremium)}</span>
          </div>
          <div class="member-total">给付合计 <strong>${wan(protectionTotal(m.cells))}</strong></div>
        </div>
        ${chips ? `<div class="cover-chips">${chips}</div>` : '<p class="empty-line">名下暂无可计入的在保保障。</p>'}
        ${structure || shared || gaps ? `<ul class="member-gaps">${structure}${shared}${gaps}</ul>` : ''}
        ${policies}
      </div>`;
    })
    .join('');

  return `<section class="report-section">
    <h2><span class="sec-no">四</span>成员保障分析</h2>
    ${blocks}
  </section>`;
}

/**
 * 四、保费分布（按投保人 / 按险种 / 按月）。
 * @param {any} data
 * @returns {string}
 */
function premiumSection(data) {
  const premium = data.premium || { byMember: [], byType: [], byMonth: [] };

  const rowTable = (rows, label) => {
    if (!rows.length) return '<p class="empty-line">暂无数据。</p>';
    return `<table class="data-table compact">
      <thead><tr><th>${label}</th><th class="num">年化保费</th><th class="num">占比</th></tr></thead>
      <tbody>
        ${rows
          .map(
            (row) => `<tr>
              <td>${escapeHtml(row.name)}</td>
              <td class="num">${fmtMoney(row.amount)}</td>
              <td class="num">${(row.ratio * 100).toFixed(1)}%</td>
            </tr>`
          )
          .join('')}
      </tbody>
    </table>`;
  };

  const months = premium.byMonth || [];
  const max = Math.max(...months.map((row) => Number(row.amount) || 0), 1);
  const bars = months
    .map(
      (row) => `<div class="month-bar">
        <div class="month-bar-track">
          <div class="month-bar-fill" style="height:${Math.round(((Number(row.amount) || 0) / max) * 100)}%"></div>
        </div>
        <div class="month-bar-label">${row.month}月</div>
        <div class="month-bar-value">${compactWan(row.amount)}</div>
      </div>`
    )
    .join('');

  const ratio = data.overview ? data.overview.premiumRatio : null;
  const ratioLine = ratio == null
    ? '未填写家庭年收入，暂不评估保费负担率。'
    : `年化保费约占家庭年收入的 <strong>${(ratio * 100).toFixed(1)}%</strong>` +
      `（经验区间 5%–15%，当前${ratio > 0.15 ? '偏高' : ratio < 0.05 ? '偏低' : '在区间内'}）。`;

  return `<section class="report-section">
    <h2><span class="sec-no">五</span>保费分布</h2>
    <p class="premium-ratio">${ratioLine}</p>
    <div class="panel-grid">
      <div><h3 class="sub-title">按投保人</h3>${rowTable(premium.byMember, '投保人')}</div>
      <div><h3 class="sub-title">按险种</h3>${rowTable(premium.byType, '险种')}</div>
    </div>
    <h3 class="sub-title">全年支出节奏（月缴平摊、年缴落在生效月）</h3>
    <div class="month-bars">${bars}</div>
    <p class="table-note">保费按<strong>投保人</strong>归属，与总览看板口径一致；已退保 / 已失效 / 已过期的保单不计入。</p>
  </section>`;
}

/**
 * 五、保单基础信息汇总。
 * @param {any} data
 * @returns {string}
 */
function policySection(data) {
  const rows = data.policies || [];
  if (!rows.length) {
    return `<section class="report-section"><h2><span class="sec-no">六</span>保单汇总</h2>
      <p class="empty-line">还没有保单。</p></section>`;
  }

  const body = rows
    .map(
      (row) => `<tr class="${row.counted ? '' : 'row-muted'}">
        <td>${escapeHtml(row.productName)}</td>
        <td>${escapeHtml(row.company)}</td>
        <td>${escapeHtml(row.insuranceType)}</td>
        <td>${escapeHtml(row.status)}${row.expired ? ' <span class="tag-warn">已过期</span>' : ''}</td>
        <td>${escapeHtml((row.insuredNames || []).join('、') || '—')}</td>
        <td>${escapeHtml(row.policyholderName)}</td>
        <td>${escapeHtml(fmtDate(row.effectiveDate))}</td>
        <td>${row.coverageEndDate ? escapeHtml(fmtDate(row.coverageEndDate)) : '终身'}</td>
        <td class="num">${wan(row.insuredAmount)}</td>
        <td class="num">${row.counted && row.annualizedPremium > 0 ? fmtMoney(row.annualizedPremium) : '—'}</td>
        <td class="num">${row.totalPeriods ? `${row.paidPeriods}/${row.totalPeriods}` : '—'}</td>
      </tr>`
    )
    .join('');

  return `<section class="report-section">
    <h2><span class="sec-no">六</span>保单汇总</h2>
    <div class="table-scroll">
      <table class="data-table compact">
        <thead><tr>
          <th>产品</th><th>公司</th><th>险种</th><th>状态</th><th>被保人</th><th>投保人</th>
          <th>生效日</th><th>保障至</th><th class="num">保额</th>
          <th class="num">年化保费</th><th class="num">已缴/总期</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    <p class="table-note">灰色行 = 未计入保障与保费合计（已退保、已失效或已过保障期）。</p>
  </section>`;
}

/**
 * 六、交费计划。
 * @param {any} data
 * @returns {string}
 */
function paymentSection(data) {
  const plan = data.paymentPlan || { items: [], total: 0, horizonDays: 365 };
  const items = plan.items || [];
  if (!items.length) {
    return `<section class="report-section"><h2><span class="sec-no">七</span>交费计划</h2>
      <p class="empty-line">未来 ${plan.horizonDays} 天没有待缴保费（可能都已缴清）。</p></section>`;
  }

  const body = items
    .map(
      (row) => `<tr>
        <td>${escapeHtml(fmtDate(row.dueDate))}</td>
        <td>${escapeHtml(row.productName)}</td>
        <td>${escapeHtml(row.company)}</td>
        <td class="num">第 ${row.periodIndex} 期</td>
        <td class="num">${fmtMoney(row.amount)}</td>
      </tr>`
    )
    .join('');

  return `<section class="report-section">
    <h2><span class="sec-no">七</span>交费计划（未来 ${plan.horizonDays} 天）</h2>
    <div class="table-scroll">
      <table class="data-table compact">
        <thead><tr><th>缴费日</th><th>产品</th><th>公司</th><th class="num">期次</th><th class="num">金额</th></tr></thead>
        <tbody>${body}</tbody>
        <tfoot><tr><td colspan="4">合计</td><td class="num">${fmtMoney(plan.total)}</td></tr></tfoot>
      </table>
    </div>
  </section>`;
}

/**
 * 七、行动清单。
 * @param {any} data
 * @returns {string}
 */
function actionSection(data) {
  const actions = data.actions || [];
  if (!actions.length) {
    return `<section class="report-section"><h2><span class="sec-no">八</span>行动清单</h2>
      <p class="empty-line">按当前数据没有需要立刻处理的事项。</p></section>`;
  }

  const items = actions
    .map(
      (action) => `<li class="${PRIORITY_CLASS[action.priority] || 'p-low'}">
        <span class="action-tag">${escapeHtml(PRIORITY_TEXT[action.priority] || action.priority)}</span>
        <div>
          <p class="action-title">${escapeHtml(action.title)}</p>
          <p class="action-detail">${escapeHtml(action.detail)}</p>
        </div>
      </li>`
    )
    .join('');

  return `<section class="report-section">
    <h2><span class="sec-no">八</span>行动清单</h2>
    <ul class="action-list">${items}</ul>
  </section>`;
}

/**
 * 八、口径与免责（打印时也要保留，这是报告的诚信底线）。
 * @param {any} data
 * @returns {string}
 */
function notesSection(data) {
  const o = data.overview || {};
  return `<section class="report-section report-notes">
    <h2><span class="sec-no">九</span>测算口径与说明</h2>
    <ol class="notes-list">
      <li>本报告由「家庭保险整理工具」按录入数据<strong>实时汇总</strong>生成，不是存档快照；
        保单信息变动后，重新打开即为新数。</li>
      <li>只有状态为「生效中 / 已缴清」且仍在保障期内的保单计入保障与保费合计；
        已退保、已失效、已过保障期的保单不计入（本次共排除
        ${fmtInt(o.endedCount + o.expiredCount)} 张：退保/失效 ${fmtInt(o.endedCount)} 张、
        过期 ${fmtInt(o.expiredCount)} 张）。</li>
      <li>给付型保额为「重大疾病 + 身故全残 + 意外」之和；医疗险是报销型限额，
        不可叠加，单列展示，不并入合计。</li>
      <li>多被保人保单的保额统一记在主被保人名下，其他被保人的份额请对照条款核对。</li>
      <li>${fmtInt(o.coverageMissing)} 张保单尚未录入责任明细，相关金额按险种与保额粗算，
        精度有限；补齐后结论会更准。</li>
      <li>额度缺口为行业通行经验值（重疾 3 倍年收入、寿险负债 + 5 倍年支出、意外 5 倍年收入），
        不是监管标准，也不构成投保建议；实际投保需结合健康状况、预算与产品条款。</li>
      <li>本报告不包含现金价值、生存金、分红与增值服务信息——这些数据不在本工具的数据范围内，
        不做推测。</li>
    </ol>
  </section>`;
}

/**
 * 完整页面 HTML。
 * @param {any} data
 * @returns {string}
 */
function pageHtml(data) {
  return `<div class="report-doc">
    ${toolbarHtml(data)}
    ${headHtml(data)}
    ${overviewSection(data)}
    ${gapSection(data)}
    ${diagnosisSection(data)}
    ${memberSection(data)}
    ${premiumSection(data)}
    ${policySection(data)}
    ${paymentSection(data)}
    ${actionSection(data)}
    ${notesSection(data)}
  </div>`;
}

/* -------------------------------------------------------------------------- */
/* 渲染与事件                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * 拉数据并重绘（唯一的刷新入口，页面状态不落地）。
 * @param {HTMLElement} container
 */
async function load(container) {
  container.innerHTML = '<div class="report-loading">正在按当前数据生成报告…</div>';
  let data;
  try {
    data = await api.getReport();
  } catch (error) {
    container.innerHTML = `<div class="report-error">
      <p>报告生成失败：${escapeHtml(error.message)}</p>
      <button class="btn" type="button" data-action="refresh">重试</button>
    </div>`;
    return;
  }
  container.innerHTML = pageHtml(data);
}

/**
 * 绑定页面内事件。
 *
 * ⚠️ 只在首次进入视图时绑定一次，全部走**事件委托**：
 * 每次刷新都会整块替换 `innerHTML`，若按渲染次数绑监听器，点一次「重新生成」
 * 会叠加出 N 个处理器，越刷越卡。
 * @param {HTMLElement} container
 */
function bind(container) {
  container.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'refresh') {
      await load(container);
    } else if (action === 'export') {
      window.location.href = api.REPORT_DOCX_URL;
    } else if (action === 'print') {
      window.print();
    }
  });

  container.addEventListener('submit', async (event) => {
    const form = event.target.closest('[data-role="param-form"]');
    if (!form) return;
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const read = (name) => {
      const field = form.querySelector(`[name="${name}"]`);
      const raw = field ? field.value.trim() : '';
      return raw === '' ? null : Number(raw);
    };
    if (submit) {
      submit.disabled = true;
      submit.textContent = '保存中…';
    }
    try {
      await api.saveReportParams({
        annualIncome: read('annualIncome'),
        totalDebt: read('totalDebt'),
        annualExpense: read('annualExpense'),
      });
      await load(container);
    } catch (error) {
      if (submit) {
        submit.disabled = false;
        submit.textContent = '保存并重新测算';
      }
      window.dispatchEvent(
        new CustomEvent('app:toast', { detail: { message: error.message, type: 'error' } })
      );
    }
  });
}

/**
 * 视图入口：每次进入都重新请求，不复用上一次的结果。
 * @param {HTMLElement} container
 */
export async function render(container) {
  if (!container.dataset.reportBound) {
    bind(container);
    container.dataset.reportBound = '1';
  }
  await load(container);
}
