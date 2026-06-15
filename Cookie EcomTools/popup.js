const countryInfo = {
  'shopee.vn': { flag: '🇻🇳', name: 'Vietnam', code: 'VN' },
  'shopee.com.my': { flag: '🇲🇾', name: 'Malaysia', code: 'MY' },
  'shopee.ph': { flag: '🇵🇭', name: 'Philippines', code: 'PH' },
  'shopee.tw': { flag: '🇹🇼', name: 'Taiwan', code: 'TW' },
  'shopee.co.th': { flag: '🇹🇭', name: 'Thailand', code: 'TH' },
  'shopee.co.id': { flag: '🇮🇩', name: 'Indonesia', code: 'ID' }
};

// Additional app info to append
const additionalCookies = '; language=en; SPC_RNBV=6073008; shopee_app_version=29627; shopee_rn_bundle_version=6073008; shopee_rn_version=1671807778';

// Endpoint mặc định của "hệ thống của mình" (Relive Studio, cổng 7863).
const DEFAULT_ENDPOINT = 'http://127.0.0.1:7863/api/shopee/cookie';

// Cookie + quốc gia hiện tại (cập nhật khi cookie sẵn sàng) — để nút "Gửi" dùng lại.
let currentCookie = '';
let currentInfo = null;

// Gọi khi đã ráp xong chuỗi cookie: hiển thị + lưu local + (nếu bật) tự gửi vào hệ thống.
function deliverCookie(finalCookieString, shopeeInfo) {
  currentCookie = finalCookieString;
  currentInfo = shopeeInfo;
  document.getElementById('cookieOutput').value = finalCookieString;
  updateStatus('cookieStatus', '✅ Cookie đã sẵn sàng', true);
  chrome.storage.local.set({
    [`cookie_${shopeeInfo.code}`]: finalCookieString,
    [`lastUpdated_${shopeeInfo.code}`]: Date.now()
  });
  const auto = document.getElementById('autoSend');
  if (auto && auto.checked) sendToSystem();
}

// POST cookie + User-Agent vào hệ thống của mình.
function sendToSystem() {
  if (!currentCookie || !currentInfo) {
    updateStatus('sendStatus', '⚠ Chưa có cookie để gửi');
    return;
  }
  const endpoint = (document.getElementById('endpoint').value || DEFAULT_ENDPOINT).trim();
  chrome.storage.local.set({ systemEndpoint: endpoint });
  const userAgent = document.getElementById('uaOutput').value || navigator.userAgent;
  updateStatus('sendStatus', '⏳ Đang gửi...');
  fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code: currentInfo.code,
      domain: currentInfo.domain,
      cookie: currentCookie,
      user_agent: userAgent
    })
  })
    .then(r => r.json().catch(() => ({ ok: r.ok })))
    .then(data => {
      if (data && data.ok) {
        updateStatus('sendStatus', `✅ Đã gửi vào hệ thống (${currentInfo.code})`, true);
      } else {
        updateStatus('sendStatus', '❌ ' + ((data && data.error) || 'hệ thống từ chối'));
      }
    })
    .catch(err => updateStatus('sendStatus', '❌ Không kết nối được hệ thống: ' + err.message));
}

function getShopeeInfo(url) {
  for (let domain in countryInfo) {
    if (url.includes(domain)) {
      return { domain, ...countryInfo[domain] };
    }
  }
  return null;
}

