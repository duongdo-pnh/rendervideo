// Relive Studio dashboard — WebSocket realtime + control actions.
const $ = (id) => document.getElementById(id);
let lastBytes = null, lastTs = null, scenesLoaded = false;

function fmtDur(ms) {
  let s = Math.floor((ms || 0) / 1000);
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  s %= 3600;
  const m = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${h}:${m}:${ss}`;
}
function fmtVND(v) { return v == null ? "—" : Number(v).toLocaleString("vi-VN") + "đ"; }

async function control(action, payload) {
  try {
    const r = await fetch(`/api/control/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    return await r.json();
  } catch (e) { console.error(e); }
}

// ---- preview image refresh ----
setInterval(() => {
  const img = $("preview");
  if (img) img.src = `/api/preview.jpg?t=${Date.now()}`;
}, 1000);

// ---- scene picker (load once) ----
async function loadScenes() {
  const r = await fetch("/api/scenes"); const d = await r.json();
  const sel = $("scene-pick");
  sel.innerHTML = "";
  (d.scenes || []).forEach((s) => {
    const o = document.createElement("option");
    o.value = s; o.textContent = s; if (s === d.current) o.selected = true;
    sel.appendChild(o);
  });
}

function render(s) {
  // topbar
  const obsOk = s.obs_connected;
  $("t-obs").textContent = "OBS: " + (obsOk ? "Connected" : "Disconnected");
  $("t-obs").className = "px-2 py-1 rounded-full border " + (obsOk ? "bg-ok/15 border-ok text-ok" : "bg-live/15 border-live text-live");
  const live = s.streaming;
  $("t-stream").textContent = "Stream: " + (live ? "LIVE" : "Stopped");
  $("t-stream").className = "px-2 py-1 rounded-full border " + (live ? "bg-live/15 border-live text-live" : "bg-panel2 border-line text-slate-400");
  $("t-livebadge").classList.toggle("hidden", !live);
  $("prev-live").classList.toggle("hidden", !live);
  const sess = s.session;
  $("t-session").textContent = (sess && sess.name) || s.current_product || s.current_video || "—";
  // panel Trạng thái live: nền tảng / RTMP / stream key (đã mask)
  if ($("st-platform")) $("st-platform").textContent = (sess && sess.platform) || "—";
  if ($("st-rtmp")) $("st-rtmp").textContent = (sess && sess.rtmp_server) || "—";
  if ($("st-key")) $("st-key").textContent = (sess && sess.stream_key) || "—";

  // scene + sources
  $("scene-name").textContent = s.scene || "—";

  // live status
  $("st-status").textContent = s.paused ? "⏸ Tạm dừng" : (live ? "● Đang LIVE" : "Đã dừng");
  $("st-dur").textContent = fmtDur(s.stream?.duration_ms);
  $("st-fps").textContent = s.stats?.fps != null ? s.stats.fps : "—";
  $("st-cpu").textContent = s.stats?.cpu != null ? s.stats.cpu + "%" : "—";
  $("st-mem").textContent = s.stats?.memory_mb != null ? s.stats.memory_mb + " MB" : "—";
  $("st-drop").textContent = s.stream ? `${s.stream.skipped}/${s.stream.total}` : "—";
  // bitrate từ delta bytes
  const now = Date.now(), bytes = s.stream?.bytes || 0;
  if (live && lastBytes != null && now > lastTs) {
    const kbps = Math.round(((bytes - lastBytes) * 8) / ((now - lastTs) / 1000) / 1000);
    $("st-bitrate").textContent = (kbps >= 0 ? kbps : 0) + " kbps";
  } else if (!live) { $("st-bitrate").textContent = "—"; }
  lastBytes = bytes; lastTs = now;

  // pause button label
  $("btn-pause").textContent = s.paused ? "▶ Tiếp tục" : "⏸ Tạm dừng";

  // đồng bộ dropdown playlist với playlist đang phát (chỉ 1 lần, khỏi đè lựa chọn người dùng)
  if (!_goPlInit && $("go-pl") && $("go-pl").options.length && s.active_pl_id != null) {
    $("go-pl").value = String(s.active_pl_id); _goPlInit = true;
  }

  // playlist — tên + chế độ phát + lọc nhóm của playlist đang chạy
  if ($("pl-name")) $("pl-name").textContent = s.playlist_name || "";
  if ($("pl-mode-badge")) $("pl-mode-badge").textContent = s.playlist_mode || "";
  if ($("pl-group-badge")) {
    const g = s.playlist_group;
    $("pl-group-badge").textContent = g ? "Nhóm: " + g : "";
    $("pl-group-badge").classList.toggle("hidden", !g);
  }
  // toggle Tự động phát / Random / Loop của playlist đang chạy
  _dPlId = s.playlist_id || null;
  if ($("d-autoplay") && document.activeElement !== $("d-autoplay")) $("d-autoplay").checked = s.playlist_autoplay !== false;
  if ($("d-loop") && document.activeElement !== $("d-loop")) $("d-loop").checked = s.playlist_loop !== false;
  if ($("d-random") && document.activeElement !== $("d-random")) $("d-random").checked = (s.playlist_mode_raw === "random");

  const body = $("pl-body"); body.innerHTML = "";
  let nextMarked = false;
  (s.playlist || []).forEach((e) => {
    const playing = e.video === s.current_video && !e.is_played;
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50";
    let st;
    if (playing) st = '<span class="text-ok">▶ Đang phát</span>';
    else if (e.is_played) st = '<span class="text-slate-500">Đã phát</span>';
    else if (!nextMarked) { st = '<span class="text-brand">⏭ Sắp tới</span>'; nextMarked = true; }
    else st = '<span class="text-slate-400">Chờ</span>';
    const thumb = e.video_id
      ? `<img src="/api/video_thumb/${e.video_id}" class="w-14 h-8 object-cover rounded bg-panel2" loading="lazy" onerror="this.style.visibility='hidden'">`
      : "";
    tr.innerHTML = `<td class="py-1.5">${e.idx}</td><td>${thumb}</td><td>${e.video}</td><td class="text-slate-400">${e.product}</td>
      <td>${e.duration ? e.duration.toFixed(0) + "s" : "—"}</td><td>${st}</td>`;
    body.appendChild(tr);
  });
  $("pl-empty").classList.toggle("hidden", (s.playlist || []).length > 0);

  // pinned product
  const p = s.product;
  $("pin-box").classList.toggle("hidden", !p);
  $("pin-empty").classList.toggle("hidden", !!p);
  _pinId = p ? p.id : null;
  if (p) {
    $("pin-name").textContent = p.name;
    $("pin-sku").textContent = p.sku ? "SKU: " + p.sku : "";
    $("pin-price").textContent = fmtVND(p.sale_price || p.price);
    $("pin-old").textContent = p.sale_price && p.price ? fmtVND(p.price) : "";
    $("pin-comm").textContent = (p.commission || 0) + "%";
    $("pin-stock").textContent = p.stock != null ? p.stock : "—";
    // ảnh sản phẩm (p.image kèm ?v=mtime -> đổi ảnh là tải mới, không dính cache)
    const img = $("pin-img");
    if (p.image) {
      img.style.visibility = "visible";
      if (img.getAttribute("src") !== p.image) img.src = p.image;
    } else img.style.visibility = "hidden";
    // link sản phẩm + copy
    const lb = $("pin-link-box");
    if (p.link) { lb.classList.remove("hidden"); $("pin-link").textContent = p.link; $("pin-link").href = p.link; }
    else lb.classList.add("hidden");
  }
  // không ghi đè khi đang sửa script
  if (!_editingScript) $("pin-script").textContent = (p && p.script) || "—";

  // hàng đợi ưu tiên (kèm intent nếu là video trả lời)
  const q = s.queue || [];
  $("q-count").textContent = q.length;
  $("q-list").innerHTML = q.map((x) =>
    `<li>${x.intent ? '<span class="text-brand">[' + x.intent + ']</span> ' : ""}${x.name}</li>`).join("");

  // logs
  const logs = $("logs");
  logs.textContent = (s.logs || []).join("\n");
  logs.scrollTop = logs.scrollHeight;
}

