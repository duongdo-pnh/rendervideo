// Trang Kịch bản trả lời: quản lý intent + video trả lời (round-robin).
const $ = (id) => document.getElementById(id);
const api = async (p, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  return (await fetch(p, opt)).json();
};
let _intents = [], _selIntent = null;

async function loadProducts() {
  const d = await api("/api/products");
  $("a-product").innerHTML = '<option value="">(không gắn SP)</option>' +
    (d.products || []).map((p) => `<option value="${p.id}">#${p.id} ${p.name}</option>`).join("");
}
async function loadDownloads() {
  const d = await api("/api/downloads");
  $("a-file").innerHTML = (d.files || []).map((f) => `<option value="${f}">${f}</option>`).join("")
    || '<option value="">(downloads/ trống)</option>';
}

async function loadIntents() {
  const d = await api("/api/intents");
  _intents = d.intents || [];
  const body = $("i-body"); body.innerHTML = "";
  _intents.forEach((it) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50 hover:bg-panel2 cursor-pointer" + (it.id === _selIntent ? " bg-brand/10" : "");
    tr.innerHTML = `<td class="py-1.5 text-white" data-pick="${it.id}">${it.name}${it.enabled ? "" : " <span class='text-slate-600'>(tắt)</span>"}</td>
      <td class="text-slate-400">${(it.keywords || "").slice(0, 40)}</td>
      <td>${it.answer_count}</td>
      <td>${it.trigger_mode === "play_now" ? "ngay" : "đợi"}</td>`;
    body.appendChild(tr);
  });
}

function fillIntent(it) {
  $("i-id").value = it.id; $("i-name").value = it.name; $("i-keywords").value = it.keywords || "";
  $("i-trigger").value = it.trigger_mode; $("i-cooldown").value = it.cooldown_sec;
  $("i-enabled").checked = !!it.enabled; $("i-del").classList.remove("hidden"); $("i-msg").textContent = "";
}
function resetIntent() {
  $("i-id").value = ""; $("i-name").value = ""; $("i-keywords").value = "";
  $("i-trigger").value = "enqueue"; $("i-cooldown").value = "30"; $("i-enabled").checked = true;
  $("i-del").classList.add("hidden"); $("i-msg").textContent = "";
}

async function selectIntent(id) {
  _selIntent = id;
  const it = _intents.find((x) => x.id === id);
  if (it) { fillIntent(it); $("a-intent").textContent = "· " + it.name; }
  loadIntents(); loadAnswers();
}

async function loadAnswers() {
  const body = $("a-body"); body.innerHTML = "";
  if (!_selIntent) { body.innerHTML = '<tr><td colspan="5" class="text-slate-500 py-3">Chọn intent để xem video trả lời.</td></tr>'; return; }
  const d = await api("/api/answers?intent_id=" + _selIntent);
  (d.answers || []).forEach((a) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50" + (a.enabled ? "" : " opacity-50");
    tr.innerHTML = `<td class="py-1.5">${a.name}</td><td class="text-slate-400">${a.product || "—"}</td>
      <td>${a.play_count}</td><td class="text-slate-500">${a.last_played_at || "—"}</td>
      <td class="text-right space-x-1">
        <button data-play="${a.id}" class="bg-ok/80 hover:bg-ok text-white rounded px-2 py-0.5">▶ Thử</button>
        <button data-del="${a.id}" class="bg-live/70 hover:bg-live text-white rounded px-2 py-0.5">🗑</button>
      </td>`;
    body.appendChild(tr);
  });
}

$("i-new").onclick = resetIntent;
$("i-save").onclick = async () => {
  if (!$("i-name").value.trim()) { $("i-msg").textContent = "⚠ Cần tên intent"; $("i-msg").className = "text-xs text-live"; return; }
  const r = await api("/api/intents/save", {
    id: $("i-id").value || null, name: $("i-name").value, keywords: $("i-keywords").value,
    trigger_mode: $("i-trigger").value, cooldown_sec: +$("i-cooldown").value || 30, enabled: $("i-enabled").checked,
  });
  if (r.ok) { $("i-msg").textContent = "✅ Đã lưu"; $("i-msg").className = "text-xs text-ok"; if (!$("i-id").value) _selIntent = r.id; resetIntent(); loadIntents(); }
};
$("i-del").onclick = async () => {
  const id = $("i-id").value; if (!id || !confirm("Xóa intent này (kèm video trả lời)?")) return;
  await api("/api/intents/delete", { id: +id }); if (_selIntent == id) _selIntent = null;
  resetIntent(); loadIntents(); loadAnswers();
};
$("a-add").onclick = async () => {
  if (!_selIntent) { alert("Chọn intent trước"); return; }
  const f = $("a-file").value; if (!f) return;
  await api("/api/answers/add", { intent_id: _selIntent, file: f, product_id: $("a-product").value || null });
  loadAnswers(); loadIntents();
};

document.addEventListener("click", async (e) => {
  const pick = e.target.closest("[data-pick]");
  if (pick) { selectIntent(+pick.dataset.pick); return; }
  const play = e.target.closest("[data-play]");
  if (play) { const r = await api("/api/answers/play", { id: +play.dataset.play }); if (!r.ok && r.error) alert(r.error); loadAnswers(); return; }
  const del = e.target.closest("[data-del]");
  if (del) { await api("/api/answers/delete", { id: +del.dataset.del }); loadAnswers(); loadIntents(); return; }
});

loadProducts(); loadDownloads(); loadIntents(); loadAnswers();
