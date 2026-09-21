"""20. moderation (brainstormity/Jev-Moderation-Bot の再実装): Discord のスパム/詐欺 URL/嫌がらせ判定と処分。

純粋なコア:
- `assess(jev, message)`  1 回の Jev 呼び出しで `kind` Choice / `severity` Score / `is_scam_url` Noul を取る
- `decide_action(assessment, policy)` 閾値で {none, warn, delete, timeout(minutes), flag_for_admin} を決める。
  confidence が低ければ**必ず** flag_for_admin (人間に回す)

Discord ボット (`run_bot`) は discord.py を遅延 import し、`--dry-run` では処分を実行せず表示だけ。監査ログは JSONL。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from jevlab.core import Choice, Jev, Noul, Score

KINDS: dict[str, str] = {
    "ok": "問題のない通常のメッセージ",
    "spam": "無意味な連投、宣伝の貼り付け、大量メンション",
    "scam_link": "詐欺/フィッシング/偽ギフト/偽エアドロップの URL 誘導",
    "harassment": "個人攻撃、差別、脅迫",
    "nsfw": "性的/グロテスクな内容や誘導",
    "self_promo": "自分のサーバー/動画/商品の宣伝 (詐欺ではない)",
    "off_topic": "チャンネルの趣旨から外れている",
}
SEVERITY = ["none: 処分不要", "mild: 注意で足りる", "moderate: 削除が妥当", "severe: 一時的に発言禁止が妥当"]
ACTIONS = ("none", "warn", "delete", "timeout", "flag_for_admin")

DEFAULT_POLICY: dict[str, Any] = {
    "min_confidence": 0.6,  # これ未満は必ず flag_for_admin
    "scam_url_threshold": 0.8,  # is_scam_url がこれ以上なら削除
    "warn_level": 1,
    "delete_level": 2,
    "timeout_level": 3,
    "timeout_minutes": 60,
    "new_member_days": 3,  # 新規メンバーは処分を 1 段階厳しくする
    "escalate_new_members": True,
}

MAX_CONTENT = 500


# ---------------------------------------------------------------------------
# 純粋コア
# ---------------------------------------------------------------------------


def compact_message(message: dict[str, Any]) -> dict[str, Any]:
    """Jev に渡す最小限の構造化 state。ユーザー ID やフル URL は含めない (ドメインのみ)。"""
    recent = [str(m)[:120] for m in list(message.get("recent_messages_by_author", []))[-5:]]
    return {
        "content": str(message.get("content", ""))[:MAX_CONTENT],
        "author_age_days": int(message.get("author_age_days", 0) or 0),
        "is_new_member": bool(message.get("is_new_member", False)),
        "has_links": bool(message.get("has_links", False)),
        "link_domains": [str(d)[:60] for d in list(message.get("link_domains", []))[:5]],
        "mentions_count": int(message.get("mentions_count", 0) or 0),
        "recent_messages_by_author": recent,
        "channel_topic": str(message.get("channel_topic", ""))[:120],
    }


def assess(jev: Jev, message: dict[str, Any]) -> dict[str, Any]:
    state = compact_message(message)
    decision = jev.decide(
        state,
        {
            "kind": Choice(KINDS, "このメッセージの種類は?"),
            "severity": Score(SEVERITY, "どの程度の処分が妥当か?"),
            "is_scam_url": Noul("含まれるリンクは詐欺/フィッシングか? (リンクがなければ no)"),
        },
    )
    kind = decision.choice("kind")
    severity = decision.score("severity")
    scam = decision.noul("is_scam_url").noul
    confidence = min(kind.confidence, severity.confidence)
    return {
        "kind": kind.choice,
        "kind_probability": round(kind.probabilities.get(kind.choice, 0.0), 4),
        "confidence": round(confidence, 4),
        "severity_level": severity.level,
        "severity_score": round(severity.score, 3),
        "is_scam_url": round(scam, 4),
        "is_new_member": state["is_new_member"] or (0 < state["author_age_days"] <= DEFAULT_POLICY["new_member_days"]),
        "latency_ms": round(decision.latency_ms, 1),
    }


def decide_action(assessment: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = {**DEFAULT_POLICY, **(policy or {})}
    if assessment["confidence"] < policy["min_confidence"]:
        return {"action": "flag_for_admin", "reason": f"confidence {assessment['confidence']:.2f} < {policy['min_confidence']}"}
    if assessment["is_scam_url"] >= policy["scam_url_threshold"] or assessment["kind"] == "scam_link":
        minutes = policy["timeout_minutes"] if assessment["severity_level"] >= policy["timeout_level"] else 0
        return {"action": "timeout" if minutes else "delete", "minutes": minutes, "reason": "scam link"}
    level = assessment["severity_level"]
    if policy["escalate_new_members"] and assessment.get("is_new_member") and assessment["kind"] != "ok" and level > 0:
        level = min(level + 1, len(SEVERITY) - 1)
    if assessment["kind"] == "ok" and level == 0:
        return {"action": "none", "reason": "ok"}
    if level >= policy["timeout_level"]:
        return {"action": "timeout", "minutes": policy["timeout_minutes"], "reason": f"{assessment['kind']} severity={level}"}
    if level >= policy["delete_level"]:
        return {"action": "delete", "reason": f"{assessment['kind']} severity={level}"}
    if level >= policy["warn_level"]:
        return {"action": "warn", "reason": f"{assessment['kind']} severity={level}"}
    return {"action": "none", "reason": f"{assessment['kind']} severity={level}"}


def moderate(jev: Jev, message: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    assessment = assess(jev, message)
    return {"assessment": assessment, "decision": decide_action(assessment, policy)}


def write_audit(path: str | Path | None, record: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), **record}, ensure_ascii=False) + "\n")


def load_policy(path: str | None) -> dict[str, Any]:
    if not path:
        return dict(DEFAULT_POLICY)
    with open(path, encoding="utf-8") as handle:
        return {**DEFAULT_POLICY, **json.load(handle)}


# ---------------------------------------------------------------------------
# Discord ボット (discord.py を遅延 import)
# ---------------------------------------------------------------------------

ADMIN_HELP = """管理者コマンド (メッセージ管理権限が必要):
  !jev override <message_id> none|warn|delete|timeout  直前の判定を上書きして実行/取消 (監査ログに記録)
  !jev policy                                           現在の閾値を表示
  !jev dry-run on|off                                  処分の実行/表示だけ を切り替え
