/**
 * 应用入口（T08）。
 *
 * 职责：
 * 1. 先过「访问密码」闸门（v1.9）：未登录则只显示登录页，不通业务视图；
 * 2. 预加载枚举字典到 store（全局下拉框字典，只取一次）；
 * 3. 注册 hash 路由（`#/dashboard` 默认 / `#/policies` / `#/members` / `#/settings`）并分发到对应视图；
 * 4. 绑定全局按钮（顶栏 tab、"+ 新增保单"）；
 * 5. 监听 store 派发的应用级事件：
 *    - `app:toast`   → 渲染轻提示；
 *    - `app:refresh` → 重新渲染当前视图（写操作后数据同步）。
 */

import * as store from './store.js';
import * as api from './api.js';
import { openModal } from './components/modal.js';
import { createPolicyForm } from './components/policyForm.js';
import { render as renderDashboard } from './views/dashboard.js';
import { render as renderPolicies } from './views/policies.js';
import { render as renderMembers } from './views/members.js';
import { render as renderSettings } from './views/settings.js';
import { hideLoginGate, isLoginGateVisible, showLoginGate } from './views/login.js';

/** 路由表：hash → { view 容器 id, render 渲染函数 }。 */
const ROUTES = {
  '#/dashboard': { view: 'view-dashboard', render: renderDashboard },
  '#/policies': { view: 'view-policies', render: renderPolicies },
  '#/members': { view: 'view-members', render: renderMembers },
  '#/settings': { view: 'view-settings', render: renderSettings, hideNewPolicy: true },
};

const DEFAULT_ROUTE = '#/dashboard';

/** @type {string} */
let currentRoute = DEFAULT_ROUTE;
/** @type {number|null} */
let toastTimer = null;

/**
 * 显示轻提示。
 * @param {string} message
 * @param {'info'|'success'|'error'} [type]
 */
function showToast(message, type = 'info') {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toast.hidden = false;
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, 3200);
}

/** 同步顶栏 tab 的高亮状态，并确保当前标签在窄屏下可见（标签栏可横向滚动）。 */
function updateTabs() {
  const tabs = document.getElementById('nav-tabs');
  let activeTab = null;
  document.querySelectorAll('#nav-tabs .tab').forEach((tab) => {
    const active = tab.dataset.route === currentRoute;
    tab.classList.toggle('active', active);
    if (active) activeTab = tab;
  });
  // 仅滚动标签栏自身（不牵动整页），把当前标签居中显示
  if (tabs && activeTab && tabs.scrollWidth > tabs.clientWidth) {
    const tabRect = activeTab.getBoundingClientRect();
    const barRect = tabs.getBoundingClientRect();
    const target =
      tabRect.left - barRect.left + tabs.scrollLeft - (tabs.clientWidth - tabRect.width) / 2;
    tabs.scrollLeft = Math.max(0, target);
  }
}

/** 按当前 hash 渲染对应视图。 */
async function route() {
  // 闸门挡着时不渲染：避免视图在遮罩后面一路 401 → 反复重绘登录页（会把已输入的密码冲掉）
  if (isLoginGateVisible()) return;

  let hash = window.location.hash;
  if (!ROUTES[hash]) {
    // 空 hash 或非法 hash → 归一到默认路由（会再次触发 hashchange）
    if (hash !== DEFAULT_ROUTE) {
      window.location.hash = DEFAULT_ROUTE;
      return;
    }
    hash = DEFAULT_ROUTE;
  }

  currentRoute = hash;
  updateTabs();

  document.querySelectorAll('.view').forEach((view) => {
    view.hidden = true;
  });

  const config = ROUTES[currentRoute];
  const container = document.getElementById(config.view);
  container.hidden = false;
  // 「+ 新增保单」是保单 / 成员 / 总览的快捷入口，在设置页无意义 → 隐藏
  const newPolicyBtn = document.getElementById('btn-new-policy');
  if (newPolicyBtn) newPolicyBtn.hidden = Boolean(config.hideNewPolicy);
  await config.render(container);
}

/** 打开「新增保单」弹窗（顶栏全局按钮）。 */
async function openNewPolicy() {
  let enums;
  let members;
  try {
    enums = await store.ensureEnums();
    members = store.get('members') || (await api.getMembers());
  } catch (error) {
    showToast(error.message, 'error');
    return;
  }

  if (!members || !members.length) {
    showToast('请先添加家庭成员，再新增保单', 'error');
    return;
  }

  const form = createPolicyForm(enums, members, null);
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

/** 绑定全局事件。 */
function bindGlobalEvents() {
  document.getElementById('nav-tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    window.location.hash = tab.dataset.route;
  });

  document.getElementById('btn-new-policy').addEventListener('click', openNewPolicy);

  window.addEventListener('hashchange', route);
  window.addEventListener('app:refresh', route);
  window.addEventListener('app:toast', (event) => {
    showToast(event.detail.message, event.detail.type);
  });
  // 任何接口返回 401（登录过期 / Cookie 被清）→ 立刻拉回登录页
  window.addEventListener('app:unauthorized', () => {
    showLoginGate(bootApp);
  });
}

/**
 * 访问密码闸门：未登录时不加载任何业务视图。
 * @returns {Promise<boolean>} 是否可以继续启动应用
 */
async function ensureAccess() {
  let status;
  try {
    status = await api.getAuthStatus();
  } catch (error) {
    // 后端不可达时不要假装「已登录」→ 也不假装「需要密码」，
    // 直接放行让各视图显示自己的错误，比对着登录页输密码强。
    showToast(error.message, 'error');
    return true;
  }
  if (status.enabled && !status.authenticated) {
    showLoginGate(bootApp);
    return false;
  }
  hideLoginGate();
  return true;
}

/** 真正启动应用：拉枚举 + 渲染当前路由。 */
async function bootApp() {
  try {
    await store.ensureEnums();
  } catch (error) {
    // 首屏枚举加载失败不阻塞渲染，各视图渲染时会再次尝试
    showToast(error.message, 'error');
  }
  await route();
}

/** 应用初始化。 */
async function init() {
  bindGlobalEvents();
  if (!(await ensureAccess())) return;
  await bootApp();
}

init();
