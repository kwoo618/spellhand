"""에이전트 — 자연어 요청을 세션 계획으로 바꾼다 (PRD 13장).

역할 분담은 PRD 13.1 그대로다. **LLM 은 해석·선별·구성·배열·요약까지다.**
판정·적응·변환·clamp·락·통계는 규칙이 한다. 그래서 이 파일은 글자 이름과
난이도 파라미터(`hold_ms`·`boundary`)와 트랙만 다룬다 — flex·각도·시리얼
명령은 LLM 출력에 들어올 자리가 없다 (R1, LLM 격리).

흐름:

```
자연어 → LLM → JSON 파싱 → 검증 10항목 → (재생성 최대 2회) → 규칙 폴백
                                              ↓
                                        HITL 승인 대기
                                              ↓
                                          세션 생성
```

**검증을 통과해도 실행되지 않는다.** 사람이 승인해야 세션이 만들어진다
(PRD 13.3). 승인 대기 상태가 시연 ⑥ 의 "라이브 정지" 항목이다.

제약 검사는 `items.py` 의 `*_violations()` 를 그대로 쓴다. 같은 제약을 두 번
구현하지 않는다 — 누가 만든 문항이든 같은 관문을 지나야 한다
(items.py 머리말, DECISIONS.md §47).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from server.config import Config, WordSet
from server.db import (
    BOUNDARIES,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    TRACKS,
    Database,
    Learner,
    load_difficulty,
    utcnow,
)
from server.items import (
    RECENT_ITEM_WINDOW,
    Difficulty,
    ItemGenerationError,
    discrimination_violations,
    generate_discrimination_items,
    generate_word_items,
    heavy_letter_caps,
    word_violations,
)
from server.session import DIFFICULTY_LADDER, SessionPlan
from server.stats import (
    depth_unlocked,
    item_contains_depth_pair,
    recent_letters,
    track_summary,
    weakest_pairs,
)

__all__ = [
    "AgentError",
    "PlanParseError",
    "PlanNotFoundError",
    "PlanStateError",
    "LlmUnavailableError",
    "PlannedItem",
    "AgentPlan",
    "Finding",
    "ValidationReport",
    "PlanContext",
    "LlmReply",
    "LlmClient",
    "AnthropicClient",
    "PlanAgent",
    "PlanStore",
    "StoredPlan",
    "parse_plan_json",
    "build_context",
    "build_prompt",
    "validate_plan",
    "rule_plan",
    "current_difficulty",
    "DEFAULT_MODEL",
    "MODEL_ENV",
    "MAX_REGENERATIONS",
    "SOURCE_LLM",
    "SOURCE_RULE",
    "ACTION_REGENERATE",
    "ACTION_REMOVED",
    "ACTION_CLAMPED",
    "ACTION_WARNING",
]

logger = logging.getLogger(__name__)

# 모델은 환경변수로 바꿀 수 있다. 기본은 가장 성능이 좋은 것을 쓴다 — 세션당
# 한 번 부르는 호출이라 비용이 문제가 되지 않는다.
MODEL_ENV = "AGENT_MODEL"
DEFAULT_MODEL = "claude-opus-5"

# 계획 JSON 은 1~2KB 다. 그런데 Opus 5 는 사고가 기본으로 켜져 있고 그 토큰도
# max_tokens 에 들어가므로 넉넉히 준다. 스트리밍 없이 안전한 범위다.
MAX_TOKENS = 16000

# 서버측 거절 폴백. `fallbacks="default"` 의 스칼라 형식과 짝이 맞는 베타 헤더다
# (배열 형식은 `-2026-06-01` 이고, 형식과 헤더를 섞으면 400 이다).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# 재생성 상한 (PRD 13.3 "재생성 (최대 2회)"). 첫 호출 + 재생성 2회 = 최대 3회.
MAX_REGENERATIONS = 2

SOURCE_LLM = "llm"
SOURCE_RULE = "rule"

# 검증 결과의 처리 방식 (PRD 13.3 의 "실패 시" 열).
ACTION_REGENERATE = "regenerate"
ACTION_REMOVED = "removed"
ACTION_CLAMPED = "clamped"
ACTION_WARNING = "warning"

# PRD 13.3 의 10항목. 이름을 상수로 둔 이유는 응답과 테스트가 같은 문자열을
# 보게 하기 위해서다 — 화면에 뜨는 검사 이름을 라우트에서 새로 짓지 않는다.
CHECK_LETTERS = "letters_in_poses"
CHECK_WORD_POOL = "word_in_pool"
CHECK_HOLD_RANGE = "hold_ms_range"
CHECK_LENGTH = "length_range"
CHECK_REPEAT = "no_adjacent_repeat"
CHECK_DEPTH_LOCKED = "depth_pair_locked"
CHECK_HEAVY_LETTER = "heavy_letter_ratio"
CHECK_WEAK_PAIRS = "weak_pairs_covered"
CHECK_RECENT = "recent_duplicate"
CHECK_ESTIMATE = "estimated_seconds"

# 10항목에 없지만 검사하는 두 가지. 트랙이나 경계 모드가 엉뚱하면 그 문항은
# **구동 단계에서** 거부된다 (게이트 ③ / `present_letters`). 승인된 계획이
# 구동되지 않는 문항을 품고 있으면 승인의 의미가 없으므로 여기서 걸러낸다
# (DECISIONS.md §47).
CHECK_TRACK = "track_valid"
CHECK_BOUNDARY = "boundary_valid"

# 연속 동일 글자를 가진 단어는 경계 모드를 explicit 로 낮춘다. 버리지 않는다 —
# `generate_word_items()` 가 같은 선택을 하며, 그 근거는 DECISIONS.md §9 다
# ("그 단어를 영영 못 내는 것보다 낫다"). 검증이 버리면 생성기와 어긋난다.
CHECK_WORD_BOUNDARY = "word_repeat_boundary"

# 문항 하나에 학습자가 답하는 데 걸리는 시간. PRD 18 의 TBD-5 추정값이고
# 설정 파일에 출처가 없어 여기 둔다 (DECISIONS.md §19). 4주차 실측으로 갱신한다.
ASSUMED_RESPONSE_SECONDS = 6

# `estimated_seconds` 허용 오차 (PRD 13.3).
ESTIMATE_TOLERANCE = 0.2

# 사다리가 시퀀스 길이의 단일 출처다 (session.DIFFICULTY_LADDER).
LENGTH_MIN = min(length for length, _ in DIFFICULTY_LADDER)
LENGTH_MAX = max(length for length, _ in DIFFICULTY_LADDER)

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*(?P<body>.*?)\s*```", re.DOTALL)


# --------------------------------------------------------------------------- #
# 구조화된 실패
# --------------------------------------------------------------------------- #

class AgentError(Exception):
    """에이전트 계층의 구조화된 실패. 라우트가 코드 그대로 실어 보낸다."""

    code = "AGENT_ERROR"
    http_status = 400


class PlanParseError(AgentError):
    """LLM 응답에서 계획 JSON 을 꺼내지 못했다."""

    code = "PLAN_PARSE_FAILED"
    http_status = 502


class PlanNotFoundError(AgentError):
    """그 계획 id 가 이 서버에 없다."""

    code = "PLAN_NOT_FOUND"
    http_status = 404


class PlanStateError(AgentError):
    """이미 승인되었거나 거부된 계획이다."""

    code = "PLAN_STATE"
    http_status = 409


class LlmUnavailableError(AgentError):
    """LLM 을 부를 수 없다 (키 없음, 네트워크, API 오류).

    이것이 올라오면 폴백이 받는다 (PRD 13.4). 라우트까지 새는 것은 폴백조차
    실패한 경우다.
    """

    code = "LLM_UNAVAILABLE"
    http_status = 503


# --------------------------------------------------------------------------- #
# 계획
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PlannedItem:
    """계획된 문항 하나 (PRD 13.2 `items[]`).

    `transition_ms` 가 없다. 난이도 축이 아니고(PRD 9.1) LLM 이 정할 값도
    아니다 — 세션이 트랙의 현재 난이도에서 읽는다 (DECISIONS.md §46.1).
    """

    track: str
    letters: str
    hold_ms: int
    boundary: str


@dataclass(frozen=True)
class AgentPlan:
    """세션 계획 (PRD 13.2). `source` 로 LLM 산출인지 규칙 폴백인지 구분한다."""

    rationale: str
    target_pairs: Tuple[Tuple[str, str], ...]
    items: Tuple[PlannedItem, ...]
    estimated_seconds: int
    source: str = SOURCE_LLM

    def counts(self) -> Dict[str, int]:
        return {
            track: sum(1 for item in self.items if item.track == track)
            for track in TRACKS
        }

    def as_session_plan(self) -> SessionPlan:
        """세션이 이 계획만큼의 칸을 갖게 한다.

        계획 문항 수가 그대로 세션의 문항 수다. 기본값(8+4)을 쓰지 않는 것은
        승인된 계획보다 많은 칸을 열면 계획 밖의 문항이 들어갈 자리가 생기기
        때문이다.
        """
        counts = self.counts()
        return SessionPlan(
            discrimination_items=counts[TRACK_DISCRIMINATION],
            word_items=counts[TRACK_WORD],
        )


@dataclass(frozen=True)
class Finding:
    """검증 한 건. `action` 이 PRD 13.3 의 "실패 시" 열이다."""

    check: str
    action: str
    detail: str
    letters: Optional[str] = None

    def as_json(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "action": self.action,
            "detail": self.detail,
            "letters": self.letters,
        }


@dataclass(frozen=True)
class ValidationReport:
    """검증 결과. `plan` 은 제거·clamp 가 반영된 **정제된** 계획이다."""

    plan: AgentPlan
    findings: Tuple[Finding, ...]
    regenerate: bool

    def of(self, action: str) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.action == action)

    @property
    def removed(self) -> Tuple[Finding, ...]:
        return self.of(ACTION_REMOVED)

    @property
    def warnings(self) -> Tuple[Finding, ...]:
        return self.of(ACTION_WARNING)

    @property
    def clamped(self) -> Tuple[Finding, ...]:
        return self.of(ACTION_CLAMPED)


# --------------------------------------------------------------------------- #
# 런타임 맥락 — 프롬프트와 검증이 같은 값을 본다
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PlanContext:
    """계획을 만들고 검증하는 데 필요한 런타임 사실 전부.

    **프롬프트와 검증이 같은 객체를 본다.** 둘이 다른 값을 보면 LLM 이 지킬 수
    없는 제약으로 계획을 거부하게 된다.
    """

    learner_name: str
    learner_id: Optional[int]
    letters: Tuple[str, ...]
    confusion_pairs: Tuple[Mapping[str, Any], ...]
    depth_pairs_unlocked: bool
    recent: Mapping[str, Tuple[str, ...]]
    difficulty: Mapping[str, Difficulty]
    weakest_pairs: Tuple[Tuple[str, str], ...]
    summaries: Mapping[str, Any]
    words: Tuple[str, ...]
    discrimination_items: int
    word_items: int

    @property
    def total_items(self) -> int:
        return self.discrimination_items + self.word_items


def current_difficulty(
    config: Config, db, learner_id: Optional[int], track: str
) -> Difficulty:
    """이 학습자가 그 트랙에서 마지막에 있던 자리 (DECISIONS.md §40).

    `SessionController._restore_difficulty()` 와 같은 자리를 읽는다. 저쪽이
    권위이고, 여기서는 계획에 담을 `length`·`hold_ms`·`boundary` 만 본다.
    `transition_ms` 는 이월하지 않는다 — 난이도 축이 아니라 오답 재생에서만
    늘리는 값이다.
    """
    saved = None if learner_id is None else load_difficulty(db, learner_id, track)
    if saved is None:
        return Difficulty(
            length=DIFFICULTY_LADDER[0][0],
            hold_ms=config.timing.hold_default_ms,
            transition_ms=config.timing.transition_ms,
            boundary=DIFFICULTY_LADDER[0][1],
        )
    return Difficulty(
        length=saved.length,
        hold_ms=saved.hold_ms,
        transition_ms=config.timing.transition_ms,
        boundary=saved.boundary,
    )


def build_context(
    config: Config,
    words: WordSet,
    database: Database,
    learner_name: str,
    *,
    discrimination_items: int,
    word_items: int,
) -> PlanContext:
    """DB 와 설정에서 계획 맥락을 모은다. **학습자를 만들지 않는다.**"""
    from sqlalchemy import select

    with database.session() as db:
        learner_id = db.scalar(
            select(Learner.id).where(Learner.name == learner_name)
        )
        unlocked = (
            depth_unlocked(
                db, config, TRACK_DISCRIMINATION, learner_id=learner_id
            )
            if learner_id is not None
            else False
        )
        recent = {
            track: recent_letters(
                db, track, window=RECENT_ITEM_WINDOW, learner_id=learner_id
            )
            for track in TRACKS
        }
        difficulty = {
            track: current_difficulty(config, db, learner_id, track)
            for track in TRACKS
        }
        weakest = tuple(
            tuple(pair.pair)
            for pair in weakest_pairs(
                db,
                config,
                TRACK_DISCRIMINATION,
                top=2,
                exclude_depth=not unlocked,
                learner_id=learner_id,
            )
        )
        summaries = {
            track: _summary_json(
                track_summary(db, track, learner_id=learner_id)
            )
            for track in TRACKS
        }

    return PlanContext(
        learner_name=learner_name,
        learner_id=learner_id,
        letters=tuple(sorted(config.poses)),
        confusion_pairs=tuple(
            {
                "pair": list(pair.pair),
                "axis": pair.axis,
                "depth": pair.depth,
                "hint": pair.hint,
            }
            for pair in config.confusion_pairs
        ),
        depth_pairs_unlocked=unlocked,
        recent=recent,
        difficulty=difficulty,
        weakest_pairs=weakest,  # type: ignore[arg-type]
        summaries=summaries,
        words=tuple(word.letters for word in words.words),
        discrimination_items=discrimination_items,
        word_items=word_items,
    )


def _summary_json(summary) -> Dict[str, Any]:
    return {
        "answered": summary.answered,
        "correct": summary.correct,
        "accuracy": summary.accuracy,
        "order_error_rate": summary.order_error_rate,
        "avg_response_ms": summary.avg_response_ms,
    }


# --------------------------------------------------------------------------- #
# 프롬프트 — 글자 목록을 하드코딩하지 않는다
# --------------------------------------------------------------------------- #

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "target_pairs": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "track": {"type": "string"},
                    "letters": {"type": "string"},
                    "hold_ms": {"type": "integer"},
                    "boundary": {"type": "string"},
                },
                "required": ["track", "letters", "hold_ms", "boundary"],
                "additionalProperties": False,
            },
        },
        "estimated_seconds": {"type": "integer"},
    },
    "required": ["rationale", "target_pairs", "items", "estimated_seconds"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
당신은 지문자(ASL 알파벳) 수용 훈련 로봇손 스테이션의 훈련 설계자다.
학습자의 기록을 보고 이번 세션의 문항을 고르고 배열한다.

당신이 정하는 것은 **글자 이름, 트랙, 유지 시간(hold_ms), 글자 경계(boundary)** 뿐이다.
손가락 각도·flex·전이 시간·시리얼 명령은 당신이 정하지 않는다. 스테이션의 설정
파일에만 있고, 당신이 그것을 지정할 방법도 없다.

훈련 모델:

- `discrimination` 트랙은 뜻 없는 글자 시퀀스로 **판별력**을 기른다. 헷갈리는
  두 글자를 한 문항에 나란히 넣어야 훈련이 된다.
- `word` 트랙은 실제 단어로 마무리한다. 주어진 단어 목록에서만 고른다.
- `hold_ms` 가 짧아질수록 어렵다. `boundary` 는 explicit(글자 사이에 중립 자세로
  경계를 보여 줌)가 쉽고 direct(글자에서 글자로 바로)가 어렵다.
- 두 트랙의 성적은 절대 합치지 않는다. 트랙마다 따로 판단한다.

지켜야 할 것:

- 지원 글자 목록에 없는 글자를 쓰지 않는다.
- `discrimination` 문항에 같은 글자를 연속으로 두지 않는다.
- 최근 출제된 문항을 다시 내지 않는다.
- `locked_pairs` 에 있는 쌍은 두 글자를 한 문항에 함께 넣지 않는다. **그 목록에
  없는 쌍에는 아무 제한이 없다.** 특히 `weakest_pairs` 는 제한이 아니라 목표다 —
  `locked_pairs` 에 없는 한 두 글자를 한 문항에 나란히 넣어야 훈련이 된다.
- 요청된 문항 수를 트랙별로 정확히 맞춘다.

**JSON 객체 하나만 출력한다.** 마크다운 코드 블록(백틱), 서두, 설명, 뒷말을
붙이지 않는다. 첫 글자는 `{` 이고 마지막 글자는 `}` 다.
"""


