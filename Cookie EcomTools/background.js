let latestCookie = '';

const shopeeDomains = [
  'shopee.vn',
  'shopee.com.my', 
  'shopee.ph',
  'shopee.tw',
  'shopee.co.th',
  'shopee.co.id'
];

function isShopeeUrl(url) {
  return shopeeDomains.some(domain => url.includes(domain));
}

function getDomainFromUrl(url) {
  for (let domain of shopeeDomains) {
    if (url.includes(domain)) {
      return domain;
    }
  }
  return null;
}

// ---- Dò endpoint Shopee Creator API (gửi method + path + tên tham số về hệ thống) ----
const _seenKeys = new Set();

function reportSeenEndpoint(details) {
  // Ghi MỌI API Shopee (kèm host) để biết domain/đường dẫn quản lý + ghim sản phẩm.
  if (!details.url.includes('/api/')) return;
  let u;
  try { u = new URL(details.url); } catch (e) { return; }
  const method = details.method || 'GET';
  const key = method + ' ' + u.host + u.pathname;
  if (_seenKeys.has(key)) return;          // mỗi đường dẫn chỉ báo 1 lần
  _seenKeys.add(key);
  const queryKeys = Array.from(u.searchParams.keys());
  chrome.storage.local.get(['systemEndpoint'], function (r) {
    const base = (r.systemEndpoint || 'http://127.0.0.1:7863/api/shopee/cookie')
      .replace(/\/api\/shopee\/.*$/, '/api/shopee/seen');
    fetch(base, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method: method, path: u.host + u.pathname,
                             query_keys: queryKeys, query: u.search })
    }).catch(() => {});
  });
}

chrome.webRequest.onBeforeSendHeaders.addListener(
  function(details) {
    if (isShopeeUrl(details.url) && details.url.includes('/api/')) {
      const domain = getDomainFromUrl(details.url);
      console.log(`Background intercepted [${domain}]:`, details.url);
      
      if (details.requestHeaders) {
        const cookieHeader = details.requestHeaders.find(header => 
          header.name.toLowerCase() === 'cookie'
        );
        
        if (cookieHeader) {
          latestCookie = cookieHeader.value;
          console.log(`Latest cookie updated [${domain}]:`, latestCookie.substring(0, 100) + '...');
          chrome.storage.local.set({
            latestCookie: latestCookie,
            lastDomain: domain,
            lastUpdated: Date.now()
          });
        }
      }

      // Dò endpoint: ghi lại API mà trang Creator gọi (để biết đường dẫn list/ghim SP).
      reportSeenEndpoint(details);
    }
  },
  {
    urls: [
      "*://shopee.vn/*", "*://*.shopee.vn/*",
      "*://shopee.com.my/*", "*://*.shopee.com.my/*",
      "*://shopee.ph/*", "*://*.shopee.ph/*", 
      "*://shopee.tw/*", "*://*.shopee.tw/*",
      "*://shopee.co.th/*", "*://*.shopee.co.th/*",
      "*://shopee.co.id/*", "*://*.shopee.co.id/*"
    ]
  },
  ["requestHeaders"]
);

function systemUrl(cb) {
  chrome.storage.local.get(['systemEndpoint'], function (r) {
    const base = (r.systemEndpoint || 'http://127.0.0.1:7863/api/shopee/cookie')
      .replace(/\/api\/shopee\/.*$/, '');
    cb(base);
  });
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'getCookie') {
    chrome.storage.local.get(['latestCookie', 'lastDomain', 'lastUpdated'], function(result) {
      sendResponse({
        cookie: result.latestCookie || latestCookie,
        domain: result.lastDomain,
        lastUpdated: result.lastUpdated
      });
    });
    return true;
  }

  // Comment thật cào từ trang live → gửi về hệ thống để khớp SP + chuyển video OBS.
  if (request.action === 'liveComments' && Array.isArray(request.comments)) {
    systemUrl(function (base) {
      fetch(base + '/api/comment/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ comments: request.comments })
      }).catch(() => {});
    });
  }
});