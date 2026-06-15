// Trang Playlist: thư viện video + dựng/sắp xếp playlist.
const $ = (id) => document.getElementById(id);
const api = async (p, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(p, opt);
  return r.json();
};

let _products = [];   // dùng cho dropdown gán SP inline trong thư viện + review

async function loadProducts() {
  const d = await api("/api/products");
  _products = d.products || [];
}

function prodOptions(selectedId) {
  return '<option value="">(chưa gắn SP)</option>' +
    _products.map((p) => `<option value="${p.id}" ${p.id === selectedId ? "selected" : ""}>#${p.id} ${p.name}</option>`).join("");
}

// Dropdown chọn intent để CHUYỂN 1 video giới thiệu -> tab Kịch bản AI (rỗng = giữ nguyên).
function intentMoveOptions() {
  return '<option value="">→ chọn intent…</option>' +
    (_intents || []).map((it) => `<option value="${it.id}">${it.name}</option>`).join("");
}

async function loadLibrary() {
  const d = await api("/api/videos");
  const body = $("lib-body"); body.innerHTML = "";
  let missing = 0;
  (d.videos || []).forEach((v) => {
    if (!v.product_id) missing++;
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50" + (v.is_error ? " opacity-50" : "");
    const warn = v.product_id ? "" : "border-amber-500";
    tr.innerHTML = `<td class="py-1.5"><div class="max-w-[11rem] truncate" title="${(v.name || "").replace(/"/g, "&quot;")}">${!v.product_id ? "<span class='text-amber-400' title='Chưa gán sản phẩm'>⚠ </span>" : ""}${v.name}</div></td>
      <td><select data-prod="${v.id}" class="bg-panel2 border ${warn} border-line rounded px-1 py-0.5 text-xs w-full max-w-[9rem]">${prodOptions(v.product_id)}</select></td>
      <td><select data-tointent="${v.id}" title="Chuyển video này sang tab Kịch bản AI theo intent" class="bg-panel2 border border-line rounded px-1 py-0.5 text-xs w-full max-w-[8rem]">${intentMoveOptions()}</select></td>
      <td class="text-center">${v.play_count}</td>
      <td><div class="flex items-center justify-end gap-1 whitespace-nowrap">
        <button data-add="${v.id}" class="bg-brand/80 hover:bg-brand text-white rounded px-2 py-0.5">+PL</button>
        <button data-err="${v.id}" data-flag="${v.is_error ? 0 : 1}" class="bg-panel2 hover:bg-line rounded px-2 py-0.5 w-7">${v.is_error ? "✓" : "⚠"}</button>
        <button data-delv="${v.id}" class="bg-live/70 hover:bg-live text-white rounded px-2 py-0.5 w-7">🗑</button>
      </div></td>`;
    body.appendChild(tr);
  });
  const m = $("lib-missing");
  if (m) m.textContent = missing ? `· ⚠ ${missing} video chưa gán SP` : "";
}

// Gán/đổi sản phẩm cho video ngay trong thư viện.
document.addEventListener("change", async (e) => {
  const s = e.target.closest("[data-prod]");
  if (!s) return;
  await api("/api/videos/update", { id: +s.dataset.prod, product_id: s.value ? +s.value : null });
  loadLibrary(); loadReview();
});

// Chuyển 1 video giới thiệu -> tab Kịch bản AI (chọn intent trong cột "Chuyển → AI").
document.addEventListener("change", async (e) => {
  const s = e.target.closest("[data-tointent]");
  if (!s || !s.value) return;
  let r;
  try { r = await api("/api/videos/to_answer", { id: +s.dataset.tointent, intent_id: +s.value }); }
  catch (err) { alert("Lỗi mạng/máy chủ"); s.value = ""; return; }   // reset để dropdown còn dùng lại
  if (!r.ok) { alert(r.error || "lỗi"); s.value = ""; return; }
  $("scan-result").innerHTML = `<span class="text-brand">📝 Đã chuyển 1 video → tab Kịch bản AI</span>`;
  $("scan-result").className = "text-[11px] mt-2";
  loadLibrary(); loadReview(); loadPlaylist(); loadPlaylists(); loadIntents(); loadAnswerReview();
  if (_selIntent) loadAnswers();
});

