// Trang Scene Assets: thư viện nền/banner/tvc + cấu hình random + xem trước OBS thật.
const $ = (id) => document.getElementById(id);
const api = async (p, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  return (await fetch(p, opt)).json();
};
const KINDS = ["background", "banner", "table", "tvc"];
const ROT = ["background", "banner", "table", "tvc"];   // loại có tự-đổi theo thời gian
let _applied = {};  // {kind: asset_id} đang thực sự lên OBS

async function loadAssets(kind) {
  const d = await api("/api/assets?kind=" + kind);
  _applied = d.applied || _applied;
  const grid = document.querySelector(`[data-grid="${kind}"]`);
  const items = d.assets || [];
  document.querySelector(`[data-empty="${kind}"]`).classList.toggle("hidden", items.length > 0);
  grid.innerHTML = items.map((a) => {
    const media = a.is_video
      ? `<video src="${a.url}" class="w-full h-20 object-cover rounded bg-black" muted preload="metadata"></video>`
      : `<img src="${a.url}" class="w-full h-20 object-cover rounded bg-black">`;
    const onAir = _applied[kind] === a.id;
    const badge = onAir ? `<span class="absolute top-1 left-1 text-[9px] bg-live text-white rounded px-1">🔴 ĐANG PHÁT</span>` : "";
    return `<div class="relative ${a.enabled ? "" : "opacity-40"}">
      <div data-use="${a.id}" class="cursor-pointer hover:ring-2 hover:ring-brand rounded" title="Bấm để PHÁT cái này lên OBS">${media}${badge}</div>
      <div class="text-[10px] text-slate-400 truncate mt-0.5">${a.name || ""}</div>
      <div class="flex gap-1 mt-0.5">
        <button data-toggle="${a.id}" data-on="${a.enabled ? 1 : 0}" title="Bật = đưa vào nhóm để dùng/random. Tắt = bỏ qua."
          class="flex-1 text-[10px] rounded py-0.5 ${a.enabled ? "bg-ok text-white" : "bg-panel2 text-slate-400 border border-line"}">
          ${a.enabled ? "✓ Bật" : "Tắt"}</button>
        <button data-del="${a.id}" class="text-[10px] bg-live/60 hover:bg-live text-white rounded px-1.5">🗑</button>
      </div>
    </div>`;
  }).join("");
}
function loadAll() { KINDS.forEach(loadAssets); }

// upload (nhiều file) — tự áp lên OBS ngay (backend auto-apply)
document.querySelectorAll("[data-up]").forEach((inp) => {
  inp.addEventListener("change", async (e) => {
    const kind = inp.dataset.up;
    for (const f of e.target.files) {
      const fd = new FormData(); fd.append("kind", kind); fd.append("file", f);
      const r = await (await fetch("/api/assets/upload", { method: "POST", body: fd })).json();
      if (!r.ok) alert("Lỗi: " + (r.error || ""));
    }
    e.target.value = ""; loadAll();
  });
});

// toggle / delete
document.addEventListener("click", async (e) => {
  const use = e.target.closest("[data-use]");
  if (use) {   // click thumbnail -> phát cái này lên OBS
    const r = await api("/api/scene/apply_one", { id: +use.dataset.use });
    if (r.ok) { msg("✅ Đã phát asset này lên OBS", "ok"); loadAll(); }
    else msg("❌ " + (r.error || "lỗi"), "live");
    return;
  }
  const t = e.target.closest("button"); if (!t) return;
  if (t.dataset.toggle) {
    await api("/api/assets/toggle", { id: +t.dataset.toggle, enabled: t.dataset.on !== "1" });
    loadAll();
  } else if (t.dataset.del) {
    if (confirm("Xóa asset này?")) { await api("/api/assets/delete", { id: +t.dataset.del }); loadAll(); }
  }
});

