// jevlab unclutter service worker: content.js からの候補をローカルサーバに中継する。
// サーバ: `jevlab unclutter serve --port 8765` (Python 側)。ページ URL や HTML は送らない。
const ENDPOINT = "http://127.0.0.1:8765/classify";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== "classify") return false;
  fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host: msg.host, candidates: msg.candidates }),
  })
    .then((r) => r.json())
    .then((data) => sendResponse({ css: data.css || "", results: data.results || [] }))
    .catch((e) => sendResponse({ css: "", error: String(e) }));
  return true; // 非同期応答
});

chrome.action.onClicked.addListener((tab) => {
  if (tab.id) chrome.tabs.sendMessage(tab.id, { type: "toggle" });
});