// ---- Quét video render xong + review video chưa gắn SP ----
async function loadReview() {
  const d = await api("/api/videos/review");
  const vids = d.videos || [];
  $("review-box").classList.toggle("hidden", vids.length === 0);
  $("review-count").textContent = vids.length ? `(${vids.length})` : "";
  const body = $("review-body"); if (!body) return;
  body.innerHTML = "";
  vids.forEach((v) => {
    const chips = (v.candidates || []).map((c) =>
      `<button data-pick="${v.id}" data-pid="${c.product_id}" class="bg-panel2 hover:bg-brand/40 rounded px-1.5 py-0.5 mr-1" title="${Math.round(c.score*100)}%">${c.name.slice(0,16)} ${Math.round(c.score*100)}%</button>`).join("");
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/40";
    tr.innerHTML = `<td class="py-1 text-slate-300">${v.file}</td>
      <td class="text-slate-500">${v.status} ${v.score}%</td>
      <td>${chips || "<span class='text-slate-600'>không gợi ý</span>"}
          <select data-prod="${v.id}" class="ml-1 bg-panel2 border border-line rounded px-1 py-0.5">${prodOptions(null)}</select></td>`;
    body.appendChild(tr);
  });
}

$("rematch-btn").addEventListener("click", async () => {
  const r = await api("/api/videos/rematch", {});
  $("scan-result").innerHTML = `<span class="text-ok">↻ Đã khớp lại ${r.rematched || 0} video</span>`;
  $("scan-result").className = "text-[11px] mt-2";
  loadLibrary(); loadReview(); loadAnswerReview();
});

// Bấm chip gợi ý -> gán SP cho video review.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-pick]");
  if (!b) return;
  await api("/api/videos/update", { id: +b.dataset.pick, product_id: +b.dataset.pid });
  loadLibrary(); loadReview();
});

let _plId = null;     // playlist (nhóm) đang chọn
let _plData = [];     // metadata các playlist
let _groups = [];     // nhóm sản phẩm

async function loadPlaylists() {
  const d = await api("/api/playlists");
  const sel = $("pl-select"); const lists = d.playlists || [];
  _plData = lists; _groups = d.groups || []; _st.defId = d.default_id;
  if (_plId === null) _plId = d.default_id;
  if (!lists.some((p) => p.id === _plId)) _plId = (lists[0] && lists[0].id) || d.default_id;
  sel.innerHTML = lists.map((p) =>
    `<option value="${p.id}" ${p.id === _plId ? "selected" : ""}>${p.name} (${p.count})</option>`).join("");
  fillSettings();
}

function fillSettings() {
  const p = _plData.find((x) => x.id === _plId) || {};
  $("pl-mode").value = p.play_mode || "order";
  $("pl-group").innerHTML = '<option value="">(tất cả nhóm)</option>' +
    _groups.map((g) => `<option value="${g}" ${g === p.group_filter ? "selected" : ""}>${g}</option>`).join("");
  $("t-autoplay").checked = p.autoplay !== false;
  $("t-loop").checked = p.loop !== false;
  $("t-random").checked = (p.play_mode === "random");
}

function fmtDur(s) {
  s = Math.round(s || 0);
  if (!s) return "—";
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : s + "s";
}

