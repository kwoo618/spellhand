"""문항 생성 테스트 (PRD 9.5, 기획서 4.1/4.2).

생성기는 난수를 쓰므로 `random.Random(seed)` 를 주입해 결정적으로 만든다.
제약은 한 번이 아니라 여러 번 생성해 확인한다 — 운 좋게 통과하는 것을
막기 위해서다.
"""

import random
from pathlib import Path

import pytest

from server.config import load_config, load_words
from server.db import BOUNDARY_DIRECT, BOUNDARY_EXPLICIT, TRACK_DISCRIMINATION, TRACK_WORD
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

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"
REAL_WORDS = REPO_ROOT / "config" / "words.json"


@pytest.fixture
def config():
    return load_config(REAL_POSES)


@pytest.fixture
def words(config):
    return load_words(REAL_WORDS, letters=config.poses)


@pytest.fixture
def rng():
    return random.Random(20260907)


@pytest.fixture
def easy(config):
    return Difficulty(
        length=3,
        hold_ms=config.timing.hold_default_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_EXPLICIT,
    )


# --------------------------------------------------------------------------- #
# 판별 트랙 — 제약
# --------------------------------------------------------------------------- #

def test_generates_the_requested_count(config, easy, rng):
    items = generate_discrimination_items(
        config, count=8, difficulty=easy, rng=rng
    )
    assert len(items) == 8
    assert all(i.track == TRACK_DISCRIMINATION for i in items)
    assert all(len(i.letters) == 3 for i in items)


def test_no_letter_repeats_next_to_itself(config, easy, rng):
    """연결 모드에서 손이 움직이지 않아 경계가 사라지는 조합을 만들지 않는다."""
    for _ in range(30):
        items = generate_discrimination_items(
            config, count=5, difficulty=easy, rng=rng
        )
        for item in items:
            assert not any(
                a == b for a, b in zip(item.letters, item.letters[1:])
            ), item.letters


def test_only_supported_letters_are_used(config, easy, rng):
    items = generate_discrimination_items(
        config, count=20, difficulty=easy, rng=rng
    )
    for item in items:
        assert set(item.letters) <= set(config.poses), item.letters


def test_generated_items_are_not_repeated_within_a_batch(config, easy, rng):
    items = generate_discrimination_items(
        config, count=20, difficulty=easy, rng=rng
    )
    assert len({i.letters for i in items}) == 20


def test_recent_items_are_avoided(config, easy, rng):
    recent = ["VWB", "DOS", "LYB"]
    items = generate_discrimination_items(
        config, count=10, difficulty=easy, recent=recent, rng=rng
    )
    for item in items:
        assert item.letters not in recent


def test_longer_lengths_are_produced(config, rng):
    difficulty = Difficulty(
        length=7, hold_ms=2000, transition_ms=900, boundary=BOUNDARY_DIRECT
    )
    items = generate_discrimination_items(
        config, count=5, difficulty=difficulty, rng=rng
    )
    assert all(len(i.letters) == 7 for i in items)
    assert all(i.boundary == BOUNDARY_DIRECT for i in items)


# --------------------------------------------------------------------------- #
# 깊이쌍 게이팅 (R4 — 완화 금지)
# --------------------------------------------------------------------------- #

def test_locked_depth_pairs_never_appear(config, easy, rng):
    for _ in range(30):
        items = generate_discrimination_items(
            config, count=5, difficulty=easy, depth_unlocked=False, rng=rng
        )
        for item in items:
            present = set(item.letters)
            assert not {"E", "I"} <= present, item.letters
            assert not {"O", "S"} <= present, item.letters
            assert not {"E", "O"} <= present, item.letters


def test_unlocking_allows_depth_pairs(config, rng):
    difficulty = Difficulty(
        length=5, hold_ms=2000, transition_ms=900, boundary=BOUNDARY_EXPLICIT
    )
    items = generate_discrimination_items(
        config, count=40, difficulty=difficulty, depth_unlocked=True, rng=rng
    )
    depth_items = [
        i
        for i in items
        if {"E", "I"} <= set(i.letters)
        or {"O", "S"} <= set(i.letters)
        or {"E", "O"} <= set(i.letters)
    ]
    assert depth_items, "해금 상태에서는 깊이쌍이 나올 수 있어야 한다"


