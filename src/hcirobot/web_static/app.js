"use strict";

const $ = (id) => document.getElementById(id);
let ws, state, defaults, clientId = "", lease = "", sequence = 0;
let connected = false, busy = false, manualAllowed = false, claimedAt = 0;
let drive = null, lastDrive = false, previousButtons = [], headLevel = 0;
let tuningSession = -1, tuningDirty = false, lastLogVersion = -1, padToggleBusy = false;
let reconnectTimer, noticeTimer, lastHeartbeatAt = 0;
const CUSTOM_SVM = "__custom__";
const GAMEPAD_BUTTON_NAMES = {0:"A", 1:"B", 2:"X", 3:"Y", 4:"LB", 5:"RB",
  6:"BACK", 7:"START", 8:"LS", 9:"RS", 11:"LT", 12:"RT"};

function gamepadAxis(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${number.toFixed(2)}` : "+0.00";
}

function gamepadButton(index) {
  return GAMEPAD_BUTTON_NAMES[index] ?? `#${index}`;
}

function setTab(name) {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`pane-${button.dataset.tab}`).hidden = !selected;
  });
}
document.querySelectorAll("[data-tab]").forEach((button, index, buttons) => {
  button.onclick = () => setTab(button.dataset.tab);
  button.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
      (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
    setTab(buttons[next].dataset.tab);
    buttons[next].focus();
  };
});
document.querySelectorAll("button[data-mobile-view]").forEach((button) => {
  button.onclick = () => {
    clearDrive();
    document.body.dataset.mobileView = button.dataset.mobileView;
    document.querySelectorAll("button[data-mobile-view]").forEach((tab) => {
      tab.setAttribute("aria-pressed", String(tab === button));
    });
  };
});
$("log-toggle").onclick = () => {
  $("logs-dialog").showModal();
  $("logs").scrollTop = $("logs").scrollHeight;
};
$("log-close").onclick = () => $("logs-dialog").close();
setTab("connection");

const tuning = [
  ["minimum_contour_area", "最小轮廓面积", "detection", 1],
  ["minimum_circularity", "最小圆度", "detection", 0.01],
  ["minimum_aspect_ratio", "最小宽高比", "detection", 0.01],
  ["maximum_aspect_ratio", "最大宽高比", "detection", 0.01],
  ["confirmation_frames", "确认帧数", "detection", 1],
  ["release_frames", "释放帧数", "detection", 1],
  ["core_a_min", "核心红色阈值 A", "detection", 1],
  ["minimum_core_fraction", "最小核心占比", "detection", 0.01],
  ["align_enter_error", "进入对准误差", "controller", 0.01],
  ["align_exit_error", "退出对准误差", "controller", 0.01],
  ["arrival_radius_ratio", "到达半径比例", "controller", 0.01],
  ["final_approach_radius_ratio", "盲区交接半径比例", "controller", 0.005],
  ["final_approach_bottom_ratio", "盲区交接底边比例", "controller", 0.01],
  ["final_approach_frames", "盲区交接确认帧数", "controller", 1],
  ["final_approach_velocity", "盲区末段速度", "controller", 0.05],
  ["final_approach_seconds", "盲区末段秒数", "controller", 0.05],
];

function showNotice(message) {
  $("notice").textContent = message;
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 9000);
}

