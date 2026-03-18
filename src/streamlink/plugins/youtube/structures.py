from enum import StrEnum, auto, Enum
from typing import Protocol, Optional
from dataclasses import dataclass, field

from streamlink.session.session import Streamlink


class JsChallengeType(Enum):
    N = 'n'


@dataclass(frozen=True)
class NChallengeInput:
    player_url: str
    challenge: str


@dataclass(frozen=True)
class JsChallengeRequest:
    type: JsChallengeType
    input: NChallengeInput
    video_id: str | None = None


@dataclass(frozen=True)
class NChallengeOutput:
    results: dict[str, str] = field(default_factory=dict)


@dataclass
class JsChallengeResponse:
    type: JsChallengeType
    output: NChallengeOutput


@dataclass
class JsChallengeProviderResponse:
    request: JsChallengeRequest
    response: JsChallengeResponse | None = None
    error: Exception | None = None


class ExtractorType(StrEnum):
    """Types of YouTube extractors."""
    VIDEO = auto()
    TAB = auto()


@dataclass(frozen=True)
class NextExtractor:
    """Pointer to next extractor in the chain."""
    extractor: ExtractorType
    url: str


@dataclass(frozen=True)
class ExtractorResult:
    """Result from an extractor: either next step or final HLS URLs."""
    next: Optional[NextExtractor] = None
    hls: list[str] | None = None


class Extractor(Protocol):
    """Protocol for YouTube extractors."""
    valid_url_re: str
    extractor_type: ExtractorType

    def extract(self, url: str) -> ExtractorResult:
        ...


class JsSolver(Protocol):
    """Protocol for JavaScript solvers."""
    def solve(self, player_url: str, challenge: str) -> JsChallengeResponse | None:
        ...


@dataclass
class Context:
    """Shared context for extractors."""
    session: Streamlink = None
    deno: JsSolver = None


ctx = Context()
