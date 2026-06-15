// Trang Lịch live: tạo/sửa phiên + start/stop/cancel + cảnh báo trùng giờ.
const $ = (id) => document.getElementById(id);
const api = async (p, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  return (await fetch(p, opt)).json();
};
const FIELDS = ["name", "platform", "rtmp_server", "stream_key", "scene", "start_at", "end_at"];
const FLAGS = ["auto_start", "auto_stop", "auto_recover"];
const PRESET = {
  YouTube: "rtmp://a.rtmp.youtube.com/live2",
  Facebook: "rtmps://live-api-s.facebook.com:443/rtmp/",
  TikTok: "", Shopee: "", Custom: "",
};
const STATUS = { scheduled: "⏳ Chờ", live: "🔴 LIVE", ended: "✓ Kết thúc",
                 canceled: "Đã hủy", error: "❌ Lỗi", skipped: "⏭ Bỏ qua" };
let _sessions = [];

async function loadScenes() {
  const d = await api("/api/scenes");
  $("f-scene").innerHTML = '<option value="">(scene mặc định)</option>' +
    (d.scenes || []).map((s) => `<option value="${s}">${s}</option>`).join("");
}

async function loadPlaylistOptions() {
  const d = await api("/api/playlists");
  $("f-pl_id").innerHTML = '<option value="">(playlist mặc định)</option>' +
    (d.playlists || []).map((p) => `<option value="${p.id}">${p.name} (${p.count})</option>`).join("");
}

async function loadProfiles() {
  const d = await api("/api/profiles");
  $("f-profile").innerHTML = '<option value="">(giữ profile hiện tại)</option>' +
    (d.profiles || []).map((p) => `<option value="${p}">${p}</option>`).join("");
}

async function loadSessions() {
  const d = await api("/api/sessions");
  _sessions = d.sessions || [];
  const body = $("sess-body"); body.innerHTML = "";
  _sessions.forEach((s) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50";
    const btns = [];
    if (s.status === "live") btns.push(`<button data-stop="${s.id}" class="bg-live/80 hover:bg-live text-white rounded px-2 py-0.5">Stop</button>`);
    else btns.push(`<button data-start="${s.id}" class="bg-ok/80 hover:bg-ok text-white rounded px-2 py-0.5">Start</button>`);
    if (["scheduled"].includes(s.status)) btns.push(`<button data-cancel="${s.id}" class="bg-panel2 hover:bg-line rounded px-2 py-0.5">Hủy</button>`);
    if (["ended","canceled","error","skipped"].includes(s.status)) btns.push(`<button data-resched="${s.id}" class="bg-panel2 hover:bg-line rounded px-2 py-0.5">Lên lại</button>`);
    btns.push(`<button data-del="${s.id}" class="bg-live/60 hover:bg-live text-white rounded px-2 py-0.5">🗑</button>`);
    tr.innerHTML = `<td class="py-1.5 text-white cursor-pointer" data-edit="${s.id}">${s.name}</td>
      <td class="text-slate-400">${s.platform || ""}</td><td>${s.start_at || "—"}</td><td>${s.end_at || "—"}</td>
      <td>${STATUS[s.status] || s.status}</td><td class="text-right space-x-1">${btns.join("")}</td>`;
    body.appendChild(tr);
  });
}

function fillForm(s) {
  $("f-id").value = s.id;
  FIELDS.forEach((k) => ($("f-" + k).value = (s[k] || "").replace(" ", "T").slice(0, 16) || s[k] || ""));
  $("f-name").value = s.name; $("f-platform").value = s.platform || "Custom";
  $("f-rtmp_server").value = s.rtmp_server || ""; $("f-stream_key").value = "";  // không prefill key (đã mask)
  $("f-scene").value = s.scene || "";
  $("f-pl_id").value = s.pl_id || "";
  $("f-profile").value = s.profile || "";
  $("f-start_at").value = (s.start_at || "").replace(" ", "T").slice(0, 16);
  $("f-end_at").value = (s.end_at || "").replace(" ", "T").slice(0, 16);
  FLAGS.forEach((k) => ($("f-" + k).checked = !!s[k]));
  $("form-title").textContent = "SỬA PHIÊN #" + s.id;
  $("del-btn").classList.remove("hidden");
  $("f-stream_key").placeholder = "Stream key (để trống = giữ key cũ)";
  $("msg").textContent = ""; checkOverlap();
}