async function loadPlaylist() {
  const d = await api("/api/playlist" + (_plId ? "?pl_id=" + _plId : ""));
  const body = $("pl-body"); body.innerHTML = "";
  const list = d.playlist || [];
  list.forEach((e, i) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50 cursor-move" + (e.is_played ? " text-slate-500" : "");
    tr.draggable = true;
    tr.dataset.plid = e.playlist_id;
    tr.dataset.vname = e.video;
    tr.dataset.played = e.is_played ? "1" : "0";
    tr.innerHTML = `<td class="py-1.5"><span class="text-slate-600 mr-1">⠿</span>${e.idx}</td>
      <td><img src="/api/video_thumb/${e.video_id}" class="w-14 h-8 object-cover rounded bg-panel2" loading="lazy" onerror="this.style.visibility='hidden'"></td>
      <td><div class="max-w-[10rem] truncate" title="${(e.video || "").replace(/"/g, "&quot;")}">${e.video}</div></td>
      <td class="text-slate-400"><div class="max-w-[8rem] truncate" title="${(e.product || "").replace(/"/g, "&quot;")}">${e.product}</div></td>
      <td class="text-slate-400">${fmtDur(e.duration)}</td>
      <td data-stcell class="text-slate-400">${e.is_played ? "Đã phát" : "Chờ"}</td>
      <td><div class="flex items-center justify-end gap-1 whitespace-nowrap">
        <button data-up="${e.playlist_id}" ${i === 0 ? "disabled" : ""} class="bg-panel2 hover:bg-line rounded px-1.5 disabled:opacity-30">▲</button>
        <button data-down="${e.playlist_id}" ${i === list.length - 1 ? "disabled" : ""} class="bg-panel2 hover:bg-line rounded px-1.5 disabled:opacity-30">▼</button>
        <button data-delpl="${e.playlist_id}" class="bg-live/70 hover:bg-live text-white rounded px-1.5">✕</button>
      </div></td>`;
    body.appendChild(tr);
  });
  $("pl-empty").classList.toggle("hidden", list.length > 0);
  window._plOrder = list.map((e) => e.playlist_id);
  paintStatus();
}

// ---- Trạng thái phát (Đang phát / Sắp tới / Đã phát / Chờ) theo /api/status ----
let _st = { cur: null, plId: null, defId: null };
function paintStatus() {
  const rows = [...document.querySelectorAll("#pl-body tr[data-plid]")];
  const viewingActive = _plId === (_st.plId || _st.defId);
  let nextMarked = false;
  rows.forEach((tr) => {
    const cell = tr.querySelector("[data-stcell]");
    if (!cell) return;
    const played = tr.dataset.played === "1";
    const playing = viewingActive && _st.cur && tr.dataset.vname === _st.cur;
    if (playing) { cell.textContent = "▶ Đang phát"; cell.className = "text-ok"; }
    else if (played) { cell.textContent = "Đã phát"; cell.className = "text-slate-500"; }
    else if (viewingActive && !nextMarked) { cell.textContent = "⏭ Sắp tới"; cell.className = "text-brand"; nextMarked = true; }
    else { cell.textContent = "Chờ"; cell.className = "text-slate-400"; }
  });
}
async function pollStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    _st.cur = s.current_video || null;
    _st.plId = s.active_pl_id || null;
    paintStatus();
  } catch (e) {}
}

// ---- Kéo-thả sắp xếp playlist ----
let _dragId = null;
(function setupDrag() {
  const body = $("pl-body");
  body.addEventListener("dragstart", (e) => {
    const tr = e.target.closest("tr[data-plid]"); if (!tr) return;
    _dragId = +tr.dataset.plid; tr.classList.add("opacity-40");
  });
  body.addEventListener("dragend", (e) => {
    const tr = e.target.closest("tr"); if (tr) tr.classList.remove("opacity-40");
  });
  body.addEventListener("dragover", (e) => {
    e.preventDefault();
    const tr = e.target.closest("tr[data-plid]");
    if (!tr || +tr.dataset.plid === _dragId) return;
    const r = tr.getBoundingClientRect();
    const after = (e.clientY - r.top) > r.height / 2;
    const dragged = body.querySelector(`tr[data-plid="${_dragId}"]`);
    if (dragged) body.insertBefore(dragged, after ? tr.nextSibling : tr);
  });
  body.addEventListener("drop", async (e) => {
    e.preventDefault();
    const order = [...body.querySelectorAll("tr[data-plid]")].map((tr) => +tr.dataset.plid);
    window._plOrder = order;
    await api("/api/playlist/reorder", { order });
    loadPlaylist();
  });
})();

