const API = "";

const $ = (id) => document.getElementById(id);

const state = {
  apiKey: localStorage.getItem("cancelshield_api_key") || "",
  uploadSubscriptionId: null,
};

$("api-key").value = state.apiKey;

function setStatus(id, message, isError = false) {
  const el = $(id);
  el.textContent = message;
  el.style.color = isError ? "#9b1b1b" : "#115c58";
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const isFormData = options.body instanceof FormData;

  if (!isFormData) {
    headers["Content-Type"] = "application/json";
  }
  if (state.apiKey) {
    headers["X-API-Key"] = state.apiKey;
  }

  const response = await fetch(`${API}${path}`, {
    ...options,
    headers,
  });

  const rawText = await response.text();
  let data = null;
  if (rawText) {
    try {
      data = JSON.parse(rawText);
    } catch {
      data = { raw: rawText };
    }
  }

  if (!response.ok) {
    const detail = data?.detail || "请求失败";
    throw new Error(`${response.status}: ${detail}`);
  }

  return data;
}

async function bootstrapTeam() {
  const name = $("team-name").value.trim();
  const ownerEmail = $("team-owner-email").value.trim();
  if (!name || !ownerEmail) {
    setStatus("bootstrap-status", "请填写团队名称和管理员邮箱", true);
    return;
  }

  try {
    const data = await request("/api/v1/teams/bootstrap", {
      method: "POST",
      body: JSON.stringify({ name, owner_email: ownerEmail }),
    });
    state.apiKey = data.api_key;
    $("api-key").value = data.api_key;
    localStorage.setItem("cancelshield_api_key", data.api_key);
    setStatus("bootstrap-status", `团队创建成功，team_id=${data.team_id}`);
    await loadTeam();
    await refreshMembers();
  } catch (err) {
    setStatus("bootstrap-status", err.message, true);
  }
}

function saveApiKey() {
  const key = $("api-key").value.trim();
  state.apiKey = key;
  localStorage.setItem("cancelshield_api_key", key);
  setStatus("team-status", "API Key 已保存");
}

async function loadTeam() {
  try {
    const data = await request("/api/v1/teams/me");
    setStatus("team-status", `当前团队：${data.team_name} (#${data.team_id})，角色=${data.role}`);
  } catch (err) {
    setStatus("team-status", err.message, true);
  }
}

async function createApiKey() {
  const label = $("key-label").value.trim();
  const role = $("key-role").value;
  const createdBy = $("key-created-by").value.trim() || null;
  if (!label) {
    setStatus("member-status", "请填写 Key 标签", true);
    return;
  }

  try {
    const data = await request("/api/v1/teams/api-keys", {
      method: "POST",
      body: JSON.stringify({ label, role, created_by_email: createdBy }),
    });
    setStatus("member-status", `新 Key: ${data.api_key}`);
  } catch (err) {
    setStatus("member-status", err.message, true);
  }
}

async function refreshMembers() {
  try {
    const rows = await request("/api/v1/teams/members");
    const tbody = $("member-rows");
    tbody.innerHTML = "";
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${row.id}</td><td>${row.email}</td><td>${row.role}</td>`;
      tbody.appendChild(tr);
    }
    setStatus("member-status", `成员数：${rows.length}`);
  } catch (err) {
    setStatus("member-status", err.message, true);
  }
}

async function addMember() {
  const email = $("member-email").value.trim();
  const role = $("member-role").value;
  if (!email) {
    setStatus("member-status", "请填写成员邮箱", true);
    return;
  }

  try {
    await request("/api/v1/teams/members", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    });
    setStatus("member-status", "成员已添加");
    await refreshMembers();
  } catch (err) {
    setStatus("member-status", err.message, true);
  }
}

async function saveChannel() {
  const provider = $("notify-provider").value;
  const webhookUrl = $("notify-webhook").value.trim();
  const enabled = $("notify-enabled").checked;
  if (!webhookUrl) {
    setStatus("notify-status", "请填写 webhook URL", true);
    return;
  }
  try {
    await request("/api/v1/notifications/channels", {
      method: "POST",
      body: JSON.stringify({ provider, webhook_url: webhookUrl, enabled }),
    });
    setStatus("notify-status", "通知通道已保存");
    await loadChannels();
  } catch (err) {
    setStatus("notify-status", err.message, true);
  }
}

async function loadChannels() {
  try {
    const rows = await request("/api/v1/notifications/channels");
    const text = rows
      .map((x) => `${x.provider}:${x.enabled ? "on" : "off"}`)
      .join(" | ");
    setStatus("notify-status", text || "暂无通知通道");
  } catch (err) {
    setStatus("notify-status", err.message, true);
  }
}

async function testChannel() {
  try {
    const data = await request("/api/v1/notifications/test", { method: "POST" });
    setStatus("notify-status", `测试完成 attempted=${data.attempted} sent=${data.sent} failed=${data.failed}`);
  } catch (err) {
    setStatus("notify-status", err.message, true);
  }
}

async function createSubscription() {
  const payload = {
    vendor: $("vendor").value.trim(),
    plan_name: $("plan-name").value.trim() || null,
    amount: Number($("amount").value),
    currency: $("currency").value.trim() || "USD",
    renewal_date: $("renewal-date").value,
    owner_email: $("owner-email").value.trim(),
    notes: $("notes").value.trim() || null,
  };

  try {
    await request("/api/v1/subscriptions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setStatus("subscription-status", "订阅已创建");
    refreshSubscriptions();
  } catch (err) {
    setStatus("subscription-status", err.message, true);
  }
}

async function runReminders() {
  try {
    const data = await request("/api/v1/reminders/run", { method: "POST" });
    setStatus(
      "subscription-status",
      `提醒任务完成：queued=${data.queued_count}，涉及订阅=${data.subscriptions.join(", ") || "无"}`,
    );
  } catch (err) {
    setStatus("subscription-status", err.message, true);
  }
}

async function previewReminders(subscriptionId) {
  try {
    const data = await request(`/api/v1/subscriptions/${subscriptionId}/reminders/preview`);
    setStatus(
      "list-status",
      `订阅 #${subscriptionId} 提醒日期：${data.reminder_dates.join(" / ")}`,
    );
  } catch (err) {
    setStatus("list-status", err.message, true);
  }
}