def test_depth_target_pairs_are_dropped_while_locked(config, easy, rng):
    """잠긴 상태에서 깊이쌍을 목표로 줘도 그 쌍을 배치하지 않는다."""
    depth_pairs = config.depth_pairs()
    items = generate_discrimination_items(
        config,
        count=10,
        difficulty=easy,
        target_pairs=depth_pairs,
        depth_unlocked=False,
        rng=rng,
    )
    for item in items:
        assert not discrimination_violations(
            config, item.letters, depth_unlocked=False
        )


def test_violations_flag_a_locked_depth_pair(config):
    problems = discrimination_violations(config, "EIB", depth_unlocked=False)
    assert any("depth pair" in p for p in problems)
    assert discrimination_violations(config, "EIB", depth_unlocked=True) == ()


def test_violations_flag_adjacent_repeats_and_unknown_letters(config):
    assert any(
        "twice in a row" in p for p in discrimination_violations(config, "BBV")
    )
    assert any(
        "not in poses" in p for p in discrimination_violations(config, "CAT")
    )


# --------------------------------------------------------------------------- #
# heavy_letters 상한
# --------------------------------------------------------------------------- #

def test_heavy_letter_caps_scale_with_the_session(config):
    assert heavy_letter_caps(config, 8) == {"D": 4}
    assert heavy_letter_caps(config, 12) == {"D": 6}


def test_heavy_letter_cap_is_respected(config, easy, rng):
    """D 는 문항의 절반을 넘지 못한다 (curriculum.heavy_letters)."""
    count = 8
    items = generate_discrimination_items(
        config, count=count, difficulty=easy, rng=rng
    )
    with_d = sum(1 for i in items if "D" in i.letters)
    assert with_d <= heavy_letter_caps(config, count)["D"]


def test_heavy_letter_budget_carries_across_calls(config, easy, rng):
    """세션이 문항을 하나씩 만들어도 상한은 세션 전체 기준이다."""
    caps = heavy_letter_caps(config, 8)
    used = {"D": 0}
    letters = []
    for _ in range(8):
        item = generate_discrimination_items(
            config,
            count=1,
            difficulty=easy,
            recent=letters,
            rng=rng,
            heavy_caps=caps,
            heavy_used=used,
        )[0]
        letters.append(item.letters)
        if "D" in item.letters:
            used["D"] += 1
    assert used["D"] <= caps["D"]


def test_a_per_item_budget_would_have_banned_d_entirely(config):
    """한 문항씩 비율을 계산하면 int(1 * 0.5) == 0 이 되어 D 가 영영 못 나온다.

    그래서 상한을 세션 전체 기준으로 잡는다.
    """
    assert heavy_letter_caps(config, 1) == {"D": 0}
    assert heavy_letter_caps(config, 8)["D"] > 0


# --------------------------------------------------------------------------- #
# 목표 쌍
# --------------------------------------------------------------------------- #

def test_target_pairs_are_placed_together(config, rng):
    difficulty = Difficulty(
        length=4, hold_ms=3000, transition_ms=900, boundary=BOUNDARY_EXPLICIT
    )
    pair = next(p for p in config.confusion_pairs if p.pair == ("V", "W"))
    items = generate_discrimination_items(
        config, count=6, difficulty=difficulty, target_pairs=[pair], rng=rng
    )
    placed = sum(1 for i in items if {"V", "W"} <= set(i.letters))
    assert placed >= 4, "약점 쌍은 대체로 함께 배치되어야 한다"


# --------------------------------------------------------------------------- #
# 통글자 트랙
# --------------------------------------------------------------------------- #

def test_word_items_come_from_the_filtered_pool(config, words, easy, rng):
    items = generate_word_items(
        config, words, count=4, difficulty=easy, rng=rng
    )
    pool = {w.letters for w in words.words}
    assert len(items) == 4
    assert all(i.track == TRACK_WORD for i in items)
    assert all(i.letters in pool for i in items)


