"""16. unclutter (kitze/unclutter の再実装): ページ内の広告/ポップアップ/ニュースレター/Cookie/SNS 系クラッタを
Jev で識別し、可逆的に非表示にする。

2 つの配信形態:

(a) Playwright ランナー (`run --url`): ページに `COLLECTOR_JS` を注入して候補要素 (最大 60 件、URL や生 HTML は含まない
    短い記述) を集め、候補ごとに Jev の Choice で {keep, ad, promotion, newsletter, social, cookie, uncertain} を判定。
    probability >= 0.9 かつ confidence >= 0.9 の時だけ非表示を採用し、拡張機能専用の <style> を注入する。
    ルールはホスト毎の JSON として保存し、`[data-jevlab-unclutter]` 属性セレクタで復元/解除できる。
(b) ブラウザ拡張 (`jevlab/apps/unclutter_extension/`, MV3): content.js が候補を `jevlab unclutter serve` の
    ローカル HTTP エンドポイントに POST し、返ってきた CSS を適用する。Python 側はこの `serve` が担当。

`classify_candidates(jev, candidates)` は純粋関数でテスト可能。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from jevlab.core import Choice, Jev

MAX_CANDIDATES = 60
MAX_TEXT = 80
MAX_HINT = 40
MIN_PROBABILITY = 0.9
MIN_CONFIDENCE = 0.9
ATTR = "data-jevlab-unclutter"  # 拡張機能が所有する属性。候補 idx を値にする

CATEGORIES: dict[str, str] = {
    "keep": "ページ本来のコンテンツ、ナビゲーション、記事本文、必要な UI。隠してはいけない",
    "ad": "第三者の広告 (広告ネットワークの iframe、スポンサード枠、バナー)",
    "promotion": "サイト自身の宣伝ポップアップ/モーダル (アプリ導入、セール、会員登録の勧誘)",
    "newsletter": "メールアドレス入力を促すニュースレター登録の枠/ポップアップ",
    "social": "SNS シェア/フォローボタンの浮遊バー、チャットウィジェット",
    "cookie": "Cookie 同意バナー/プライバシー通知",
    "uncertain": "判断できない。安全側に倒して隠さない",
}
HIDE_LABELS = {"ad", "promotion", "newsletter", "social", "cookie"}

# ブラウザ側で走らせる候補収集スクリプト。要素に ATTR を振り、短い記述だけ返す (URL/HTML は返さない)。
COLLECTOR_JS = r"""
(() => {
  const ATTR = "__ATTR__";
  const MAX = __MAX__;
  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  const adDomains = ["doubleclick", "googlesyndication", "adnxs", "adsystem", "taboola", "outbrain", "criteo", "amazon-adsystem", "adform"];
  const out = [];
  const short = (s, n) => (s || "").toString().replace(/\s+/g, " ").trim().slice(0, n);
  const nodes = document.querySelectorAll("div, section, aside, iframe, form, footer, header, nav, dialog");
  for (const el of nodes) {
    if (out.length >= MAX) break;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    const pos = cs.position;
    const z = parseInt(cs.zIndex, 10) || 0;
    const ratio = (r.width * r.height) / (vw * vh);
    const text = short(el.innerText, __MAXTEXT__);
    let iframeHint = "";
    if (el.tagName === "IFRAME") {
      const src = (el.getAttribute("src") || "").toLowerCase();
      for (const d of adDomains) if (src.includes(d)) { iframeHint = d; break; }
    }
    const closeBtn = !!el.querySelector('[aria-label*="close" i], [class*="close" i], button[title*="close" i]');
    const interesting = pos === "fixed" || pos === "sticky" || pos === "absolute" || z >= 100 || iframeHint || closeBtn || el.tagName === "IFRAME" || el.tagName === "DIALOG";
    if (!interesting) continue;
    const idx = out.length;
    el.setAttribute(ATTR, String(idx));
    out.push({
      idx, tag: el.tagName.toLowerCase(),
      id_hint: short(el.id, __MAXHINT__), class_hint: short(el.className, __MAXHINT__),
      role: short(el.getAttribute("role") || el.getAttribute("aria-label"), __MAXHINT__),
      text, position: pos, size_ratio: Math.round(ratio * 1000) / 1000, z_index: z,
      has_close_button: closeBtn, iframe_ad_domain_hint: iframeHint,
    });
  }
  return out;
})()
""".replace("__ATTR__", ATTR).replace("__MAX__", str(MAX_CANDIDATES)).replace("__MAXTEXT__", str(MAX_TEXT)).replace("__MAXHINT__", str(MAX_HINT))


# ---------------------------------------------------------------------------
# 純粋ロジック
# ---------------------------------------------------------------------------


def normalize_candidate(raw: dict[str, Any], idx: int | None = None) -> dict[str, Any]:
    """ブラウザ/拡張から来た候補を、Jev に渡してよい上限付きの形に揃える。"""
    return {
        "idx": int(raw.get("idx", idx if idx is not None else 0)),
        "tag": str(raw.get("tag", "div"))[:16],
        "id_hint": str(raw.get("id_hint", ""))[:MAX_HINT],
        "class_hint": str(raw.get("class_hint", ""))[:MAX_HINT],
        "role": str(raw.get("role", ""))[:MAX_HINT],
        "text": str(raw.get("text", ""))[:MAX_TEXT],
        "position": str(raw.get("position", "static"))[:8],
        "size_ratio": round(float(raw.get("size_ratio", 0.0) or 0.0), 3),
        "z_index": int(raw.get("z_index", 0) or 0),
        "has_close_button": bool(raw.get("has_close_button", False)),
        "iframe_ad_domain_hint": str(raw.get("iframe_ad_domain_hint", ""))[:MAX_HINT],
    }


def normalize_candidates(raws: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_candidate(raw, i) for i, raw in enumerate(raws[:MAX_CANDIDATES])]


def classify_candidates(jev: Jev, candidates: list[dict[str, Any]], min_probability: float = MIN_PROBABILITY, min_confidence: float = MIN_CONFIDENCE) -> list[dict[str, Any]]:
    """候補ごとに Jev に種類を聞く。厳格な受け入れ (probability と confidence が両方閾値以上) でなければ keep。"""
    candidates = normalize_candidates(candidates)
    if not candidates:
        return []
    question = {"kind": Choice(CATEGORIES, "この要素はページ本来の内容か、それとも隠してよいクラッタか? 迷ったら keep か uncertain")}
    decisions = jev.decide_many((candidate, question) for candidate in candidates)
    results = []
    for candidate, decision in zip(candidates, decisions):
        answer = decision.choice("kind")
        probability = answer.probabilities.get(answer.choice, 0.0)
        accepted = answer.choice in HIDE_LABELS and probability >= min_probability and answer.confidence >= min_confidence
        results.append(
            {
                "idx": candidate["idx"],
                "raw_label": answer.choice,
                "label": answer.choice if accepted else "keep",
                "probability": round(probability, 4),
                "confidence": round(answer.confidence, 4),
                "accepted": accepted,
                "selector": selector_for(candidate),
                "text": candidate["text"][:MAX_TEXT],
            }
        )
    return results


def selector_for(candidate: dict[str, Any]) -> str:
    """永続ルール用のセレクタ。id があれば id 属性、なければ拡張所有の属性で指す。"""
    if candidate.get("id_hint"):
        return f'[id="{candidate["id_hint"]}"]'
    return f'[{ATTR}="{candidate["idx"]}"]'


def build_css(results: list[dict[str, Any]]) -> str:
    """採用された候補だけを隠す CSS。属性セレクタのみで、!important で display:none。"""
    selectors = [f'[{ATTR}="{r["idx"]}"]' for r in results if r.get("accepted")]
    if not selectors:
        return ""
    return ",\n".join(selectors) + " { display: none !important; }"


def build_rules(host: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "host": host,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rules": [{"selector": r["selector"], "label": r["label"], "text": r["text"]} for r in results if r.get("accepted")],
    }


def rules_path(rules_dir: str | Path, host: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in host) or "unknown"
    return Path(rules_dir) / f"{safe}.json"


def save_rules(rules_dir: str | Path, rules: dict[str, Any]) -> Path:
    path = rules_path(rules_dir, rules["host"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_rules(rules_dir: str | Path, host: str) -> dict[str, Any] | None:
    path = rules_path(rules_dir, host)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def css_from_rules(rules: dict[str, Any]) -> str:
    selectors = [r["selector"] for r in rules.get("rules", [])]
    return ",\n".join(selectors) + " { display: none !important; }" if selectors else ""


STYLE_ID = "jevlab-unclutter-style"
INJECT_STYLE_JS = """
(css) => {
  let s = document.getElementById("%s");
  if (!s) { s = document.createElement("style"); s.id = "%s"; document.head.appendChild(s); }
  s.textContent = css;
}
""" % (STYLE_ID, STYLE_ID)
REMOVE_STYLE_JS = '() => { const s = document.getElementById("%s"); if (s) s.remove(); }' % STYLE_ID  # 解除 (可逆)


# ---------------------------------------------------------------------------
# (a) Playwright ランナー
# ---------------------------------------------------------------------------


def run_playwright(url: str, jev: Jev, headless: bool = True, rules_dir: str | Path = "unclutter_rules", dry_run: bool = False, wait_ms: int = 1500) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as error:  # pragma: no cover - 依存なし環境
        raise SystemExit("playwright が必要です: pip install playwright && playwright install chromium") from error
    from urllib.parse import urlparse

    host = urlparse(url).hostname or "unknown"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        candidates = page.evaluate(COLLECTOR_JS)
        results = classify_candidates(jev, candidates)
        css = build_css(results)
        if css and not dry_run:
            page.evaluate(INJECT_STYLE_JS, css)
        rules = build_rules(host, results)
        if not dry_run:
            save_rules(rules_dir, rules)
        if not headless and not dry_run:
            page.wait_for_timeout(5000)
        browser.close()
    return {"host": host, "candidates": len(candidates), "hidden": len(rules["rules"]), "css": css, "results": results}


# ---------------------------------------------------------------------------
# (b) 拡張機能向けローカルサーバ
# ---------------------------------------------------------------------------


def handle_request(jev: Jev, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /classify の本体。{host, candidates[]} → {results, css, rules}。テストから直接呼べる。"""
    results = classify_candidates(jev, list(payload.get("candidates", [])))
    return {"results": results, "css": build_css(results), "rules": build_rules(str(payload.get("host", "unknown")), results)}


