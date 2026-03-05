from dataclasses import dataclass, field
from enum import Enum



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


