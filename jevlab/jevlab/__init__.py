"""jevlab: Jev (TypeSafe System One) と SAM 3.1 を組み合わせた実験場。

- `jevlab.core`  : Jev クライアント (choice / score / noul)。TypeSafe 直接・OpenRouter・ローカル・モックの各バックエンド
- `jevlab.sam`   : Meta Model API 経由の SAM 3.1 クライアントとシーングラフ
- `jevlab.apps`  : 30 本の Jev ユースケース実装
- `jevlab.local` : 公開モデルで Jev 風の判断をローカルで行う研究実装
- `jevlab.scenejudge` : CV x Jev のフラッグシップ
"""

from jevlab.core import Choice, Decision, Jev, Noul, Score

__all__ = ["Jev", "Choice", "Score", "Noul", "Decision"]
__version__ = "0.1.0"