def build_prompt(context: PlanContext, request: str) -> Tuple[str, str]:
    """(system, user) 를 만든다.

    **글자 목록·혼동쌍·단어 목록·난이도 범위를 하드코딩하지 않는다.** 전부
    `context` 에서 온다. 3주차 판별 테스트로 세트가 축소되면 `poses.json` 만
    바뀌고 이 함수는 그대로다 (DECISIONS.md §47).
    """
    facts = {
        "learner": context.learner_name,
        "supported_letters": list(context.letters),
        "tracks": list(TRACKS),
        "boundaries": list(BOUNDARIES),
        "word_pool": list(context.words),
        "confusion_pairs": [dict(pair) for pair in context.confusion_pairs],
        "depth_pairs_unlocked": context.depth_pairs_unlocked,
        # **어느 쌍이 잠겼는지를 명시한다.** `depth_pairs_unlocked` 만 주면 모델이
        # "약점 쌍이 곧 잠긴 쌍" 이라고 읽고 약점 쌍을 피해 간다. 실제로 그랬다
        # (DECISIONS.md §47.8).
        "locked_pairs": (
            []
            if context.depth_pairs_unlocked
            else [
                list(pair["pair"])
                for pair in context.confusion_pairs
                if pair["depth"]
            ]
        ),
        "weakest_pairs": [list(pair) for pair in context.weakest_pairs],
        "by_track": {
            track: {
                "stats": context.summaries[track],
                "current_difficulty": {
                    "length": context.difficulty[track].length,
                    "hold_ms": context.difficulty[track].hold_ms,
                    "boundary": context.difficulty[track].boundary,
                },
                "recent_items": list(context.recent[track]),
            }
            for track in TRACKS
        },
        "requested_items": {
            TRACK_DISCRIMINATION: context.discrimination_items,
            TRACK_WORD: context.word_items,
        },
        "output_schema": PLAN_SCHEMA,
    }
    user = (
        "훈련 담당자의 요청:\n"
        f"{request.strip()}\n\n"
        "스테이션이 알려 준 런타임 사실:\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
        "위 사실만으로 이번 세션의 계획을 JSON 하나로 내라."
    )
    return SYSTEM_PROMPT, user