def test_words_are_not_repeated(config, words, easy, rng):
    items = generate_word_items(
        config, words, count=20, difficulty=easy, rng=rng
    )
    assert len({i.letters for i in items}) == 20


def test_recent_words_are_avoided(config, words, easy, rng):
    recent = ["WISE", "LOVE", "BODY"]
    items = generate_word_items(
        config, words, count=10, difficulty=easy, recent=recent, rng=rng
    )
    assert all(i.letters not in recent for i in items)


def test_repeat_letter_words_are_forced_to_explicit(config, words, rng):
    """BLESS 의 SS 는 연결 모드에서 경계가 사라진다."""
    direct = Difficulty(
        length=5, hold_ms=2000, transition_ms=900, boundary=BOUNDARY_DIRECT
    )
    items = generate_word_items(
        config, words, count=32, difficulty=direct, rng=rng
    )
    by_word = {i.letters: i for i in items}
    for name in ("BLESS", "FOSSIL"):
        assert by_word[name].boundary == BOUNDARY_EXPLICIT, name
    # 나머지는 요청한 대로 연결 모드다.
    assert by_word["WISE"].boundary == BOUNDARY_DIRECT


def test_depth_gating_does_not_apply_to_words(config, words, rng):
    """PRD 9.2 의 트랙별 제약표에서 깊이쌍 게이팅은 판별 트랙에만 있다.

    단어까지 막으면 WISE 를 비롯한 상당수가 초기에 출제 불가가 되어
    "판별 8 + 단어 4" 세션이 성립하지 않는다.
    """
    difficulty = Difficulty(
        length=4, hold_ms=3000, transition_ms=900, boundary=BOUNDARY_EXPLICIT
    )
    items = generate_word_items(
        config, words, count=32, difficulty=difficulty, rng=rng
    )
    chosen = {i.letters for i in items}
    assert "WISE" in chosen
    assert word_violations(config, words, "WISE", BOUNDARY_EXPLICIT) == ()


def test_word_violations_catch_unknown_words(config, words):
    assert any(
        "not in the filtered word pool" in p
        for p in word_violations(config, words, "CAT", BOUNDARY_EXPLICIT)
    )


def test_word_violations_catch_direct_mode_repeats(config, words):
    problems = word_violations(config, words, "BLESS", BOUNDARY_DIRECT)
    assert any("explicit" in p for p in problems)
    assert word_violations(config, words, "BLESS", BOUNDARY_EXPLICIT) == ()


def test_running_out_of_words_is_an_error(config, words, easy, rng):
    with pytest.raises(ItemGenerationError) as exc:
        generate_word_items(
            config, words, count=len(words.words) + 1, difficulty=easy, rng=rng
        )
    assert "no unseen words left" in str(exc.value)


# --------------------------------------------------------------------------- #
# 실패 경로
# --------------------------------------------------------------------------- #

def test_impossible_constraints_raise_rather_than_loop(config, rng):
    """만들 수 없으면 조용히 규칙을 어기지 말고 실패해야 한다."""
    difficulty = Difficulty(
        length=3, hold_ms=3000, transition_ms=900, boundary=BOUNDARY_EXPLICIT
    )
    # 가능한 모든 3글자 조합을 최근 목록에 넣어 버린다.
    alphabet = sorted(config.poses)
    everything = [
        a + b + c
        for a in alphabet
        for b in alphabet
        for c in alphabet
    ]
    with pytest.raises(ItemGenerationError):
        generate_discrimination_items(
            config, count=1, difficulty=difficulty, recent=everything, rng=rng
        )


def test_negative_count_is_rejected(config, easy, rng):
    with pytest.raises(ValueError):
        generate_discrimination_items(
            config, count=-1, difficulty=easy, rng=rng
        )


def test_recent_window_constant_is_twenty():
    """기획서 4.1/4.2 "최근 20문항과 중복 금지"."""
    assert RECENT_ITEM_WINDOW == 20