async function move(plid, dir) {
  const order = window._plOrder.slice();
  const i = order.indexOf(plid);
  const j = i + dir;
  if (j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  await api("/api/playlist/reorder", { order });
  loadPlaylist();
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.id === "clear-pl") {
    if (confirm("Xóa tất cả video trong playlist này?")) { await api("/api/playlist/clear", { pl_id: _plId }); loadPlaylist(); loadPlaylists(); }
  } else if (t.id === "pl-new") {
    const name = prompt("Tên playlist mới:"); if (!name) return;
    const r = await api("/api/playlists/add", { name });
    if (r.ok) { _plId = r.id; await loadPlaylists(); loadPlaylist(); } else if (r.error) alert(r.error);
  } else if (t.id === "pl-rename") {
    const cur = $("pl-select").selectedOptions[0]; if (!cur) return;
    const name = prompt("Đổi tên playlist:", cur.textContent.replace(/ \(\d+\)$/, "")); if (!name) return;
    await api("/api/playlists/rename", { id: _plId, name }); loadPlaylists();
  } else if (t.id === "pl-del") {
    if (!confirm("Xóa playlist này (kèm toàn bộ video trong nó)?")) return;
    const r = await api("/api/playlists/delete", { id: _plId });
    if (!r.ok) { alert(r.error || "lỗi"); return; }
    _plId = null; await loadPlaylists(); loadPlaylist();
  } else if (t.dataset.add) {
    const r = await api("/api/playlist/add", { video_id: +t.dataset.add, pl_id: _plId });
    if (!r.ok && r.error) alert(r.error);
    loadPlaylist(); loadPlaylists();
  } else if (t.dataset.delv) { await api("/api/videos/delete", { id: +t.dataset.delv }); loadLibrary(); loadPlaylist();
  } else if (t.dataset.err) { await api("/api/videos/error", { id: +t.dataset.err, flag: +t.dataset.flag }); loadLibrary();
  } else if (t.dataset.delpl) { await api("/api/playlist/remove", { playlist_id: +t.dataset.delpl }); loadPlaylist(); loadPlaylists();
  } else if (t.dataset.up) { move(+t.dataset.up, -1);
  } else if (t.dataset.down) { move(+t.dataset.down, 1); }
});

$("pl-select").addEventListener("change", (e) => { _plId = +e.target.value; fillSettings(); loadPlaylist(); });
$("pl-mode").addEventListener("change", async (e) => {
  $("t-random").checked = (e.target.value === "random");
  await api("/api/playlists/update", { id: _plId, play_mode: e.target.value }); loadPlaylists();
});
$("pl-group").addEventListener("change", async (e) => {
  await api("/api/playlists/update", { id: _plId, group_filter: e.target.value }); loadPlaylists();
});
$("t-random").addEventListener("change", async (e) => {
  const mode = e.target.checked ? "random" : "order";
  $("pl-mode").value = mode;
  await api("/api/playlists/update", { id: _plId, play_mode: mode }); loadPlaylists();
});
$("t-autoplay").addEventListener("change", async (e) => {
  await api("/api/playlists/update", { id: _plId, autoplay: e.target.checked }); loadPlaylists();
});
$("t-loop").addEventListener("change", async (e) => {
  await api("/api/playlists/update", { id: _plId, loop: e.target.checked }); loadPlaylists();
});
setInterval(pollStatus, 2000);

// ============================================================
//  TAB "KỊCH BẢN AI": intent + video trả lời (gộp từ trang /scripts).
//  Dùng data-attr riêng (data-ipick/data-aplay/data-adel/data-aprod)
//  để KHÔNG đụng handler data-pick/data-del của phần playlist ở trên.
// ============================================================
let _intents = [], _selIntent = null;

