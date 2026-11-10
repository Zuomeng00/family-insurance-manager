/**
 * 访问密码登录闸门（v1.9）。
 *
 * 未登录时用**全屏遮罩**挡住整个应用（而不是只挡数据）：
 * 后端已经拦住所有业务接口，遮罩只是让「看不见任何东西」这件事在界面上也成立，
 * 免得用户对着一堆报错猜哪里坏了。
 *
 * 关键约束：
 * 1. 密码只提交给 `/api/auth/login`，不在前端保存、不写 localStorage；
 * 2. 登录态是 HttpOnly Cookie，前端读不到也不需要读；
 * 3. 遮罩本身可重复调用（登录过期时会被再次拉起来）。
 */

import * as api from '../api.js';

/** 首次挂在闸门上的「登录成功后要做什么」。 */
let onSuccessHandler = null;

/**
 * 显示登录闸门。
 * @param {() => void|Promise<void>} [onSuccess] 登录成功后的回调（重新引导应用）
 */
export function showLoginGate(onSuccess) {
  const gate = document.getElementById('auth-gate');
  if (!gate) return;
  if (typeof onSuccess === 'function') onSuccessHandler = onSuccess;

  gate.innerHTML = `
    <form class="auth-card" id="auth-form" novalidate>
      <div class="auth-brand">
        <svg viewBox="0 0 32 32" width="34" height="34" aria-hidden="true" focusable="false">
          <defs>
            <linearGradient id="auth-shield" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stop-color="#2f6feb"></stop>
              <stop offset="1" stop-color="#1b4fd8"></stop>
            </linearGradient>
          </defs>
          <path d="M16 2 L28 7 V15 C28 22.5 22.8 27.6 16 30 C9.2 27.6 4 22.5 4 15 V7 Z" fill="url(#auth-shield)"></path>
          <path d="M10.5 16.2 L14.4 20.1 L21.8 12.2" fill="none" stroke="#ffffff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
        <span class="auth-brand-name">家庭保险整理工具</span>
      </div>
      <p class="auth-tip">该站点已开启访问密码，请输入密码后继续</p>
      <input
        class="auth-input"
        type="password"
        id="auth-password"
        placeholder="访问密码"
        autocomplete="current-password"
        enterkeyhint="go"
      >
      <p class="auth-error" id="auth-error" hidden></p>
      <button class="btn primary auth-submit" type="submit" id="auth-submit">进入</button>
      <p class="auth-foot">
        忘记密码？在 <code>docker-compose.yml</code> 里加一行
        <code>AUTH_DISABLED=true</code> 后重启容器，即可临时跳过密码。
      </p>
    </form>
  `;

  gate.hidden = false;
  document.body.classList.add('auth-locked');

  const form = gate.querySelector('#auth-form');
  const input = gate.querySelector('#auth-password');
  const errorEl = gate.querySelector('#auth-error');
  const submitBtn = gate.querySelector('#auth-submit');

  const showError = (message) => {
    errorEl.textContent = message;
    errorEl.hidden = false;
  };

  input.addEventListener('input', () => {
    errorEl.hidden = true;
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const password = input.value;
    if (!password) {
      showError('请输入访问密码');
      input.focus();
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = '验证中…';
    try {
      await api.login(password);
      hideLoginGate();
      if (onSuccessHandler) await onSuccessHandler();
    } catch (error) {
      // 错误文案由后端给（密码不正确 / 错误次数过多请 N 分钟后再试）
      showError(error.message);
      input.select();
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = '进入';
    }
  });

  input.focus();
}

/** 隐藏登录闸门（未显示时调用也安全）。 */
export function hideLoginGate() {
  const gate = document.getElementById('auth-gate');
  if (!gate) return;
  gate.hidden = true;
  gate.innerHTML = '';
  document.body.classList.remove('auth-locked');
}

/**
 * 闸门当前是否可见。
 * @returns {boolean}
 */
export function isLoginGateVisible() {
  const gate = document.getElementById('auth-gate');
  return Boolean(gate && !gate.hidden);
}