function resetForm() {
  $("f-id").value = ""; FIELDS.forEach((k) => ($("f-" + k).value = ""));
  $("f-pl_id").value = ""; $("f-profile").value = "";
  $("f-platform").value = "TikTok"; $("f-stream_key").placeholder = "Stream key";
  FLAGS.forEach((k) => ($("f-" + k).checked = true));
  $("form-title").textContent = "TẠO PHIÊN LIVE"; $("del-btn").classList.add("hidden");
  $("msg").textContent = ""; $("overlap").classList.add("hidden");
}

function checkOverlap() {
  const s = $("f-start_at").value, e = $("f-end_at").value, id = $("f-id").value;
  if (!s || !e) { $("overlap").classList.add("hidden"); return; }
  const hit = _sessions.some((x) => String(x.id) !== id &&
    ["scheduled", "live"].includes(x.status) && x.start_at && x.end_at &&
    s.replace("T", " ") < x.end_at && x.start_at < e.replace("T", " "));
  $("overlap").classList.toggle("hidden", !hit);
}

$("f-platform").onchange = () => {
  const p = $("f-platform").value;
  if (PRESET[p] && !$("f-rtmp_server").value) $("f-rtmp_server").value = PRESET[p];
};
$("f-start_at").onchange = checkOverlap;
$("f-end_at").onchange = checkOverlap;
$("key-eye").onclick = () => {
  const k = $("f-stream_key"); k.type = k.type === "password" ? "text" : "password";
};
$("new-btn").onclick = resetForm;

$("save-btn").onclick = async () => {
  if (!$("f-name").value.trim()) { $("msg").textContent = "⚠ Cần tên phiên"; $("msg").className = "text-xs text-live"; return; }
  const body = { id: $("f-id").value || null, pl_id: $("f-pl_id").value || null,
                 profile: $("f-profile").value || null };
  FIELDS.forEach((k) => (body[k] = $("f-" + k).value));
  // nếu sửa mà để trống key -> bỏ field để giữ key cũ
  if (body.id && !body.stream_key) delete body.stream_key;
  FLAGS.forEach((k) => (body[k] = $("f-" + k).checked));
  const r = await api("/api/sessions/save", body);
  if (r.ok) { $("msg").textContent = "✅ Đã lưu"; $("msg").className = "text-xs text-ok"; resetForm(); loadSessions(); }
  else { $("msg").textContent = "❌ " + (r.error || "lỗi"); $("msg").className = "text-xs text-live"; }
};

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-edit],[data-start],[data-stop],[data-cancel],[data-resched],[data-del]");
  if (!t) return;
  if (t.dataset.edit) { fillForm(_sessions.find((x) => String(x.id) === t.dataset.edit)); return; }
  if (t.dataset.del) { if (confirm("Xóa phiên này?")) { await api("/api/sessions/delete", { id: +t.dataset.del }); loadSessions(); } return; }
  const map = { start: "start", stop: "stop", cancel: "cancel", resched: "reschedule" };
  for (const k in map) if (t.dataset[k]) {
    const r = await api(`/api/sessions/${+t.dataset[k]}/${map[k]}`);
    if (!r.ok && r.error) alert(r.error);
    loadSessions(); return;
  }
});

loadScenes(); loadPlaylistOptions(); loadProfiles(); loadSessions();
setInterval(loadSessions, 5000);  // cập nhật trạng thái phiên
