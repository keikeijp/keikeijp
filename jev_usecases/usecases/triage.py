"""01/API 最小例: 問い合わせの仕分け。

記事末尾の `refundRequested` の boolean 例を、担当部署の選択・緊急度の採点・
人が確認すべきかの判断まで広げたもの。返信文の生成や返金処理は行わない。
"""

from __future__ import annotations

from typing import Any

from ..client import Result
from ..questions import choice, noul, score
from .base import Outcome, UseCase, band, state_text

DEPARTMENTS = {
    "billing": "Charges, invoices, refunds, duplicate payments, subscription changes.",
    "bug": "Something is broken, an error message, a crash, data not saving, a feature not working.",
    "howto": "Asking how to use a feature or where a setting is; nothing is broken.",
    "account": "Login, password reset, email change, account deletion, permissions.",
    "sales": "Pricing questions, quotes, plan comparison, enterprise contracts.",
    "other": "None of the other departments clearly fits.",
}

URGENCY = [
    "Can wait: no deadline, general question.",
    "This week: the customer is inconvenienced but working.",
    "Today: the customer is blocked or losing money.",
    "Right now: outage, security incident, legal threat, or data loss in progress.",
]


class TriageUseCase(UseCase):
    name = "triage"
    title = "問い合わせの仕分け"
    summary = "担当部署・緊急度・返金要望・人が見るべきかを 1 回の呼び出しで判断する"
    article_ref = "API の最小例 / 08 MCP 接続"
    label_field = "department"
    positive_labels = frozenset({"billing", "bug", "account"})

    def __init__(self, *, act_threshold: float = 0.8, review_threshold: float = 0.55):
        self.act = act_threshold
        self.review = review_threshold

    def from_text(self, text: str) -> Any:
        return {"body": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = state_text(item, "subject", "body", "customer_tier", "product")
        questions = {
            "department": choice("Which department should handle this ticket?", DEPARTMENTS),
            "urgency": score("How urgent is this ticket?", URGENCY),
            "refund_requested": noul("Is the customer asking for a refund or a reversal of a charge?"),
            "needs_human": noul(
                "Does this ticket need a human agent rather than an automated reply?",
                true="Complaint, legal or safety issue, ambiguous request, or an angry customer.",
                false="A routine question that a canned answer or a FAQ link can resolve.",
            ),
        }
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        dept = result.choice("department")
        urgency = result.score("urgency")
        refund = result.noul("refund_requested")
        human = result.noul("needs_human")
        confidence_band = band(dept.confidence, act=self.act, review=self.review)
        needs_human = human >= 0.5 or confidence_band != "act" or urgency.level >= 3
        return {
            "department": dept.choice,
            "department_confidence": round(dept.confidence, 3),
            "band": confidence_band,
            "urgency": urgency.level,
            "urgency_label": urgency.label,
            "refund_requested": refund >= 0.5,
            "refund_probability": round(refund, 3),
            "needs_human": needs_human,
            "queue": "human_review" if needs_human else f"auto:{dept.choice}",
        }

    def example_items(self) -> list[Any]:
        return [
            {"subject": "Charged twice", "body": "I was charged twice this month for the same subscription. Please refund the duplicate charge today.", "customer_tier": "business"},
            {"subject": "Export button", "body": "Where is the CSV export? I can't find it in the settings page.", "customer_tier": "free"},
            {"subject": "App crashes on login", "body": "Since yesterday's update the app crashes right after I enter my password. I can't work at all.", "customer_tier": "pro"},
            {"subject": "Enterprise pricing", "body": "We have 300 seats. Can you send a quote and the SSO options?", "customer_tier": "trial"},
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        subj = outcome.input.get("subject") if isinstance(outcome.input, dict) else ""
        return f"[{d['queue']}] {subj!s:30.30} dept={d['department']}({d['department_confidence']:.2f}) urgency={d['urgency']} refund={d['refund_requested']} human={d['needs_human']}"
