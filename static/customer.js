const $ = (selector) => document.querySelector(selector);
const storageKey = "rag-customer-session";
let sessionId = sessionStorage.getItem(storageKey) || null;
const messages = $("#customerMessages");

function addMessage(role, text, sources = []) {
  const item = document.createElement("article");
  item.className = "message " + role;
  const body = document.createElement("div");
  body.textContent = text;
  item.append(body);
  if (sources.length) {
    const note = document.createElement("small");
    note.textContent = "参考：" + sources.map(source => source.heading_path).join("；");
    item.append(note);
  }
  messages.append(item);
  item.scrollIntoView({ block: "end", behavior: "smooth" });
}

function greet() {
  addMessage("assistant", "你好呀，我是小衣橱客服。想帮宝宝看尺码、材质，还是了解退换货？");
}

async function ask(question) {
  if (!question) return;
  addMessage("user", question);
  const input = $("#customerInput"), button = $("#customerSend");
  input.value = ""; input.style.height = "auto"; button.disabled = true;
  const typing = document.createElement("p"); typing.className = "typing"; typing.textContent = "正在查找店铺资料…"; messages.append(typing);
  try {
    const response = await fetch("/api/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question, rewrite: true, top_k: 3, session_id: sessionId }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "暂时无法回答，请稍后再试。");
    sessionId = data.session_id; sessionStorage.setItem(storageKey, sessionId);
    addMessage("assistant", data.answer, data.citations || []);
  } catch (error) {
    addMessage("assistant", error.message || "暂时无法回答，请稍后再试。");
  } finally {
    typing.remove(); button.disabled = false; input.focus();
  }
}

$("#customerForm").addEventListener("submit", event => { event.preventDefault(); ask($("#customerInput").value.trim()); });
$("#customerInput").addEventListener("input", event => { event.target.style.height = "auto"; event.target.style.height = Math.min(event.target.scrollHeight, 110) + "px"; });
document.querySelectorAll("[data-question]").forEach(button => button.addEventListener("click", () => ask(button.dataset.question)));
$("#customerNew").addEventListener("click", async () => { if (sessionId) await fetch("/api/conversations/" + encodeURIComponent(sessionId), { method: "DELETE" }); sessionId = null; sessionStorage.removeItem(storageKey); messages.replaceChildren(); greet(); });
greet();
