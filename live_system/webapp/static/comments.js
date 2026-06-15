// Trang Comment live: chọn tài khoản, start/stop scanner, hiển thị comment realtime (poll theo seq).
const $ = (id) => document.getElementById(id);
let sinceSeq = 0;
let timer = null;

function esc(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function fmtTs(ts) {
  if (!ts) return "";
  let ms = Number(ts);
  if (ms < 1e12) ms *= 1000;            // giây -> ms
  const d = new Date(ms);
  return isNaN(d) ? "" : d.toLocaleTimeString("vi-VN");
}

async function loadCodes() {
  const d = await (await fetch("/api/shopee/cookies")).json();
  const sel = $("code");
  sel.innerHTML = "";
  (d.cookies || []).forEach((c) => {
    const o = document.createElement("option");
    o.value = c.code;
    o.textContent = `${c.code} · ${c.domain || ""} (${c.cookie_len} ký tự)`;
    sel.appendChild(o);
  });
  if (!(d.cookies || []).length) {
    const o = document.createElement("option");
    o.textContent = "Chưa có cookie — lấy qua extension";
    sel.appendChild(o);
  }
}

function renderStatus(s) {
  $("st-run").textContent = s.running ? "🟢 Đang quét" : "⚪ Dừng";
  $("st-run").className = "font-semibold " + (s.running ? "text-ok" : "text-slate-400");
  $("st-session").textContent = s.session_id ? `${s.session_id} — ${s.session_title || ""}` : "—";
  $("st-count").textContent = s.count || 0;
  $("st-poll").textContent = s.last_poll ? fmtTs(s.last_poll) : "—";
  $("st-err").textContent = s.error || "";
}

function matchBadge(m) {
  if (!m) return "";
  if (m.method === "error")
    return `<span class="text-live text-[10px]">⚠ ${esc(m.reason || "")}</span>`;
  if (!m.matched) return `<span class="text-slate-600 text-[10px]">❌ no match</span>`;
  const icon = m.method === "ai" ? "🤖" : "✅";
  const parts = [];
  if (m.product) parts.push(esc(m.product.name));
  else if (m.ctx_product) parts.push(esc(m.ctx_product) + "(ctx)");
  if (m.intent) parts.push("·" + m.intent.replace("ASK_", ""));
  let tail = "";
  if (m.triggered) tail = ` · ▶ ${esc(m.video || "")}`;
  else if (m.skipped) tail = ` · ⏸ ${m.skipped}`;
  const color = m.triggered ? "text-ok" : "text-amber-400";
  return `<span class="${color} text-[10px]">${icon} ${parts.join(" ")}${tail}</span>`;
}

function appendComments(list) {
  const feed = $("feed");
  list.forEach((c) => {
    sinceSeq = Math.max(sinceSeq, c.seq);
    const row = document.createElement("div");
    row.className = "bg-panel2 rounded px-2 py-1.5";
    row.innerHTML = `<span class="text-brand font-semibold">${esc(c.user)}</span>
      <span class="text-[10px] text-slate-500 ml-1">${fmtTs(c.ts)}</span>
      <span class="ml-1">${matchBadge(c.match)}</span>
      <div class="text-slate-200">${esc(c.content)}</div>`;
    feed.appendChild(row);
  });
  if (list.length && $("autoscroll").checked) feed.scrollTop = feed.scrollHeight;
}

async function poll() {
  try {
    const d = await (await fetch(`/api/shopee/scan/comments?since=${sinceSeq}`)).json();
    renderStatus(d);
    if (d.comments && d.comments.length) appendComments(d.comments);
  } catch (e) { /* bỏ qua, thử lại vòng sau */ }
}

function startPolling() {
  if (timer) return;
  poll();
  timer = setInterval(poll, 1500);
}

$("start-btn").onclick = async () => {
  const code = $("code").value;
  $("feed").innerHTML = ""; sinceSeq = 0;
  const r = await (await fetch("/api/shopee/scan/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  })).json();
  renderStatus(r);
  startPolling();
};

$("stop-btn").onclick = async () => {
  const r = await (await fetch("/api/shopee/scan/stop", { method: "POST" })).json();
  renderStatus(r);
};

// ---- TEST COMMENT ----
$("test-btn").onclick = async () => {
  const content = $("test-input").value.trim();
  if (!content) return;
  $("test-result").innerHTML = "<span class='text-slate-500'>⏳ đang phân tích...</span>";
  const r = await (await fetch("/api/comment/test", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, trigger: $("test-trigger").checked }),
  })).json();
  if (!r.ok) { $("test-result").innerHTML = `<span class='text-live'>❌ ${esc(r.error || "lỗi")}</span>`; return; }
  showMatchResult(r);
};

const SCOPE_LABEL = { product: "video product+intent", general: "video chung của intent",
  shop: "video chung của shop", intro: "video giới thiệu SP", intro_ctx: "video giới thiệu SP (context)" };

