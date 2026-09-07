"""문항 생성 (PRD 9.5, 기획서 4.1/4.2).

규칙 기반이다. LLM 은 나중에 이 자리를 대신할 수 있지만, 그때도 **결과는 같은
제약을 통과해야 한다** — 그래서 생성기와 제약 검사를 분리해 두었다.
`violations()` 는 누가 만든 문항이든 검사할 수 있다.

DB 를 보지 않는다. 최근 출제 목록·목표 쌍·해금 여부는 전부 인자로 받는다.
`stats.py` 와 `session.py` 가 그것을 물어다 준다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from server.config import Config, ConfusionPair, WordSet
from server.db import (
    BOUNDARY_DIRECT,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
)
from server.stats import item_contains_depth_pair

__all__ = [
    "ItemSpec",
    "Difficulty",
    "RECENT_ITEM_WINDOW",
    "ItemGenerationError",
    "heavy_letter_caps",
    "generate_discrimination_items",
    "generate_word_items",
    "discrimination_violations",
    "word_violations",
]

# 기획서 4.1/4.2 "최근 20문항과 중복 금지". 설정 파일에 출처가 없어 여기 둔다.
# `curriculum.depth_pairs_unlock.window_items` 와 값이 같지만 다른 개념이므로
# 재사용하지 않는다 (DECISIONS.md §19).
RECENT_ITEM_WINDOW = 20

# 후보를 몇 번 뽑아 보고 포기할 것인가. 제약이 서로 모순이면 무한히 돌지 않는다.
_MAX_ATTEMPTS = 500


class ItemGenerationError(Exception):
    """주어진 제약을 모두 만족하는 문항을 만들 수 없다."""


@dataclass(frozen=True)
class Difficulty:
    """난이도 3축 (PRD 9.1). `transition_ms` 는 난이도 축이 아니다."""

    length: int
    hold_ms: int
    transition_ms: int
    boundary: str


@dataclass(frozen=True)
class ItemSpec:
    track: str
    letters: str
    hold_ms: int
    transition_ms: int
    boundary: str


# --------------------------------------------------------------------------- #
# 제약 검사 — 누가 만든 문항이든 통과해야 한다
# --------------------------------------------------------------------------- #

def discrimination_violations(
    config: Config,
    letters: str,
    *,
    recent: Sequence[str] = (),
    depth_unlocked: bool = False,
) -> Tuple[str, ...]:
    """판별 트랙 문항이 어긴 제약들. 빈 튜플이면 통과."""
    problems: List[str] = []
    text = letters.upper()

    unknown = sorted({c for c in text if c not in config.poses})
    if unknown:
        problems.append(f"letters not in poses: {unknown}")
    if any(a == b for a, b in zip(text, text[1:])):
        problems.append("the same letter appears twice in a row")
    if text in {r.upper() for r in recent}:
        problems.append(f"already presented in the last {len(recent)} items")
    if not depth_unlocked and item_contains_depth_pair(config, text):
        problems.append("contains a depth pair while depth pairs are locked")
    return tuple(problems)


def word_violations(
    config: Config,
    words: WordSet,
    letters: str,
    boundary: str,
    *,
    recent: Sequence[str] = (),
) -> Tuple[str, ...]:
    """통글자 트랙 문항이 어긴 제약들.

    깊이쌍 게이팅은 검사하지 않는다. PRD 9.2 의 트랙별 제약표에서 깊이쌍
    게이팅은 판별 트랙에만 걸려 있다 (DECISIONS.md §20).
    """
    problems: List[str] = []
    text = letters.upper()

    known = {word.letters: word for word in words.words}
    word = known.get(text)
    if word is None:
        problems.append("not in the filtered word pool")
    elif word.has_repeat and boundary == BOUNDARY_DIRECT:
        problems.append(
            "has adjacent repeated letters, so it can only be presented "
            "in explicit mode"
        )
    if text in {r.upper() for r in recent}:
        problems.append(f"already presented in the last {len(recent)} items")
    return tuple(problems)


# --------------------------------------------------------------------------- #
# 판별 트랙
# --------------------------------------------------------------------------- #

def heavy_letter_caps(config: Config, total_items: int) -> Dict[str, int]:
    """`curriculum.heavy_letters` 비율을 문항 수 상한으로 바꾼다.

    `total_items` 는 **비율이 적용되는 전체 문항 수**다. 세션이 문항을 하나씩
    생성하더라도 상한은 세션 전체를 기준으로 잡아야 한다 — 한 문항씩 계산하면
    `int(1 * 0.5) == 0` 이 되어 해당 글자가 영영 나오지 못한다.
    """
    heavy = config.curriculum.get("heavy_letters") or {}
    return {
        letter.upper(): int(total_items * ratio) for letter, ratio in heavy.items()
    }


def generate_discrimination_items(
    config: Config,
    *,
    count: int,
    difficulty: Difficulty,
    recent: Sequence[str] = (),
    target_pairs: Sequence[ConfusionPair] = (),
    depth_unlocked: bool = False,
    rng: Optional[random.Random] = None,
    heavy_caps: Optional[Mapping[str, int]] = None,
    heavy_used: Optional[Mapping[str, int]] = None,
) -> Tuple[ItemSpec, ...]:
    """무의미 글자 시퀀스를 만든다.

    `target_pairs` 가 주어지면 그 쌍의 두 글자를 문항에 함께 넣으려 한다 —
    약점 쌍을 나란히 놓아야 판별을 훈련시킨다. 다른 제약과 부딪히면 목표 쌍을
    포기하고 제약을 지킨다. 제약은 안전·커리큘럼 규칙이고 목표 쌍은 선호다.
    """
    rng = rng or random.Random()
    if count < 0:
        raise ValueError(f"count is {count}, expected zero or more")

    alphabet = sorted(config.poses)
    # 깊이쌍은 조합 단계에서 거른다. 글자 자체를 알파벳에서 빼지는 않는다 —
    # E 하나만 든 문항은 깊이 판별을 요구하지 않기 때문이다.

    caps = dict(heavy_caps) if heavy_caps is not None else heavy_letter_caps(
        config, count
    )
    used: Dict[str, int] = {letter: 0 for letter in caps}
    for letter, already in (heavy_used or {}).items():
        used[letter.upper()] = used.get(letter.upper(), 0) + already
    seen = [r.upper() for r in recent]
    produced: List[ItemSpec] = []

    usable_pairs = [
        pair
        for pair in target_pairs
        if depth_unlocked or not pair.depth
    ]

    for index in range(count):
        preferred = usable_pairs[index % len(usable_pairs)] if usable_pairs else None
        letters = _build_sequence(
            config,
            alphabet=alphabet,
            length=difficulty.length,
            preferred=preferred,
            recent=seen,
            depth_unlocked=depth_unlocked,
            caps=caps,
            used=used,
            rng=rng,
        )
        for letter in caps:
            if letter in letters:
                used[letter] += 1
        seen.append(letters)
        produced.append(
            ItemSpec(
                track=TRACK_DISCRIMINATION,
                letters=letters,
                hold_ms=difficulty.hold_ms,
                transition_ms=difficulty.transition_ms,
                boundary=difficulty.boundary,
            )
        )
    return tuple(produced)


def _build_sequence(
    config: Config,
    *,
    alphabet: Sequence[str],
    length: int,
    preferred: Optional[ConfusionPair],
    recent: Sequence[str],
    depth_unlocked: bool,
    caps: Dict[str, int],
    used: Dict[str, int],
    rng: random.Random,
) -> str:
    if length < 1:
        raise ValueError(f"length is {length}, expected at least 1")

    blocked = {
        letter for letter, cap in caps.items() if used.get(letter, 0) >= cap
    }

    # 1차: 목표 쌍을 심어서 시도. 2차: 목표 쌍 없이 시도.
    for seeds in ((preferred,) if preferred else ()) + (None,):
        pool = [c for c in alphabet if c not in blocked]
        seed_letters = list(seeds.pair) if seeds is not None else []
        if any(letter in blocked for letter in seed_letters):
            continue
        for letter in seed_letters:
            if letter not in pool:
                pool.append(letter)
        if not pool:
            continue

        for _ in range(_MAX_ATTEMPTS):
            letters = _sample_sequence(seed_letters, pool, length, rng)
            if letters is None:
                continue
            if discrimination_violations(
                config, letters, recent=recent, depth_unlocked=depth_unlocked
            ):
                continue
            return letters

    raise ItemGenerationError(
        f"could not build a {length}-letter discrimination item: "
        f"alphabet={sorted(alphabet)}, blocked_by_quota={sorted(blocked)}, "
        f"depth_unlocked={depth_unlocked}, recent={len(recent)} items"
    )


def _sample_sequence(
    seed_letters: Sequence[str],
    pool: Sequence[str],
    length: int,
    rng: random.Random,
) -> Optional[str]:
    """씨앗 글자를 넣고 나머지를 채운다. 같은 글자가 연속되면 포기한다."""
    seeds = [letter for letter in seed_letters][:length]
    filler = [rng.choice(list(pool)) for _ in range(length - len(seeds))]
    letters = seeds + filler
    rng.shuffle(letters)
    text = "".join(letters)
    if any(a == b for a, b in zip(text, text[1:])):
        return None
    return text


# --------------------------------------------------------------------------- #
# 통글자 트랙
# --------------------------------------------------------------------------- #

def generate_word_items(
    config: Config,
    words: WordSet,
    *,
    count: int,
    difficulty: Difficulty,
    recent: Sequence[str] = (),
    rng: Optional[random.Random] = None,
) -> Tuple[ItemSpec, ...]:
    """단어 문항을 만든다.

    연속 동일 글자를 가진 단어는 요청된 경계 모드와 무관하게 explicit 로
    낮춘다. 연결 모드에서는 손이 움직이지 않아 경계가 사라지기 때문이다
    (DECISIONS.md §9). 단어를 버리지 않고 모드를 낮추는 쪽을 택한다 — 그 단어를
    영영 못 내는 것보다 낫다.
    """
    rng = rng or random.Random()
    if count < 0:
        raise ValueError(f"count is {count}, expected zero or more")

    seen = {r.upper() for r in recent}
    produced: List[ItemSpec] = []

    for _ in range(count):
        candidates = [w for w in words.words if w.letters not in seen]
        if not candidates:
            raise ItemGenerationError(
                f"no unseen words left: pool has {len(words.words)} word(s), "
                f"{len(seen)} already presented"
            )
        word = rng.choice(candidates)
        boundary = (
            BOUNDARY_EXPLICIT if word.has_repeat else difficulty.boundary
        )
        seen.add(word.letters)
        produced.append(
            ItemSpec(
                track=TRACK_WORD,
                letters=word.letters,
                hold_ms=difficulty.hold_ms,
                transition_ms=difficulty.transition_ms,
                boundary=boundary,
            )
        )
    return tuple(produced)
