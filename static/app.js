"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ROLE_LABELS = { product: "商品图", reference: "人物参考图", driver: "动作驱动视频", last_frame: "尾帧图", voice: "口播音频" };
  const TOOL_LABELS = { auto: "自动选择", product: "商品广告", talking: "人物口播", motion: "动作迁移" };
  const STATE_LABELS = { queued: "排队中", running: "生成中", awaiting_review: "待确认参考帧", submission_unknown: "提交结果待确认", interrupted: "执行已中断", completed: "已完成", failed: "执行失败", paused: "已暂停" };
  const ACTIVE_STATES = new Set(["queued", "running"]);
  const OPTION_LABELS = { duration: "时长", duration_seconds: "时长（秒）", total_seconds: "时长（秒）", seconds: "时长（秒）", aspect: "画幅", aspect_ratio: "画幅", resolution: "分辨率", style: "风格", prompt: "生成提示词", positive_prompt: "画面要求", motion_prompt: "动作要求", script: "口播文案", text: "文案", quality_audit_enabled: "自动质检", quality_enabled: "自动质检", quality_audit_max_rounds: "最多修复轮数", quality_max_rounds: "最多修复轮数", segment_seconds: "分段时长（秒）", generation_mode: "生成模式", seam_transition_mode: "接缝方式", voice: "音色", voice_enabled: "更换音色", language: "语言", title: "标题", workflow: "工作流", workflow_preset: "生成规格", max_parallel_segments: "片段并发数", pipeline: "商品呈现", sound: "声音", creative_mode: "创作类型", notes: "补充要求", backend: "生成引擎", video_mode: "视频模式" };
  const OPTION_VALUES = { pipeline: { direct: "直接生成", multiview: "多视角参考" }, sound: { music: "背景音乐", ambient: "环境音", silent: "静音" }, creative_mode: { auto: "自动选择", ad: "广告", lifestyle: "生活场景", studio: "棚拍" }, generation_mode: { independent: "独立片段", continuous: "连续动作" }, seam_transition_mode: { rife: "帧插值", minimax_h3_all: "H3 过渡", minimax_h3: "H3 过渡", flf2v_all: "首尾帧过渡" }, backend: { wan: "WanAnimate2", h3_action: "MiniMax H3" }, video_mode: { legacy: "标准", two_pass: "两阶段", veda_two_pass: "两阶段（VEDA）", latent_two_pass: "两阶段（Latent）" } };
  const state = { settings: null, sessions: [], current: null, selectedTool: "auto", sending: false, uploading: false, executing: false, switching: 0, pollBusy: false, planSignature: "", messagesSignature: "", assetsSignature: "", jobsSignature: "", jobElements: new Map(), bindings: new Map(), actions: new Set(), requestIds: new Map(), submittedPlans: new Set(), noticeTimer: null };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function listValue(value, keys = ["sessions", "jobs", "items"]) {
    if (Array.isArray(value)) return value;
    for (const key of keys) if (Array.isArray(value?.[key])) return value[key];
    return [];
  }
  function unwrap(value, key) { return value?.[key] || value; }
  function safeUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try { const url = new URL(value, window.location.href); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; }
  }
  function stringify(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "boolean") return value ? "开启" : "关闭";
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }
  function readableError(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((item) => typeof item === "string" ? item : item.msg || stringify(item)).join("；");
    return detail?.message || detail?.error || stringify(detail) || "请求未完成，请稍后再试。";
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") {
      if (!state.settings?.app_token) throw new Error("服务尚未连接，请刷新页面后重试。");
      headers.set("X-App-Token", state.settings.app_token);
      if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    }
    let response;
    try { response = await fetch(path, { ...options, headers }); }
    catch { throw new Error("无法连接创作服务。请检查服务是否运行；已提交的云端任务可能仍在执行。"); }
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`服务返回了无法读取的响应（${response.status}）。请稍后刷新任务状态。`); }
    if (!response.ok) throw new Error(readableError(data.detail ?? data.error ?? data.message ?? data));
    return data;
  }
  function post(path, body = {}) { return api(path, { method: "POST", body: JSON.stringify(body) }); }
  function toast(message, error = false) {
    clearTimeout(state.noticeTimer);
    $("toast").textContent = message;
    $("toast").className = error ? "toast error" : "toast";
    $("toast").hidden = false;
    state.noticeTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 9000 : 4500);
  }
  function connection(ok, label) {
    $("connection-dot").className = `connection-dot ${ok ? "connected" : "disconnected"}`;
    $("connection-label").textContent = label || (ok ? "创作服务已连接" : "暂时无法连接");
  }
  function saveSession(id) { try { localStorage.setItem("video-agent-current-session", id); } catch { /* Storage may be unavailable. */ } }
  function storedSession() { try { return localStorage.getItem("video-agent-current-session"); } catch { return null; } }
  function uuid() { return globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  function toolLabel(id) { return TOOL_LABELS[id] || (id === "clarify" ? "补充需求" : null) || state.settings?.tools?.find((tool) => tool.id === id)?.label || id || "视频创作"; }
  function sessionTitle(session) {
    if (session.title) return session.title;
    const first = (session.messages || []).find((message) => message.role === "user");
    return String(first?.text || first?.content || "未命名创作").slice(0, 38);
  }
  function dateLabel(value) {
    if (!value) return "新会话";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }
  function renderSessions() {
    $("session-list").replaceChildren();
    $("session-count").textContent = state.sessions.length || "";
    if (!state.sessions.length) { $("session-list").append(element("p", "muted sidebar-empty", "还没有创作会话")); return; }
    for (const session of state.sessions) {
      const button = element("button", `session-item${session.id === state.current?.id ? " active" : ""}`);
      button.type = "button";
      button.setAttribute("aria-current", session.id === state.current?.id ? "true" : "false");
      button.append(element("span", "session-item-title", sessionTitle(session)), element("span", "session-item-date", dateLabel(session.updated_at || session.created_at)));
      button.addEventListener("click", () => { if (session.id !== state.current?.id) switchSession(session.id).catch((error) => toast(error.message, true)); });
      $("session-list").append(button);
    }
  }
  async function refreshSessions() {
    state.sessions = listValue(await api("/api/sessions"));
    renderSessions();
  }
  function renderTools() {
    $("tool-picker").replaceChildren();
    for (const [id, label] of Object.entries(TOOL_LABELS)) {
      const available = id === "auto" || state.settings?.tools?.find((tool) => tool.id === id)?.available !== false;
      const button = element("button", `tool-option${state.selectedTool === id ? " selected" : ""}`, label);
      button.type = "button";
      button.disabled = !available || state.sending;
      button.setAttribute("aria-pressed", state.selectedTool === id ? "true" : "false");
      const tool = state.settings?.tools?.find((item) => item.id === id);
      button.title = !available ? tool?.reason || "此流程暂不可用" : tool?.description || label;
      button.addEventListener("click", () => { state.selectedTool = id; renderTools(); });
      $("tool-picker").append(button);
    }
  }
  function updateControls() {
    $("send-button").disabled = !state.settings || !state.current || state.sending || state.uploading || !$("message-input").value.trim();
    $("upload-button").disabled = !state.settings || !state.current || state.uploading || state.sending;
    $("asset-role").disabled = state.uploading;
    $("new-session").disabled = !state.settings || state.sending || state.uploading;
    $("thinking").hidden = !state.sending;
    $("upload-button").textContent = state.uploading ? "正在上传…" : "＋ 上传素材";
    const plan = state.current?.plan;
    const toolUnavailable = plan && state.settings?.tools?.find((tool) => tool.id === plan.tool)?.available === false;
    const jobRunning = (state.current?.jobs || []).some((job) => ACTIVE_STATES.has(job.state) || job.state === "awaiting_review" || job.state === "submission_unknown");
    const alreadySubmitted = plan && state.submittedPlans.has(plan.id);
    $("execute-button").disabled = !plan?.ready || toolUnavailable || state.executing || state.sending || state.uploading || jobRunning || alreadySubmitted;
    $("execute-button").textContent = state.executing ? "正在提交…" : "▷ 确认计划并生成";
    $("execute-hint").textContent = jobRunning ? "已有任务在进行，请在下方查看或继续" : alreadySubmitted ? "本计划已提交，可在任务中查看进度" : !plan ? "补全需求与素材后即可开始" : !plan.ready ? "请根据计划补充需求或上传所需素材" : toolUnavailable ? "所选创作服务暂不可用" : "准备就绪，确认后开始生成";
  }
  function renderMessages() {
    const messages = state.current?.messages || [];
    const signature = JSON.stringify(messages);
    if (signature === state.messagesSignature) return;
    state.messagesSignature = signature;
    const nearBottom = $("conversation").scrollHeight - $("conversation").scrollTop - $("conversation").clientHeight < 100;
    $("messages").replaceChildren();
    $("welcome").hidden = messages.length > 0;
    for (const message of messages) {
      const role = ["user", "assistant", "system"].includes(message.role) ? message.role : "assistant";
      const row = element("article", `message ${role}`);
      const body = element("div", "message-body");
      body.append(element("div", "message-author", role === "user" ? "你" : role === "system" ? "创作提示" : "创作 Agent"));
      body.append(element("div", "message-content", message.text ?? message.content ?? ""));
      row.append(element("div", "message-avatar", role === "user" ? "我" : "✦"), body);
      $("messages").append(row);
    }
    if (nearBottom || state.sending) requestAnimationFrame(() => { $("conversation").scrollTop = $("conversation").scrollHeight; });
  }
  function renderAssets() {
    const assets = state.current?.assets || [];
    const signature = JSON.stringify(assets);
    if (signature === state.assetsSignature) return;
    state.assetsSignature = signature;
    $("asset-list").replaceChildren();
    $("asset-count").textContent = assets.length;
    for (const asset of assets) {
      const url = safeUrl(asset.url);
      const chip = element(url ? "a" : "div", "asset-chip");
      if (url) { chip.href = url; chip.target = "_blank"; chip.rel = "noopener noreferrer"; }
      if (url && (asset.kind === "image" || /\.(png|jpg|jpeg|webp|gif)(\?|$)/i.test(url))) {
        const image = element("img", "asset-thumb"); image.src = url; image.alt = asset.name || "创作素材"; image.loading = "lazy"; chip.append(image);
      } else chip.append(element("span", "asset-icon", asset.role === "driver" ? "▷" : asset.role === "voice" ? "♪" : "▧"));
      const label = element("span");
      label.append(element("span", "asset-name", asset.name || "未命名素材"), element("span", "asset-role", ROLE_LABELS[asset.role] || asset.role || "创作素材"));
      chip.title = `${asset.name || "素材"} · ${ROLE_LABELS[asset.role] || asset.role || ""}`;
      chip.append(label);
      $("asset-list").append(chip);
    }
  }
  function noticeText(value) {
    if (typeof value === "string") return value;
    return value?.message || value?.label || value?.description || value?.reason || (value?.role ? `需要补充：${ROLE_LABELS[value.role] || value.role}` : stringify(value));
  }
  function renderPlan() {
    const plan = state.current?.plan;
    const signature = JSON.stringify(plan || null);
    if (signature === state.planSignature) return;
    state.planSignature = signature;
    const panel = $("plan-panel"); panel.replaceChildren();
    if (!plan) {
      const empty = element("div", "empty-plan"); empty.append(element("div", "", "◇"), element("h3", "", "先聊聊你的想法"), element("p", "", "创作流程、所需素材和执行步骤会整理在这里。")); panel.append(empty); return;
    }
    const card = element("div", "plan-card");
    const top = element("div", "plan-topline"); top.append(element("span", "tool-label", toolLabel(plan.tool)), element("span", `status-pill ${plan.ready ? "good" : "warning"}`, plan.ready ? "可执行" : "待补充"));
    card.append(top, element("h3", "plan-title", plan.title || "视频创作计划"), element("p", "plan-summary", plan.summary || ""));
    const options = Object.entries(plan.options || {}).filter(([key, value]) => value !== null && value !== "" && !/key|token|secret|password|api_base|workflow_id|frame_mode|seed/i.test(key));
    if (options.length) {
      const rows = element("div", "plan-options");
      for (const [key, value] of options) { const row = element("div", "option-row"); row.append(element("span", "option-name", OPTION_LABELS[key] || key), element("span", "option-value", OPTION_VALUES[key]?.[value] || stringify(value))); rows.append(row); }
      card.append(rows);
    }
    if (plan.steps?.length) {
      const steps = element("ol", "plan-steps");
      plan.steps.forEach((step, index) => { const row = element("li"); row.append(element("span", "step-number", index + 1), element("span", "", typeof step === "string" ? step : step.title || step.label || step.description || stringify(step))); steps.append(row); });
      card.append(steps);
    }
    const notices = [...(plan.missing || []).map((item) => typeof item === "string" && ROLE_LABELS[item] ? `请上传${ROLE_LABELS[item]}` : noticeText(item)), ...(plan.warnings || []).map(noticeText)];
    if (notices.length) { const list = element("div", "plan-notices"); notices.forEach((text) => list.append(element("p", "plan-notice", text))); card.append(list); }
    panel.append(card);
  }
  function artifactKind(artifact, url) {
    if (["image", "video", "audio"].includes(artifact.kind)) return artifact.kind;
    if (/\.(png|jpe?g|webp|gif)(\?|$)/i.test(url)) return "image";
    if (/\.(mp4|webm|mov)(\?|$)/i.test(url)) return "video";
    if (/\.(mp3|wav|m4a|ogg|flac)(\?|$)/i.test(url)) return "audio";
    return "file";
  }
  function renderArtifacts(artifacts) {
    const group = element("div", "artifacts");
    const seen = new Set();
    for (const item of artifacts) {
      const artifact = typeof item === "string" ? { url: item, label: "生成素材" } : item;
      const url = safeUrl(artifact?.url);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      const block = element("div", "artifact");
      const label = artifact.label || artifact.name || "生成素材";
      const kind = artifactKind(artifact, url);
      block.append(element("div", "artifact-label", label));
      if (kind !== "file") {
        const media = element(kind === "image" ? "img" : kind); media.src = url;
        if (kind === "image") { media.alt = label; media.loading = "lazy"; }
        else { media.controls = true; media.preload = "metadata"; if (kind === "video") media.playsInline = true; }
        block.append(media);
      }
      const link = element("a", "artifact-link", `${kind === "file" ? "查看" : "打开"}${label} ↗`); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; block.append(link);
      group.append(block);
    }
    return group;
  }
  function actionButton(label, callback, secondary = true) {
    const button = element("button", `button ${secondary ? "button-secondary" : "execute-button"}`, label); button.type = "button"; button.addEventListener("click", callback); return button;
  }
  async function jobAction(job, action, body = {}) {
    if (state.actions.has(job.id)) return;
    state.actions.add(job.id); renderJobs(true);
    try {
      const updated = unwrap(await post(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, body), "job");
      if (state.current?.id === job.session_id || (state.current?.jobs || []).some((item) => item.id === job.id)) {
        const jobs = state.current.jobs || []; state.current.jobs = jobs.map((item) => item.id === job.id ? updated : item);
      }
      toast(action === "pause" ? "已请求暂停，任务状态将持续更新。" : "已请求继续，正在更新任务进度。");
    } catch (error) { toast(error.message, true); }
    finally { state.actions.delete(job.id); renderJobs(true); updateControls(); }
  }
  function renderJob(job) {
    const card = element("article", "job-card"); card.dataset.jobId = job.id;
    const heading = element("div", "job-heading"); const title = element("div");
    title.append(element("div", "job-title", toolLabel(job.tool)), element("div", "job-id", job.id));
    const tone = job.state === "completed" ? "good" : ["failed", "submission_unknown"].includes(job.state) ? "error" : ["awaiting_review", "paused", "interrupted"].includes(job.state) ? "warning" : "";
    heading.append(title, element("span", `status-pill ${tone}`, STATE_LABELS[job.state] || job.state || "等待更新")); card.append(heading);
    let progress = job.progress;
    if (progress && typeof progress === "object") progress = progress.message || progress.text || progress.label || "任务正在执行";
    card.append(element("p", "job-progress", typeof progress === "number" ? `已完成 ${Math.round(progress <= 1 ? progress * 100 : progress)}%` : progress || "等待任务进度…"));
    if (ACTIVE_STATES.has(job.state)) {
      const track = element("div", "progress-track"); const fill = element("div", "progress-fill");
      const percentage = typeof job.progress === "number" ? job.progress : job.progress?.percent;
      if (typeof percentage === "number") fill.style.width = `${Math.max(0, Math.min(100, percentage <= 1 ? percentage * 100 : percentage))}%`; else fill.classList.add("indeterminate");
      track.append(fill); card.append(track);
    }
    if (job.error) card.append(element("p", "job-error", readableError(job.error)));
    const previews = listValue(job.details?.preview_artifacts || job.details?.frame_artifacts, ["artifacts", "items"]);
    const artifacts = [...previews, ...listValue(job.artifacts)];
    if (artifacts.length) card.append(renderArtifacts(artifacts));
    if (job.quality_status) {
      const labels = { passed: "质检通过", approved: "质检通过", failed: "质检未通过，请复核结果", needs_review: "结果需要人工复核", review_required: "结果需要人工复核", not_checked: "尚未质检", not_run: "尚未质检", skipped: "本次未执行质检", pending: "等待质检", warning: "质检发现问题，请复核结果" };
      card.append(element("p", `quality-label${["failed", "needs_review", "review_required", "warning"].includes(job.quality_status) ? " needs-review" : ""}`, `质量状态：${labels[job.quality_status] || stringify(job.quality_status)}`));
    }
    const actions = element("div", "job-actions");
    if (job.state === "awaiting_review") {
      actions.append(element("p", "review-note", "请检查上方参考帧的人物、商品和构图。确认后将继续调用付费视频生成接口。"));
      actions.append(actionButton("确认参考帧并继续生成", () => jobAction(job, "resume", { frames_approved: true }), false));
      actions.append(actionButton("暂时暂停", () => jobAction(job, "pause")));
    } else if (job.state === "submission_unknown") {
      const talking = job.tool === "talking";
      actions.append(element("p", "review-note", talking ? "某个付费步骤可能已提交成功。请核对 RunningHub 中的已有任务，并补录该步骤的云端 taskId，避免重复付费。" : "付费任务可能已提交成功。请打开原创作页面，查找对应的本地任务编号后绑定；这里需要原项目的任务 ID，不是 RunningHub 云端 taskId。"));
      const unknownSteps = talking ? Object.entries(job.details?.steps || {}).filter(([, step]) => step.intent && !step.task_id) : [["", {}]];
      if (!unknownSteps.length) actions.append(element("p", "review-note", "请核对原任务后等待状态更新；当前没有可补录的步骤。"));
      for (const [stepName] of unknownSteps) {
        const bindingKey = `${job.id}:${stepName}`;
        if (stepName) actions.append(element("label", "artifact-label", stepName === "composite" ? "人物持商品参考帧" : stepName === "voice" ? "音色替换" : stepName.startsWith("segment:") ? `口播片段 ${stepName.split(":")[1]}` : stepName));
        const input = element("input", "binding-input"); input.type = "text"; input.placeholder = talking ? "填写此步骤的云端 taskId" : "填写原项目的本地任务 ID"; input.setAttribute("aria-label", talking ? `${stepName} 的云端 taskId` : "原项目本地任务 ID"); input.autocomplete = "off"; input.dataset.bindingFor = job.id; input.dataset.bindingKey = bindingKey; input.value = state.bindings.get(bindingKey) || "";
        const bind = actionButton("绑定已有任务并继续", () => jobAction(job, "resume", talking ? { step_name: stepName, task_id: input.value.trim() } : { remote_job_id: input.value.trim() }), false); bind.disabled = !input.value.trim();
        input.addEventListener("input", () => { state.bindings.set(bindingKey, input.value); bind.disabled = !input.value.trim() || state.actions.has(job.id); }); actions.append(input, bind);
      }
    } else if (["paused", "interrupted", "failed"].includes(job.state)) {
      actions.append(element("p", "review-note", "继续会先恢复原任务；请根据错误提示补充素材或检查原生成服务。"));
      actions.append(actionButton("恢复原任务", () => jobAction(job, "resume"), false));
    } else if (ACTIVE_STATES.has(job.state)) {
      actions.append(actionButton("暂停后续步骤", () => jobAction(job, "pause")));
    }
    const backendUrl = safeUrl(job.backend_url);
    if (backendUrl) { const link = element("a", "artifact-link", "打开原创作页面 ↗"); link.href = backendUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    if (state.actions.has(job.id)) for (const button of actions.querySelectorAll("button")) button.disabled = true;
    if (actions.childNodes.length) card.append(actions);
    return card;
  }
  function renderJobs(force = false) {
    const jobs = state.current?.jobs || [];
    const signature = JSON.stringify(jobs);
    if (signature === state.jobsSignature && !force) return;
    state.jobsSignature = signature;
    const root = $("jobs-list");
    if (!jobs.length) { root.replaceChildren(element("div", "empty-jobs", "生成任务将在这里显示。\n切换会话不会中断云端任务。")); state.jobElements.clear(); return; }
    root.querySelector(".empty-jobs")?.remove();
    const ids = new Set(jobs.map((job) => job.id));
    for (const [id, entry] of state.jobElements) if (!ids.has(id)) { entry.node.remove(); state.jobElements.delete(id); }
    for (const job of jobs) {
      const jobSignature = JSON.stringify(job) + state.actions.has(job.id);
      const existing = state.jobElements.get(job.id);
      if (!force && existing?.signature === jobSignature) continue;
      const focused = document.activeElement?.dataset?.bindingFor === job.id;
      const selection = focused ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
      const bindingKey = focused ? document.activeElement.dataset.bindingKey : null;
      const node = renderJob(job);
      if (existing) existing.node.replaceWith(node); else root.append(node);
      state.jobElements.set(job.id, { signature: jobSignature, node });
      if (focused) { const input = [...node.querySelectorAll("input")].find((field) => field.dataset.bindingKey === bindingKey); input?.focus({ preventScroll: true }); if (input && selection) input.setSelectionRange(...selection); }
    }
  }
  function applySession(session) {
    if (!session || !session.id) return;
    state.current = { ...session, assets: session.assets || [], jobs: session.jobs || [] };
    $("session-title").textContent = sessionTitle(session) === "未命名创作" ? "开始一次创作" : sessionTitle(session);
    renderMessages(); renderAssets(); renderPlan(); renderJobs(); renderSessions(); updateControls();
  }
  async function switchSession(id) {
    const sequence = ++state.switching;
    const session = unwrap(await api(`/api/sessions/${encodeURIComponent(id)}`), "session");
    if (sequence !== state.switching) return;
    state.planSignature = ""; state.messagesSignature = ""; state.assetsSignature = ""; state.jobsSignature = "";
    state.jobElements.clear(); $("jobs-list").replaceChildren();
    applySession(session); saveSession(session.id); $("message-input").value = ""; updateControls();
    requestAnimationFrame(() => { $("conversation").scrollTop = $("conversation").scrollHeight; });
  }
  async function createSession() {
    const created = unwrap(await post("/api/sessions"), "session");
    await refreshSessions(); await switchSession(created.id); $("message-input").focus();
  }
  async function sendMessage(event) {
    event?.preventDefault();
    const text = $("message-input").value.trim();
    if (!text || !state.current || state.sending || state.uploading) return;
    const sessionId = state.current.id;
    state.sending = true; updateControls(); renderTools();
    $("composer-status").textContent = "正在理解需求并制定计划，不会提交视频生成任务。";
    try {
      const result = await post(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, { text, tool: state.selectedTool });
      if (state.current?.id === sessionId) {
        const updated = result.session || state.current;
        const session = { ...state.current, ...updated, assets: updated.assets || state.current.assets, jobs: updated.jobs || state.current.jobs, plan: result.plan || updated.plan || state.current.plan };
        if (!result.session?.messages && result.reply) session.messages = [...(state.current.messages || []), { role: "user", text }, { role: "assistant", text: result.reply }];
        applySession(session); $("message-input").value = "";
        requestAnimationFrame(() => { $("conversation").scrollTop = $("conversation").scrollHeight; });
      }
      await refreshSessions();
    } catch (error) { toast(error.message, true); }
    finally { state.sending = false; $("composer-status").textContent = "发送需求只制定计划；确认生成后才会调用付费生成接口。"; updateControls(); renderTools(); }
  }
  async function uploadFiles(files) {
    if (!files.length || !state.current || state.uploading || state.sending) return;
    const sessionId = state.current.id; const role = $("asset-role").value;
    state.uploading = true; updateControls(); let uploaded = 0;
    try {
      for (const file of files) {
        const body = new FormData(); body.append("file", file); body.append("role", role); body.append("session_id", sessionId);
        const asset = unwrap(await api("/api/assets", { method: "POST", body }), "asset");
        uploaded += 1;
        if (state.current?.id === sessionId) { state.current.assets = [...(state.current.assets || []), asset]; renderAssets(); }
      }
      toast(`已上传 ${uploaded} 份${ROLE_LABELS[role]}。发送需求后会更新创作计划。`);
      if (state.current?.id === sessionId) applySession(unwrap(await api(`/api/sessions/${encodeURIComponent(sessionId)}`), "session"));
    } catch (error) { toast(`${uploaded ? `已上传 ${uploaded} 份素材。` : ""}${error.message}`, true); }
    finally { state.uploading = false; $("file-input").value = ""; updateControls(); }
  }
  async function executePlan() {
    const plan = state.current?.plan;
    if (!plan?.ready || $("execute-button").disabled) return;
    const sessionId = state.current.id;
    if (!state.requestIds.has(plan.id)) state.requestIds.set(plan.id, uuid());
    state.executing = true; updateControls();
    try {
      const job = unwrap(await post(`/api/plans/${encodeURIComponent(plan.id)}/execute`, { request_id: state.requestIds.get(plan.id) }), "job");
      state.submittedPlans.add(plan.id);
      if (state.current?.id === sessionId) { state.current.jobs = [job, ...(state.current.jobs || []).filter((item) => item.id !== job.id)]; renderJobs(); }
      toast("已提交生成任务，可在右侧查看进度。");
    } catch (error) { toast(`${error.message} 再次点击将沿用本次请求编号。`, true); }
    finally { state.executing = false; updateControls(); }
  }
  async function poll() {
    if (!state.current || state.pollBusy || state.sending || state.uploading || document.hidden) return;
    const id = state.current.id; const sequence = state.switching; state.pollBusy = true;
    try {
      const session = unwrap(await api(`/api/sessions/${encodeURIComponent(id)}`), "session");
      if (state.current?.id === id && sequence === state.switching && !state.sending && !state.uploading) applySession(session);
      connection(true); $("polling-label").textContent = "每 3 秒更新";
    } catch { connection(false); $("polling-label").textContent = "连接中断，正在重试"; }
    finally { state.pollBusy = false; }
  }
  function bindEvents() {
    $("message-form").addEventListener("submit", sendMessage);
    $("message-input").addEventListener("input", updateControls);
    $("message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); } });
    $("new-session").addEventListener("click", () => createSession().catch((error) => toast(error.message, true)));
    $("execute-button").addEventListener("click", executePlan);
    $("upload-button").addEventListener("click", () => $("file-input").click());
    $("file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]));
    $("asset-role").addEventListener("change", () => { $("file-input").accept = $("asset-role").value === "driver" ? "video/*" : $("asset-role").value === "voice" ? "audio/*" : "image/*"; });
    for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => { $("message-input").value = button.dataset.prompt; $("message-input").focus(); updateControls(); });
    const zone = $("drop-zone"); let dragDepth = 0;
    zone.addEventListener("dragenter", (event) => { event.preventDefault(); dragDepth += 1; zone.classList.add("dragging"); });
    zone.addEventListener("dragover", (event) => event.preventDefault());
    zone.addEventListener("dragleave", (event) => { event.preventDefault(); dragDepth -= 1; if (dragDepth <= 0) zone.classList.remove("dragging"); });
    zone.addEventListener("drop", (event) => { event.preventDefault(); dragDepth = 0; zone.classList.remove("dragging"); uploadFiles([...event.dataTransfer.files]); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }
  async function init() {
    bindEvents(); updateControls();
    try {
      state.settings = await api("/api/settings"); connection(true);
      const mode = state.settings.planner_mode;
      $("planner-mode").textContent = mode === "deepseek" || mode === "llm" ? "DeepSeek 规划" : mode === "rules" || mode === "rule_based" ? "规则规划" : mode ? `规划 · ${mode}` : "创作助手";
      const unavailable = (state.settings.tools || []).filter((tool) => tool.available === false);
      if (unavailable.length) { $("global-notice").textContent = unavailable.map((tool) => `${tool.label || toolLabel(tool.id)}：${tool.reason || "服务暂不可用"}`).join("；"); $("global-notice").hidden = false; }
      const maxUpload = state.settings.limits?.upload_mb || state.settings.limits?.max_upload_mb || state.settings.limits?.max_file_mb;
      if (maxUpload) $("upload-hint").textContent = `支持拖入 · 图片 12 MB / 音视频 ${maxUpload} MB`;
      renderTools(); await refreshSessions();
      const saved = storedSession();
      if (saved && state.sessions.some((session) => session.id === saved)) await switchSession(saved);
      else if (state.sessions.length) await switchSession(state.sessions[0].id);
      else await createSession();
    } catch (error) { connection(false); $("planner-mode").textContent = "服务未连接"; $("global-notice").textContent = error.message; $("global-notice").hidden = false; toast(error.message, true); }
    finally { updateControls(); }
    setInterval(poll, 3000);
  }
  init();
})();