"""


def run_bot(token: str, jev: Jev, policy: dict[str, Any] | None = None, dry_run: bool = True, audit_path: str | None = "moderation_audit.jsonl") -> None:  # pragma: no cover - 実機
    try:
        import discord  # type: ignore
    except ImportError as error:
        raise SystemExit("discord.py が必要です: pip install 'discord.py>=2.3'") from error
    import datetime

    policy = {**DEFAULT_POLICY, **(policy or {})}
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)
    state = {"dry_run": dry_run, "last": {}}

    async def apply(message: Any, decision: dict[str, Any]) -> None:
        action = decision["action"]
        if state["dry_run"]:
            print(f"[dry-run] {action} {decision.get('minutes', '')} <- {message.author} : {message.content[:80]!r}")
            return
        if action == "warn":
            await message.reply("このメッセージはサーバーのルールに触れる可能性があります。ご注意ください。", mention_author=True)
        elif action == "delete":
            await message.delete()
        elif action == "timeout":
            await message.delete()
            await message.author.timeout(datetime.timedelta(minutes=decision.get("minutes", 60)), reason="jevlab moderation")
        elif action == "flag_for_admin":
            await message.add_reaction("🚩")

    @client.event
    async def on_message(message: Any) -> None:
        if message.author.bot:
            return
        if message.content.startswith("!jev ") and message.author.guild_permissions.manage_messages:
            parts = message.content.split()
            if parts[1:2] == ["policy"]:
                await message.reply(f"```{json.dumps(policy, ensure_ascii=False, indent=1)}```")
            elif parts[1:2] == ["dry-run"] and len(parts) > 2:
                state["dry_run"] = parts[2] == "on"
                await message.reply(f"dry_run={state['dry_run']}")
            elif parts[1:2] == ["override"] and len(parts) > 3 and parts[3] in ACTIONS:
                target = await message.channel.fetch_message(int(parts[2]))
                decision = {"action": parts[3], "minutes": policy["timeout_minutes"], "reason": f"admin override by {message.author}"}
                write_audit(audit_path, {"event": "override", "message_id": parts[2], "decision": decision})
                await apply(target, decision)
            else:
                await message.reply(f"```{ADMIN_HELP}```")
            return
        joined = getattr(message.author, "joined_at", None)
        age_days = (datetime.datetime.now(datetime.timezone.utc) - joined).days if joined else 0
        payload = {
            "content": message.content,
            "author_age_days": age_days,
            "is_new_member": age_days <= policy["new_member_days"],
            "has_links": "http" in message.content,
            "link_domains": sorted({w.split("/")[2] for w in message.content.split() if w.startswith("http") and w.count("/") >= 2}),
            "mentions_count": len(message.mentions) + len(message.role_mentions),
            "recent_messages_by_author": state["last"].get(message.author.id, []),
            "channel_topic": getattr(message.channel, "topic", "") or "",
        }
        state["last"].setdefault(message.author.id, []).append(message.content[:120])
        state["last"][message.author.id] = state["last"][message.author.id][-5:]
        result = moderate(jev, payload, policy)
        write_audit(audit_path, {"event": "assess", "message_id": message.id, "author": str(message.author), **result})
        await apply(message, result["decision"])

    client.run(token)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab moderation", description="Discord メッセージのスパム/詐欺 URL/嫌がらせ判定と処分")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--policy", default=None, help="閾値を上書きする JSON")
    parser.add_argument("--audit", default=None, help="監査ログ JSONL の保存先")
    sub = parser.add_subparsers(dest="command", required=True)
    ass = sub.add_parser("assess", help="メッセージ JSON をオフラインで判定")
    ass.add_argument("--json", required=True, help="{content, author_age_days, has_links, link_domains, mentions_count, is_new_member, recent_messages_by_author} または配列")
    run = sub.add_parser("run", help="Discord ボットを起動 (要 discord.py)")
    run.add_argument("--token-env", default="DISCORD_TOKEN")
    run.add_argument("--dry-run", action="store_true", help="処分を実行せず表示だけ")
    args = parser.parse_args(argv)
    jev = Jev(args.backend)
    policy = load_policy(args.policy)

    if args.command == "assess":
        with open(args.json, encoding="utf-8") as handle:
            data = json.load(handle)
        messages = data if isinstance(data, list) else [data]
        results = []
        for message in messages:
            result = moderate(jev, message, policy)
            write_audit(args.audit, {"event": "assess", **result})
            results.append(result)
        print(json.dumps(results if isinstance(data, list) else results[0], ensure_ascii=False, indent=2))
        return 0
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"環境変数 {args.token_env} にボットトークンを設定してください", file=sys.stderr)
        return 2
    run_bot(token, jev, policy, dry_run=args.dry_run, audit_path=args.audit or "moderation_audit.jsonl")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
