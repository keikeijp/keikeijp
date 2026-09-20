"""jev_usecases: TypeSafe AI の判断モデル Jev を業務に組み込むためのユースケース集。

Jev は文章を生成せず、渡した状態 (state) に対して「どれを選ぶか (choice)」
「基準のどの段階か (score)」「ある条件が成り立つ確率 (noul)」を返すモデル。
このパッケージは公式 REST API (`POST /v1/systemone`) を薄くラップし、
記事で紹介されている使い方を実行可能なパイプラインとして実装する。

    from jev_usecases import JevClient
    from jev_usecases.usecases import get_usecase

    client = JevClient.from_env()          # TYPESAFE_API_KEY を読む
    triage = get_usecase("triage")
    for outcome in triage.run(client, triage.example_items()):
        print(outcome.decision)
"""

from .client import (
    Answer,
    ChoiceAnswer,
    HttpBackend,
    JevAPIError,
    JevClient,
    JevError,
    MockBackend,
    NoulAnswer,
    Result,
    ScoreAnswer,
    ScriptedBackend,
    estimate_cost_usd,
)
from .questions import choice, noul, score

__all__ = [
    "Answer",
    "ChoiceAnswer",
    "HttpBackend",
    "JevAPIError",
    "JevClient",
    "JevError",
    "MockBackend",
    "NoulAnswer",
    "Result",
    "ScoreAnswer",
    "ScriptedBackend",
    "choice",
    "estimate_cost_usd",
    "noul",
    "score",
]
