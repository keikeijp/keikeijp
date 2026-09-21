"""22. home (AboveColin/HA-Jev の再実装): Home Assistant の状態を Jev に判定させ、日常の自動化に通知する。

**安全用途には使わない。** 鍵・警報・コンロ・ガス・医療機器の制御や監視には絶対に使わないこと。
このアプリが呼ぶ HA サービスは `notify.*` だけで、それ以外のサービス (lock / alarm_control_panel / switch など) は呼べない。

- `HAClient`: REST (`GET /api/states`, `GET /api/history/period`, `POST /api/services/notify/<service>`) を urllib で遅延呼び出し。
  長期アクセストークンは環境変数から
- `StaticHA`: JSON ({"states": [...], "history": {entity_id: [...]}}) のオフライン実装
- ルール (YAML/JSON): {name, entities[], question, threshold, cooldown_minutes, history_hours?, notify: {service, message}}
- `evaluate(jev, ha, rules, memory)`: ルール毎に、そのルールが列挙した entity だけの小さな state を作り Noul を聞く。
  threshold 以上で cooldown を過ぎていれば通知 (dry-run では表示のみ)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from jevlab.core import Jev, Noul

DEFAULT_THRESHOLD = 0.8
DEFAULT_COOLDOWN_MINUTES = 60
ATTR_KEYS = ("unit_of_measurement", "device_class", "friendly_name", "brightness", "temperature", "current_temperature", "hvac_mode", "battery_level", "media_title")
MAX_HISTORY_POINTS = 24
FORBIDDEN_DOMAINS = ("lock", "alarm_control_panel", "cover", "climate", "switch", "valve", "siren")  # 判定対象にはできるが、操作は一切しない
UNSAFE_HINTS = ("lock", "alarm", "stove", "oven", "gas", "smoke", "co_", "carbon", "medical", "コンロ", "鍵", "警報")


# ---------------------------------------------------------------------------
# HA クライアント
# ---------------------------------------------------------------------------


class HAClient:
    """Home Assistant REST API。token は環境変数 (既定 HA_TOKEN) から。"""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.sent: list[dict[str, Any]] = []

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        import urllib.request

        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            text = response.read().decode("utf-8")
        return json.loads(text) if text else None

    def states(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/states"))

    def history(self, entity_id: str, hours: float) -> list[dict[str, Any]]:
        start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - hours * 3600))
        result = self._request("GET", f"/api/history/period/{start}?filter_entity_id={entity_id}&minimal_response")
        return list(result[0]) if result else []

    def notify(self, service: str, message: str, title: str | None = None) -> None:
        service = service.split(".", 1)[-1]  # "notify.mobile_app_x" → "mobile_app_x"
        body = {"message": message, **({"title": title} if title else {})}
        self._request("POST", f"/api/services/notify/{service}", body)
        self.sent.append({"service": service, "message": message})


class StaticHA:
    """JSON からのオフライン実装。notify は記録するだけ。"""

    def __init__(self, data: dict[str, Any]):
        self._states = list(data.get("states", []))
        self._history = dict(data.get("history", {}))
        self.sent: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticHA":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def states(self) -> list[dict[str, Any]]:
        return self._states

    def history(self, entity_id: str, hours: float) -> list[dict[str, Any]]:
        return list(self._history.get(entity_id, []))[-MAX_HISTORY_POINTS:]

    def notify(self, service: str, message: str, title: str | None = None) -> None:
        self.sent.append({"service": service, "message": message, "title": title})


# ---------------------------------------------------------------------------
# ルールと評価
# ---------------------------------------------------------------------------


def load_rules(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as error:  # pragma: no cover
            raise SystemExit("YAML ルールには pyyaml が必要です: pip install pyyaml (または JSON で書く)") from error
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    rules = data["rules"] if isinstance(data, dict) else data
    for rule in rules:
        validate_rule(rule)
    return list(rules)


def validate_rule(rule: dict[str, Any]) -> None:
    for key in ("name", "entities", "question", "notify"):
        if key not in rule:
            raise ValueError(f"ルール {rule.get('name', '?')!r} に {key} がありません")
    service = str(rule["notify"].get("service", ""))
    if not service or (service.split(".", 1)[0] not in ("notify",) and "." in service):
        raise ValueError(f"ルール {rule['name']!r}: notify.service は notify.* だけ許可されます ({service!r})")
    name_q = (rule["name"] + " " + rule["question"]).lower()
    if any(hint in name_q for hint in UNSAFE_HINTS):
        print(f"[home] 警告: ルール {rule['name']!r} は安全に関わる用途に見えます。このアプリは安全用途に使ってはいけません", file=sys.stderr)


def compact_state(ha: Any, rule: dict[str, Any], states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """ルールが列挙した entity だけを、state + 一部の属性 + (任意で) 直近履歴に絞って返す。"""
    index = {s.get("entity_id"): s for s in (states if states is not None else ha.states())}
    entities: dict[str, Any] = {}
    history_hours = float(rule.get("history_hours", 0) or 0)
    for entity_id in rule["entities"]:
        state = index.get(entity_id)
        if state is None:
            entities[entity_id] = {"state": "unavailable"}
            continue
        attrs = state.get("attributes", {}) or {}
        entry: dict[str, Any] = {"state": state.get("state"), "attributes": {k: attrs[k] for k in ATTR_KEYS if k in attrs}, "last_changed": state.get("last_changed")}
        if history_hours > 0:
            entry["recent_history"] = [{"state": h.get("state"), "t": h.get("last_changed", h.get("last_updated"))} for h in ha.history(entity_id, history_hours)][-MAX_HISTORY_POINTS:]
        entities[entity_id] = entry
    return {"rule": rule["name"], "now": time.strftime("%Y-%m-%d %H:%M"), "entities": entities}


def evaluate(jev: Jev, ha: Any, rules: list[dict[str, Any]], memory: dict[str, Any], now: float | None = None, dry_run: bool = True) -> list[dict[str, Any]]:
    """全ルールを 1 回ずつ判定し、閾値以上かつ cooldown を過ぎたものを通知する。memory はルール名 → 最終通知時刻 (epoch)。"""
    now = time.time() if now is None else now
    states = ha.states()
    items = [(compact_state(ha, rule, states), {"answer": Noul(rule["question"])}) for rule in rules]
    decisions = jev.decide_many(items) if items else []
    results = []
    for rule, decision in zip(rules, decisions):
        probability = decision.noul("answer").noul
        threshold = float(rule.get("threshold", DEFAULT_THRESHOLD))
        cooldown = float(rule.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)) * 60
        last = memory.get(rule["name"])
        fired = probability >= threshold
        in_cooldown = fired and last is not None and (now - float(last)) < cooldown  # 未通知なら cooldown は無い
        result = {"rule": rule["name"], "probability": round(probability, 3), "threshold": threshold, "fired": fired, "in_cooldown": in_cooldown, "notified": False}
        if fired and not in_cooldown:
            message = str(rule["notify"].get("message", rule["name"])).replace("{probability}", f"{probability:.0%}")
            result["message"] = message
            if dry_run:
                print(f"[dry-run] notify {rule['notify']['service']}: {message} (p={probability:.2f})")
            else:
                ha.notify(rule["notify"]["service"], message, rule["notify"].get("title"))
            memory[rule["name"]] = now
            result["notified"] = True
        results.append(result)
    return results


def load_memory(path: str | None) -> dict[str, Any]:
    if path and Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def save_memory(path: str | None, memory: dict[str, Any]) -> None:
    if path:
        Path(path).write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")


def loop(jev: Jev, ha: Any, rules: list[dict[str, Any]], memory_path: str | None, interval: float, dry_run: bool) -> None:  # pragma: no cover - 常駐用
    memory = load_memory(memory_path)
    print(f"home loop: {len(rules)} rules every {interval:.0f}s dry_run={dry_run} (Ctrl-C で終了)")
    try:
        while True:
            try:
                results = evaluate(jev, ha, rules, memory, dry_run=dry_run)
                save_memory(memory_path, memory)
                print(json.dumps(results, ensure_ascii=False))
            except Exception as error:  # noqa: BLE001 - HA 一時停止などで止まらないようにする
                print(f"[home] error: {error}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab home", description="Home Assistant の状態を Jev で判定して通知する (安全用途には使わない)")
    parser.add_argument("--rules", required=True, help="ルール YAML / JSON")
    parser.add_argument("--states", default=None, help="オフライン用の状態 JSON ({states, history})")
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://homeassistant.local:8123"))
    parser.add_argument("--token-env", default="HA_TOKEN")
    parser.add_argument("--memory", default=None, help="cooldown 用の最終通知時刻 JSON")
    parser.add_argument("--dry-run", action="store_true", help="通知を送らず表示だけ")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=300.0, help="--loop の間隔 (秒)")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)
    if args.states:
        ha: Any = StaticHA.from_file(args.states)
    else:
        token = os.environ.get(args.token_env, "").strip()
        if not token:
            print(f"環境変数 {args.token_env} に Home Assistant の長期アクセストークンを設定するか、--states でオフライン JSON を渡してください", file=sys.stderr)
            return 2
        ha = HAClient(args.ha_url, token)
    jev = Jev(args.backend)
    if args.loop:
        loop(jev, ha, rules, args.memory, args.interval, args.dry_run)
        return 0
    memory = load_memory(args.memory)
    results = evaluate(jev, ha, rules, memory, dry_run=args.dry_run)
    save_memory(args.memory, memory)
    print(json.dumps({"results": results, "jev": jev.stats()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