// ---- random settings ----
const BOOL = ["random_bg", "random_banner", "random_tvc", "random_table"];
async function loadSettings() {
  const d = await api("/api/scene/random_settings");
  const s = d.settings || {};
  BOOL.forEach((k) => ($("r-" + k).checked = !!s[k]));
  $("r-seed_mode").value = s.seed_mode || "auto";
  $("r-seed_value").value = s.seed_value || "";
  ROT.forEach((k) => {                                   // tự-đổi theo thời gian
    $("rot-" + k + "-en").checked = !!s[k + "_rotate_enabled"];
    const iv = +(s[k + "_rotate_interval"] || 300);
    if (iv % 60 === 0) { $("rot-" + k + "-num").value = iv / 60; $("rot-" + k + "-unit").value = "60"; }
    else { $("rot-" + k + "-num").value = iv; $("rot-" + k + "-unit").value = "1"; }
  });
}
$("r-save").onclick = async () => {
  const body = {};
  BOOL.forEach((k) => (body[k] = $("r-" + k).checked));
  body.seed_mode = $("r-seed_mode").value;
  body.seed_value = $("r-seed_value").value.trim();
  ROT.forEach((k) => {                                   // tự-đổi theo thời gian
    body[k + "_rotate_enabled"] = $("rot-" + k + "-en").checked;
    let iv = (+$("rot-" + k + "-num").value || 5) * (+$("rot-" + k + "-unit").value || 60);
    body[k + "_rotate_interval"] = Math.max(10, Math.min(3600, iv));
  });
  await api("/api/scene/random_settings", body);
  msg("✅ Đã lưu cấu hình (tự-đổi áp ngay nếu đang stream)", "ok");
};

// ---- đếm ngược tự-đổi asset (đọc /api/status) ----
setInterval(async () => {
  try {
    const s = await (await fetch("/api/status")).json();
    const rs = s.rotate_status || {};
    ROT.forEach((k) => {
      const el = $("rot-" + k + "-cd"); if (!el) return;
      const r = rs[k] || {};
      if (!r.enabled) { el.textContent = ""; return; }
      if (r.next_in == null) { el.textContent = "(chờ stream)"; el.className = "text-slate-500 ml-auto"; return; }
      const m = Math.floor(r.next_in / 60), sec = r.next_in % 60;
      el.textContent = "🔄 " + m + ":" + String(sec).padStart(2, "0");
      el.className = "text-brand ml-auto";
    });
  } catch (e) {}
}, 1000);

// ---- xem trước = canvas OBS thật (tự refresh) ----
setInterval(() => {
  const img = $("pv-obs");
  if (img) { img.src = "/api/preview.jpg?t=" + Date.now(); }
}, 1500);
$("pv-obs").addEventListener("load", () => $("pv-empty").classList.add("hidden"));

// ---- đổi combo ngẫu nhiên (áp thẳng lên OBS) ----
$("btn-random").onclick = async () => {
  msg("Đang áp combo ngẫu nhiên...", "slate");
  const d = await api("/api/scene/apply", {});
  if (d.ok) {
    const p = d.pick || {};
    const names = ["background", "banner", "tvc"].filter((k) => p[k]).map((k) => p[k].name);
    $("pv-info").textContent = "Combo: " + (names.join(" · ") || "—");
    msg("✅ Đã áp lên OBS", "ok");
    loadAll();   // cập nhật badge "đang phát"
  } else msg("❌ " + (d.error || d.message || "lỗi"), "live");
};

$("btn-ensure").onclick = async () => {
  const d = await api("/api/scene/ensure_sources", {});
  msg(d.ok ? "✅ Đã tạo/đảm bảo source trong OBS" : "❌ Không tạo được (OBS chưa mở?)", d.ok ? "ok" : "live");
};
$("btn-reset-layout").onclick = async () => {
  if (!confirm("Đặt lại vị trí/kích thước về mặc định? (ghi đè những gì bạn đã chỉnh tay trong OBS)")) return;
  const d = await api("/api/scene/reset_layout", {});
  msg(d.ok ? "✅ Đã reset layout mặc định" : "❌ Reset lỗi", d.ok ? "ok" : "live");
};
function msg(t, cls) { const m = $("msg"); m.textContent = t; m.className = "text-xs text-" + (cls === "ok" ? "ok" : cls === "live" ? "live" : "slate-400"); }

loadAll(); loadSettings();