// ---- danh sách INTENT cho "Phát video trả lời" ----
async function loadIntents() {
  try {
    const d = await (await fetch("/api/intents")).json();
    const sel = $("pv-select");
    if (!sel) return;
    const items = (d.intents || []).filter((i) => i.enabled);
    sel.innerHTML = items.map((i) =>
      `<option value="${i.id}">${i.name} (${i.answer_count} video)</option>`).join("")
      || '<option value="">(chưa có intent — tạo ở trang Kịch bản)</option>';
  } catch (e) {}
}
async function playIntent(mode) {
  const id = $("pv-select").value; if (!id) return;
  const r = await (await fetch("/api/answer_by_intent", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ intent_id: +id, mode }),
  })).json();
  if (!r.ok && r.error) alert(r.error);
}
if ($("pv-now")) {
  $("pv-now").onclick = () => playIntent("play_now");
  $("pv-queue").onclick = () => playIntent("enqueue");
  $("q-clear").onclick = () => control("clear_queue");
  $("pv-rescan").onclick = loadIntents;
  loadIntents();
}

// ---- WebSocket ----
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => { try { render(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connect, 2000);
}

// ---- wire buttons ----
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (b) { control(b.dataset.act); return; }
  const sg = e.target.closest("[data-scene-go]");
  if (sg) { control("scene", { name: $("scene-pick").value }); return; }
  const st = e.target.closest("[data-src-toggle]");
  if (st) { control("source_visible", { source: st.dataset.srcToggle, visible: true }); return; }
  const mu = e.target.closest("[data-mute]");
  if (mu) { control("toggle_mute", { source: mu.dataset.mute }); return; }
});

