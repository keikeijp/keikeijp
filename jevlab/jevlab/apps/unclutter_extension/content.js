// jevlab unclutter content script (MV3).
// 1) 候補要素に data-jevlab-unclutter="<idx>" を振り、短い記述 (URL/HTML なし) を集める
// 2) background 経由でローカルの `jevlab unclutter serve` に POST
// 3) 返ってきた CSS を拡張所有の <style id="jevlab-unclutter-style"> に入れる (削除すれば元に戻る)
const ATTR = "data-jevlab-unclutter";
const STYLE_ID = "jevlab-unclutter-style";
const MAX = 60, MAX_TEXT = 80, MAX_HINT = 40;

function collect() {
  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  const adDomains = ["doubleclick", "googlesyndication", "adnxs", "adsystem", "taboola", "outbrain", "criteo", "amazon-adsystem", "adform"];
  const short = (s, n) => (s || "").toString().replace(/\s+/g, " ").trim().slice(0, n);
  const out = [];
  for (const el of document.querySelectorAll("div, section, aside, iframe, form, footer, header, nav, dialog")) {
    if (out.length >= MAX) break;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    const pos = cs.position, z = parseInt(cs.zIndex, 10) || 0;
    let iframeHint = "";
    if (el.tagName === "IFRAME") {
      const src = (el.getAttribute("src") || "").toLowerCase();
      for (const d of adDomains) if (src.includes(d)) { iframeHint = d; break; }
    }
    const closeBtn = !!el.querySelector('[aria-label*="close" i], [class*="close" i], button[title*="close" i]');
    if (!(pos === "fixed" || pos === "sticky" || pos === "absolute" || z >= 100 || iframeHint || closeBtn || el.tagName === "IFRAME" || el.tagName === "DIALOG")) continue;
    const idx = out.length;
    el.setAttribute(ATTR, String(idx));
    out.push({
      idx, tag: el.tagName.toLowerCase(), id_hint: short(el.id, MAX_HINT), class_hint: short(el.className, MAX_HINT),
      role: short(el.getAttribute("role") || el.getAttribute("aria-label"), MAX_HINT), text: short(el.innerText, MAX_TEXT),
      position: pos, size_ratio: Math.round((r.width * r.height) / (vw * vh) * 1000) / 1000, z_index: z,
      has_close_button: closeBtn, iframe_ad_domain_hint: iframeHint,
    });
  }
  return out;
}

function applyCss(css) {
  let s = document.getElementById(STYLE_ID);
  if (!s) { s = document.createElement("style"); s.id = STYLE_ID; document.head.appendChild(s); }
  s.textContent = css || "";
}

function removeCss() { const s = document.getElementById(STYLE_ID); if (s) s.remove(); }

async function runOnce() {
  const candidates = collect();
  if (!candidates.length) return;
  const reply = await chrome.runtime.sendMessage({ type: "classify", host: location.hostname, candidates });
  if (reply && reply.css) applyCss(reply.css);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "toggle") { document.getElementById(STYLE_ID) ? removeCss() : runOnce(); }
});

runOnce().catch(() => {});
