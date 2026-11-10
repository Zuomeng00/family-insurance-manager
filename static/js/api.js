/**
 * 后端 API 封装（同源，`BASE = '/api'`）。
 *
 * - 所有请求走相对路径，保证「本地 8000 端口」与「Docker 容器」两套运行方式完全一致；
 * - `request()` 统一处理 JSON 解析与错误：非 2xx 时抛出 `{ code, message, detail, status }`，
 *   与后端统一错误格式（见 ARCHITECTURE.md 4.1）一一对应，调用方可直接展示 `message`。
 */

const BASE = '/api';

/**
 * 统一 fetch 封装。
 * @param {string} path 以 `/` 开头的路径（不含 BASE 前缀）
 * @param {{method?: string, body?: any}} [options]
 * @returns {Promise<any>} 解析后的 JSON 数据
 */
async function request(path, options = {}) {
  const init = { method: options.method || 'GET', headers: {} };
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  if (isFormData) {
    // multipart/form-data：**不手动设 Content-Type**，交由浏览器自动带 boundary
    init.body = options.body;
  } else if (options.body !== undefined && options.body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(BASE + path, init);
  } catch (error) {
    // 网络层失败（后端未启动 / 断网）
    throw { code: 'NETWORK_ERROR', message: '无法连接服务器，请确认后端已启动', detail: null, status: 0 };
  }

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (error) {
      data = null;
    }
  }

  if (!response.ok) {
    // 401 = 未登录/登录过期：全局广播，由入口统一弹登录闸门。
    // 登录接口本身除外——否则一次输错密码就重渲染登录页，把已输入的内容和错误提示一起冲掉。
    if (response.status === 401 && path !== '/auth/login') {
      window.dispatchEvent(new CustomEvent('app:unauthorized'));
    }
    const err = data && typeof data === 'object' ? data : {};
    throw {
      code: err.code || `HTTP_${response.status}`,
      message: err.message || `请求失败（HTTP ${response.status}）`,
      detail: err.detail !== undefined ? err.detail : null,
      status: response.status,
    };
  }
  return data;
}

/**
 * 将对象拼装为查询字符串（跳过空值）。
 * @param {Record<string, any>} params
 * @returns {string} 形如 `?a=1&b=2`，无参数时返回空串
 */
function buildQuery(params) {
  const usp = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    usp.append(key, value);
  });
  const qs = usp.toString();
  return qs ? `?${qs}` : '';
}

// ---- 元数据 / 健康检查 ----
export function getHealth() {
  return request('/health');
}

export function getEnums() {
  return request('/meta/enums');
}

// ---- 家庭成员 ----
export function getMembers() {
  return request('/members');
}

export function createMember(payload) {
  return request('/members', { method: 'POST', body: payload });
}

export function updateMember(id, payload) {
  return request(`/members/${encodeURIComponent(id)}`, { method: 'PUT', body: payload });
}