def serve(jev: Jev, port: int = 8765, rules_dir: str | Path = "unclutter_rules") -> None:
    """拡張機能からの POST /classify を受ける http.server。テストでは起動しない。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204, {})

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/rules/"):
                rules = load_rules(rules_dir, self.path[len("/rules/") :])
                self._send(200, rules or {"rules": []})
                return
            self._send(200, {"ok": True, "backend": jev.backend.name})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                response = handle_request(jev, payload)
                if response["rules"]["rules"]:
                    save_rules(rules_dir, response["rules"])
                self._send(200, response)
            except Exception as error:  # noqa: BLE001
                self._send(400, {"error": str(error)})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[unclutter serve] {fmt % args}", file=sys.stderr)

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"unclutter serve: http://127.0.0.1:{port}/classify (backend={jev.backend.name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab unclutter", description=__doc__.split("\n")[0])
    parser.add_argument("--backend", default=None, help="Jev バックエンド (typesafe/openrouter/mock)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Playwright でページを開き、クラッタを隠す")
    run.add_argument("--url", required=True)
    run.add_argument("--headless", action="store_true")
    run.add_argument("--rules-dir", default="unclutter_rules")
    run.add_argument("--dry-run", action="store_true", help="判定だけして CSS を注入・保存しない")
    srv = sub.add_parser("serve", help="拡張機能向けローカル HTTP サーバ")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--rules-dir", default="unclutter_rules")
    cls = sub.add_parser("classify", help="候補 JSON をオフラインで分類")
    cls.add_argument("--json", required=True, help="候補配列 (または {host, candidates}) の JSON ファイル")
    cls.add_argument("--host", default="unknown")
    args = parser.parse_args(argv)
    jev = Jev(args.backend)

    if args.command == "classify":
        with open(args.json, encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            payload = {"host": args.host, "candidates": payload}
        response = handle_request(jev, payload)
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0
    if args.command == "serve":
        serve(jev, args.port, args.rules_dir)
        return 0
    result = run_playwright(args.url, jev, headless=args.headless, rules_dir=args.rules_dir, dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
