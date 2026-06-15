// Trả User-Agent cho popup.
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === "GET_USER_AGENT") {
    sendResponse({userAgent: navigator.userAgent});
  }
  return true;
});

// Nhận comment do inject.js (MAIN world) bắt từ API live → chuyển cho background gửi về hệ thống.
window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (d && d.__ecomtools_comments && Array.isArray(d.comments) && d.comments.length) {
    chrome.runtime.sendMessage({ action: "liveComments", comments: d.comments });
  }
});
