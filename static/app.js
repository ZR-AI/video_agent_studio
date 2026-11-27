"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ROLE_LABELS = { product: "商品图", scene: "布景参考图", reference: "人物参考图", costume: "服装参考图", pose: "姿势参考图", hair: "发型参考图", driver: "动作驱动视频", last_frame: "尾帧图", voice: "音色参考音频" };
  const TOOL_LABELS = { auto: "自动选择", product: "商品广告", talking: "人物口播", motion: "动作迁移", qwen_image: "参考图造型", portrait_image: "文字生人像", background: "直播布景" };
  const STATE_LABELS = { queued: "排队中", running: "生成中", awaiting_review: "待确认参考帧", submission_unknown: "提交结果待确认", interrupted: "执行已中断", completed: "已完成", failed: "执行失败", paused: "已暂停" };
  const ACTIVE_STATES = new Set(["queued", "running"]);
  const PROMPT_EXAMPLE = {
    product: "例如：用商品图做一条 15 秒竖屏广告…",
    talking: "例如：用人物参考图做口播，文案：…",
    motion: "例如：让参考图里的角色复现驱动视频的动作…",
    qwen_image: "例如：把参考人物换成通勤造型，生成两张…",
    portrait_image: "例如：生成两张白底、3:4 的坐姿人像…",
    background: "例如：用两张商品图和布景参考图生成直播间…"
  };
  const PRIMARY_OPTIONS = new Set(["aspect", "ratio", "seconds", "count", "model", "backend", "background", "region", "style", "pipeline", "scene_mode", "product_count"]);
  const OPTION_LABELS = { duration: "时长", duration_seconds: "时长（秒）", total_seconds: "时长（秒）", seconds: "时长（秒）", aspect: "画幅", aspect_ratio: "画幅", resolution: "分辨率", style: "风格", prompt: "生成提示词", positive_prompt: "画面要求", motion_prompt: "动作要求", script: "口播文案", text: "文案", quality_audit_enabled: "自动质检", quality_enabled: "自动质检", quality_audit_max_rounds: "最多修复轮数", quality_max_rounds: "最多修复轮数", segment_seconds: "分段时长（秒）", generation_mode: "生成模式", seam_transition_mode: "接缝方式", voice: "音色", voice_enabled: "更换音色", language: "语言", title: "标题", workflow: "工作流", workflow_preset: "生成规格", max_parallel_segments: "片段并发数", pipeline: "商品呈现", sound: "声音", creative_mode: "创作类型", notes: "补充要求", backend: "生成引擎", video_mode: "视频模式" };
  const OPTION_VALUES = { pipeline: { direct: "直接生成", multiview: "多视角参考" }, sound: { music: "背景音乐", ambient: "环境音", silent: "静音" }, creative_mode: { auto: "自动选择", ad: "广告", lifestyle: "生活场景", studio: "棚拍" }, generation_mode: { independent: "独立片段", continuous: "连续动作" }, seam_transition_mode: { rife: "帧插值", minimax_h3_all: "H3 过渡", minimax_h3: "H3 过渡", flf2v_all: "首尾帧过渡" }, backend: { wan: "WanAnimate2", h3_action: "MiniMax H3" }, video_mode: { legacy: "标准", two_pass: "两阶段", veda_two_pass: "两阶段（VEDA）", latent_two_pass: "两阶段（Latent）" } };
  Object.assign(OPTION_LABELS, { model: "模型", instruction: "造型要求", ratio: "画幅", count: "张数", parallel: "并行生成", randomize: "图库随机参考", background: "背景", region: "地域风格", custom_region: "自定义地域", custom_style: "自定义穿搭", custom_color: "背景颜色" });
  Object.assign(OPTION_LABELS, { scene_mode: "布景方式", product_count: "商品图张数", host_side: "主播留位", lang: "标语语言", name: "商品名", slogan: "标语", hold_product: "手持商品" });
  Object.assign(OPTION_VALUES, {
    model: { qwen: "Qwen Image 2.1", "h3-image": "MiniMax H3 Image" },
    pipeline: { ...OPTION_VALUES.pipeline, workflow: "双阶段人像工作流", myhuman: "MYHuman 应用" },
    style: { elegant: "端庄优雅", sexy: "性感时尚", casual: "清新日常", office: "知性通勤", fashion: "高级时装", custom: "自定义", premium: "高级感", bright: "明亮", warm: "暖调", tech: "科技", guochao: "国潮", minimal: "极简" },
    scene_mode: { reference: "素材布景", booth: "AI 电商展台", scene: "AI 实景房间" },
    host_side: { center: "居中", left: "偏左", right: "偏右" },
    aspect: { portrait: "竖屏 9:16", landscape: "横屏 16:9" },
    lang: { zh: "简体中文", "zh-TW": "繁体中文", en: "英语", fil: "菲律宾语", ja: "日语", ko: "韩语", ru: "俄语", de: "德语", it: "意大利语", ar: "阿拉伯语", th: "泰语", vi: "越南语", id: "印尼语", ms: "马来语", fr: "法语", pt: "葡萄牙语", es: "西班牙语" },
    background: { green: "绿幕", white: "白背景", blue: "蓝幕", gray: "浅灰", black: "纯黑", auto: "自动搭配", custom: "自定义纯色" },
    region: { east_asia: "东亚", southeast_asia: "东南亚", western: "欧美", japan_korea: "日韩", south_asia: "南亚", middle_east: "中东", latin_america: "拉美", africa: "非洲", auto: "自动选择", custom: "自定义" },
    randomize: { portrait: "人像", costume: "服装", pose: "姿势", hair: "发型" }
  });
  const state = { settings: null, sessions: [], current: null, selectedTool: "auto", sending: false, uploading: false, executing: false, switching: 0, assetRevision: 0, pollBusy: false, planSignature: "", messagesSignature: "", assetsSignature: "", jobsSignature: "", jobElements: new Map(), bindings: new Map(), actions: new Set(), requestIds: new Map(), submittedPlans: new Set(), noticeTimer: null };

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
  async function api(path, options = {}, responseType = "json") {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") {
      if (!state.settings?.app_token) throw new Error("服务尚未连接，请刷新页面后重试。");
      headers.set("X-App-Token", state.settings.app_token);
      if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    }
    let response;
    try { response = await fetch(path, { ...options, headers }); }
    catch { throw new Error("无法连接创作服务。请检查服务是否运行；已提交的云端任务可能仍在执行。"); }
    if (response.ok && responseType === "response") return response;
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
  function showConnectionIssue(message = "") {
    $("global-notice").textContent = message;
    $("global-notice").hidden = !message;
  }
  function saveSession(id) { try { localStorage.setItem("video-agent-current-session", id); } catch { /* Storage may be unavailable. */ } }
  function storedSession() { try { return localStorage.getItem("video-agent-current-session"); } catch { return null; } }
  function uuid() { return globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  function toolLabel(id) { return TOOL_LABELS[id] || { clarify: "补充需求", composition: "直播间合成" }[id] || state.settings?.tools?.find((tool) => tool.id === id)?.label || id || "视频创作"; }
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
    if (!state.sessions.length) return;
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
    $("message-input").placeholder = PROMPT_EXAMPLE[state.selectedTool] || "描述你想生成的图片或视频…";
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
  function currentReference(role = "reference") {
    return state.current?.assets?.find((asset) => asset.role === role);
  }
  function updateControls() {
    $("send-button").disabled = !state.settings || !state.current || state.sending || state.uploading || !$("message-input").value.trim();
    $("upload-button").disabled = !state.settings || !state.current || state.uploading || state.sending;
    $("asset-role").disabled = state.uploading;
    $("new-session").disabled = !state.settings || state.sending || state.uploading;
    $("open-composition").disabled = !state.current || !state.settings?.composition_available;
    $("thinking").hidden = !state.sending;
    $("upload-button").textContent = state.uploading ? "正在添加…" : "＋ 添加素材";
    const plan = state.current?.plan;
    const tool = state.settings?.tools?.find((tool) => tool.id === plan?.tool);
    const toolUnavailable = tool?.available === false;
    const jobRunning = (state.current?.jobs || []).some((job) => ACTIVE_STATES.has(job.state) || job.state === "awaiting_review" || job.state === "submission_unknown");
    const alreadySubmitted = plan && (state.submittedPlans.has(plan.id) || state.current.jobs.some((job) => job.plan_id === plan.id));
    const changedRole = ["reference", "scene"].find((role) => {
      const reference = currentReference(role);
      return reference && plan?.asset_ids?.[role] && plan.asset_ids[role] !== reference.id;
    });
    $("execute-panel").hidden = !plan?.ready || alreadySubmitted;
    $("execute-button").disabled = !plan?.ready || toolUnavailable || state.executing || state.sending || state.uploading || jobRunning || alreadySubmitted || !!changedRole;
    $("execute-button").textContent = state.executing ? "正在提交…" : "确认并生成";
    const blocker = changedRole ? `${ROLE_LABELS[changedRole]}已更新，请发送新需求。` : toolUnavailable ? tool.reason || "生成服务暂不可用。" : jobRunning ? "请先处理当前任务。" : "";
    $("execute-hint").textContent = blocker;
    $("execute-hint").hidden = !blocker;
    $("payment-note").hidden = !!blocker || plan?.tool === "qwen_image";
    for (const button of document.querySelectorAll("[data-reference-url]")) {
      const role = button.dataset.reuseRole;
      const selected = currentReference(role)?.source_reference_url === button.dataset.referenceUrl;
      button.textContent = `${selected ? "已用作" : "用作"}${ROLE_LABELS[role]}`;
      button.disabled = selected || state.uploading || state.sending || state.executing;
    }
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
      body.append(element("span", "sr-only", role === "user" ? "你" : role === "system" ? "创作提示" : "创作助手"));
      const text = String(message.text ?? message.content ?? "");
      const content = role === "assistant" ? text.replace(/(?:\n\n|^)计划已准备，请检查右侧设置后点击“确认并生成”。$/, "") : text;
      if (!content) continue;
      body.append(element("div", "message-content", content));
      row.append(body);
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
    for (const asset of assets) {
      const url = safeUrl(asset.url);
      const chip = element(url ? "a" : "div", "asset-chip");
      if (url) { chip.href = url; chip.target = "_blank"; chip.rel = "noopener noreferrer"; }
      if (url && (asset.kind === "image" || /\.(png|jpg|jpeg|webp|gif)(\?|$)/i.test(url))) {
        const image = element("img", "asset-thumb"); image.src = url; image.alt = asset.name || "创作素材"; image.loading = "lazy"; chip.append(image);
      } else chip.append(element("span", "asset-icon", asset.role === "driver" ? "▷" : asset.role === "voice" ? "♪" : "▧"));
      const label = element("span");
      const role = asset.role;
      const isCurrent = ["reference", "scene"].includes(role) && asset.id === currentReference(role)?.id;
      const roleLabel = isCurrent ? `当前${ROLE_LABELS[role]}` : ROLE_LABELS[role] || role || "创作素材";
      label.append(element("span", "asset-name", asset.name || "未命名素材"), element("span", "asset-role", roleLabel));
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
      panel.append(element("p", "empty-panel", "暂无计划"));
      return;
    }
    const card = element("div", "plan-card");
    card.append(element("h3", "plan-title", toolLabel(plan.tool)));
    const options = Object.entries(plan.options || {}).filter(([key, value]) => value !== null && value !== "" &&
      (!Array.isArray(value) || value.length) && (key !== "custom_color" || plan.options.background === "custom") &&
      !/key|token|secret|password|api_base|workflow_id|frame_mode|seed/i.test(key));
    const rows = element("dl", "plan-options");
    const additional = element("dl", "plan-options");
    for (const [key, value] of options) {
      const row = element("div", "option-row");
      const display = Array.isArray(value) ? value.map((item) => OPTION_VALUES[key]?.[item] || item).join("、") : OPTION_VALUES[key]?.[value] || stringify(value);
      const label = key === "pipeline" && plan.tool === "portrait_image" ? "生图工作流" : OPTION_LABELS[key] || key;
      row.append(element("dt", "option-name", label), element("dd", "option-value", display));
      (PRIMARY_OPTIONS.has(key) ? rows : additional).append(row);
    }
    if (rows.childElementCount) card.append(rows);
    const details = element("details", "plan-details");
    details.append(element("summary", "", "参数与步骤"));
    if (additional.childElementCount) details.append(additional);
    if (plan.steps?.length) {
      const steps = element("ol", "plan-steps");
      for (const step of plan.steps) steps.append(element("li", "", typeof step === "string" ? step : step.title || step.label || step.description || stringify(step)));
      details.append(steps);
    }
    const notices = [...(plan.missing || []).map((item) => typeof item === "string" && ROLE_LABELS[item] ? `请上传${ROLE_LABELS[item]}` : noticeText(item)), ...(plan.warnings || []).map(noticeText)];
    if (notices.length) { const list = element("div", "plan-notices"); notices.forEach((text) => list.append(element("p", "plan-notice", text))); card.append(list); }
    if (details.childElementCount > 1) card.append(details);
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
      const open = element("a", kind === "image" ? "artifact-preview" : "artifact-link");
      open.href = url; open.target = "_blank"; open.rel = "noopener noreferrer";
      if (kind !== "file") {
        const media = element(kind === "image" ? "img" : kind); media.src = url;
        if (kind === "image") {
          media.alt = label; media.loading = "lazy";
          open.setAttribute("aria-label", `打开${label}`);
          open.append(media); block.append(open);
        } else {
          media.controls = true; media.preload = "metadata";
          if (kind === "video") media.playsInline = true;
          block.append(media);
        }
      }
      const controls = element("div", "artifact-controls");
      if (kind !== "image") {
        open.textContent = "打开文件";
        controls.append(open);
      }
      const downloadUrl = new URL(url);
      if (["image", "video"].includes(kind) && downloadUrl.origin === location.origin && downloadUrl.pathname.startsWith("/api/jobs/")) {
        downloadUrl.searchParams.set("download", "true");
        const download = element("a", "artifact-link", kind === "image" ? "下载图片" : "下载视频");
        download.href = downloadUrl.href; controls.append(download);
      }
      const referenceUrl = artifact.reference_url;
      if (kind === "image" && typeof referenceUrl === "string" && /^\/api\/jobs\/[^/]+\/artifacts\/[a-f0-9]{64}\/reference$/.test(referenceUrl)) {
        const role = artifact.reuse_role === "scene" ? "scene" : "reference";
        const button = actionButton(`用作${ROLE_LABELS[role]}`, () => useAsReference(referenceUrl));
        button.classList.add("artifact-reference");
        button.dataset.referenceUrl = referenceUrl;
        button.dataset.reuseRole = role;
        controls.append(button);
      }
      if (controls.childElementCount) block.append(controls);
      group.append(block);
    }
    return group;
  }
  function actionButton(label, callback, secondary = true) {
    const button = element("button", `button ${secondary ? "button-secondary" : "execute-button"}`, label); button.type = "button"; button.addEventListener("click", callback); return button;
  }
  async function useAsReference(url) {
    if (!state.current || state.uploading || state.sending || state.executing) return;
    const sessionId = state.current.id;
    state.uploading = true; updateControls();
    try {
      const asset = await post(url);
      state.assetRevision += 1;
      if (state.current?.id === sessionId) {
        state.current.assets = [asset, ...state.current.assets.filter((item) => item.id !== asset.id)];
        renderAssets();
        toast(`已设为${ROLE_LABELS[asset.role]}`);
      }
    } catch (error) { toast(error.message, true); }
    finally { state.uploading = false; updateControls(); }
  }
  async function jobAction(job, action, body = {}) {
    if (state.actions.has(job.id)) return;
    state.actions.add(job.id); renderJobs(true);
    try {
      const updated = unwrap(await post(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, body), "job");
      if (state.current?.id === job.session_id || (state.current?.jobs || []).some((item) => item.id === job.id)) {
        const jobs = state.current.jobs || []; state.current.jobs = jobs.map((item) => item.id === job.id ? updated : item);
      }
      toast(action === "pause" ? "已请求暂停" : "已请求继续");
    } catch (error) { toast(error.message, true); }
    finally { state.actions.delete(job.id); renderJobs(true); updateControls(); }
  }
  function renderJob(job) {
    const card = element("article", "job-card"); card.dataset.jobId = job.id;
    const heading = element("div", "job-heading");
    const title = element("h3", "job-title", toolLabel(job.tool));
    title.title = `任务编号 ${job.id}`;
    const tone = job.state === "completed" ? "good" : ["failed", "submission_unknown"].includes(job.state) ? "error" : ["awaiting_review", "paused", "interrupted"].includes(job.state) ? "warning" : "";
    heading.append(title, element("span", `status-pill ${tone}`, STATE_LABELS[job.state] || job.state || "等待更新")); card.append(heading);
    let progress = job.progress;
    if (progress && typeof progress === "object") progress = progress.message || progress.text || progress.label || "任务正在执行";
    if (job.state !== "completed" && progress) {
      card.append(element("p", "job-progress", typeof progress === "number" ? `已完成 ${Math.round(progress <= 1 ? progress * 100 : progress)}%` : progress));
    }
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
    const qualityLabels = { passed: "质检通过", approved: "质检通过", failed: "质检未通过，请复核结果", needs_review: "结果需要人工复核", review_required: "结果需要人工复核", pending: "等待质检", warning: "质检发现问题，请复核结果" };
    const qualityNote = job.details?.quality_note || qualityLabels[job.quality_status];
    if (qualityNote) {
      card.append(element("p", `quality-label${["failed", "needs_review", "review_required", "warning"].includes(job.quality_status) ? " needs-review" : ""}`, qualityNote));
    }
    const actions = element("div", "job-actions");
    if (job.state === "awaiting_review") {
      actions.append(element("p", "review-note", "确认参考帧后将使用云端额度。"));
      actions.append(actionButton("确认参考帧并继续生成", () => jobAction(job, "resume", { frames_approved: true }), false));
      actions.append(actionButton("暂时暂停", () => jobAction(job, "pause")));
    } else if (job.state === "submission_unknown") {
      const talking = job.tool === "talking";
      const unknownNote = talking ? "请核对 RunningHub 中的已有任务，并补录该步骤的云端 taskId。" : "生成请求可能已接单。请在原创作页面核对对应的本地任务编号后绑定。";
      actions.append(element("p", "review-note", unknownNote));
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
      actions.append(actionButton("恢复原任务", () => jobAction(job, "resume"), false));
    } else if (ACTIVE_STATES.has(job.state)) {
      actions.append(actionButton("暂停后续步骤", () => jobAction(job, "pause")));
    }
    const backendUrl = safeUrl(job.backend_url);
    if (backendUrl) { const link = element("a", "artifact-link", "原工作台"); link.href = backendUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
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
    if (!jobs.length) { root.replaceChildren(element("p", "empty-panel", "暂无任务")); state.jobElements.clear(); return; }
    root.querySelector(".empty-panel")?.remove();
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
    const title = sessionTitle(session);
    $("session-title").textContent = ["未命名创作", "新视频"].includes(title) ? "新创作" : title;
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
    finally { state.sending = false; updateControls(); renderTools(); }
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
        state.assetRevision += 1;
        if (state.current?.id === sessionId) { state.current.assets = [asset, ...(state.current.assets || [])]; renderAssets(); }
      }
      toast(`已添加 ${uploaded} 份${ROLE_LABELS[role]}`);
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
      toast("已提交生成任务");
    } catch (error) { toast(error.message, true); }
    finally { state.executing = false; updateControls(); }
  }
  let composition = null;

  function compositionOptions() {
    const options = {};
    for (const field of ["scene", "person", "color"]) options[field] = $(`composition-${field}`).value;
    for (const field of ["similarity", "x", "y", "scale"]) options[field] = $(`composition-${field}`).valueAsNumber;
    return options;
  }
  function compositionPerson() {
    return composition?.people.find((person) => person.id === $("composition-person").value);
  }
  function updateComposition() {
    const person = compositionPerson();
    const options = compositionOptions();
    $("composition-time").hidden = person?.kind !== "video";
    $("render-composition").disabled = !options.scene || !person || composition?.busy;
    const seconds = person?.kind === "video" ? $("composition-seconds").valueAsNumber || 0 : 0;
    const signature = JSON.stringify({ ...options, seconds });
    $("export-composition").disabled = person?.kind !== "video" || !!composition?.busy || composition?.approved !== signature;
    $("composition-stage").classList.toggle("preview-outdated", !!composition?.image && composition.shown !== signature);
    return { options, seconds, signature };
  }
  async function openComposition() {
    if (!state.current || !state.settings?.composition_available) return;
    const dialog = $("composition-dialog");
    const draft = { url: `/api/sessions/${encodeURIComponent(state.current.id)}/composition`, people: [], busy: true };
    composition = draft;
    $("composition-form").reset();
    const scene = $("composition-scene"), person = $("composition-person");
    scene.replaceChildren(); person.replaceChildren();
    $("composition-placeholder").hidden = false;
    $("composition-status").textContent = "正在加载当前会话的素材…";
    updateComposition();
    dialog.showModal();
    try {
      const catalog = await api(draft.url);
      if (composition !== draft) return;
      draft.people = catalog.people;
      const choices = (items, label) => items.length ? items.map((item) => {
        const text = [item.label, item.date ? dateLabel(item.date) : ""].filter(Boolean).join(" · ");
        return new Option(text, item.id);
      }) : [new Option(label, "")];
      scene.append(...choices(catalog.scenes, "暂无已分层布景"));
      for (const [kind, label] of [["video", "口播或动作视频"], ["image", "人物图"]]) {
        const items = draft.people.filter((item) => item.kind === kind);
        if (!items.length) continue;
        const group = element("optgroup"); group.label = label;
        group.append(...choices(items)); person.append(group);
      }
      if (!draft.people.length) person.append(new Option("暂无人物素材", ""));
      $("composition-status").textContent = !scene.value ? "先生成一套含干净背景和透明前景的直播布景。" :
        !person.value ? "先生成或添加一张绿幕人物图，或生成人物口播、动作视频。" : "";
    } catch (error) {
      if (composition === draft) $("composition-status").textContent = error.message;
    } finally {
      if (composition === draft) { draft.busy = false; updateComposition(); }
    }
  }
  function closeComposition() {
    if (composition?.image) URL.revokeObjectURL(composition.image);
    composition = null;
    const image = $("composition-preview");
    image.hidden = true;
    image.removeAttribute("src");
  }
  async function renderComposition(event) {
    event.preventDefault();
    const draft = composition;
    if (!draft || $("render-composition").disabled || !$("composition-form").reportValidity()) return;
    const { options, seconds, signature } = updateComposition();
    draft.busy = true;
    draft.approved = null;
    $("composition-status").textContent = "正在渲染预览…";
    updateComposition();
    try {
      const response = await api(draft.url + "/preview", { method: "POST", body: JSON.stringify({ ...options, seconds }) }, "response");
      const image = await response.blob();
      if (composition !== draft) return;
      if (draft.image) URL.revokeObjectURL(draft.image);
      draft.image = URL.createObjectURL(image);
      draft.shown = signature;
      const target = $("composition-preview");
      target.src = draft.image; target.hidden = false;
      $("composition-placeholder").hidden = true;
      const usable = response.headers.get("X-Matte-Usable") === "true";
      if (usable) draft.approved = signature;
      $("composition-status").textContent = !usable ? "抠像未分离人物和幕布，请检查幕布颜色或调整抠像强度。" :
        compositionPerson()?.kind === "image" ? "可用此图预览构图。导出时请选择口播或动作视频。" : "";
    } catch (error) {
      if (composition === draft) $("composition-status").textContent = error.message;
    } finally {
      if (composition === draft) { draft.busy = false; updateComposition(); }
    }
  }
  async function exportComposition() {
    const draft = composition;
    if (!draft || $("export-composition").disabled) return;
    const { options } = updateComposition();
    const sessionId = state.current.id;
    draft.busy = true; updateComposition();
    $("composition-status").textContent = "正在提交导出…";
    try {
      const job = await post(draft.url + "/export", options);
      if (state.current?.id === sessionId) {
        state.current.jobs = [job, ...state.current.jobs.filter((item) => item.id !== job.id)];
        renderJobs(); updateControls();
      }
      if (composition === draft) $("composition-dialog").close();
      toast("已加入任务与结果");
    } catch (error) {
      if (composition === draft) $("composition-status").textContent = error.message;
    } finally {
      if (composition === draft) { draft.busy = false; updateComposition(); }
    }
  }
  async function poll() {
    if (!state.current || state.pollBusy || state.sending || state.uploading || document.hidden) return;
    const id = state.current.id; const sequence = state.switching; const assetRevision = state.assetRevision; state.pollBusy = true;
    try {
      const session = unwrap(await api(`/api/sessions/${encodeURIComponent(id)}`), "session");
      if (state.current?.id === id && sequence === state.switching && assetRevision === state.assetRevision && !state.sending && !state.uploading) applySession(session);
      showConnectionIssue();
    } catch { showConnectionIssue("连接中断，正在重试…"); }
    finally { state.pollBusy = false; }
  }
  function bindEvents() {
    $("open-composition").addEventListener("click", openComposition);
    $("close-composition").addEventListener("click", () => $("composition-dialog").close());
    $("composition-dialog").addEventListener("close", closeComposition);
    $("composition-form").addEventListener("input", () => {
      if (!composition?.busy && composition?.image) $("composition-status").textContent = "参数已更新，可重新预览。";
      updateComposition();
    });
    $("composition-form").addEventListener("submit", renderComposition);
    $("export-composition").addEventListener("click", exportComposition);
    $("message-form").addEventListener("submit", sendMessage);
    $("message-input").addEventListener("input", updateControls);
    $("message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); } });
    $("new-session").addEventListener("click", () => createSession().catch((error) => toast(error.message, true)));
    $("execute-button").addEventListener("click", executePlan);
    $("upload-button").addEventListener("click", () => $("file-input").click());
    $("file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]));
    $("asset-role").addEventListener("change", () => { $("file-input").accept = $("asset-role").value === "driver" ? "video/*" : $("asset-role").value === "voice" ? "audio/*" : "image/*"; });
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
      state.settings = await api("/api/settings"); showConnectionIssue();
      const maxUpload = state.settings.limits?.upload_mb || state.settings.limits?.max_upload_mb || state.settings.limits?.max_file_mb;
      if (maxUpload) $("upload-button").title = `选择或拖入素材。图片上限 12 MB，音视频上限 ${maxUpload} MB。`;
      renderTools(); await refreshSessions();
      const saved = storedSession();
      if (saved && state.sessions.some((session) => session.id === saved)) await switchSession(saved);
      else if (state.sessions.length) await switchSession(state.sessions[0].id);
      else await createSession();
    } catch (error) { showConnectionIssue(error.message); }
    finally { updateControls(); }
    setInterval(poll, 3000);
  }
  init();
})();
