// Trang Quy tắc phát: nạp/lưu cấu hình rule engine.
const $ = (id) => document.getElementById(id);
const api = async (p, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  return (await fetch(p, opt)).json();
};

const BOOL = ["play_limit_enabled"];
const NUM = ["follow_every_n", "voucher_every_min", "top_every_min", "top_count"];
const TXT = ["follow_category", "voucher_category"];

async function load() {
  const d = await api("/api/rules");
  const r = d.rules || {};
  BOOL.forEach((k) => ($("r-" + k).checked = !!r[k]));
  NUM.forEach((k) => ($("r-" + k).value = r[k] != null ? r[k] : 0));
  TXT.forEach((k) => ($("r-" + k).value = r[k] || ""));
}

$("r-save").onclick = async () => {
  const body = {};
  BOOL.forEach((k) => (body[k] = $("r-" + k).checked));
  NUM.forEach((k) => (body[k] = parseInt($("r-" + k).value || "0", 10) || 0));
  TXT.forEach((k) => (body[k] = $("r-" + k).value.trim()));
  const r = await api("/api/rules/save", body);
  const msg = $("r-msg");
  if (r.ok) { msg.textContent = "✅ Đã lưu"; msg.className = "text-xs text-ok"; }
  else { msg.textContent = "❌ Lỗi"; msg.className = "text-xs text-live"; }
};

load();