async function loadIntents() {
  const d = await api("/api/intents");
  _intents = d.intents || [];
  const total = _intents.reduce((n, it) => n + (it.answer_count || 0), 0);
  const badge = $("ai-tab-count");
  if (badge) badge.textContent = total ? total : "";
  const body = $("i-body"); if (!body) return;
  body.innerHTML = "";
  _intents.forEach((it) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50 hover:bg-panel2 cursor-pointer" + (it.id === _selIntent ? " bg-brand/10" : "");
    tr.innerHTML = `<td class="py-1.5 text-white" data-ipick="${it.id}">${it.name}${it.enabled ? "" : " <span class='text-slate-600'>(tắt)</span>"}</td>
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
  const body = $("a-body"); if (!body) return;
  body.innerHTML = "";
  if (!_selIntent) { body.innerHTML = '<tr><td colspan="5" class="text-slate-500 py-3">Chọn intent để xem video trả lời.</td></tr>'; return; }
  const d = await api("/api/answers?intent_id=" + _selIntent);
  (d.answers || []).forEach((a) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50" + (a.enabled ? "" : " opacity-50");
    tr.innerHTML = `<td class="py-1.5">${a.product_id ? "" : "<span class='text-amber-400' title='Chưa gắn SP'>⚠ </span>"}${a.name}</td>
      <td><select data-aprod="${a.id}" class="bg-panel2 border ${a.product_id ? "border-line" : "border-amber-500"} rounded px-1 py-0.5 text-xs max-w-[10rem]">${prodOptions(a.product_id)}</select></td>
      <td>${a.play_count}</td><td class="text-slate-500">${a.last_played_at || "—"}</td>
      <td><div class="flex items-center justify-end gap-1 whitespace-nowrap">
        <button data-aplay="${a.id}" class="bg-ok/80 hover:bg-ok text-white rounded px-2 py-0.5">▶ Thử</button>
        <button data-tovideo="${a.id}" title="Chuyển về video giới thiệu (tab Playlist video)" class="bg-panel2 hover:bg-line rounded px-2 py-0.5">→ PL</button>
        <button data-adel="${a.id}" class="bg-live/70 hover:bg-live text-white rounded px-2 py-0.5 w-7">🗑</button>
      </div></td>`;
    body.appendChild(tr);
  });
  if (!(d.answers || []).length) body.innerHTML = '<tr><td colspan="5" class="text-slate-500 py-3">Intent này chưa có video trả lời. Tải video <code>…__' + (_intents.find(x=>x.id===_selIntent)?.name||"INTENT") + '.mp4</code> ở tab Playlist video.</td></tr>';
}

// Cảnh báo video trả lời CHƯA gắn SP (mọi intent) — auto-map; chỉ video không khớp mới gắn tay.
async function loadAnswerReview() {
  const box = $("a-review-box"); if (!box) return;
  const d = await api("/api/answers/review");
  const rows = d.answers || [];
  box.classList.toggle("hidden", rows.length === 0);
  $("a-review-count").textContent = rows.length ? `(${rows.length})` : "";
  const body = $("a-review-body"); body.innerHTML = "";
  rows.forEach((a) => {
    const chips = (a.candidates || []).map((c) =>
      `<button data-apick="${a.id}" data-apid="${c.product_id}" class="bg-panel2 hover:bg-brand/40 rounded px-1.5 py-0.5 mr-1" title="${Math.round((c.score||0)*100)}%">${(c.name||"").slice(0,16)} ${Math.round((c.score||0)*100)}%</button>`).join("");
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/40";
    tr.innerHTML = `<td class="py-1 text-slate-300">${a.name} <span class="text-slate-600">[${a.intent || "?"}]</span></td>
      <td class="text-slate-500">${a.status} ${a.score}%</td>
      <td>${chips || "<span class='text-slate-600'>không gợi ý</span>"}
          <select data-aprev="${a.id}" class="ml-1 bg-panel2 border border-line rounded px-1 py-0.5">${prodOptions(null)}</select></td>`;
    body.appendChild(tr);
  });
}

// Nút form intent (id riêng, không đụng playlist)
$("i-new").onclick = resetIntent;
$("i-save").onclick = async () => {
  if (!$("i-name").value.trim()) { $("i-msg").textContent = "⚠ Cần tên intent"; $("i-msg").className = "text-xs text-live"; return; }
  const isNew = !$("i-id").value;
  const r = await api("/api/intents/save", {
    id: $("i-id").value || null, name: $("i-name").value, keywords: $("i-keywords").value,
    trigger_mode: $("i-trigger").value, cooldown_sec: +$("i-cooldown").value || 30, enabled: $("i-enabled").checked,
  });
  if (r.ok) {
    $("i-msg").textContent = "✅ Đã lưu"; $("i-msg").className = "text-xs text-ok";
    resetIntent();
    if (isNew && r.id) selectIntent(r.id);          // mở ngay intent mới (pane + nhãn refresh)
    else { loadIntents(); if (_selIntent) loadAnswers(); }
  }
};
$("i-del").onclick = async () => {
  const id = $("i-id").value; if (!id || !confirm("Xóa intent này (kèm video trả lời)?")) return;
  await api("/api/intents/delete", { id: +id }); if (_selIntent == id) _selIntent = null;
  resetIntent(); loadIntents(); loadAnswers();
};
// Handler click riêng cho tab Kịch bản AI (data-attr không trùng phần playlist)
document.addEventListener("click", async (e) => {
  const pick = e.target.closest("[data-ipick]");
  if (pick) { selectIntent(+pick.dataset.ipick); return; }
  const play = e.target.closest("[data-aplay]");
  if (play) { const r = await api("/api/answers/play", { id: +play.dataset.aplay }); if (!r.ok && r.error) alert(r.error); loadAnswers(); return; }
  const tov = e.target.closest("[data-tovideo]");
  if (tov) {
    const r = await api("/api/answers/to_video", { id: +tov.dataset.tovideo });
    if (!r.ok) { alert(r.error || "lỗi"); return; }
    loadAnswers(); loadIntents(); loadLibrary(); loadReview(); loadAnswerReview();
    return;
  }
  const apick = e.target.closest("[data-apick]");   // chip gợi ý trong bảng cảnh báo
  if (apick) {
    await api("/api/answers/update", { id: +apick.dataset.apick, product_id: +apick.dataset.apid });
    loadAnswerReview(); loadAnswers(); loadIntents(); return;
  }
  const del = e.target.closest("[data-adel]");
  if (del) { if (!confirm("Xóa video trả lời này?")) return; await api("/api/answers/delete", { id: +del.dataset.adel }); loadAnswers(); loadIntents(); loadAnswerReview(); return; }
});

// Nút "↻ Tự khớp lại" trong cảnh báo kịch bản AI — khớp lại cả video + answer.
$("a-rematch-btn").onclick = async () => {
  await api("/api/videos/rematch", {});
  loadAnswerReview(); loadAnswers(); loadIntents(); loadLibrary(); loadReview();
};

// Gán/đổi SP cho video trả lời ngay trong bảng + trong bảng cảnh báo (data-aprev).
document.addEventListener("change", async (e) => {
  const s = e.target.closest("[data-aprod]");
  if (s) {
    await api("/api/answers/update", { id: +s.dataset.aprod, product_id: s.value ? +s.value : null });
    loadAnswers(); loadIntents(); loadAnswerReview(); return;
  }
  const sp = e.target.closest("[data-aprev]");
  if (sp && sp.value) {
    await api("/api/answers/update", { id: +sp.dataset.aprev, product_id: +sp.value });
    loadAnswerReview(); loadAnswers(); loadIntents(); return;
  }
});

// ---- Chuyển tab Playlist <-> Kịch bản AI ----
// Đồng bộ highlight 2 mục sidebar (Playlist video / Kịch bản AI) trỏ cùng trang.
function syncSidebar(tab) {
  const ai = document.querySelector('aside a[href$="/playlist#ai"]');
  const pl = document.querySelector('aside a[href$="/playlist"]:not([href*="#"])');
  const ON = ["bg-brand/15", "text-white", "border-brand"];
  const OFF = ["text-slate-400", "border-transparent"];
  const set = (el, on) => {
    if (!el) return;
    ON.forEach((c) => el.classList.toggle(c, on));
    OFF.forEach((c) => el.classList.toggle(c, !on));
  };
  if (ai) { set(ai, tab === "ai"); set(pl, tab !== "ai"); }
  else { set(pl, true); }   // không còn mục sidebar AI riêng -> luôn sáng "Playlist video" cho cả 2 subtab
}

function showTab(tab) {
  $("pane-pl").classList.toggle("hidden", tab !== "pl");
  $("pane-ai").classList.toggle("hidden", tab !== "ai");
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("border-brand", on);
    b.classList.toggle("text-white", on);
    b.classList.toggle("border-transparent", !on);
    b.classList.toggle("text-slate-400", !on);
  });
  syncSidebar(tab);
  // giữ URL khớp tab mà KHÔNG kích hoạt hashchange (tránh đệ quy)
  const want = tab === "ai" ? "#ai" : "";
  if ((location.hash || "") !== want) history.replaceState(null, "", location.pathname + want);
  if (tab === "ai") { loadIntents(); loadAnswers(); loadAnswerReview(); }
}
document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
// Bấm mục sidebar 'Kịch bản (AI)' (href=/playlist#ai) khi ĐANG ở /playlist -> đổi hash, không reload.
window.addEventListener("hashchange", () => showTab(location.hash === "#ai" ? "ai" : "pl"));

const _VID_RE = /\.(mp4|mov|mkv|webm|avi|m4v|flv)$/i;

async function uploadFiles(fileList, btn) {
  const files = [...fileList].filter((f) => _VID_RE.test(f.name));   // lọc đúng file video
  if (!files.length) { alert("Không tìm thấy file video nào."); return; }
  const old = btn.textContent; btn.textContent = `⏳ Tải ${files.length} video...`;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const r = await (await fetch("/api/videos/upload", { method: "POST", body: fd })).json();
  btn.textContent = old;
  if (r.ok) {
    const aiNote = r.answer ? ` · <span class="text-brand">📝 ${r.answer} video kịch bản → tab Kịch bản AI</span>` : "";
    $("scan-result").innerHTML = `<span class="text-ok">✅ Thêm ${r.added} (gắn SP ${r.linked})${aiNote} · review ${r.review} · chưa khớp ${r.unmatched} · bỏ qua ${r.skipped}</span>`;
    $("scan-result").className = "text-[11px] mt-2";
    loadLibrary(); loadReview(); loadIntents(); loadAnswerReview();   // badge + ds kịch bản + cảnh báo
    if (_selIntent) loadAnswers();
  } else alert("Lỗi tải lên: " + (r.error || ""));
}

$("upload-vid").addEventListener("click", () => $("upload-file").click());
$("upload-file").addEventListener("change", async (e) => {
  if (e.target.files.length) await uploadFiles(e.target.files, $("upload-vid"));
  e.target.value = "";
});

$("upload-folder-btn").addEventListener("click", () => $("upload-folder").click());
$("upload-folder").addEventListener("change", async (e) => {
  if (e.target.files.length) await uploadFiles(e.target.files, $("upload-folder-btn"));
  e.target.value = "";
});

async function init() {
  await loadProducts();              // _products có trước -> dropdown SP của cả 2 tab đúng
  await loadIntents();               // _intents có trước -> cột "Chuyển → AI" trong thư viện đầy đủ
  loadLibrary(); loadReview();
  await loadPlaylists(); loadPlaylist(); pollStatus();
  if (location.hash === "#ai") showTab("ai");
}
init();