// ---- Sản phẩm đang ghim: copy link + sửa script ----
let _pinId = null, _editingScript = false;
if ($("pin-copy")) $("pin-copy").onclick = () => {
  const link = $("pin-link").textContent;
  if (link) navigator.clipboard?.writeText(link).then(() => {
    const b = $("pin-copy"); b.textContent = "✓"; setTimeout(() => (b.textContent = "⧉"), 1200);
  });
};
function _scriptEdit(on) {
  _editingScript = on;
  $("pin-script").classList.toggle("hidden", on);
  $("pin-script-edit").classList.toggle("hidden", !on);
  $("pin-script-btn").classList.toggle("hidden", on);
  $("pin-script-save").classList.toggle("hidden", !on);
  $("pin-script-cancel").classList.toggle("hidden", !on);
}
if ($("pin-script-btn")) $("pin-script-btn").onclick = () => {
  if (!_pinId) { alert("Chưa ghim sản phẩm nào"); return; }
  const cur = $("pin-script").textContent;
  $("pin-script-edit").value = cur === "—" ? "" : cur;
  _scriptEdit(true);
};
if ($("pin-script-cancel")) $("pin-script-cancel").onclick = () => _scriptEdit(false);
if ($("pin-script-save")) $("pin-script-save").onclick = async () => {
  const script = $("pin-script-edit").value;
  await fetch("/api/products/script", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: _pinId, script }),
  });
  $("pin-script").textContent = script || "—";
  _scriptEdit(false);
};

// ---- Toggle Tự động phát / Random / Loop của playlist đang chạy (trên dashboard) ----
let _dPlId = null;
async function _updatePl(fields) {
  if (!_dPlId) return;
  await fetch("/api/playlists/update", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: _dPlId, ...fields }),
  });
}
if ($("d-autoplay")) $("d-autoplay").addEventListener("change", (e) => _updatePl({ autoplay: e.target.checked }));
if ($("d-loop")) $("d-loop").addEventListener("change", (e) => _updatePl({ loop: e.target.checked }));
if ($("d-random")) $("d-random").addEventListener("change", (e) => _updatePl({ play_mode: e.target.checked ? "random" : "order" }));

// ---- Bắt đầu live ngay từ dashboard (set RTMP + playlist + start) ----
let _goPlInit = false;
async function loadGoPlaylists() {
  if (!$("go-pl")) return;
  const d = await (await fetch("/api/playlists")).json();
  $("go-pl").innerHTML = (d.playlists || []).map((p) =>
    `<option value="${p.id}">${p.name} (${p.count})</option>`).join("");
}
if ($("go-pl")) {
  $("go-pl").addEventListener("change", (e) => control("set_playlist", { pl_id: +e.target.value }));
}
const GO_PRESET = {
  YouTube: "rtmp://a.rtmp.youtube.com/live2",
  Facebook: "rtmps://live-api-s.facebook.com:443/rtmp/",
  TikTok: "", Shopee: "", Custom: "",
};
if ($("go-platform")) {
  $("go-platform").addEventListener("change", (e) => {
    const p = GO_PRESET[e.target.value];
    if (p && !$("go-rtmp").value) $("go-rtmp").value = p;
  });
}
if ($("go-eye")) {
  $("go-eye").onclick = () => {
    const k = $("go-key"); k.type = k.type === "password" ? "text" : "password";
  };
}
if ($("go-live-btn")) {
  $("go-live-btn").onclick = async () => {
    const msg = $("go-msg");
    const rtmp_server = $("go-rtmp").value.trim(), stream_key = $("go-key").value.trim();
    if (!rtmp_server || !stream_key) { msg.textContent = "⚠ Cần nhập RTMP server và Stream key"; msg.className = "text-[11px] text-live"; return; }
    msg.textContent = "Đang kết nối..."; msg.className = "text-[11px] text-slate-400";
    const r = await control("go_live", { rtmp_server, stream_key, platform: $("go-platform").value,
                                         pl_id: $("go-pl") ? $("go-pl").value : null });
    if (r && r.ok) { msg.textContent = "✅ Đã bắt đầu live"; msg.className = "text-[11px] text-ok"; }
    else { msg.textContent = "❌ " + ((r && r.error) || "lỗi"); msg.className = "text-[11px] text-live"; }
  };
}
document.addEventListener("change", (e) => {
  const v = e.target.closest("[data-vol]");
  if (v) control("volume", { source: v.dataset.vol, mul: v.value / 100 });
});

loadScenes();
loadGoPlaylists();
connect();