# --------------------------------------------------------------------------- #
# 파싱 — 백틱·서두가 붙어 와도 살린다
# --------------------------------------------------------------------------- #

def parse_plan_json(text: str) -> AgentPlan:
    """LLM 응답에서 계획을 꺼낸다.

    구조화 출력(`output_config.format`)을 쓰면 JSON 만 오지만, 그것을 믿고
    파서를 두지 않으면 모델이나 설정이 바뀔 때 조용히 깨진다. 백틱으로 감싸거나
    서두를 붙인 응답도 살린다 — 시연 당일에 파싱 하나로 폴백까지 내려가는 것을
    막는 값싼 방어선이다.
    """
    raw = (text or "").strip()
    if not raw:
        raise PlanParseError("the model returned an empty response")

    fenced = _FENCE.search(raw)
    if fenced is not None:
        raw = fenced.group("body").strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise PlanParseError(
            f"no JSON object in the response (first 200 chars: {raw[:200]!r})"
        )

    try:
        payload = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise PlanParseError(f"the response is not valid JSON: {exc}") from exc

    if not isinstance(payload, Mapping):
        raise PlanParseError(
            f"the response is a {type(payload).__name__}, expected an object"
        )
    return _plan_from_payload(payload)


def _plan_from_payload(payload: Mapping[str, Any]) -> AgentPlan:
    raw_items = payload.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise PlanParseError(
            f"$.items is {type(raw_items).__name__}, expected an array"
        )

    items: List[PlannedItem] = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, Mapping):
            raise PlanParseError(
                f"$.items[{index}] is {type(entry).__name__}, expected an object"
            )
        try:
            items.append(
                PlannedItem(
                    track=str(entry["track"]),
                    letters=str(entry["letters"]).upper().strip(),
                    hold_ms=int(entry["hold_ms"]),
                    boundary=str(entry["boundary"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanParseError(f"$.items[{index}]: {exc}") from exc

    pairs: List[Tuple[str, str]] = []
    for entry in payload.get("target_pairs") or []:
        if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)):
            letters = [str(letter).upper() for letter in entry]
            if len(letters) == 2:
                pairs.append((letters[0], letters[1]))

    try:
        estimate = int(payload.get("estimated_seconds") or 0)
    except (TypeError, ValueError):
        estimate = 0

    return AgentPlan(
        rationale=str(payload.get("rationale") or "").strip(),
        target_pairs=tuple(pairs),
        items=tuple(items),
        estimated_seconds=estimate,
        source=SOURCE_LLM,
    )


# --------------------------------------------------------------------------- #
# 검증 10항목 (PRD 13.3)
# --------------------------------------------------------------------------- #

def estimate_seconds(config: Config, items: Sequence[PlannedItem]) -> int:
    """계획이 실제로 걸릴 시간.

    문항마다 글자 수 × (전이 + 유지) 에, explicit 이면 글자 사이 중립 전이를
    더하고, 학습자 응답 시간을 더한다. 전이 시간은 설정에서 읽는다.
    """
    transition = config.timing.transition_ms
    total_ms = 0
    for item in items:
        count = len(item.letters)
        total_ms += count * (transition + item.hold_ms)
        if item.boundary == BOUNDARY_EXPLICIT and count > 1:
            total_ms += (count - 1) * transition
    return round(total_ms / 1000) + len(items) * ASSUMED_RESPONSE_SECONDS


def validate_plan(
    config: Config,
    words: WordSet,
    plan: AgentPlan,
    context: PlanContext,
) -> ValidationReport:
    """PRD 13.3 의 10항목을 적용하고 정제된 계획을 돌려준다.

    항목별 처리가 다르다. **글자 미지원만 재생성 신호**이고 나머지는 제거·clamp·
    경고로 흡수된다 — 지원하지 않는 글자는 계획 전체의 전제가 틀렸다는 뜻이라
    한 문항을 빼서 고칠 수 있는 종류가 아니다 (DECISIONS.md §47).
    """
    findings: List[Finding] = []
    supported = set(context.letters)
    timing = config.timing

    unsupported = sorted(
        {
            letter
            for item in plan.items
            for letter in item.letters
            if letter not in supported
        }
    )
    if unsupported:
        findings.append(
            Finding(
                check=CHECK_LETTERS,
                action=ACTION_REGENERATE,
                detail=(
                    f"letters {unsupported} are not in poses; "
                    f"this hand can express {sorted(supported)}"
                ),
            )
        )
        # 정제를 계속하지 않는다. 재생성이 먼저다.
        return ValidationReport(
            plan=plan, findings=tuple(findings), regenerate=True
        )

    # 최근 출제 목록에 이번 계획 안의 중복도 쌓아 간다 — 같은 문항을 두 번 넣은
    # 계획도 걸러야 한다.
    seen = {track: list(context.recent[track]) for track in TRACKS}
    repeating_words = {
        word.letters for word in words.words if word.has_repeat
    }
    kept: List[PlannedItem] = []

    for item in plan.items:
        if item.track not in TRACKS:
            findings.append(
                Finding(
                    CHECK_TRACK,
                    ACTION_REMOVED,
                    f"track is {item.track!r}, expected one of {list(TRACKS)}",
                    item.letters,
                )
            )
            continue
        if item.boundary not in BOUNDARIES:
            findings.append(
                Finding(
                    CHECK_BOUNDARY,
                    ACTION_REMOVED,
                    f"boundary is {item.boundary!r}, "
                    f"expected one of {list(BOUNDARIES)}",
                    item.letters,
                )
            )
            continue

        # hold_ms 범위 → clamp
        bounded = min(max(item.hold_ms, timing.hold_min_ms), timing.hold_max_ms)
        if bounded != item.hold_ms:
            findings.append(
                Finding(
                    CHECK_HOLD_RANGE,
                    ACTION_CLAMPED,
                    f"hold_ms {item.hold_ms} -> {bounded} "
                    f"(range {timing.hold_min_ms}..{timing.hold_max_ms})",
                    item.letters,
                )
            )
            item = replace(item, hold_ms=bounded)

        # length 범위 → 제거
        if not LENGTH_MIN <= len(item.letters) <= LENGTH_MAX:
            findings.append(
                Finding(
                    CHECK_LENGTH,
                    ACTION_REMOVED,
                    f"length is {len(item.letters)}, "
                    f"expected {LENGTH_MIN}..{LENGTH_MAX}",
                    item.letters,
                )
            )
            continue

        # 연속 동일 글자 단어는 버리지 않고 경계 모드를 낮춘다 (DECISIONS.md §9).
        # `generate_word_items()` 와 같은 선택이다.
        if (
            item.track == TRACK_WORD
            and item.letters in repeating_words
            and item.boundary != BOUNDARY_EXPLICIT
        ):
            findings.append(
                Finding(
                    CHECK_WORD_BOUNDARY,
                    ACTION_CLAMPED,
                    f"{item.letters!r} has adjacent repeated letters, so the "
                    f"boundary drops from {item.boundary!r} to "
                    f"{BOUNDARY_EXPLICIT!r}",
                    item.letters,
                )
            )
            item = replace(item, boundary=BOUNDARY_EXPLICIT)

        problems = (
            discrimination_violations(
                config,
                item.letters,
                recent=seen[item.track],
                depth_unlocked=context.depth_pairs_unlocked,
            )
            if item.track == TRACK_DISCRIMINATION
            else word_violations(
                config,
                words,
                item.letters,
                item.boundary,
                recent=seen[item.track],
            )
        )
        if problems:
            depth_locked = not context.depth_pairs_unlocked and (
                item_contains_depth_pair(config, item.letters)
            )
            findings.append(
                Finding(
                    _check_for(item, seen[item.track], depth_locked),
                    ACTION_REMOVED,
                    "; ".join(problems),
                    item.letters,
                )
            )
            # 깊이쌍은 제거 **+ 경고**다 (PRD 13.3). 잠긴 쌍을 노렸다는 것은
            # 계획 의도가 커리큘럼과 어긋났다는 신호라 조용히 빼지 않는다.
            if depth_locked:
                findings.append(
                    Finding(
                        CHECK_DEPTH_LOCKED,
                        ACTION_WARNING,
                        f"{item.letters!r} targets a depth pair that is still "
                        f"locked for this learner",
                        item.letters,
                    )
                )
            continue

        seen[item.track].append(item.letters)
        kept.append(item)

    findings.extend(_plan_level_findings(config, plan, kept, context))
    return ValidationReport(
        plan=replace(plan, items=tuple(kept)),
        findings=tuple(findings),
        regenerate=False,
    )


def _check_for(
    item: PlannedItem, seen: Sequence[str], depth_locked: bool
) -> str:
    """제거 사유에 붙일 검사 이름.

    `violations()` 가 돌려준 문자열을 파싱하지 않는다 — 저쪽 문구가 바뀌면
    조용히 오분류된다. 대신 어느 검사에 해당하는지 여기서 다시 판정한다.
    한 문항이 두 검사를 동시에 어길 수 있으므로 **순서가 곧 우선순위**이고,
    어긴 것 전부는 `detail` 에 그대로 담긴다.
    """
    if item.letters in {text.upper() for text in seen}:
        return CHECK_RECENT
    if depth_locked:
        return CHECK_DEPTH_LOCKED
    if any(a == b for a, b in zip(item.letters, item.letters[1:])):
        return CHECK_REPEAT
    # 남는 것은 단어 목록에 없는 단어뿐이다. 글자 미지원은 이 지점 전에
    # 재생성으로 빠졌고, 길이·트랙·경계는 위에서 걸렀다.
    return CHECK_WORD_POOL


def _plan_level_findings(
    config: Config,
    plan: AgentPlan,
    kept: Sequence[PlannedItem],
    context: PlanContext,
) -> List[Finding]:
    """문항 단위로 볼 수 없는 세 항목: D 비중, 약점 쌍, 소요 시간 추정."""
    findings: List[Finding] = []

    # D 포함 비율 상한 → 경고. 상한은 판별 문항 수 기준이다 (session.start 와 동일).
    discrimination = [i for i in kept if i.track == TRACK_DISCRIMINATION]
    caps = heavy_letter_caps(config, len(discrimination))
    for letter, cap in caps.items():
        used = sum(1 for item in discrimination if letter in item.letters)
        if used > cap:
            findings.append(
                Finding(
                    CHECK_HEAVY_LETTER,
                    ACTION_WARNING,
                    f"{letter} appears in {used} of {len(discrimination)} "
                    f"discrimination items, over the cap of {cap}",
                )
            )

    # 약점 상위 쌍이 들어 있는가 → 경고. **차단하지 않는다** (PRD 13.3).
    if context.weakest_pairs:
        covered = [
            pair
            for pair in context.weakest_pairs
            if any(set(pair) <= set(item.letters) for item in discrimination)
        ]
        if not covered:
            findings.append(
                Finding(
                    CHECK_WEAK_PAIRS,
                    ACTION_WARNING,
                    f"none of the weakest pairs "
                    f"{[list(p) for p in context.weakest_pairs]} appear together "
                    f"in a discrimination item",
                )
            )

    # estimated_seconds ±20% → 경고.
    actual = estimate_seconds(config, kept)
    if actual:
        low = actual * (1 - ESTIMATE_TOLERANCE)
        high = actual * (1 + ESTIMATE_TOLERANCE)
        if not low <= plan.estimated_seconds <= high:
            findings.append(
                Finding(
                    CHECK_ESTIMATE,
                    ACTION_WARNING,
                    f"estimated_seconds is {plan.estimated_seconds}, "
                    f"the station computes {actual} "
                    f"(tolerance {low:.0f}..{high:.0f})",
                )
            )

    return findings


# --------------------------------------------------------------------------- #
# 규칙 폴백 (PRD 13.4)
# --------------------------------------------------------------------------- #

def rule_plan(
    config: Config,
    words: WordSet,
    context: PlanContext,
    *,
    rng: Optional[random.Random] = None,
) -> AgentPlan:
    """LLM 없이 계획을 만든다. **발표 당일 네트워크 장애 대비다** (PRD 13.4).

    `items.py` 의 생성기를 그대로 부른다. 세션이 문항을 하나씩 만들 때 쓰는 것과
    같은 코드이므로, 폴백으로 만든 계획은 규칙 기반 세션과 같은 문항 분포를
    갖는다.
    """
    rng = rng or random.Random()
    produced: List[PlannedItem] = []

    if context.discrimination_items:
        difficulty = context.difficulty[TRACK_DISCRIMINATION]
        pairs = tuple(
            pair
            for pair in config.confusion_pairs
            if tuple(pair.pair) in context.weakest_pairs
        )
        specs = generate_discrimination_items(
            config,
            count=context.discrimination_items,
            difficulty=difficulty,
            recent=context.recent[TRACK_DISCRIMINATION],
            target_pairs=pairs,
            depth_unlocked=context.depth_pairs_unlocked,
            rng=rng,
        )
        produced.extend(
            PlannedItem(
                track=spec.track,
                letters=spec.letters,
                hold_ms=spec.hold_ms,
                boundary=spec.boundary,
            )
            for spec in specs
        )

    if context.word_items:
        difficulty = context.difficulty[TRACK_WORD]
        specs = generate_word_items(
            config,
            words,
            count=context.word_items,
            difficulty=difficulty,
            recent=context.recent[TRACK_WORD],
            rng=rng,
        )
        produced.extend(
            PlannedItem(
                track=spec.track,
                letters=spec.letters,
                hold_ms=spec.hold_ms,
                boundary=spec.boundary,
            )
            for spec in specs
        )

    return AgentPlan(
        rationale=(
            "규칙 기반 폴백. LLM 계획을 쓸 수 없어 약점 쌍과 트랙별 현재 "
            "난이도에서 문항을 생성했다."
        ),
        target_pairs=context.weakest_pairs,
        items=tuple(produced),
        estimated_seconds=estimate_seconds(config, produced),
        source=SOURCE_RULE,
    )


# --------------------------------------------------------------------------- #
# LLM 호출
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LlmReply:
    """모델이 돌려준 것. **토큰 사용량을 함께 담는다** (비용 관리)."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0

    def as_json(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


class LlmClient(Protocol):
    """계획을 내놓는 모델. 테스트는 픽스처를 재생하는 구현을 넣는다."""

    def complete(self, system: str, user: str) -> LlmReply: ...


class AnthropicClient:
    """Anthropic Messages API 로 계획을 받아 온다 (anthropic 1.4.0).

    **구조화 출력을 쓴다.** `output_config.format` 이 JSON 스키마를 강제하므로
    응답에 백틱이나 서두가 붙지 않는다. 그래도 `parse_plan_json()` 의 관용
    파서는 남겨 둔다 (DECISIONS.md §47).

    assistant prefill 로 `{` 를 밀어 넣는 흔한 수법은 쓰지 않는다 — Opus 5
    계열에서 400 이다.

    **서버측 거절 폴백을 켠다.** 안전 분류기가 요청을 거절하면 같은 호출 안에서
    다른 모델이 이어받는다. 훈련 담당자의 자연어 문장이 오탐을 건드리는 일이
    실제로 있었고(§47.8), 그때 규칙 폴백으로 내려가면 에이전트가 아무 일도 하지
    않은 것이 된다.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = MAX_TOKENS,
        client: Optional[Any] = None,
    ) -> None:
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._client = client
        self._api_key = api_key

    def _anthropic(self) -> Any:
        """SDK 클라이언트를 만든다.

        **키를 직접 확인하지 않는다.** SDK 가 `ANTHROPIC_API_KEY` →
        `ANTHROPIC_AUTH_TOKEN` → 로그인 프로파일 순으로 자격증명을 찾는다.
        여기서 환경변수만 보고 거절하면, 프로파일로 로그인한 기계에서 쓸 수
        있는 자격증명을 두고 폴백으로 내려간다.
        """
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - 의존성 누락
            raise LlmUnavailableError(f"the anthropic package is missing: {exc}")

        key = self._api_key or os.environ.get("ANTHROPIC_API_KEY") or None
        try:
            self._client = anthropic.Anthropic(api_key=key)
        except Exception as exc:  # noqa: BLE001 - 자격증명이 아예 없는 경우
            raise LlmUnavailableError(
                f"no Anthropic credentials are available "
                f"({type(exc).__name__}: {exc}); the rule fallback will be used"
            ) from exc
        return self._client

    def complete(self, system: str, user: str) -> LlmReply:
        client = self._anthropic()
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": PLAN_SCHEMA}
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 폴백이 받는다
            raise LlmUnavailableError(
                f"the plan request failed: {type(exc).__name__}: {exc}"
            ) from exc

        served_by = getattr(response, "model", self.model)
        usage = response.usage
        tokens = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }

        # **`content` 를 읽기 전에 `stop_reason` 을 본다.** 거절은 HTTP 200 으로
        # 오고 `content` 가 비어 있다. 그것을 "빈 응답" 으로 처리하면 원인이
        # 로그에서 사라진다 (§47.8).
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LlmUnavailableError(
                f"the request was declined by a safety classifier "
                f"(category={getattr(details, 'category', None)!r}): "
                f"{getattr(details, 'explanation', '')}"
            )

        if served_by != self.model:
            # 서버측 폴백이 이어받았다. 계획은 나왔지만 어느 모델이 낸 것인지
            # 로그에 남아야 한다.
            logger.warning(
                "agent: %s declined; the plan was served by %s",
                self.model,
                served_by,
            )

        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        if not text.strip():
            raise LlmUnavailableError(
                f"the response carried no text "
                f"(stop_reason={stop_reason!r}, blocks="
                f"{[block.type for block in response.content]}, "
                f"output_tokens={tokens['output_tokens']})"
            )

        return LlmReply(text=text, model=served_by, **tokens)


