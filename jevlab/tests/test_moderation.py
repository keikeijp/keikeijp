import json

from jevlab.apps import moderation
from jevlab.core import Jev, ScriptedBackend


def _msg(**overrides):
    base = {"content": "hello everyone", "author_age_days": 200, "has_links": False, "link_domains": [], "mentions_count": 0, "is_new_member": False, "recent_messages_by_author": []}
    return {**base, **overrides}


def test_action_policy_thresholds_with_scripted_backend():
    backend = ScriptedBackend(
        [
            {"kind": "ok", "severity": 0, "is_scam_url": False},
            {"kind": "off_topic", "severity": 1, "is_scam_url": False},
            {"kind": "spam", "severity": 2, "is_scam_url": False},
            {"kind": "harassment", "severity": 3, "is_scam_url": False},
            {"kind": "scam_link", "severity": 2, "is_scam_url": 0.97},
        ]
    )
    jev = Jev(backend)
    actions = [moderation.moderate(jev, _msg())["decision"]["action"] for _ in range(5)]
    assert actions == ["none", "warn", "delete", "timeout", "delete"]
    timeout = moderation.decide_action({"kind": "harassment", "confidence": 0.9, "severity_level": 3, "is_scam_url": 0.0, "is_new_member": False})
    assert timeout["minutes"] == 60
    custom = moderation.decide_action({"kind": "spam", "confidence": 0.9, "severity_level": 2, "is_scam_url": 0.0}, {"delete_level": 3, "timeout_minutes": 10})
    assert custom["action"] == "warn"
    # 送った state に content の切り詰めとドメインのみが入る
    state = backend.calls[0]["state"]
    assert set(state) >= {"content", "link_domains", "mentions_count", "recent_messages_by_author"}


def test_low_confidence_always_flags_for_admin():
    backend = ScriptedBackend([{"kind": {"spam": 0.4, "ok": 0.35, "off_topic": 0.25}, "severity": 2, "is_scam_url": False}])
    result = moderation.moderate(Jev(backend), _msg(content="check this out"))
    assert result["assessment"]["confidence"] < 0.6
    assert result["decision"]["action"] == "flag_for_admin"
    assert moderation.decide_action({"kind": "spam", "confidence": 0.3, "severity_level": 3, "is_scam_url": 0.99})["action"] == "flag_for_admin"


def test_new_member_escalation_and_scam_timeout():
    new_member = moderation.decide_action({"kind": "self_promo", "confidence": 0.95, "severity_level": 1, "is_scam_url": 0.1, "is_new_member": True})
    assert new_member["action"] == "delete"  # mild → moderate に 1 段階エスカレート
    scam = moderation.decide_action({"kind": "scam_link", "confidence": 0.95, "severity_level": 3, "is_scam_url": 0.9})
    assert scam["action"] == "timeout" and scam["minutes"] == 60


def test_main_assess(tmp_path, capsys):
    path = tmp_path / "msg.json"
    path.write_text(json.dumps(_msg(content="FREE NITRO click here", has_links=True, link_domains=["discord-nitro.example"])), encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    assert moderation.main(["--backend", "mock", "--audit", str(audit), "assess", "--json", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"]["action"] in moderation.ACTIONS
    assert audit.read_text(encoding="utf-8").count("\n") == 1