async function exportDispute(subscriptionId) {
  try {
    const data = await request(`/api/v1/subscriptions/${subscriptionId}/dispute-export`, {
      method: "POST",
    });
    setStatus(
      "list-status",
      `订阅 #${subscriptionId} 导出完成，证据数=${data.evidence_count}，文件=${data.export_path}`,
    );
  } catch (err) {
    setStatus("list-status", err.message, true);
  }
}

async function addEvidence(subscriptionId) {
  const eventType = prompt("事件类型", "cancel_attempt");
  if (!eventType) return;
  const actor = prompt("操作者", "owner@company.com");
  if (!actor) return;

  try {
    await request(`/api/v1/subscriptions/${subscriptionId}/evidence`, {
      method: "POST",
      body: JSON.stringify({
        event_type: eventType,
        actor,
        occurred_at: new Date().toISOString(),
        details: "captured from console",
      }),
    });
    setStatus("list-status", `订阅 #${subscriptionId} 证据已新增`);
  } catch (err) {
    setStatus("list-status", err.message, true);
  }
}

function startUploadEvidence(subscriptionId) {
  state.uploadSubscriptionId = subscriptionId;
  $("evidence-file").value = "";
  $("evidence-file").click();
}

async function uploadEvidenceFromPicker() {
  const fileInput = $("evidence-file");
  const file = fileInput.files?.[0];
  const subscriptionId = state.uploadSubscriptionId;
  if (!file || !subscriptionId) {
    return;
  }

  const actor = prompt("上传证据-操作者", "owner@company.com");
  if (!actor) return;
  const eventType = prompt("上传证据-事件类型", "cancel_attempt");
  if (!eventType) return;

  try {
    const base64Content = await fileToBase64(file);
    const data = await request(`/api/v1/subscriptions/${subscriptionId}/evidence/upload`, {
      method: "POST",
      body: JSON.stringify({
        actor,
        event_type: eventType,
        occurred_at: new Date().toISOString(),
        details: "uploaded from console",
        file_name: file.name,
        file_content_base64: base64Content,
      }),
    });
    setStatus("list-status", `上传成功：证据 #${data.id}`);
  } catch (err) {
    setStatus("list-status", err.message, true);
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const raw = String(reader.result || "");
      const idx = raw.indexOf(",");
      if (idx < 0) {
        reject(new Error("文件读取失败"));
        return;
      }
      resolve(raw.slice(idx + 1));
    };
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}

function renderRows(items) {
  const tbody = $("subscription-rows");
  tbody.innerHTML = "";

  for (const item of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${item.id}</td>
      <td>${item.vendor}</td>
      <td>${item.plan_name || "-"}</td>
      <td>${item.amount} ${item.currency}</td>
      <td>${item.renewal_date}</td>
      <td>${item.owner_email}</td>
      <td class="row-actions">
        <button data-action="preview" data-id="${item.id}">提醒预览</button>
        <button data-action="evidence" data-id="${item.id}">新增证据</button>
        <button data-action="upload" data-id="${item.id}">上传证据文件</button>
        <button data-action="export" data-id="${item.id}">导出争议包</button>
      </td>
    `;
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.id);
      const action = btn.dataset.action;
      if (action === "preview") previewReminders(id);
      if (action === "evidence") addEvidence(id);
      if (action === "upload") startUploadEvidence(id);
      if (action === "export") exportDispute(id);
    });
  });
}

async function refreshSubscriptions() {
  try {
    const data = await request("/api/v1/subscriptions");
    renderRows(data || []);
    setStatus("list-status", `已加载 ${data.length} 条订阅`);
  } catch (err) {
    setStatus("list-status", err.message, true);
  }
}

$("bootstrap-btn").addEventListener("click", bootstrapTeam);
$("save-key-btn").addEventListener("click", saveApiKey);
$("load-team-btn").addEventListener("click", loadTeam);
$("create-sub-btn").addEventListener("click", createSubscription);
$("refresh-btn").addEventListener("click", refreshSubscriptions);
$("run-reminders-btn").addEventListener("click", runReminders);
$("add-member-btn").addEventListener("click", addMember);
$("refresh-members-btn").addEventListener("click", refreshMembers);
$("create-key-btn").addEventListener("click", createApiKey);
$("save-channel-btn").addEventListener("click", saveChannel);
$("test-channel-btn").addEventListener("click", testChannel);
$("load-channel-btn").addEventListener("click", loadChannels);
$("evidence-file").addEventListener("change", uploadEvidenceFromPicker);

if (state.apiKey) {
  loadTeam();
  refreshSubscriptions();
  refreshMembers();
  loadChannels();
}
