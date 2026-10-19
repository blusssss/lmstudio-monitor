"use strict";
const $ = (id) => document.getElementById(id);
const fmt = (n, d = 1) => n == null || !Number.isFinite(Number(n)) ? "—" : Number(n).toFixed(d);
const clock = (t) => new Date(t * 1e3).toLocaleTimeString("zh-CN", { hour12: false });
const dateTime = (t) => t ? new Date(t * 1e3).toLocaleString("zh-CN", { hour12: false }) : "未观测";
const phaseNames = { prefill: "处理输入", decode: "正在生成", observed: "运行中", completed: "已结束", cancelled: "已取消", interrupted: "已中断", error: "错误" };
let state = null, history = [], modelId = localStorage.getItem("model") || "", gpuId = "", view = "tasks", range = 1800, pausedAt = null, lastPoint = 0, drawer = null, tableSignature = "", historyRevision = null;
function el(tag, value = "", cls = "") {
  const n = document.createElement(tag);
  n.textContent = value;
  if (cls) n.className = cls;
  return n;
}
function text(id, value) {
  $(id).textContent = value;
}
function selectOptions(select, options, value) {
  const signature = JSON.stringify(options);
  if (select.dataset.options !== signature) {
    select.replaceChildren(...options.map(([id, label]) => {
      const o = el("option", label);
      o.value = id;
      return o;
    }));
    select.dataset.options = signature;
  }
  select.value = options.some(([id]) => id === value) ? value : options[0]?.[0] || "";
  return select.value;
}
function modelInstances() {
  return (state?.models || []).flatMap((m) => (m.loaded_instances || []).map((i) => ({ model: m, instance: i })));
}
function selectedRows(rows) {
  return (rows || []).filter((r) => modelId === "*" || r.model === modelId);
}
function slots() {
  return Object.entries(state?.live || {}).filter(([id]) => modelId === "*" || id === modelId).flatMap(([, l]) => Object.values(l.slots || {}));
}
function render(data) {
  const revision = data.history_revision || 0;
  if (historyRevision != null && revision < historyRevision) return;
  if (historyRevision != null && revision !== historyRevision) {
    history = [];
    lastPoint = 0;
    tableSignature = "";
    pausedAt = null;
    text("pause", "暂停");
    $("pause").setAttribute("aria-pressed", "false");
    if ($("detail-dialog").open) $("detail-dialog").close();
    drawer = null;
  }
  historyRevision = revision;
  state = data;
  text("clock", clock(data.ts));
  const sources = data.sources || {}, runtime = sources.runtime || {};
  $("connection").className = `status ${data.lm_connected ? runtime.ok ? "ok" : "warn" : "warn"}`;
  text("connection", !data.lm_connected ? "LM Studio 未连接" : !runtime.ok ? "日志未连接" : "已连接");
  const loaded = modelInstances(), ids = [.../* @__PURE__ */ new Set([...loaded.map((x) => x.instance.id), ...data.requests.map((r) => r.model), ...(data.summaries || []).map((r) => r.model)])];
  const options = [...ids.map((id) => [id, loaded.some((x) => x.instance.id === id) ? id : `${id}（历史）`]), ["*", "全部实例"]];
  modelId = selectOptions($("model-select"), options, modelId || loaded[0]?.instance.id || "*");
  localStorage.setItem("model", modelId);
  gpuId = selectOptions($("gpu-select"), data.gpu.length ? data.gpu.map((g) => [g.uuid, `GPU ${g.index} · ${g.name}`]) : [["", "暂无 GPU"]], gpuId);
  const notice = !data.lm_connected ? "LM Studio API 未连接。启动本机服务后将自动接入；GPU 与系统资源继续采集。" : !runtime.ok ? "Runtime 日志尚未连接，实时速度与任务状态暂不可用。" : " ";
  $("notice").hidden = notice === " ";
  text("notice", notice);
  const active = slots().filter((s) => s.active), speeds = active.filter((s) => s.decode_tps != null);
  text("decode", fmt(speeds.length ? speeds.reduce((n, s) => n + s.decode_tps, 0) : null));
  const phase = active.some((s) => s.status === "decode") ? "decode" : active.length ? "prefill" : null;
  text("stage", phase ? phaseNames[phase] : !data.lm_connected ? "未连接" : loaded.some((x) => modelId === "*" || x.instance.id === modelId) ? "空闲" : "未加载");
  $("stage").className = `state ${active.length ? "running" : ""}`;
  const rows = selectedRows(data.requests), summaries = selectedRows(data.summaries), recent = rows.find((r) => r.tokensPerSecond != null) || summaries[0];
  text("decode-note", active.length ? speeds.length ? `${active.length} 个活跃任务 · 约 3 秒更新` : active.some((s) => s.stale && s.status === "decode") ? "速度数据已过期，等待更新" : "等待首条解码速度" : recent ? `最近平均 ${fmt(recent.tokensPerSecond)} tok/s` : "等待生成");
  const latestTtft = summaries.find((r) => r.timeToFirstTokenSec != null);
  const latestPrefill = rows.filter((r) => r.prefill_tps != null).sort((a, b) => b.updated_at - a.updated_at)[0];
  text("ttft", fmt(latestTtft == null ? null : latestTtft.timeToFirstTokenSec * 1e3, 1));
  text("ttft-note", latestTtft ? `${clock(latestTtft.ts)} · ${modelId === "*" ? latestTtft.model : "完成统计"}` : "等待完成统计");
  $("ttft-note").title = "最近一条可用的模型结束统计；未与 Runtime 任务强行关联";
  text("prefill", fmt(latestPrefill?.prefill_tps, 0));
  text("prefill-note", latestPrefill ? `${clock(latestPrefill.updated_at)} · 处理 ${latestPrefill.prefill_tokens} tokens` : "等待运行时统计");
  $("prefill-note").title = latestPrefill ? `${latestPrefill.model} · 实际 Prefill 耗时 ${fmt(latestPrefill.prefill_ms)} ms` : "实际输入处理速度通常在请求结束时提供";
  const gpu = data.gpu.find((g) => g.uuid === gpuId) || {};
  text("gpu-util", fmt(gpu.utilization, 0));
  text("gpu-note", gpu.name || "GPU 采样不可用");
  text("vram", fmt(gpu.memory_used == null ? null : gpu.memory_used / 1024, 2));
  text("vram-note", gpu.memory_total ? `${fmt(gpu.memory_total / 1024, 1)} GiB 总容量 · ${fmt(gpu.temperature, 0)} °C · ${fmt(gpu.power, 0)} W` : "GPU 采样不可用");
  const ended = rows.filter((r) => r.context_tokens != null).sort((a, b) => b.updated_at - a.updated_at)[0];
  const current = loaded.find((x) => x.instance.id === (ended?.model || modelId)), cap = ended ? ended.context_capacity : current?.instance.config.context_length;
  text("context", ended ? ended.context_tokens.toLocaleString("zh-CN") : "—");
  text("context-unit", cap ? `/ ${cap.toLocaleString("zh-CN")}` : "tokens");
  text("context-note", ended ? `${clock(ended.updated_at)} · slot ${ended.slot}${ended.truncated ? " · 已截断" : ""}${active.length ? " · 生成中不代表当前占用" : ""}` : "暂无结束时上下文记录");
  text("system-note", `CPU ${fmt(data.system.cpu, 0)}% · 内存 ${fmt(data.system.memory_used == null ? null : data.system.memory_used / 1024, 1)} / ${fmt(data.system.memory_total == null ? null : data.system.memory_total / 1024, 1)} GiB`);
  text("speed-caption", modelId === "*" ? "全部实例合计" : "所选实例合计");
  if (data.ts - lastPoint >= 0.8) {
    const instances = {};
    for (const [id, live] of Object.entries(data.live)) {
      const measured = Object.values(live.slots).filter((s) => s.active && s.decode_tps != null);
      instances[id] = { decode_tps: measured.length ? measured.reduce((n, s) => n + s.decode_tps, 0) : null };
    }
    const values = Object.values(instances).filter((x) => x.decode_tps != null);
    history.push({ ts: data.ts, gpu: data.gpu, instances, decode_tps: values.length ? values.reduce((n, v) => n + v.decode_tps, 0) : null });
    lastPoint = data.ts;
    history = history.filter((x) => x.ts > data.ts - 86400);
  }
  renderTable();
  if (!pausedAt) draw();
  if (drawer) renderDrawer();
}
function renderTable() {
  const filter = $("status-filter").value;
  let rows = selectedRows(view === "tasks" ? state.requests : state.summaries);
  if (view === "tasks" && filter !== "all") rows = rows.filter((r) => filter === "active" ? !["completed", "cancelled", "interrupted", "error"].includes(r.status) : r.status === filter);
  text("request-count", `${rows.length} 条`);
  $("status-filter").hidden = view !== "tasks";
  text("record-note", view === "tasks" ? "按实例、进程、slot 和任务编号区分；输入数为实际 Prefill 处理量。点击时间查看详情。" : "模型结束统计不包含稳定的请求 ID，因此不与运行任务按时间强行合并。首 Token 延迟以此处为准。");
  const headers = view === "tasks" ? ["观测时间", "模型实例", "状态", "处理输入 / 输出", "Prefill", "平均解码", "运行时耗时"] : ["完成时间", "模型实例", "输入 / 输出", "平均解码", "首 Token", "总耗时", "结束原因"];
  const signature = JSON.stringify([view, filter, rows, modelId]);
  if (signature === tableSignature) return;
  tableSignature = signature;
  const focusedRow = document.activeElement?.dataset?.rowId;
  const tr = el("tr");
  headers.forEach((h) => tr.append(el("th", h)));
  $("request-head").replaceChildren(tr);
  if (!rows.length) {
    const r = el("tr", "", "empty"), td = el("td", view === "tasks" ? "暂无运行任务。采集器在线时发送一次推理请求即可查看。" : "暂无完成统计。");
    td.colSpan = headers.length;
    r.append(td);
    $("request-body").replaceChildren(r);
    return;
  }
  $("request-body").replaceChildren(...rows.map((r) => {
    const row = el("tr"), time = el("td"), button = el("button", clock(r.first_observed_at || r.ts), "row-open");
    button.dataset.rowId = String(r.id);
    button.setAttribute("aria-label", `查看 ${r.model} ${clock(r.first_observed_at || r.ts)} 的详情`);
    button.addEventListener("click", () => openDrawer({ type: view === "tasks" ? "task" : "summary", id: r.id }));
    time.append(button);
    row.append(time, el("td", r.model, "model"));
    const cells = view === "tasks" ? [phaseNames[r.status] || r.status, `${r.prefill_tokens ?? "—"} / ${r.predictedTokensCount ?? r.generated_tokens ?? "—"}`, `${fmt(r.prefill_tps, 0)} tok/s`, `${fmt(r.tokensPerSecond ?? r.average_tps)} tok/s`, r.totalTimeSec != null ? `${fmt(r.totalTimeSec, 2)} s` : "—"] : [`${r.promptTokensCount ?? "—"} / ${r.predictedTokensCount ?? "—"}`, `${fmt(r.tokensPerSecond)} tok/s`, `${fmt(r.timeToFirstTokenSec == null ? null : r.timeToFirstTokenSec * 1e3, 0)} ms`, `${fmt(r.totalTimeSec, 2)} s`, r.stopReason || "—"];
    cells.forEach((v, i) => row.append(el("td", v, view === "tasks" && i === 0 ? `phase ${r.status}` : "")));
    return row;
  }));
  if (focusedRow) [...$("request-body").querySelectorAll(".row-open")].find((b) => b.dataset.rowId === focusedRow)?.focus({ preventScroll: true });
}
function plot(canvas, rows, getter, color, unit, start, end, ceiling) {
  const box = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  if (box.width < 1) return;
  canvas.width = Math.round(box.width * ratio);
  canvas.height = Math.round(box.height * ratio);
  const c = canvas.getContext("2d");
  c.scale(ratio, ratio);
  const left = 39, right = 15, top = 15, bottom = 27, width = box.width - left - right, height = box.height - top - bottom;
  const points = rows.filter((r) => r.ts >= start && r.ts <= end).map((r) => ({ ts: r.ts, value: getter(r) }));
  const numbers = points.filter((p) => p.value != null && Number.isFinite(p.value));
  const maximum = ceiling || Math.max(unit === "GiB" ? 1 : 10, ...numbers.map((p) => p.value)) * 1.15;
  c.font = "10px system-ui";
  c.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = top + height * i / 4;
    c.strokeStyle = "#e9edf1";
    c.beginPath();
    c.moveTo(left, y);
    c.lineTo(box.width - right, y);
    c.stroke();
    c.fillStyle = "#8a939f";
    c.textAlign = "right";
    c.fillText(fmt(maximum * (1 - i / 4), unit === "GiB" ? 1 : 0), left - 8, y + 3);
  }
  c.strokeStyle = color;
  c.lineWidth = 1.8;
  c.beginPath();
  let previous = null;
  for (const p of points) {
    if (p.value == null) {
      previous = null;
      continue;
    }
    const x = left + (p.ts - start) / (end - start) * width, y = top + height * (1 - p.value / maximum);
    if (!previous || p.ts - previous.ts > 15) c.moveTo(x, y);
    else c.lineTo(x, y);
    previous = p;
  }
  c.stroke();
  if (numbers.length <= 3) {
    c.fillStyle = color;
    for (const p of numbers) {
      c.beginPath();
      c.arc(left + (p.ts - start) / (end - start) * width, top + height * (1 - p.value / maximum), 2, 0, Math.PI * 2);
      c.fill();
    }
  }
  c.fillStyle = "#8a939f";
  for (let i = 0; i <= 2; i++) {
    c.textAlign = i === 0 ? "left" : i === 2 ? "right" : "center";
    c.fillText(clock(start + (end - start) * i / 2), left + width * i / 2, box.height - 7);
  }
  if (!numbers.length) {
    c.textAlign = "center";
    c.fillStyle = "#929aa5";
    c.fillText("暂无可用数据", left + width / 2, top + height / 2);
  }
}
function draw() {
  const end = pausedAt || Date.now() / 1e3, start = end - range;
  plot($("speed-chart"), history, (r) => modelId === "*" ? r.decode_tps : r.instances?.[modelId]?.decode_tps, "#4e76a5", "tok/s", start, end);
  const metric = $("resource").value, unit = { utilization: "%", memory_used: "GiB", power: "W", temperature: "°C" }[metric];
  plot($("resource-chart"), history, (r) => {
    const g = r.gpu.find((x) => x.uuid === gpuId), v = g?.[metric];
    return v == null ? null : metric === "memory_used" ? v / 1024 : v;
  }, "#657d74", unit, start, end, metric === "utilization" ? 100 : null);
  text("chart-time", `${clock(start)} — ${clock(end)}${pausedAt ? " · 已暂停" : ""}`);
}
function section(title, pairs, note) {
  const node = el("section", "", "detail-section");
  node.append(el("h3", title));
  if (pairs) {
    const dl = el("dl");
    for (const [k, v] of pairs) {
      const d = el("div");
      d.append(el("dt", k), el("dd", v));
      dl.append(d);
    }
    node.append(dl);
  }
  if (note) node.append(el("p", note));
  return node;
}
function openDrawer(spec) {
  drawer = spec;
  renderDrawer();
  $("detail-dialog").showModal();
}
function renderDrawer() {
  if ($("detail-content").contains(document.activeElement)) return;
  const content = $("detail-content");
  if (drawer.type === "model" || drawer.type === "diagnostics") {
    text("detail-title", drawer.type === "model" ? "模型与采集" : "采集诊断");
    const nodes2 = [];
    if (drawer.type === "model") {
      const models = modelInstances().filter((x) => modelId === "*" || x.instance.id === modelId);
      for (const { model: m, instance: i } of models) {
        const c = i.config;
        nodes2.push(section(m.display_name, [["实例", i.id], ["量化", m.quantization?.name || "—"], ["实例上下文", `${c.context_length} tokens`], ["模型最大上下文", `${m.max_context_length} tokens`], ["并发上限", c.parallel ?? "—"], ["Flash Attention", c.flash_attention ? "开启" : "关闭"], ["KV GPU Offload", c.offload_kv_cache_to_gpu == null ? "未知" : c.offload_kv_cache_to_gpu ? "开启" : "关闭"]], !state.lm_connected ? "以上为最后一次成功读取的配置。" : null));
      }
      if (!models.length) nodes2.push(section("模型", null, "当前没有加载的模型实例。"));
      nodes2.push(section("KV Cache", [["实际分配容量", "不可用"], ["有效占用", "不可用"], ["命中率", "不可用"]], "当前已验证的运行时日志未提供这些字段。不使用显存差值推算。"));
      const gpu = state.gpu.find((g) => g.uuid === gpuId), processes = state.gpu_processes.filter((p) => p.gpu_uuid === gpuId);
      nodes2.push(section(gpu?.name || "GPU 进程", processes.map((p) => [`${p.name.split("/").pop()} · PID ${p.pid}`, `${fmt(p.memory, 0)} MiB`]), "整卡指标包含其他进程；进程显存不等于 KV Cache 占用。"));
    }
    const sources = section("采集来源");
    for (const [key, label] of Object.entries({ lm_api: "模型 API", runtime: "Runtime 日志", model: "完成统计", gpu: "GPU", system: "系统资源" })) {
      const s = state.sources[key] || {};
      const line = el("p", `${label} · ${s.detail || "等待连接"}${s.last_event_at ? " · 最近事件 " + clock(s.last_event_at) : ""}`, `source-status ${s.ok ? "good" : "bad"}`);
      sources.append(line);
    }
    nodes2.push(sources);
    nodes2.push(section("数据口径", null, "实时速度约每 3 秒更新，超过 12 秒未更新即隐藏。运行时耗时由实际 Prefill 和 Decode 耗时相加，不包含未知的排队时间。历史每 5 秒保存一次。"));
    if (drawer.type === "diagnostics") {
      nodes2.push(section("最近诊断", (state.diagnostics || []).map((d) => [clock(d.ts), d.code]), "诊断不包含提示词、模型输出或日志原文。"));
      const b = el("button", "下载诊断");
      b.addEventListener("click", async () => {
        const r = await fetch("/api/diagnostics");
        if (r.ok) download(await r.json(), "diagnostics");
      });
      nodes2.push(b);
    }
    content.replaceChildren(...nodes2);
    return;
  }
  const row = (drawer.type === "task" ? state.requests : state.summaries).find((r) => r.id === drawer.id);
  text("detail-title", drawer.type === "task" ? "任务详情" : "完成统计");
  if (!row) {
    content.replaceChildren(section("记录不可用", null, "记录已超出当前展示范围。"));
    return;
  }
  const runtime = drawer.type === "task";
  const nodes = [section("记录", [["模型实例", row.model], ["完成时间", dateTime(row.ended_at || (!runtime ? row.ts : null))], ...runtime ? [["状态", phaseNames[row.status] || row.status], ["进程 / slot / 任务", `${row.pid ?? "—"} / ${row.slot} / ${row.task}`], ["观测开始", dateTime(row.first_observed_at)]] : [["来源", "模型结束统计"], ["任务关联", "未关联（缺少稳定请求 ID）"]]])];
  if (runtime) {
    nodes.push(section("阶段耗时", [["Prefill", `${fmt(row.prefill_ms, 1)} ms`], ["Decode", `${fmt(row.decode_ms, 1)} ms`], ["排队 / 加载", "不可用"], ["首 Token 延迟", "未关联，请查看完成统计"]], "以上为运行时实测耗时。下方时间线记录阶段事件到达时刻；首条速度日志可能晚于实际开始生成。"));
    const timeline = section("观测时间线"), list = el("ol", "", "timeline");
    for (const e of row.stage_events || []) {
      const item = el("li", phaseNames[e.phase] || e.phase);
      item.append(el("time", clock(e.ts)));
      list.append(item);
    }
    timeline.append(list);
    nodes.push(timeline);
    const live = slots().find((s) => s.request_key === row.request_key);
    nodes.push(section("Token 与上下文", [["实际 Prefill 处理量", row.prefill_tokens ?? "—"], ["输出 Token", row.predictedTokensCount ?? row.generated_tokens ?? "—"], ["Prefill 速度", `${fmt(row.prefill_tps)} tok/s`], ["平均解码", `${fmt(row.tokensPerSecond ?? row.average_tps)} tok/s`], ["结束时上下文", row.context_tokens ?? "未观测"], ["生成中上下文估算", live?.context_estimate ?? "不可用"], ["截断", row.truncated == null ? "未知" : row.truncated ? "是" : "否"], ["结束原因", row.stopReason || "未提供"]], "生成中估算仅在同时具备已处理输入与已生成 Token 时显示，缓存复用可能使其低估总占用。"));
    const graph = section("任务期间 GPU 利用率"), canvas = el("canvas", "", "detail-chart");
    canvas.setAttribute("aria-label", "任务期间 GPU 利用率");
    graph.append(canvas);
    nodes.push(graph);
    content.replaceChildren(...nodes);
    const start = (row.started_at || row.first_observed_at) - 3, end = (row.ended_at || state.ts) + 3;
    requestAnimationFrame(() => plot(canvas, history, (r) => r.gpu.find((g) => g.uuid === gpuId)?.utilization ?? null, "#657d74", "%", start, Math.max(start + 10, end), 100));
  } else {
    nodes.push(section("请求统计", [["输入 Token", row.promptTokensCount ?? "—"], ["输出 Token", row.predictedTokensCount ?? "—"], ["平均解码", `${fmt(row.tokensPerSecond)} tok/s`], ["首 Token 延迟", `${fmt(row.timeToFirstTokenSec == null ? null : row.timeToFirstTokenSec * 1e3, 1)} ms`], ["总耗时", `${fmt(row.totalTimeSec, 2)} s`], ["结束原因", row.stopReason || "—"]], "该记录包含 API 口径的 Token 统计，不推测其对应的并发 slot。"));
    content.replaceChildren(...nodes);
  }
}
function download(value, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" })), a = el("a");
  a.href = url;
  a.download = `lmstudio-${name}-${Date.now()}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1e3);
}
async function loadHistory() {
  try {
    const response = await fetch("/api/history");
    if (!response.ok) throw Error();
    const records = await response.json();
    history = [...records.filter((r) => r.ts >= (historyRevision || 0)), ...history.filter((h) => h.ts > (records.at(-1)?.ts || 0))];
    draw();
  } catch {
    text("history-note", "历史读取失败；实时采集继续。");
  }
}
$("model-select").addEventListener("change", (e) => {
  modelId = e.target.value;
  localStorage.setItem("model", modelId);
  tableSignature = "";
  if (state) render(state);
});
$("gpu-select").addEventListener("change", (e) => {
  gpuId = e.target.value;
  draw();
  if (drawer) renderDrawer();
});
$("resource").addEventListener("change", draw);
$("range").addEventListener("change", (e) => {
  range = Number(e.target.value);
  draw();
});
$("pause").addEventListener("click", () => {
  pausedAt = pausedAt ? null : Date.now() / 1e3;
  text("pause", pausedAt ? "继续" : "暂停");
  $("pause").setAttribute("aria-pressed", String(!!pausedAt));
  draw();
});
$("status-filter").addEventListener("change", renderTable);
for (const [id, kind] of [["tasks-tab", "tasks"], ["summaries-tab", "summaries"]]) $(id).addEventListener("click", () => {
  view = kind;
  for (const k of ["tasks", "summaries"]) {
    $(`${k}-tab`).classList.toggle("selected", k === kind);
    $(`${k}-tab`).setAttribute("aria-pressed", String(k === kind));
  }
  if (state) renderTable();
});
$("clear-history").addEventListener("click", async () => {
  const button = $("clear-history");
  button.disabled = true;
  try {
    const response = await fetch("/api/history/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!response.ok) throw Error();
    const result = await response.json();
    render(result.state);
    $("history-feedback").hidden = false;
    text("history-feedback", `历史已清空，保留 ${result.active_preserved} 个正在运行的任务。`);
  } catch {
    $("history-feedback").hidden = false;
    text("history-feedback", "清空失败，请检查采集器连接后重试。");
  } finally {
    button.disabled = false;
  }
});
$("export").addEventListener("click", () => {
  if (state) download({ exported_at: (/* @__PURE__ */ new Date()).toISOString(), state, history }, "metrics");
});
$("model-info").addEventListener("click", () => {
  if (state) openDrawer({ type: "model" });
});
$("diagnostics").addEventListener("click", () => {
  if (state) openDrawer({ type: "diagnostics" });
});
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("close", () => {
  drawer = null;
});
const stream = new EventSource("/api/events");
stream.onmessage = (e) => {
  try {
    render(JSON.parse(e.data));
  } catch (error) {
    console.error("数据呈现失败", error);
  }
};
stream.onerror = () => {
  $("connection").className = "status warn";
  text("connection", "采集器断开，正在重连");
  text("decode", "—");
  text("decode-note", "数据停止更新");
  text("stage", "未连接");
  $("notice").hidden = false;
  text("notice", "采集器连接中断。页面数据为最后一次快照，连接恢复后自动更新。");
};
new ResizeObserver(draw).observe($("speed-chart").parentElement);
loadHistory();
