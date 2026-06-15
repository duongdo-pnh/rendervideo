// Trang Sản phẩm: SP chỉ cào từ phiên live (không thêm tay). Đồng bộ + khớp lại video + xóa.
const $ = (id) => document.getElementById(id);

async function loadProducts() {
  const d = await (await fetch("/api/products")).json();
  const body = $("prod-body"); body.innerHTML = "";
  (d.products || []).forEach((p) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-line/50 hover:bg-panel2";
    const link = p.link
      ? `<a href="${p.link}" target="_blank" rel="noopener" class="text-brand hover:underline truncate inline-block max-w-[18rem]">${p.link}</a>`
      : "<span class='text-slate-600'>—</span>";
    const item = p.shopee_item_id
      ? `<span class="text-ok">${p.shopee_item_id}</span>`
      : "<span class='text-amber-400'>⚠ chưa có</span>";
    const price = p.price ? `<span class="text-amber-300">${Number(p.price).toLocaleString("vi-VN")}đ</span>` : "—";
    tr.innerHTML = `<td class="py-1.5">${p.image ? `<img src="${p.image}" class="w-9 h-9 object-cover rounded" onerror="this.style.display='none'">` : ""}</td>
      <td class="text-white">${p.name}</td>
      <td>${price}</td>
      <td>${item}</td>
      <td>${link}</td>
      <td><button data-del="${p.id}" class="bg-live/60 hover:bg-live text-white rounded px-1.5">🗑</button></td>`;
    body.appendChild(tr);
  });
  if (!(d.products || []).length) {
    body.innerHTML = `<tr><td colspan="6" class="py-3 text-slate-500">Chưa có sản phẩm — bật quét phiên live rồi bấm <b>Đồng bộ SP từ live</b>.</td></tr>`;
  }
}

function _msg(html, cls) {
  const b = $("match-result"); b.innerHTML = html; b.className = "text-[11px] mb-2 " + (cls || "");
}

// Danh sách phiên live gần đây để chọn ĐÚNG phiên (không phụ thuộc status chập chờn).
async function loadSessions() {
  const sel = $("session-select");
  if (!sel) return;
  sel.innerHTML = '<option value="">⏳ đang tải phiên…</option>';
  try {
    const d = await (await fetch("/api/shopee/sessions")).json();
    if (!d.ok) { sel.innerHTML = `<option value="">❌ ${d.error || "lỗi"}</option>`; return; }
    sel.innerHTML = (d.sessions || []).map((s) => {
      const t = s.start_time ? new Date(s.start_time).toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
      const n = s.n_products == null ? "" : ` · ${s.n_products} SP`;
      const live = s.is_live ? "🔴 " : "";
      const act = String(s.session_id) === String(d.active) ? " ✓đang dùng" : "";
      return `<option value="${s.session_id}">${live}${t}${n} · #${s.session_id}${act}</option>`;
    }).join("") || '<option value="">(không có phiên)</option>';
    if (d.active) sel.value = String(d.active);   // ưu tiên phiên đang khóa
  } catch (e) {
    sel.innerHTML = `<option value="">❌ ${e}</option>`;
  }
}

$("reload-sessions").onclick = loadSessions;

$("sync-btn").onclick = async () => {
  const sid = $("session-select").value;
  if (!sid) { _msg("⚠ Chọn phiên live trước (bấm ⟳ nếu trống).", "text-amber-400"); return; }
  _msg("⏳ Đang đồng bộ SP + khóa phiên đã chọn...", "text-slate-400");
  const r = await (await fetch("/api/shopee/sync_products", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sid }),
  })).json();
  if (!r.ok) { _msg("❌ " + (r.error || "lỗi"), "text-live"); return; }
  _msg(`✅ Tạo ${r.created} · cập nhật ${r.updated} · gắn ${r.attached} · khớp lại video ${r.rematched || 0} (tổng ${r.total} item · phiên #${r.session_id})`, "text-ok");
  loadProducts(); loadSessions();
};

$("rematch-vid-btn").onclick = async () => {
  _msg("⏳ Đang khớp lại video chưa gắn SP...", "text-slate-400");
  const r = await (await fetch("/api/videos/rematch", { method: "POST" })).json();
  _msg(`↻ Đã khớp lại ${r.rematched || 0} video`, "text-ok");
};

// Xóa SP từ bảng
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]");
  if (!b) return;
  if (!confirm("Xóa sản phẩm này?")) return;
  await fetch("/api/products/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: +b.dataset.del }) });
  loadProducts();
});

loadProducts();
loadSessions();
