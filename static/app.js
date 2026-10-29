const $ = (selector) => document.querySelector(selector);
let sessionId = sessionStorage.getItem("rag-session-id") || null;
const json = async (url, options = {}) => {
  const response = await fetch(url, { headers: { "content-type": "application/json" }, ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "请求失败");
  return body;
};
const setStatus = async () => {
  try { const data = await json("/api/status"); $("#status").textContent = "知识库 " + data.chunk_count + " 个切片 · " + data.source_files.join("、"); }
  catch { $("#status").textContent = "无法连接后端"; }
};
const renderSyncLog = (data) => {
  const sync = data.sync, log = $("#syncLog");
  const records = [
    ...sync.added_documents.map(name => ({ action: "新增", name, detail: "已切片并向量化" })),
    ...sync.updated_documents.map(name => ({ action: "更新", name, detail: "旧切片已替换并重新向量化" })),
    ...sync.deleted_documents.map(name => ({ action: "删除", name, detail: "已从 Milvus 清理对应切片" })),
    ...sync.unchanged_documents.map(name => ({ action: "跳过", name, detail: "内容和切分配置未变化，未调用 embedding" })),
  ];
  log.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "本次调整日志";
  log.append(title);
  const meta = document.createElement("small");
  meta.textContent = new Date().toLocaleTimeString() + " · 耗时 " + data.duration_ms + "ms";
  log.append(meta);
  const list = document.createElement("ul");
  records.forEach(record => {
    const item = document.createElement("li");
    item.textContent = "[" + record.action + "] " + record.name + " — " + record.detail;
    list.append(item);
  });
  if (!records.length) { const item = document.createElement("p"); item.textContent = "没有检测到可同步的文档。"; log.append(item); }
  else log.append(list);
};
$("#ingest").onclick = async () => {
  const button = $("#ingest"); button.disabled = true; button.textContent = "正在向量化…";
  try {
    const data = await json("/api/ingest", { method: "POST", body: JSON.stringify({ strategy: $("#strategy").value, chunk_size: Number($("#chunkSize").value), overlap: Number($("#overlap").value) }) });
    const sync = data.sync;
    const summary = "新增 " + sync.added_documents.length + " 篇 · 更新 " + sync.updated_documents.length + " 篇 · 删除 " + sync.deleted_documents.length + " 篇 · 跳过 " + sync.unchanged_documents.length + " 篇 · 本次向量化 " + sync.embedded_chunks + " 个切片";
    const preview = data.preview.length ? "<br>" + data.preview.map(x => "<span>" + x.heading_path + "</span> " + x.text.slice(0, 96) + "…").join("<br>") : "";
    $("#preview").innerHTML = "<strong>知识库共 " + data.chunk_count + " 个切片</strong> · " + summary + " · " + data.duration_ms + "ms" + preview;
    renderSyncLog(data);
    await setStatus();
  } catch (error) { $("#preview").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "增量同步知识库"; }
};
$("#ask").onclick = async () => {
  const question = $("#question").value.trim();
  if (!question) return;
  const button = $("#ask"), answer = $("#answer"); button.disabled = true; answer.className = "answer"; answer.textContent = "正在通过 LangGraph 检索与生成…"; $("#trace").innerHTML = ""; $("#citations").innerHTML = "";
  try {
    const data = await json("/api/ask", { method: "POST", body: JSON.stringify({ question, rewrite: $("#rewrite").checked, top_k: 4, session_id: sessionId }) });
    sessionId = data.session_id; sessionStorage.setItem("rag-session-id", sessionId);
    addMessage("用户", question); addMessage("助手", data.answer); $("#question").value = "";
    answer.textContent = data.answer; if (data.warning) answer.insertAdjacentHTML("beforeend", "<p class='warning'>" + data.warning + "</p>");
    $("#citations").innerHTML = data.citations.map(c => "<article><b>" + c.heading_path + "</b><small>" + c.chunk_id + " · score " + c.score + "</small><p>" + c.text + "</p></article>").join("");
    data.trace.forEach(event => { const t = $("#traceItem").content.cloneNode(true); t.querySelector("summary").textContent = event.node + " · " + event.duration_ms + "ms"; t.querySelector("pre").textContent = JSON.stringify(event.details, null, 2); $("#trace").append(t); });
  } catch (error) { answer.textContent = error.message; }
  finally { button.disabled = false; }
};
const addMessage = (role, content) => {
  const item = document.createElement("article"); item.className = "message " + (role === "用户" ? "user" : "assistant");
  const label = document.createElement("b"); label.textContent = role;
  const text = document.createElement("p"); text.textContent = content;
  item.append(label, text); $("#messages").append(item); item.scrollIntoView({ block: "nearest" });
};
$("#newConversation").onclick = async () => {
  if (sessionId) await fetch("/api/conversations/" + encodeURIComponent(sessionId), { method: "DELETE" });
  sessionId = null; sessionStorage.removeItem("rag-session-id");
  $("#messages").innerHTML = ""; $("#answer").className = "answer empty"; $("#answer").textContent = "已开始新会话。"; $("#citations").innerHTML = ""; $("#trace").innerHTML = "";
};
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("#ask").click(); }
});
let editingScriptId = null, scripts = [];
const scriptField = (id) => $("#" + id).value.trim();
const scriptPayload = () => ({ title: scriptField("scriptTitle"), intent: scriptField("scriptIntent") || "通用", scene: scriptField("scriptScene") || "售前", status: $("#scriptPublish").value, operator: scriptField("scriptOperator") || "运营", content: scriptField("scriptContent") });
const resetScriptForm = () => { editingScriptId = null; $("#scriptForm").reset(); $("#scriptIntent").value = "通用"; $("#scriptScene").value = "售前"; $("#scriptOperator").value = "运营"; $("#scriptPublish").value = "draft"; $("#saveScript").textContent = "保存话术"; };
const setScriptLog = (message) => { $("#scriptLog").textContent = message; };
const renderScripts = () => {
  const root = $("#scriptList"); root.replaceChildren();
  scripts.forEach(script => {
    const item = document.createElement("article"); item.className = "script-item";
    const body = document.createElement("div"), title = document.createElement("h3"), tag = document.createElement("span"), meta = document.createElement("div"), content = document.createElement("p");
    title.textContent = script.title + " "; tag.className = "status-tag " + script.status; tag.textContent = ({ draft: "草稿", published: "已发布", offline: "已下线" })[script.status]; title.append(tag);
    meta.className = "script-meta"; meta.textContent = script.scene + " · " + script.intent + " · v" + script.version + " · " + script.operator + " · " + new Date(script.updated_at).toLocaleString();
    content.textContent = script.content; body.append(title, meta, content);
    const actions = document.createElement("div"); actions.className = "script-buttons";
    const edit = document.createElement("button"); edit.className = "secondary"; edit.textContent = "编辑"; edit.onclick = () => { editingScriptId = script.id; $("#scriptTitle").value = script.title; $("#scriptIntent").value = script.intent; $("#scriptScene").value = script.scene; $("#scriptPublish").value = script.status; $("#scriptOperator").value = script.operator; $("#scriptContent").value = script.content; $("#saveScript").textContent = "保存修改"; $("#scriptTitle").focus(); };
    const remove = document.createElement("button"); remove.className = "secondary"; remove.textContent = "删除"; remove.onclick = async () => { if (!confirm("确定删除这条话术？对应向量也会移除。")) return; try { await json("/api/scripts/" + encodeURIComponent(script.id), { method: "DELETE" }); setScriptLog("已删除并移除对应向量。"); await loadScripts(); } catch (error) { setScriptLog(error.message); } };
    actions.append(edit, remove); item.append(body, actions); root.append(item);
  });
};
const loadScripts = async () => { try { const data = await json("/api/scripts"); scripts = data.items; $("#scriptStatus").textContent = "共 " + data.total + " 条话术"; renderScripts(); } catch (error) { $("#scriptStatus").textContent = error.message; } };
$("#scriptForm").onsubmit = async (event) => { event.preventDefault(); const button = $("#saveScript"); button.disabled = true; try { const method = editingScriptId ? "PUT" : "POST", url = editingScriptId ? "/api/scripts/" + encodeURIComponent(editingScriptId) : "/api/scripts"; const saved = await json(url, { method, body: JSON.stringify(scriptPayload()) }); setScriptLog(saved.status === "published" ? "已保存并同步到 Milvus。" : "已保存；非发布状态不会参与检索。"); resetScriptForm(); await loadScripts(); } catch (error) { setScriptLog(error.message); } finally { button.disabled = false; } };
$("#cancelEdit").onclick = resetScriptForm;
$("#importScripts").onclick = async () => { const file = $("#scriptFile").files[0]; if (!file) return setScriptLog("请先选择 CSV 或 Excel 文件。"); const button = $("#importScripts"); button.disabled = true; try { const form = new FormData(); form.append("file", file); const response = await fetch("/api/scripts/import", { method: "POST", body: form }); const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)); setScriptLog("导入完成：新增 " + data.created + " 条，更新 " + data.updated + " 条，其中已发布并同步 " + data.published_synced + " 条。"); $("#scriptFile").value = ""; await loadScripts(); } catch (error) { setScriptLog(error.message); } finally { button.disabled = false; } };
setStatus();
loadScripts();