# --------------------------------------------------------------------------- #
# 오케스트레이션
# --------------------------------------------------------------------------- #

@dataclass
class PlanAttempt:
    """한 번의 계획 시도. 화면과 로그가 같은 것을 본다."""

    index: int
    report: Optional[ValidationReport]
    usage: Optional[LlmReply]
    error: Optional[str] = None

    def as_json(self) -> Dict[str, Any]:
        return {
            "attempt": self.index,
            "findings": (
                [f.as_json() for f in self.report.findings] if self.report else []
            ),
            "usage": self.usage.as_json() if self.usage else None,
            "error": self.error,
        }


class PlanAgent:
    """자연어 요청 → 검증된 계획. **실행하지 않는다.**

    LLM 실패나 재생성 2회 실패는 규칙 폴백으로 흡수한다 (PRD 13.4). 폴백까지
    실패하는 것은 `items.py` 가 제약을 만족하는 문항을 못 만드는 경우이고,
    그때는 예외가 올라간다 — 조용히 빈 계획을 내놓지 않는다.
    """

    def __init__(
        self,
        config: Config,
        words: WordSet,
        database: Database,
        *,
        llm: Optional[LlmClient] = None,
        rng: Optional[random.Random] = None,
        max_regenerations: int = MAX_REGENERATIONS,
    ) -> None:
        self._config = config
        self._words = words
        self._db = database
        self._llm = llm
        self._rng = rng or random.Random()
        self._max_regenerations = max_regenerations

    def plan(
        self,
        learner_name: str,
        request: str,
        *,
        discrimination_items: int,
        word_items: int,
    ) -> Tuple[AgentPlan, ValidationReport, Tuple[PlanAttempt, ...]]:
        context = build_context(
            self._config,
            self._words,
            self._db,
            learner_name,
            discrimination_items=discrimination_items,
            word_items=word_items,
        )
        attempts: List[PlanAttempt] = []

        if self._llm is not None:
            system, user = build_prompt(context, request)
            for index in range(1 + self._max_regenerations):
                attempt = self._attempt(index + 1, system, user, context)
                attempts.append(attempt)
                if attempt.report is None:
                    # LLM 이나 파싱이 실패했다. 재시도하지 않는다 — 같은 프롬프트로
                    # 다시 물어도 같은 실패가 돌아온다. 재생성은 **검증** 실패에만
                    # 쓴다 (PRD 13.3).
                    break
                if attempt.report.regenerate:
                    user = _regeneration_note(user, attempt.report)
                    continue
                if not attempt.report.plan.items:
                    # 통과한 문항이 하나도 없다. 검증 실패는 아니지만 쓸 수 없다.
                    attempt.error = "every planned item was removed by validation"
                    break
                return attempt.report.plan, attempt.report, tuple(attempts)

        fallback = rule_plan(
            self._config, self._words, context, rng=self._rng
        )
        report = validate_plan(self._config, self._words, fallback, context)
        logger.warning(
            "agent: falling back to the rule planner after %d attempt(s)",
            len(attempts),
        )
        return fallback, report, tuple(attempts)

    def _attempt(
        self, index: int, system: str, user: str, context: PlanContext
    ) -> PlanAttempt:
        assert self._llm is not None
        try:
            reply = self._llm.complete(system, user)
        except AgentError as exc:
            logger.warning("agent: attempt %d could not reach the model: %s", index, exc)
            return PlanAttempt(index=index, report=None, usage=None, error=str(exc))

        logger.info(
            "agent: attempt %d used model=%s input=%d output=%d cache_read=%d",
            index,
            reply.model,
            reply.input_tokens,
            reply.output_tokens,
            reply.cache_read_tokens,
        )
        try:
            plan = parse_plan_json(reply.text)
        except PlanParseError as exc:
            logger.warning("agent: attempt %d returned unusable JSON: %s", index, exc)
            return PlanAttempt(index=index, report=None, usage=reply, error=str(exc))

        report = validate_plan(self._config, self._words, plan, context)
        logger.info(
            "agent: attempt %d kept %d of %d item(s); %d finding(s)",
            index,
            len(report.plan.items),
            len(plan.items),
            len(report.findings),
        )
        return PlanAttempt(index=index, report=report, usage=reply)