async function api(path, body, method = "POST") {
  const response = await fetch(path, {method, credentials: "same-origin",
    headers: body === undefined ? {} : {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) $("login-panel").hidden = false;
    const message = typeof result.detail === "string" ? result.detail : "参数格式不正确，请检查输入值";
    throw new Error(message);
  }
  return result;
}

function context() { return {client_id: clientId, lease, session_id: state?.session_id ?? 0}; }
async function perform(callback) {
  if (busy) return;
  busy = true;
  render();
  try { await callback(); } catch (error) { showNotice(error.message); }
  finally { busy = false; render(); }
}

function send(payload) {
  if (connected && ws?.readyState === WebSocket.OPEN && ws.bufferedAmount < 4096) {
    ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}
function sendHeartbeat() {
  if (!lease || document.hidden || Date.now() - lastHeartbeatAt < 450) return;
  if (send({type:"heartbeat", lease})) lastHeartbeatAt = Date.now();
}
function motion(v, steer, lateral) {
  if (!manualAllowed || !lease || !state) return;
  send({type: "manual", lease, session_id: state.session_id,
    seq: sequence++, velocity: v, steer, lateral});
}
function clearDrive() {
  drive = null;
  document.querySelectorAll(".drive.active").forEach((button) => button.classList.remove("active"));
  if (lastDrive) motion(0, 0, 0);
  lastDrive = false;
}

function render() {
  $("connection").textContent = connected ? "服务已连接" : "服务未连接";
  $("connection-dot").classList.toggle("green", connected);
  $("estop").disabled = !connected;
  const owns = connected && Boolean(lease) && state?.owner === "you";
  const caps = state?.capabilities ?? {};
  manualAllowed = Boolean(owns && caps.manual && !busy);
  $("start").disabled = !owns || !caps.start || busy;
  $("start").textContent = $("control-only").checked ? "▶ 开始手动会话" : "▶ 开始预览";
  $("arm").disabled = !owns || !(state?.armed || caps.arm) || busy;
  $("arm").textContent = state?.armed ? "解除自治" : "武装自治";
  $("arm-mode").disabled = !owns || !caps.arm || busy || Boolean(state?.armed);
  $("arm-forward-seconds").disabled = $("arm-mode").value !== "startup" || $("arm-mode").disabled;
  $("stop").disabled = !connected || !caps.stop;
  $("reset").disabled = !owns || !caps.reset || busy;
  $("claim").disabled = !connected || busy || (state?.owner !== "none" && !owns);
  $("claim").textContent = owns ? "释放控制权" : "取得控制权";
  $("settings").disabled = !owns || !caps.start || busy;
  $("apply-tuning").disabled = !owns || state?.session_state !== "running" || state?.control_only || busy;
  $("capture").disabled = !owns || state?.session_state !== "running" || state?.control_only || busy;
  $("browser-gamepad").disabled = !owns || !window.isSecureContext || !navigator.getGamepads;
  document.querySelectorAll(".drive, [data-action], #hold").forEach((button) => {
    button.disabled = !manualAllowed;
  });
  if (!manualAllowed) { drive = null; lastDrive = false; }
  if (!state) return;
  $("session-badge").textContent = state.session_label;
  $("session-badge").classList.toggle("danger", Boolean(state.fault || state.estop_latched));
  $("video-status").textContent = connected ? state.video_status : "连接已断开";
  $("control-state").textContent = state.control_state;
  $("target").textContent = state.target;
  $("error").textContent = state.horizontal_error;
  $("radius").textContent = state.radius_ratio;
  $("bottom-ratio").textContent = state.bottom_ratio;
  $("frame-count").textContent = `${state.frame_count} FRAMES`;
  $("robot-status").textContent = state.robot_status;
  $("owner-status").textContent = ({you:"当前页面",other:"其他页面",local:"香橙派手柄",none:"未分配"})[state.owner];
  $("distance").textContent = `${state.obstacle_distance} mm`;
  $("obstacle-state").textContent = state.obstacle_state;
  $("avoid-count").textContent = state.avoid_count;
  $("output-source").textContent = state.output.source;
  const localPad = state.gamepad ?? {};
  $("local-pad").textContent = localPad.message ?? "未启用本机手柄";
  $("local-pad-live").textContent = localPad.connected ? localPad.name || localPad.message : localPad.message;
  $("local-pad-live").classList.toggle("connected", Boolean(localPad.connected));
  $("local-pad-axes").textContent = `前后 ${gamepadAxis(localPad.drive)} · 横移 ${gamepadAxis(localPad.lateral)} · 转向 ${gamepadAxis(localPad.steer)} · 俯仰 ${gamepadAxis(localPad.head)}`;
  $("local-pad-buttons").textContent = `按键 ${(localPad.buttons ?? []).map(gamepadButton).join("+") || "--"}`;
  $("manual-mode").textContent = state.armed ? "自治已武装" : manualAllowed ? "手动模式" : "仅观察";
  $("output").textContent = `v ${state.output.v.toFixed(2)} · 转向 ${state.output.steer.toFixed(2)}`;
  const capture = state.capture;
  $("capture").textContent = capture.active ? "停止采集" : "开始采集";
  $("capture-count").textContent = `已保存 ${capture.saved} 帧 · 丢弃 ${capture.dropped} 帧`;
  $("capture-path").textContent = capture.directory;
  const showVideo = connected && state.frame_ready && state.session_state === "running";
  $("video").hidden = !showVideo;
  $("video-empty").hidden = showVideo;
  if (showVideo && !$("video").getAttribute("src")) $("video").src = "/stream.mjpg";
  if (!showVideo) $("video").removeAttribute("src");
  $("source-caption").textContent = showVideo ? `CAMERA / ${state.video_status}` : "CAMERA / STANDBY";
  if (state.log_version !== lastLogVersion) {
    const logs = $("logs");
    const nearEnd = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 45;
    logs.textContent = state.logs.join("\n") || "会话日志将在这里显示";
    $("log-count").textContent = `${state.logs.length} 条`;
    if (nearEnd) logs.scrollTop = logs.scrollHeight;
    lastLogVersion = state.log_version;
  }
  if (state.tuning && tuningSession !== state.session_id) {
    fillTuning(state.tuning);
    tuningSession = state.session_id;
  }
}

function connect() {
  clearTimeout(reconnectTimer);
  if (document.hidden || (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState))) return;
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events`);
  ws = socket;
  socket.onmessage = (event) => {
    if (ws !== socket) return;
    const data = JSON.parse(event.data);
    if (data.type === "error") { showNotice(data.message); return; }
    connected = true;
    clientId = data.client_id;
    state = data.state;
    if (lease && state.owner !== "you" && Date.now() - claimedAt > 500) {
      lease = "";
      $("browser-gamepad").checked = false;
      clearDrive();
    }
    sendHeartbeat();
    render();
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    connected = false;
    lease = "";
    clientId = "";
    $("browser-gamepad").checked = false;
    clearDrive();
    render();
    reconnectTimer = setTimeout(connect, 1500);
  };
  socket.onerror = () => socket.close();
}

async function initialize() {
  try {
    defaults = await api("/api/defaults", undefined, "GET");
    $("login-panel").hidden = true;
    for (const key of ["source", "backend", "host", "port"]) $(key).value = defaults[key];
    $("approach").value = defaults.approach_mode;
    $("detector").value = defaults.detector_backend ?? "lab";
    $("ascend-model-path").value = defaults.ascend_model_path ?? "";
    fillSvmModels(defaults.svm_presets ?? [], defaults.svm_model_path ?? "");
    updateDetectorFields();
    $("obstacle").checked = defaults.obstacle;
    $("arm-mode").value = "direct";
    $("arm-forward-seconds").value = defaults.startup_forward_seconds;
    $("arm-mode-startup").disabled = !defaults.startup_enabled;
    $("arm-mode-startup").textContent = defaults.startup_enabled
      ? "先直走再自治"
      : "起步路线未启用";
    $("local-gamepad").disabled = !defaults.gamepad_enabled;
    fillTuning(defaults);
    connect();
  } catch (error) { showNotice(error.message); }
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    await api("/api/login", {token: $("token").value});
    $("token").value = "";
    await initialize();
  });
});
$("claim").onclick = () => perform(async () => {
  if (lease && state.owner === "you") {
    clearDrive();
    await api("/api/control/release", {client_id: clientId, lease});
    lease = "";
  } else {
    const result = await api("/api/control/claim", {client_id: clientId});
    lease = result.lease;
    claimedAt = Date.now();
    lastHeartbeatAt = 0;
    sequence = 0;
    sendHeartbeat();
  }
});
$("start").onclick = () => perform(async () => {
  const detector = $("detector").value;
  const modelPath = selectedModelPath();
  if (detector !== "lab" && !modelPath && !$("control-only").checked) {
    throw new Error("请填写服务端模型路径");
  }
  await api("/api/session/start", {
    client_id: clientId, lease, source: $("source").value.trim(), backend: $("backend").value,
    host: $("host").value.trim(), port: Number($("port").value), mirror: $("mirror").value.trim(),
    control_only: $("control-only").checked, local_gamepad: $("local-gamepad").checked,
    obstacle: $("obstacle").checked,
    approach_mode: $("approach").value,
    detector_backend: detector, detector_model_path: modelPath,
  });
  setTab("status");
});
$("arm").onclick = () => perform(async () => {
  if (state.armed) {
    await api("/api/session/disarm", context());
    return;
  }
  const armMode = $("arm-mode").value;
  const payload = {...context(), arm_mode: armMode};
  if (armMode === "startup") {
    const seconds = Number($("arm-forward-seconds").value);
    if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 120) {
      throw new Error("直走时长需要在 0–120 秒之间");
    }
    payload.startup_forward_seconds = seconds;
  }
  await api("/api/session/arm", payload);
  // A long open-loop lead-in is always opt-in for each individual arm action.
  $("arm-mode").value = "direct";
});
$("reset").onclick = () => perform(() => api("/api/session/reset", context()));
// Stop is independent of in-flight tuning, connect and capture requests.
for (const operation of ["stop", "estop"]) $(operation).onclick = async () => {
  clearDrive();
  $("browser-gamepad").checked = false;
  try { await api(`/api/session/${operation}`, {}); } catch (error) { showNotice(error.message); }
};
function selectedModelPath() {
  if ($("detector").value === "ascend") return $("ascend-model-path").value.trim();
  if ($("detector").value !== "svm") return "";
  return $("svm-model").value === CUSTOM_SVM
    ? $("svm-custom-path").value.trim() : $("svm-model").value;
}
function updateSvmDescription() {
  const option = $("svm-model").selectedOptions[0];
  $("svm-custom-field").hidden = $("svm-model").value !== CUSTOM_SVM;
  $("svm-description").textContent = option?.dataset.description ?? "";
}
function fillSvmModels(presets, selectedPath) {
  const select = $("svm-model");
  select.replaceChildren();
  for (const preset of presets) {
    const option = document.createElement("option");
    option.value = preset.path;
    option.textContent = `${preset.label}${preset.available ? "" : "（服务器未找到）"}`;
    option.dataset.description = preset.description;
    option.disabled = !preset.available;
    select.append(option);
  }
  const custom = document.createElement("option");
  custom.value = CUSTOM_SVM;
  custom.textContent = "自定义模型路径…";
  custom.dataset.description = "手动填写服务器上的 .json / .npz 路径";
  select.append(custom);
  const known = [...select.options].some((option) => option.value === selectedPath && !option.disabled);
  select.value = known ? selectedPath : CUSTOM_SVM;
  if (selectedPath && !known) $("svm-custom-path").value = selectedPath;
  updateSvmDescription();
}
function updateDetectorFields() {
  $("ascend-model-field").hidden = $("detector").value !== "ascend";
  $("svm-model-field").hidden = $("detector").value !== "svm";
}
$("detector").onchange = updateDetectorFields;
$("svm-model").onchange = updateSvmDescription;
$("control-only").onchange = render;
$("arm-mode").onchange = render;
$("capture").onclick = () => perform(() => api("/api/capture", {...context(), active: !state.capture.active}));
document.querySelectorAll("[data-action]").forEach((button) => {
  button.onclick = () => perform(() => api(`/api/action/${button.dataset.action}`, context()));
});
document.querySelectorAll(".drive").forEach((button) => {
  const begin = () => {
    if (!manualAllowed) return;
    $("browser-gamepad").checked = false;
    drive = [Number(button.dataset.v), Number(button.dataset.s), Number(button.dataset.l)];
    button.classList.add("active");
    motion(...drive);
    lastDrive = true;
  };
  button.onpointerdown = (event) => {
    if (!manualAllowed || event.button !== 0) return;
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    begin();
  };
  button.onpointerup = button.onpointercancel = button.onlostpointercapture = clearDrive;
  button.onkeydown = (event) => {
    if ([" ", "Enter"].includes(event.key)) { event.preventDefault(); if (!event.repeat) begin(); }
  };
  button.onkeyup = button.onblur = clearDrive;
});
$("hold").onclick = () => { clearDrive(); $("browser-gamepad").checked = false; motion(0, 0, 0); };
$("browser-gamepad").onchange = () => { clearDrive(); motion(0, 0, 0); previousButtons = []; };
window.addEventListener("blur", () => { clearDrive(); $("browser-gamepad").checked = false; motion(0, 0, 0); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearDrive(); ws?.close(); }
  else if (defaults) connect();
});
window.addEventListener("pagehide", () => { clearDrive(); ws?.close(); });
setInterval(sendHeartbeat, 250);

function deadzone(raw) {
  const value = Number.isFinite(raw) ? Math.max(-1, Math.min(1, raw)) : 0;
  return Math.abs(value) <= 0.2 ? 0 : Math.sign(value) * (Math.abs(value) - 0.2) / 0.8;
}
setInterval(() => {
  if (document.hidden) return;
  if (drive && manualAllowed) { motion(...drive); lastDrive = true; return; }
  if (!window.isSecureContext || !navigator.getGamepads) {
    $("pad-status").textContent = "浏览器手柄需要 HTTPS 或 localhost；香橙派直连手柄不受影响";
    return;
  }
  let pad;
  try { pad = Array.from(navigator.getGamepads()).find((item) => item?.connected); }
  catch (_) { $("pad-status").textContent = "浏览器未开放手柄访问权限"; return; }
  $("pad-status").textContent = pad ? (pad.mapping === "standard" ? pad.id : "此手柄无标准映射，暂不能遥控") : "按一下手柄按键以连接";
  if (!$("browser-gamepad").checked || !connected || !lease || !pad || pad.mapping !== "standard") {
    if (lastDrive) motion(0, 0, 0);
    lastDrive = false;
    previousButtons = [];
    return;
  }
  const pressed = pad.buttons.map((button) => button.pressed);
  const rising = (index) => pressed[index] && !previousButtons[index];
  if (rising(1) && !padToggleBusy && state?.session_state === "running") {
    padToggleBusy = true;
    api(`/api/session/${state.armed ? "disarm" : "arm"}`, context())
      .catch((error) => showNotice(error.message)).finally(() => { padToggleBusy = false; });
  }
  if (manualAllowed) {
    const actions = {5:"right_grip",6:"left_trigger",7:"right_trigger"};
    for (const [index, name] of Object.entries(actions)) {
      if (rising(index)) api(`/api/action/${name}`, context()).catch((error) => showNotice(error.message));
    }
    if (rising(12) || rising(13)) {
      const next = Math.max(-1, Math.min(1, headLevel + (rising(12) ? 1 : -1)));
      if (next !== headLevel) {
        headLevel = next;
        api(`/api/action/${["head_down","head_center","head_up"][headLevel+1]}`, context())
          .catch((error) => showNotice(error.message));
      }
    }
    motion(deadzone(-pad.axes[1]), deadzone(pad.axes[2]), deadzone(pad.axes[0]));
    lastDrive = true;
  }
  previousButtons = pressed;
}, 50);

function makeInput(id, labelText, step) {
  const wrapper = document.createElement("div");
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = labelText;
  const input = document.createElement("input");
  input.id = id; input.type = "number"; input.step = step; input.required = true;
  input.oninput = () => { tuningDirty = true; };
  wrapper.append(label, input);
  return wrapper;
}
for (const bound of ["min", "max"]) {
  const title = document.createElement("label");
  title.textContent = `LAB ${bound === "min" ? "下限" : "上限"}（L / A / B）`;
  const row = document.createElement("div");
  row.className = "tuning-fields-row";
  ["L", "A", "B"].forEach((name, index) => row.append(makeInput(`lab_${bound}_${index}`, name, 1)));
  $("tuning-fields").append(title, row);
}
for (const [key, label, , step] of tuning) $("tuning-fields").append(makeInput(`tune-${key}`, label, step));
function fillTuning(values) {
  for (const bound of ["min", "max"]) values.detection[`lab_${bound}`].forEach((value, index) => {
    $(`lab_${bound}_${index}`).value = value;
  });
  for (const [key, , group] of tuning) $(`tune-${key}`).value = values[group][key];
  tuningDirty = false;
}
$("restore-tuning").onclick = () => { if (defaults) fillTuning(defaults); };
$("tuning-form").onsubmit = (event) => {
  event.preventDefault();
  perform(async () => {
    const values = {detection: {}, controller: {}};
    for (const bound of ["min", "max"]) values.detection[`lab_${bound}`] = [0,1,2].map((index) => Number($(`lab_${bound}_${index}`).value));
    for (const [key, , group] of tuning) values[group][key] = Number($(`tune-${key}`).value);
    const result = await api("/api/tuning", {...context(), ...values}, "PATCH");
    if (tuningDirty) fillTuning(result);
    showNotice("参数已应用到当前会话");
  });
};
render();
initialize();