export function deleteMember(id) {
  return request(`/members/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ---- 保单 ----
export function getPolicies(filters = {}) {
  return request(`/policies${buildQuery(filters)}`);
}

export function getPolicy(id) {
  return request(`/policies/${encodeURIComponent(id)}`);
}

export function createPolicy(payload) {
  return request('/policies', { method: 'POST', body: payload });
}

export function updatePolicy(id, payload) {
  return request(`/policies/${encodeURIComponent(id)}`, { method: 'PUT', body: payload });
}

export function deletePolicy(id) {
  return request(`/policies/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/**
 * 把「最早未缴期次」标记为已缴（写入缴费记录）。
 * @param {string} id 保单 id
 * @returns {Promise<any>} 更新后的保单
 */
export function payNextPeriod(id) {
  return request(`/policies/${encodeURIComponent(id)}/pay-next`, { method: 'POST' });
}

// ---- 批量编辑 / 常用值（v1.2）----

/**
 * 批量编辑售后字段（三态语义由服务端处理：键缺失=不改 / 字符串=设值 / null=清空）。
 * @param {string[]} ids 目标保单 id
 * @param {Record<string, string|null>} values 只包含「要修改」的字段
 * @returns {Promise<{requested:number, updated:number, updatedIds:string[], missingIds:string[]}>}
 */
export function batchUpdatePolicies(ids, values) {
  return request('/policies/batch-update', { method: 'POST', body: { ids, values } });
}

/**
 * 售后字段「常用值」聚合（6 个字段，`companyPhone` 不参与）。
 * @param {number} [limit] 每个字段返回的最大条数（1~50）
 * @returns {Promise<Record<string, Array<{value:string, count:number}>>>}
 */
export function getFieldSuggestions(limit) {
  return request(`/policies/suggestions${buildQuery({ limit })}`);
}

// ---- 电子保单附件（v1.2）----

/**
 * 获取某保单的附件列表。
 * @param {string} policyId
 * @returns {Promise<{policyId:string, attachmentCount:number, attachments:Array}>}
 */
export function getAttachments(policyId) {
  return request(`/policies/${encodeURIComponent(policyId)}/attachments`);
}

/**
 * 上传一个或多个 PDF 附件（multipart/form-data，字段名固定为 `files`）。
 * @param {string} policyId
 * @param {FileList|File[]} files
 * @returns {Promise<{uploaded:Array, attachmentCount:number}>}
 */
export function uploadAttachments(policyId, files) {
  const form = new FormData();
  Array.from(files || []).forEach((file) => form.append('files', file));
  return request(`/policies/${encodeURIComponent(policyId)}/attachments`, {
    method: 'POST',
    body: form,
  });
}

/**
 * 删除单个附件。
 * @param {string} policyId
 * @param {string} attId
 * @returns {Promise<{deleted:boolean, id:string}>}
 */
export function deleteAttachment(policyId, attId) {
  return request(
    `/policies/${encodeURIComponent(policyId)}/attachments/${encodeURIComponent(attId)}`,
    { method: 'DELETE' }
  );
}

/**
 * 生成附件查看 / 下载 URL（**返回字符串**，供 `<a href>` / `window.open` 直接使用，不走 fetch）。
 * @param {string} policyId
 * @param {string} attId
 * @param {'inline'|'attachment'} [disposition] 默认 `inline`（浏览器内预览）
 * @returns {string}
 */
export function attachmentContentUrl(policyId, attId, disposition = 'inline') {
  const mode = disposition === 'attachment' ? 'attachment' : 'inline';
  return `${BASE}/policies/${encodeURIComponent(policyId)}/attachments/${encodeURIComponent(attId)}/content?disposition=${mode}`;
}

// ---- 统计看板 ----
export function getDashboard(today) {
  return request(`/stats/dashboard${buildQuery({ today })}`);
}

/**
 * 读取「不计入统计」排除配置（v1.7）。
 * @returns {Promise<{excludedPolicyholderIds:string[], excludedInsuranceTypes:string[]}>}
 */
export function getExclusions() {
  return request('/stats/exclusions');
}

/**
 * 全量替换「不计入统计」排除配置（v1.7）。
 * @param {{excludedPolicyholderIds:string[], excludedInsuranceTypes:string[]}} payload
 * @returns {Promise<{excludedPolicyholderIds:string[], excludedInsuranceTypes:string[]}>}
 */
export function saveExclusions(payload) {
  return request('/stats/exclusions', { method: 'PUT', body: payload });
}

// ---- 消息通知（v1.8）----

/**
 * 读取通知配置。
 * @returns {Promise<{enabled:boolean, webhook:string, webhookConfigured:boolean,
 *   expiryEnabled:boolean, expiryDays:number, dueEnabled:boolean, dueDays:number,
 *   sendTime:string, lastSentDate:string|null, today:string}>}
 */
export function getNotifySettings() {
  return request('/notifications/settings');
}

/**
 * 部分更新通知配置（只提交要改的字段；`webhook` 留空 = 不修改）。
 * @param {Record<string, any>} payload
 * @returns {Promise<any>} 更新后的完整配置（Webhook 已脱敏）
 */
export function saveNotifySettings(payload) {
  return request('/notifications/settings', { method: 'PUT', body: payload });
}

/**
 * 立即发送一条测试消息（不受启用开关 / 发送时间限制）。
 * @returns {Promise<{sent:boolean, expiring:number, due:number}>}
 */
export function sendNotifyTest() {
  return request('/notifications/test', { method: 'POST' });
}

// ---- 访问密码（v1.9）----

/**
 * 读取访问密码状态（公开接口，登录页本身也要先调它）。
 * @returns {Promise<{enabled:boolean, passwordSet:boolean, authenticated:boolean,
 *   forcedDisabled:boolean, sessionDays:number}>}
 */
export function getAuthStatus() {
  return request('/auth/status');
}

/**
 * 用访问密码登录；成功后服务端写入 HttpOnly Cookie（前端读不到，也不需要）。
 * @param {string} password
 * @returns {Promise<any>} 登录后的状态
 */
export function login(password) {
  return request('/auth/login', { method: 'POST', body: { password } });
}

/**
 * 退出登录（清除 Cookie）。
 * @returns {Promise<{ok:boolean}>}
 */
export function logout() {
  return request('/auth/logout', { method: 'POST' });
}

/**
 * 设置 / 修改访问密码。
 * @param {{newPassword: string, currentPassword?: string}} payload
 * @returns {Promise<any>} 更新后的状态（同时已签发新的登录态）
 */
export function setAuthPassword(payload) {
  return request('/auth/password', { method: 'PUT', body: payload });
}

/**
 * 开启 / 关闭访问限制（开启前必须已设置密码）。
 * @param {boolean} enabled
 * @returns {Promise<any>} 更新后的状态
 */
export function setAuthEnabled(enabled) {
  return request('/auth/enabled', { method: 'PUT', body: { enabled } });
}

export { BASE };