def _regeneration_note(user: str, report: ValidationReport) -> str:
    """재생성 요청. 무엇이 틀렸는지 알려 주고 다시 묻는다."""
    reasons = "\n".join(
        f"- {finding.check}: {finding.detail}"
        for finding in report.of(ACTION_REGENERATE)
    )
    return (
        f"{user}\n\n"
        "직전 계획은 다음 이유로 거부되었다. 같은 잘못을 반복하지 말고 다시 내라.\n"
        f"{reasons}"
    )


# --------------------------------------------------------------------------- #
# HITL — 승인 대기 저장소
# --------------------------------------------------------------------------- #

PLAN_PENDING = "pending"
PLAN_APPROVED = "approved"
PLAN_REJECTED = "rejected"


@dataclass
class StoredPlan:
    """승인을 기다리는 계획.

    **`session_id` 는 승인된 뒤에만 채워진다.** 이 필드가 비어 있는 동안 세션은
    존재하지 않는다 — 그것이 HITL 이다 (PRD 13.3).
    """

    plan_id: int
    learner_name: str
    request: str
    plan: AgentPlan
    report: ValidationReport
    attempts: Tuple[PlanAttempt, ...]
    created_at: datetime
    status: str = PLAN_PENDING
    session_id: Optional[int] = None
    decided_at: Optional[datetime] = None
    # 아직 표현되지 않은 문항. 승인 시 계획에서 복사한다.
    queue: List[PlannedItem] = field(default_factory=list)
    # 직전에 내보낸 문항. 오답 1회 뒤 재생에서 같은 글자열을 다시 내기 위한 것.
    last: Optional[PlannedItem] = None

    def as_json(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "learner_name": self.learner_name,
            "request": self.request,
            "source": self.plan.source,
            "session_id": self.session_id,
            "rationale": self.plan.rationale,
            "target_pairs": [list(pair) for pair in self.plan.target_pairs],
            "items": [
                {
                    "track": item.track,
                    "letters": item.letters,
                    "hold_ms": item.hold_ms,
                    "boundary": item.boundary,
                }
                for item in self.plan.items
            ],
            "counts": self.plan.counts(),
            "estimated_seconds": self.plan.estimated_seconds,
            "findings": [f.as_json() for f in self.report.findings],
            "attempts": [a.as_json() for a in self.attempts],
            "remaining_items": len(self.queue),
        }


