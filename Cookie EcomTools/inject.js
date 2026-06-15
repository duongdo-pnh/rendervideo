// Chạy trong MAIN world trang Shopee: hook fetch + XHR + WebSocket để bắt comment phiên live
// (cả creator.shopee.* lẫn banhang.shopee.*), rồi postMessage cho content script gửi về hệ thống.
(function () {
  function looksLikeComment(o) {
    return o && typeof o === "object" &&
      (o.content || o.text || o.msg || o.comment) &&
      (o.username || o.userName || o.nickname || o.nickName || o.userId || o.user_id || o.uid);
  }

  // Tìm mảng comment trong 1 object JSON bất kỳ (REST body hoặc WS message).
  function extract(obj) {
    if (!obj || typeof obj !== "object") return [];
    const d = obj.data !== undefined && obj.data !== null ? obj.data : obj;
    let arr = (d && (d.comments || d.list || d.records)) || (Array.isArray(d) ? d : null);
    if (Array.isArray(arr)) {
      const cs = arr.filter(looksLikeComment);
      if (cs.length) return cs;
    }
    if (looksLikeComment(d)) return [d];
    if (looksLikeComment(obj)) return [obj];
    return [];
  }

  function send(comments) {
    if (comments && comments.length) {
      window.postMessage({ __ecomtools_comments: true, comments: comments }, "*");
    }
  }

  function tryParse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  // URL có vẻ liên quan comment/live (lọc nhẹ cho fetch/XHR để khỏi parse mọi response).
  function maybeComment(url) {
    url = String(url || "").toLowerCase();
    return url.includes("comment") || url.includes("livestream") || url.includes("realtime");
  }

  // --- fetch ---
  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const url = (args[0] && args[0].url) || args[0];
    return origFetch.apply(this, args).then((res) => {
      try {
        if (maybeComment(url)) res.clone().text().then((t) => send(extract(tryParse(t)))).catch(() => {});
      } catch (e) {}
      return res;
    });
  };

  // --- XHR ---
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...r) { this.__ecom_url = u; return origOpen.call(this, m, u, ...r); };
  XMLHttpRequest.prototype.send = function (...a) {
    this.addEventListener("load", function () {
      try { if (maybeComment(this.__ecom_url)) send(extract(tryParse(this.responseText))); } catch (e) {}
    });
    return origSend.apply(this, a);
  };

  // --- WebSocket (comment live thường đẩy qua WS) ---
  try {
    const OWS = window.WebSocket;
    const Wrapped = function (url, protocols) {
      const ws = protocols !== undefined ? new OWS(url, protocols) : new OWS(url);
      ws.addEventListener("message", (ev) => {
        try {
          if (typeof ev.data === "string") send(extract(tryParse(ev.data)));
        } catch (e) {}
      });
      return ws;
    };
    Wrapped.prototype = OWS.prototype;
    Wrapped.CONNECTING = OWS.CONNECTING; Wrapped.OPEN = OWS.OPEN;
    Wrapped.CLOSING = OWS.CLOSING; Wrapped.CLOSED = OWS.CLOSED;
    window.WebSocket = Wrapped;
  } catch (e) {}

  console.log("[EcomTools] comment hook đã cài (fetch + XHR + WebSocket).");
})();