function timeAgo(timestamp) {
  if (!timestamp) return '';
  const seconds = Math.floor((Date.now() - timestamp) / 1000);

  if (seconds < 60) return `${seconds} giây trước`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} giờ trước`;
  return `${Math.floor(seconds / 86400)} ngày trước`;
}

function updateCountryInfo(shopeeInfo) {
  const flagEl = document.getElementById('countryFlag');
  const nameEl = document.getElementById('countryName');

  if (shopeeInfo) {
    flagEl.textContent = shopeeInfo.flag;
    nameEl.textContent = `${shopeeInfo.name} (${shopeeInfo.domain})`;
  } else {
    flagEl.textContent = '🌍';
    nameEl.textContent = 'Không phải trang Shopee';
  }
}

function updateStatus(elementId, message, isSuccess = false) {
  const element = document.getElementById(elementId);
  element.textContent = message;
  element.className = `status ${isSuccess ? 'success' : 'error'}`;
}

document.addEventListener('DOMContentLoaded', function () {

  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    const currentTab = tabs[0];
    const shopeeInfo = getShopeeInfo(currentTab.url);

    updateCountryInfo(shopeeInfo);

    if (shopeeInfo) {

      chrome.cookies.getAll({ url: currentTab.url }, function (cookies) {
        const cookiesFromAPI = cookies
          .map(cookie => `${cookie.name}=${cookie.value}`)
          .join('; ');

        console.log(`Cookies from chrome.cookies.getAll [${shopeeInfo.domain}]:`, cookiesFromAPI);

        chrome.scripting.executeScript({
          target: { tabId: currentTab.id },
          func: () => {

            let csrfFromCookie = '';
            const cookieMatch = document.cookie.match(/csrftoken=([^;]+)/);
            if (cookieMatch) {
              csrfFromCookie = cookieMatch[1];
            }

            const metaTag = document.querySelector('meta[name="csrf-token"]');
            const csrfFromMeta = metaTag ? metaTag.getAttribute('content') : '';

            const hiddenInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
            const csrfFromInput = hiddenInput ? hiddenInput.value : '';

            const csrfFromWindow = window.csrftoken || window.csrf_token || '';

            return {
              documentCookie: document.cookie,
              csrfFromCookie: csrfFromCookie,
              csrfFromMeta: csrfFromMeta,
              csrfFromInput: csrfFromInput,
              csrfFromWindow: csrfFromWindow
            };
          }
        }, function (result) {
          if (result && result[0] && result[0].result) {
            const data = result[0].result;
            console.log('CSRF search results:', data);

            let csrfToken = data.csrfFromCookie || data.csrfFromMeta || data.csrfFromInput || data.csrfFromWindow;

            if (!csrfToken) {
              console.log('No CSRF found, trying API call...');

              const apiEndpoint = `https://${shopeeInfo.domain}/api/v4/homepage/campaign_modules`;

              chrome.scripting.executeScript({
                target: { tabId: currentTab.id },
                func: async (apiUrl) => {
                  try {
                    const response = await fetch(apiUrl, {
                      method: 'GET',
                      credentials: 'include'
                    });
                    return {
                      status: response.status,
                      documentCookieAfterAPI: document.cookie
                    };
                  } catch (error) {
                    return { error: error.message, documentCookieAfterAPI: document.cookie };
                  }
                },
                args: [apiEndpoint]
              }, function (apiResult) {
                let finalCookieString = cookiesFromAPI;

                if (apiResult && apiResult[0] && apiResult[0].result) {
                  const apiData = apiResult[0].result;
                  console.log('API call result:', apiData);
                  if (apiData.documentCookieAfterAPI) {
                    const csrfMatch = apiData.documentCookieAfterAPI.match(/csrftoken=([^;]+)/);
                    if (csrfMatch) {
                      csrfToken = csrfMatch[1];
                    }
                  }
                }

                if (csrfToken) {
                  finalCookieString = `csrftoken=${csrfToken}; ` + cookiesFromAPI;
                  console.log('Added CSRF token:', csrfToken);
                }

                // Add additional app info cookies
                finalCookieString += additionalCookies;

                console.log('Final cookie string with app info:', finalCookieString);
                deliverCookie(finalCookieString, shopeeInfo);
              });

            } else {
              let finalCookieString = `csrftoken=${csrfToken}; ` + cookiesFromAPI;

              // Add additional app info cookies
              finalCookieString += additionalCookies;

              console.log('Found CSRF token:', csrfToken);
              console.log('Final cookie string with app info:', finalCookieString);
              deliverCookie(finalCookieString, shopeeInfo);
            }
          }
        });
      });

    } else {
      document.getElementById('cookieOutput').value = 'Không phải trang Shopee hợp lệ\n\nCác trang được hỗ trợ:\n• shopee.vn (Vietnam)\n• shopee.com.my (Malaysia)\n• shopee.ph (Philippines)\n• shopee.tw (Taiwan)\n• shopee.co.th (Thailand)\n• shopee.co.id (Indonesia)';
      updateStatus('cookieStatus', '❌ Không hỗ trợ trang này');
    }
  });

  chrome.storage.local.get(['lastUserAgent'], function (result) {
    document.getElementById('uaOutput').value = result.lastUserAgent || navigator.userAgent;
  });

  chrome.runtime.sendMessage({ action: 'getCookie' }, function (response) {
    if (response && response.cookie && response.domain) {
      const info = countryInfo[response.domain];
      if (info) {
        document.getElementById('cookieLastUpdated').textContent =
          `${info.flag} ${response.domain} - ${timeAgo(response.lastUpdated)}`;
      }
    }
  });

  document.getElementById('copyCookie').addEventListener('click', function () {
    const cookieText = document.getElementById('cookieOutput');
    cookieText.select();
    document.execCommand('copy');

    const button = this;
    const originalText = button.textContent;
    button.textContent = '✅ Đã copy!';
    setTimeout(() => {
      button.textContent = originalText;
    }, 2000);
  });

  document.getElementById('copyUA').addEventListener('click', function () {
    const uaText = document.getElementById('uaOutput');
    uaText.select();
    document.execCommand('copy');

    const button = this;
    const originalText = button.textContent;
    button.textContent = '✅ Đã copy!';
    setTimeout(() => {
      button.textContent = originalText;
    }, 2000);
  });

  // Nạp endpoint + tuỳ chọn auto-send đã lưu; nối nút "Gửi vào hệ thống".
  chrome.storage.local.get(['systemEndpoint', 'autoSend'], function (r) {
    document.getElementById('endpoint').value = r.systemEndpoint || DEFAULT_ENDPOINT;
    if (typeof r.autoSend === 'boolean') document.getElementById('autoSend').checked = r.autoSend;
  });
  document.getElementById('endpoint').addEventListener('change', function () {
    chrome.storage.local.set({ systemEndpoint: this.value.trim() });
  });
  document.getElementById('autoSend').addEventListener('change', function () {
    chrome.storage.local.set({ autoSend: this.checked });
  });
  document.getElementById('sendSystem').addEventListener('click', sendToSystem);
});