class PlanStore:
    """승인 대기 계획들. 프로세스 메모리에 있다.

    DB 에 두지 않는다. 승인은 **지금 눈앞에서** 일어나는 일이고, 서버가 내려갔다
    올라오면 사람이 다시 계획을 만드는 것이 맞다 — 재시작을 건너온 계획을 나중에
    승인하면, 그 사이에 바뀐 학습자 기록을 보지 않은 계획이 실행된다
    (DECISIONS.md §47).
    """

    def __init__(self, *, clock=utcnow) -> None:
        self._clock = clock
        self._plans: Dict[int, StoredPlan] = {}
        self._next_id = 1
        # 세션 → 그 세션을 만든 계획. `/planned` 가 이 표로 큐를 찾는다.
        self._by_session: Dict[int, int] = {}

    def add(
        self,
        learner_name: str,
        request: str,
        plan: AgentPlan,
        report: ValidationReport,
        attempts: Tuple[PlanAttempt, ...],
    ) -> StoredPlan:
        stored = StoredPlan(
            plan_id=self._next_id,
            learner_name=learner_name,
            request=request,
            plan=plan,
            report=report,
            attempts=attempts,
            created_at=self._clock(),
        )
        self._plans[stored.plan_id] = stored
        self._next_id += 1
        return stored

    def get(self, plan_id: int) -> StoredPlan:
        stored = self._plans.get(plan_id)
        if stored is None:
            raise PlanNotFoundError(f"plan {plan_id} is not on this server")
        return stored

    def pending(self, plan_id: int) -> StoredPlan:
        """승인·거부를 받을 수 있는 계획. 이미 결정된 것은 거부한다."""
        stored = self.get(plan_id)
        if stored.status != PLAN_PENDING:
            raise PlanStateError(
                f"plan {plan_id} is already {stored.status}; "
                f"a decided plan cannot be decided again"
            )
        return stored

    def approve(self, plan_id: int, session_id: int) -> StoredPlan:
        """승인을 기록하고 문항 큐를 채운다. 세션은 호출자가 이미 만들었다."""
        stored = self.get(plan_id)
        stored.status = PLAN_APPROVED
        stored.session_id = session_id
        stored.decided_at = self._clock()
        stored.queue = list(stored.plan.items)
        self._by_session[session_id] = plan_id
        return stored

    def reject(self, plan_id: int) -> StoredPlan:
        stored = self.pending(plan_id)
        stored.status = PLAN_REJECTED
        stored.decided_at = self._clock()
        return stored

    def for_session(self, session_id: int) -> StoredPlan:
        plan_id = self._by_session.get(session_id)
        if plan_id is None:
            raise PlanNotFoundError(
                f"session {session_id} was not started from an approved plan"
            )
        return self.get(plan_id)

    def forget_session(self, session_id: int) -> None:
        self._by_session.pop(session_id, None)

    def list(self) -> Tuple[StoredPlan, ...]:
        return tuple(self._plans[key] for key in sorted(self._plans, reverse=True))