function showMatchResult(r) {
  let head;
  if (r.method === "error") head = `<div class='text-live'>⚠ ${esc(r.reason || "lỗi")}</div>`;
  else if (!r.matched) head = `<div class='text-slate-400'>❌ Không match (sẽ bỏ qua / đưa hàng chờ NV)</div>`;
  else {
    const icon = r.method === "ai" ? "🤖" : "✅";
    const pr = r.product ? esc(r.product.name) : (r.ctx_product ? esc(r.ctx_product) + " (context)" : "—");
    const it = r.intent ? r.intent : "(không rõ intent)";
    head = `<div class='text-ok'>${icon} SP: ${pr} · Intent: ${it}</div>`;
  }
  const lines = [];
  lines.push(`Method: ${esc(r.method)} · Confidence: ${r.confidence}%`);
  if (r.video) lines.push(`Video: ${esc(r.video)} (${SCOPE_LABEL[r.scope] || r.scope || ""})`);
  else if (r.matched) lines.push("Chưa có video phù hợp → cần thêm answer video / video SP");
  if (r.triggered) lines.push("▶ ĐÃ phát lên OBS");
  else if (r.skipped) lines.push("⏸ Bỏ qua: " + r.skipped);
  else if (r.would_play) lines.push("(xem trước — sẽ phát: " + esc(r.would_play) + ")");
  $("test-result").innerHTML = head +
    `<div class='text-[11px] text-slate-500 mt-0.5'>${lines.join(" · ")}</div>` +
    (r.reason ? `<div class='text-[11px] text-slate-500'>Lý do AI: ${esc(r.reason)}</div>` : "");
}

// ---- Giả lập comment LIVE (test local): khớp + trigger OBS + hiện trong feed ----
$("inject-btn").onclick = async () => {
  const content = $("test-input").value.trim();
  if (!content) return;
  $("test-result").innerHTML = "<span class='text-slate-500'>⏳ đang giả lập...</span>";
  const r = await (await fetch("/api/comment/inject", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  })).json();
  if (!r.ok) { $("test-result").innerHTML = `<span class='text-live'>❌ ${esc(r.error || "lỗi")}</span>`; return; }
  showMatchResult(r);
  startPolling();   // kéo comment vừa giả lập vào feed
  poll();
};

// ---- AI config ----
async function loadConfig() {
  const c = await (await fetch("/api/comment/ai_config")).json();
  $("cfg-ai").checked = c.ai_enabled;
  $("cfg-auto").checked = c.auto_trigger;
  $("cfg-mode").value = c.trigger_mode || "play_now";
  $("cfg-pick").value = c.video_pick || "rotate";
  if ($("cfg-cd")) $("cfg-cd").value = c.cooldown;
  $("cfg-thr").value = c.threshold;
  $("cfg-model").value = c.model;
  $("cfg-key").placeholder = c.has_key ? "•••• đã lưu (để trống = giữ)" : "sk-...";
}
$("cfg-save").onclick = async () => {
  const body = {
    ai_enabled: $("cfg-ai").checked, auto_trigger: $("cfg-auto").checked,
    trigger_mode: $("cfg-mode").value, video_pick: $("cfg-pick").value,
    cooldown: $("cfg-cd") ? parseInt($("cfg-cd").value) : undefined,
    threshold: parseFloat($("cfg-thr").value), model: $("cfg-model").value.trim(),
  };
  if ($("cfg-key").value.trim()) body.api_key = $("cfg-key").value.trim();
  const r = await (await fetch("/api/comment/ai_config", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })).json();
  $("cfg-key").value = "";
  $("cfg-status").textContent = r.has_key ? "✅ Đã lưu (có API key)" : "✅ Đã lưu (chưa có key → tầng 2 tắt)";
  $("cfg-status").className = "text-[11px] " + (r.has_key ? "text-ok" : "text-amber-400");
  loadConfig();
};

// ---- triggers ----
async function loadTriggers() {
  const d = await (await fetch("/api/triggers")).json();
  const sel = $("trg-product"); sel.innerHTML = "";
  (d.products || []).forEach((p) => {
    const o = document.createElement("option"); o.value = p.id; o.textContent = `#${p.id} ${p.name}`;
    sel.appendChild(o);
  });
  const body = $("trg-body"); body.innerHTML = "";
  (d.triggers || []).forEach((t) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/40";
    tr.innerHTML = `<td class="py-1 text-slate-300">${esc(t.product_name)}</td>
      <td class="text-white">${esc(t.keyword)}</td>
      <td><button data-del="${t.id}" class="text-live hover:text-red-400">🗑</button></td>`;
    body.appendChild(tr);
  });
}
$("trg-add").onclick = async () => {
  const product_id = $("trg-product").value, keyword = $("trg-keyword").value.trim();
  if (!product_id || !keyword) return;
  await fetch("/api/triggers/add", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product_id: +product_id, keyword }),
  });
  $("trg-keyword").value = ""; loadTriggers();
};
$("trg-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]"); if (!b) return;
  await fetch("/api/triggers/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: +b.dataset.del }),
  });
  loadTriggers();
});

(async () => {
  await loadCodes();
  await loadConfig();
  await loadTriggers();
  // Luôn poll feed: comment đến từ extension (cào thật) hoặc giả lập đều hiện, kể cả khi
  // server-scanner chưa "Bắt đầu quét".
  const s = await (await fetch("/api/shopee/scan/status")).json();
  renderStatus(s);
  startPolling();
})();
