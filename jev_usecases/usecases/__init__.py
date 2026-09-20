"""ユースケースのレジストリ。"""

from __future__ import annotations

from .base import Outcome, UseCase, band
from .compact import CompactUseCase
from .complexity import ComplexityUseCase
from .feed import FeedUseCase
from .guard import GuardUseCase
from .rank import RankUseCase
from .review import ReviewUseCase
from .route import RouteUseCase
from .sample_pick import SamplePickUseCase
from .sniff import SniffUseCase
from .triage import TriageUseCase
from .viral import ViralUseCase

USECASES: dict[str, type[UseCase]] = {
    cls.name: cls
    for cls in (
        TriageUseCase,
        GuardUseCase,
        ReviewUseCase,
        ComplexityUseCase,
        SniffUseCase,
        ViralUseCase,
        FeedUseCase,
        RankUseCase,
        CompactUseCase,
        RouteUseCase,
        SamplePickUseCase,
    )
}


def get_usecase(name: str, **kwargs) -> UseCase:
    try:
        return USECASES[name](**kwargs)
    except KeyError:
        raise KeyError(f"unknown usecase {name!r}; available: {', '.join(USECASES)}") from None


__all__ = ["USECASES", "Outcome", "UseCase", "band", "get_usecase"]
