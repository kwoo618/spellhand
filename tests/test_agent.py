"""에이전트 테스트 (PRD 13장).

**API 를 부르지 않는다.** LLM 응답은 아래 픽스처 문자열을 `ReplayLlm` 이
재생한다. 실제 호출은 `SPELLHAND_LIVE_LLM=1` 일 때만 도는 테스트 하나뿐이고,
기본 실행에서는 건너뛴다 — 스위트가 돌 때마다 돈이 나가면 안 된다.

검증은 `validate_plan()` 을 직접 불러 항목별로 본다. `PlanContext` 가 frozen
dataclass 라 `replace()` 로 맥락(최근 출제·해금 여부)만 바꿔 끼울 수 있고,
그래서 DB 를 seed 하지 않고도 10항목을 각각 고정할 수 있다.
"""

import dataclasses
import json
import os
import random

import pytest
from fastapi.testclient import TestClient

from server.agent import (
    ACTION_CLAMPED,
    ACTION_REGENERATE,
    ACTION_REMOVED,
    ACTION_WARNING,
    CHECK_BOUNDARY,
    CHECK_DEPTH_LOCKED,
    CHECK_ESTIMATE,
    CHECK_HEAVY_LETTER,
    CHECK_HOLD_RANGE,
    CHECK_LENGTH,
    CHECK_LETTERS,
    CHECK_RECENT,
    CHECK_REPEAT,
    CHECK_TRACK,
    CHECK_WEAK_PAIRS,
    CHECK_WORD_BOUNDARY,
    CHECK_WORD_POOL,
    MAX_REGENERATIONS,
    PLAN_SCHEMA,
    SOURCE_LLM,
    SOURCE_RULE,
    AgentPlan,
    AnthropicClient,
    LlmReply,
    LlmUnavailableError,
    PlanAgent,
    PlannedItem,
    PlanParseError,
    build_context,
    build_prompt,
    estimate_seconds,
    parse_plan_json,
    rule_plan,
    validate_plan,
)
from server.db import TRACK_DISCRIMINATION, TRACK_WORD, Database
from server.device.mock import MockDevice
from server.routes import build_station, create_app
from tests.conftest import REAL_POSES, REAL_WORDS

LIVE = os.environ.get("SPELLHAND_LIVE_LLM")

# 각도·flex·전이 시간을 LLM 이 지정하는 길이 스키마에 없어야 한다 (R1).
FORBIDDEN_FIELD_HINTS = (
    "flex",
    "angle",
    "deg",
    "servo",
    "pin",
    "pwm",
    "permille",
    "transition",
)


# --------------------------------------------------------------------------- #
# 픽스처 — 저장된 응답을 재생한다
# --------------------------------------------------------------------------- #

def plan_json(items, *, rationale="V/W 혼동이 가장 많다", pairs=(("V", "W"),),
              estimated_seconds=None):
    """계획 JSON 문자열. 실제 모델 응답과 같은 모양이다."""
    payload = {
        "rationale": rationale,
        "target_pairs": [list(p) for p in pairs],
        "items": [
            {
                "track": track,
                "letters": letters,
                "hold_ms": hold_ms,
                "boundary": boundary,
            }
            for track, letters, hold_ms, boundary in items
        ],
        "estimated_seconds": (
            estimated_seconds if estimated_seconds is not None else 60
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


# 정상 응답. 판별 2 + 단어 1.
GOOD_RESPONSE = plan_json(
    [
        (TRACK_DISCRIMINATION, "VWB", 3000, "explicit"),
        (TRACK_DISCRIMINATION, "WVF", 2500, "explicit"),
        (TRACK_WORD, "WISE", 3000, "explicit"),
    ],
    estimated_seconds=62,
)

# 지원하지 않는 글자만 내놓는 응답. 재생성 신호다.
UNSUPPORTED_RESPONSE = plan_json(
    [(TRACK_DISCRIMINATION, "CAT", 3000, "explicit")]
)

# 모델이 백틱과 서두·뒷말을 붙인 응답.
MESSY_RESPONSE = (
    "좋습니다! 요청을 반영한 계획입니다.\n\n"
    "```json\n" + GOOD_RESPONSE + "\n```\n\n"
    "필요하면 문항 수를 조정해 드리겠습니다."
)


class ReplayLlm:
    """저장된 응답을 순서대로 재생한다. 목록이 바닥나면 마지막 것을 반복한다."""

    def __init__(self, *texts, usage=(1200, 300)):
        self.texts = list(texts)
        self.usage = usage
        self.calls = 0
        self.prompts = []

    def complete(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        text = self.texts[min(self.calls - 1, len(self.texts) - 1)]
        return LlmReply(
            text=text,
            model="replay",
            input_tokens=self.usage[0],
            output_tokens=self.usage[1],
        )


class BrokenLlm:
    """모델에 닿지 못하는 경우 (키 없음, 네트워크)."""

    def __init__(self):
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        raise LlmUnavailableError("no credentials in this test")


@pytest.fixture
def words(config):
    from server.config import load_words

    return load_words(REAL_WORDS, letters=config.poses)


@pytest.fixture
def context(config, words, db):
    return build_context(
        config, words, db, "haru", discrimination_items=4, word_items=2
    )


def one(config, words, context, item, **overrides):
    """문항 하나만 든 계획을 검증하고 보고서를 돌려준다."""
    plan = AgentPlan(
        rationale="r",
        target_pairs=(),
        items=(item,),
        estimated_seconds=overrides.pop("estimated_seconds", 0),
    )
    return validate_plan(config, words, plan, dataclasses.replace(context, **overrides))


def checks(report, action):
    return [f.check for f in report.of(action)]


# --------------------------------------------------------------------------- #
# LLM 격리 (R1)
# --------------------------------------------------------------------------- #

def _field_names(schema):
    names = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names.extend(value)
            names.extend(_field_names(value))
    elif isinstance(schema, list):
        for entry in schema:
            names.extend(_field_names(entry))
    return names


def test_the_output_schema_has_no_flex_or_angle_or_transition_field():
    """LLM 이 각도를 지정하는 경로가 **스키마 수준에서** 없다 (R1, PRD 15.2).

    MCP 도구 스키마와 같은 방어선이다. 여기에 한 필드를 더하려는 사람이 이
    테스트에서 막히는 것이 요점이다.
    """
    offenders = [
        name
        for name in _field_names(PLAN_SCHEMA)
        if any(hint in name.lower() for hint in FORBIDDEN_FIELD_HINTS)
    ]
    assert offenders == []


def test_a_planned_item_carries_only_letters_track_and_difficulty():
    assert [f.name for f in dataclasses.fields(PlannedItem)] == [
        "track",
        "letters",
        "hold_ms",
        "boundary",
    ]


def test_the_prompt_does_not_hardcode_the_letter_set(config, context):
    """글자 목록은 프롬프트 **본문**이 아니라 런타임 맥락에서 온다.

    3주차 판별 테스트로 세트가 축소되면 poses.json 만 바뀌고 이 코드는 그대로다.
    """
    from server.agent import SYSTEM_PROMPT

    for letter in sorted(config.poses):
        assert f'"{letter}"' not in SYSTEM_PROMPT

    _, user = build_prompt(context, "V와 W 집중")
    for letter in sorted(config.poses):
        assert letter in json.loads(user[user.index("{") : user.rindex("}") + 1])[
            "supported_letters"
        ]


def test_the_prompt_carries_the_word_pool_and_the_current_difficulty(
    context, words
):
    _, user = build_prompt(context, "단어로 마무리")
    facts = json.loads(user[user.index("{") : user.rindex("}") + 1])
    assert set(facts["word_pool"]) == {w.letters for w in words.words}
    assert facts["by_track"][TRACK_DISCRIMINATION]["current_difficulty"]["length"] == 3
    assert facts["depth_pairs_unlocked"] is False


def test_the_prompt_names_which_pairs_are_locked(config, context):
    """`depth_pairs_unlocked` 만 주면 모델이 약점 쌍을 잠긴 쌍으로 읽는다.

    실제 호출에서 그렇게 됐다 — V/W 를 "잠긴 깊이쌍" 으로 오해해 요청의 핵심인
    V/W 대비를 피해 갔다 (DECISIONS.md §47.8). 어느 쌍이 잠겼는지 명시한다.
    """
    _, user = build_prompt(context, "V와 W 집중")
    facts = json.loads(user[user.index("{") : user.rindex("}") + 1])

    locked = {frozenset(pair) for pair in facts["locked_pairs"]}
    assert locked == {frozenset(pair.pair) for pair in config.depth_pairs()}
    # 약점 쌍은 잠긴 쌍이 아니다. 둘을 구분할 근거가 프롬프트 안에 있어야 한다.
    for pair in facts["weakest_pairs"]:
        assert frozenset(pair) not in locked
    # 깊이 여부는 혼동쌍 목록에도 들어 있다.
    assert any(pair["depth"] for pair in facts["confusion_pairs"])


def test_the_prompt_locks_nothing_once_depth_pairs_are_unlocked(context):
    unlocked = dataclasses.replace(context, depth_pairs_unlocked=True)
    _, user = build_prompt(unlocked, "E와 I 집중")
    facts = json.loads(user[user.index("{") : user.rindex("}") + 1])
    assert facts["locked_pairs"] == []


# --------------------------------------------------------------------------- #
# 파싱 — 백틱·서두가 붙어 와도 살린다
# --------------------------------------------------------------------------- #

def test_bare_json_parses():
    plan = parse_plan_json(GOOD_RESPONSE)
    assert len(plan.items) == 3
    assert plan.items[0].letters == "VWB"
    assert plan.source == SOURCE_LLM


def test_a_fenced_response_with_a_preamble_parses():
    plan = parse_plan_json(MESSY_RESPONSE)
    assert [i.letters for i in plan.items] == ["VWB", "WVF", "WISE"]


def test_a_bare_fence_without_a_language_tag_parses():
    plan = parse_plan_json("```\n" + GOOD_RESPONSE + "\n```")
    assert len(plan.items) == 3


def test_letters_are_upper_cased_on_the_way_in():
    plan = parse_plan_json(
        plan_json([(TRACK_DISCRIMINATION, "vwb", 3000, "explicit")])
    )
    assert plan.items[0].letters == "VWB"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "죄송합니다. 계획을 만들 수 없습니다.",
        "{not json at all}",
        '{"rationale": "r", "estimated_seconds": 1}',
        '{"rationale": "r", "items": "VWB", "estimated_seconds": 1}',
        '{"rationale": "r", "items": [{"track": "discrimination"}]}',
        "[1, 2, 3]",
    ],
)
def test_an_unusable_response_raises_a_structured_parse_error(text):
    with pytest.raises(PlanParseError):
        parse_plan_json(text)


# --------------------------------------------------------------------------- #
# 검증 10항목 (PRD 13.3)
# --------------------------------------------------------------------------- #

def test_1_unsupported_letters_ask_for_a_regeneration(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "CAT", 3000, "explicit"),
    )
    assert report.regenerate is True
    assert checks(report, ACTION_REGENERATE) == [CHECK_LETTERS]
    # 재생성이 먼저다. 다른 항목의 정제를 진행하지 않는다.
    assert report.removed == ()


def test_2_a_word_outside_the_pool_is_removed(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_WORD, "BDEF", 3000, "explicit"),
    )
    assert report.regenerate is False
    assert checks(report, ACTION_REMOVED) == [CHECK_WORD_POOL]
    assert report.plan.items == ()


def test_3_hold_ms_out_of_range_is_clamped_not_removed(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "BDE", 99999, "explicit"),
    )
    assert checks(report, ACTION_CLAMPED) == [CHECK_HOLD_RANGE]
    assert len(report.plan.items) == 1
    assert report.plan.items[0].hold_ms == config.timing.hold_max_ms

    low = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "BDE", 1, "explicit"),
    )
    assert low.plan.items[0].hold_ms == config.timing.hold_min_ms


@pytest.mark.parametrize("letters", ["BD", "BDEFILOS"])
def test_4_a_length_outside_the_ladder_is_removed(config, words, context, letters):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, letters, 3000, "explicit"),
    )
    assert checks(report, ACTION_REMOVED) == [CHECK_LENGTH]


def test_5_an_adjacent_repeat_is_removed(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "BBD", 3000, "explicit"),
    )
    assert checks(report, ACTION_REMOVED) == [CHECK_REPEAT]


def test_6_a_locked_depth_pair_is_removed_and_warned(config, words, context):
    """제거 **+ 경고** 다 (PRD 13.3). 조용히 빼지 않는다."""
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "EIB", 3000, "explicit"),
    )
    assert checks(report, ACTION_REMOVED) == [CHECK_DEPTH_LOCKED]
    assert CHECK_DEPTH_LOCKED in checks(report, ACTION_WARNING)
    assert report.plan.items == ()


def test_6b_the_same_depth_pair_is_allowed_once_unlocked(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "EIB", 3000, "explicit"),
        depth_pairs_unlocked=True,
    )
    assert report.removed == ()
    assert [i.letters for i in report.plan.items] == ["EIB"]


def test_7_too_much_of_a_heavy_letter_only_warns(config, words, context):
    """D 비중 초과는 **경고**다. 문항을 빼지 않는다 (PRD 13.3)."""
    plan = AgentPlan(
        rationale="r",
        target_pairs=(),
        items=(
            PlannedItem(TRACK_DISCRIMINATION, "DVW", 3000, "explicit"),
            PlannedItem(TRACK_DISCRIMINATION, "DWV", 3000, "explicit"),
            PlannedItem(TRACK_DISCRIMINATION, "DVB", 3000, "explicit"),
            PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "explicit"),
        ),
        estimated_seconds=0,
    )
    report = validate_plan(config, words, plan, context)
    assert CHECK_HEAVY_LETTER in checks(report, ACTION_WARNING)
    # 상한(4문항의 50% = 2)을 넘겼지만 네 문항 전부 남는다.
    assert len(report.plan.items) == 4


def test_8_missing_the_weakest_pairs_only_warns(config, words, context):
    """약점 상위 쌍 누락은 경고다. **차단이 아니다** (PRD 13.3)."""
    assert context.weakest_pairs  # 기록이 없어도 설정 순서로 채워진다
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "ILY", 3000, "explicit"),
    )
    assert CHECK_WEAK_PAIRS in checks(report, ACTION_WARNING)
    assert len(report.plan.items) == 1


def test_8b_covering_a_weakest_pair_raises_no_warning(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "explicit"),
        estimated_seconds=estimate_seconds(
            config, [PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "explicit")]
        ),
    )
    assert CHECK_WEAK_PAIRS not in checks(report, ACTION_WARNING)


def test_9_an_item_from_the_recent_window_is_removed(config, words, context):
    report = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "BDE", 3000, "explicit"),
        recent={TRACK_DISCRIMINATION: ("BDE",), TRACK_WORD: ()},
    )
    assert checks(report, ACTION_REMOVED) == [CHECK_RECENT]


def test_9b_a_plan_that_repeats_itself_keeps_only_the_first(config, words, context):
    """계획 **안의** 중복도 걸러야 한다. 최근 출제 목록에 쌓아 가며 본다."""
    plan = AgentPlan(
        rationale="r",
        target_pairs=(),
        items=(
            PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "explicit"),
            PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "explicit"),
        ),
        estimated_seconds=0,
    )
    report = validate_plan(config, words, plan, context)
    assert len(report.plan.items) == 1
    assert CHECK_RECENT in checks(report, ACTION_REMOVED)


def test_10_a_wrong_time_estimate_only_warns(config, words, context):
    item = PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "explicit")
    report = one(config, words, context, item, estimated_seconds=1)
    assert CHECK_ESTIMATE in checks(report, ACTION_WARNING)
    assert len(report.plan.items) == 1

    honest = one(
        config, words, context, item,
        estimated_seconds=estimate_seconds(config, [item]),
    )
    assert CHECK_ESTIMATE not in checks(honest, ACTION_WARNING)


# --------------------------------------------------------------------------- #
# 10항목 밖에서 보는 것들
# --------------------------------------------------------------------------- #

def test_an_item_that_could_not_be_driven_is_removed(config, words, context):
    """구동 단계에서 거부될 문항을 품은 계획은 승인의 의미가 없다."""
    bad_track = one(
        config, words, context,
        PlannedItem("bogus", "VWB", 3000, "explicit"),
    )
    assert checks(bad_track, ACTION_REMOVED) == [CHECK_TRACK]

    bad_boundary = one(
        config, words, context,
        PlannedItem(TRACK_DISCRIMINATION, "VWB", 3000, "sideways"),
    )
    assert checks(bad_boundary, ACTION_REMOVED) == [CHECK_BOUNDARY]


def test_a_word_with_a_repeat_drops_to_explicit_instead_of_being_removed(
    config, words, context
):
    """§9 의 선택을 따른다 — 버리지 않고 경계를 낮춘다.

    `generate_word_items()` 와 같은 처리여야 검증과 생성기가 어긋나지 않는다.
    """
    repeating = next(w.letters for w in words.words if w.has_repeat)
    report = one(
        config, words, context,
        PlannedItem(TRACK_WORD, repeating, 3000, "direct"),
    )
    assert checks(report, ACTION_CLAMPED) == [CHECK_WORD_BOUNDARY]
    assert [i.boundary for i in report.plan.items] == ["explicit"]


# --------------------------------------------------------------------------- #
# 재생성과 규칙 폴백 (PRD 13.4)
# --------------------------------------------------------------------------- #

def agent(config, words, db, llm, seed=7):
    return PlanAgent(config, words, db, llm=llm, rng=random.Random(seed))


def test_a_good_first_answer_needs_no_regeneration(config, words, db):
    llm = ReplayLlm(GOOD_RESPONSE)
    plan, report, attempts = agent(config, words, db, llm).plan(
        "haru", "V와 W 집중", discrimination_items=2, word_items=1
    )
    assert llm.calls == 1
    assert plan.source == SOURCE_LLM
    assert [i.letters for i in plan.items] == ["VWB", "WVF", "WISE"]
    assert len(attempts) == 1
    assert report.regenerate is False


def test_two_failed_regenerations_fall_back_to_the_rules(config, words, db):
    """재생성 2회가 실패하면 규칙 폴백이다 (PRD 13.4)."""
    llm = ReplayLlm(UNSUPPORTED_RESPONSE)
    plan, report, attempts = agent(config, words, db, llm).plan(
        "haru", "CAT 보여줘", discrimination_items=3, word_items=1
    )
    assert llm.calls == 1 + MAX_REGENERATIONS == 3
    assert plan.source == SOURCE_RULE
    assert len(plan.items) == 4
    assert len(attempts) == 3
    assert all(a.report is not None and a.report.regenerate for a in attempts)


def test_a_regeneration_tells_the_model_what_was_wrong(config, words, db):
    llm = ReplayLlm(UNSUPPORTED_RESPONSE, GOOD_RESPONSE)
    plan, _, attempts = agent(config, words, db, llm).plan(
        "haru", "CAT", discrimination_items=2, word_items=1
    )
    assert llm.calls == 2
    assert plan.source == SOURCE_LLM
    # 두 번째 프롬프트에 첫 거부 사유가 들어 있다.
    second = llm.prompts[1][1]
    assert CHECK_LETTERS in second
    assert "'C'" in second or "C" in second


def test_an_unreachable_model_falls_back_without_retrying(config, words, db):
    """네트워크·키 실패는 재시도하지 않는다. 같은 실패가 돌아온다."""
    llm = BrokenLlm()
    plan, _, attempts = agent(config, words, db, llm).plan(
        "haru", "아무거나", discrimination_items=3, word_items=1
    )
    assert llm.calls == 1
    assert plan.source == SOURCE_RULE
    assert attempts[0].report is None
    assert attempts[0].error


def test_an_unparseable_answer_falls_back_without_retrying(config, words, db):
    llm = ReplayLlm("죄송합니다, 계획을 만들 수 없습니다.")
    plan, _, attempts = agent(config, words, db, llm).plan(
        "haru", "아무거나", discrimination_items=3, word_items=1
    )
    assert llm.calls == 1
    assert plan.source == SOURCE_RULE
    assert attempts[0].report is None
    # 토큰은 썼으므로 사용량은 남는다.
    assert attempts[0].usage is not None


def test_a_plan_whose_items_are_all_removed_falls_back(config, words, db):
    """검증 실패가 아니어도 쓸 수 없는 계획은 폴백으로 간다."""
    llm = ReplayLlm(plan_json([(TRACK_DISCRIMINATION, "BB", 3000, "explicit")]))
    plan, _, attempts = agent(config, words, db, llm).plan(
        "haru", "아무거나", discrimination_items=3, word_items=1
    )
    assert plan.source == SOURCE_RULE
    assert attempts[0].error


def test_no_llm_at_all_means_the_rule_planner(config, words, db):
    """`build_station(llm=None)` 상태. 네트워크가 없는 발표장 기본값이다."""
    plan, report, attempts = PlanAgent(
        config, words, db, llm=None, rng=random.Random(3)
    ).plan("haru", "아무거나", discrimination_items=8, word_items=4)
    assert attempts == ()
    assert plan.source == SOURCE_RULE
    assert plan.counts() == {TRACK_DISCRIMINATION: 8, TRACK_WORD: 4}


def test_the_fallback_plan_passes_its_own_validation(config, words, db, context):
    """폴백이 만든 계획은 제거·재생성 대상이 없어야 한다."""
    plan = rule_plan(config, words, context, rng=random.Random(4))
    report = validate_plan(config, words, plan, context)
    assert report.regenerate is False
    assert report.removed == ()
    assert report.clamped == ()
    assert len(report.plan.items) == context.total_items
    # 소요 시간도 스테이션 계산과 일치한다.
    assert CHECK_ESTIMATE not in checks(report, ACTION_WARNING)


def test_the_fallback_respects_the_depth_pair_gate(config, words, db, context):
    plan = rule_plan(config, words, context, rng=random.Random(9))
    from server.stats import item_contains_depth_pair

    assert not any(
        item_contains_depth_pair(config, item.letters)
        for item in plan.items
        if item.track == TRACK_DISCRIMINATION
    )


def test_token_usage_is_recorded_for_every_attempt(config, words, db):
    llm = ReplayLlm(UNSUPPORTED_RESPONSE, usage=(1500, 420))
    _, _, attempts = agent(config, words, db, llm).plan(
        "haru", "CAT", discrimination_items=3, word_items=1
    )
    assert len(attempts) == 3
    for attempt in attempts:
        assert attempt.usage.as_json() == {
            "model": "replay",
            "input_tokens": 1500,
            "output_tokens": 420,
            "cache_read_tokens": 0,
        }


# --------------------------------------------------------------------------- #
# HITL — 승인 전에는 아무것도 실행되지 않는다 (PRD 13.3)
# --------------------------------------------------------------------------- #

SMALL = {"discrimination_items": 2, "word_items": 1}


@pytest.fixture
def station(config, clock):
    return build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        rng=random.Random(20260924),
        background=lambda work: work(),
        llm=ReplayLlm(GOOD_RESPONSE),
    )


@pytest.fixture
def http(station):
    with TestClient(create_app(station)) as client:
        yield client


def make_plan(http, **overrides):
    body = {"learner_name": "haru", "request": "V와 W 집중", **SMALL, **overrides}
    response = http.post("/api/agent/plan", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_making_a_plan_creates_no_session_and_takes_no_lock(http, station):
    body = make_plan(http)
    assert body["status"] == "pending"
    assert body["session_id"] is None
    assert station.controllers == {}
    assert station.lock.holder() is None
    assert station.device.is_busy is False
    # 구동 요청도 한 건 없다.
    assert station.gate.request_count == 0


def test_the_plan_response_carries_the_items_and_the_findings(http):
    body = make_plan(http)
    assert [i["letters"] for i in body["items"]] == ["VWB", "WVF", "WISE"]
    assert body["counts"] == {TRACK_DISCRIMINATION: 2, TRACK_WORD: 1}
    assert body["source"] == SOURCE_LLM
    assert body["attempts"][0]["usage"]["input_tokens"] == 1200
    assert "findings" in body


def test_approving_is_the_only_thing_that_creates_a_session(http, station):
    plan_id = make_plan(http)["plan_id"]
    assert station.controllers == {}

    response = http.post(f"/api/agent/plans/{plan_id}/approve")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["session_id"] in station.controllers
    assert body["total_items"] == 3
    assert station.lock.holder() == body["session_id"]


def test_rejecting_creates_nothing(http, station):
    plan_id = make_plan(http)["plan_id"]
    body = http.post(f"/api/agent/plans/{plan_id}/reject").json()
    assert body["status"] == "rejected"
    assert body["session_id"] is None
    assert station.controllers == {}
    assert station.lock.holder() is None


@pytest.mark.parametrize("first", ["approve", "reject"])
def test_a_decided_plan_cannot_be_decided_again(http, station, first):
    plan_id = make_plan(http)["plan_id"]
    http.post(f"/api/agent/plans/{plan_id}/{first}")
    before = list(station.controllers)

    for second in ("approve", "reject"):
        response = http.post(f"/api/agent/plans/{plan_id}/{second}")
        assert response.status_code == 409, (second, response.text)
        assert response.json()["code"] == "PLAN_STATE"
    assert list(station.controllers) == before


def test_an_unknown_plan_is_a_structured_404(http):
    response = http.post("/api/agent/plans/999/approve")
    assert response.status_code == 404
    assert response.json()["code"] == "PLAN_NOT_FOUND"


def test_an_empty_plan_cannot_be_approved(config, clock):
    """정제 후 문항이 없는 계획은 승인되지 않는다."""
    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
        # 첫 답은 재생성을 부르고, 이후에도 쓸 수 없는 문항만 온다.
        llm=ReplayLlm(plan_json([(TRACK_DISCRIMINATION, "BB", 3000, "explicit")])),
    )
    with TestClient(create_app(station)) as http:
        body = make_plan(http)
        # 폴백이 받았으므로 계획에는 문항이 있다. 빈 계획을 직접 만들어 넣는다.
        empty = station.plans.add(
            "haru",
            "빈 계획",
            AgentPlan("r", (), (), 0, SOURCE_LLM),
            validate_plan(
                station.config,
                station.words,
                AgentPlan("r", (), (), 0, SOURCE_LLM),
                build_context(
                    station.config,
                    station.words,
                    station.database,
                    "haru",
                    discrimination_items=0,
                    word_items=0,
                ),
            ),
            (),
        )
        response = http.post(f"/api/agent/plans/{empty.plan_id}/approve")
        assert response.status_code == 409
        assert response.json()["code"] == "PLAN_STATE"
        assert station.controllers == {}
        assert body["plan_id"] != empty.plan_id


# --------------------------------------------------------------------------- #
# 승인된 계획의 집행
# --------------------------------------------------------------------------- #

def approved(http, station):
    plan_id = make_plan(http)["plan_id"]
    body = http.post(f"/api/agent/plans/{plan_id}/approve").json()
    return plan_id, body["session_id"]


def answer(http, station, session_id, text=None):
    controller = station.controllers[session_id]
    given = controller._active_answer() if text is None else text
    return http.post(
        f"/api/sessions/{session_id}/answer", json={"answer": given}
    )


def test_the_planned_items_are_presented_in_order(http, station):
    _, session_id = approved(http, station)

    seen = []
    for expected_left in (2, 1, 0):
        response = http.post(f"/api/sessions/{session_id}/planned")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["remaining_items"] == expected_left
        seen.append(station.controllers[session_id]._active_answer())
        assert answer(http, station, session_id).json()["result"] == "correct"

    assert seen == ["VWB", "WVF", "WISE"]


def test_driving_past_the_plan_is_refused(http, station):
    _, session_id = approved(http, station)
    for _ in range(3):
        http.post(f"/api/sessions/{session_id}/planned")
        answer(http, station, session_id)

    response = http.post(f"/api/sessions/{session_id}/planned")
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_FINISHED"


def test_a_replay_presents_the_same_item_without_draining_the_queue(http, station):
    """오답 1회 뒤에는 같은 문항이 다시 나온다 (기획서 5.2)."""
    _, session_id = approved(http, station)
    first = http.post(f"/api/sessions/{session_id}/planned").json()
    assert first["remaining_items"] == 2

    outcome = answer(http, station, session_id, "BDE").json()
    assert outcome["retry"] is True

    again = http.post(f"/api/sessions/{session_id}/planned").json()
    assert again["index"] == first["index"]
    assert again["attempt"] == 2
    # 큐는 줄지 않았다.
    assert again["remaining_items"] == 2
    assert station.controllers[session_id]._active_answer() == "VWB"


def test_a_session_that_did_not_come_from_a_plan_has_nothing_to_drive(http, station):
    started = http.post(
        "/api/sessions", json={"learner_name": "haru", **SMALL}
    ).json()
    response = http.post(f"/api/sessions/{started['session_id']}/planned")
    assert response.status_code == 404
    assert response.json()["code"] == "PLAN_NOT_FOUND"


def test_the_gate_still_applies_to_the_planned_route(http, station):
    """락이 없으면 계획도 구동되지 않는다 (게이트 ①, R4)."""
    _, session_id = approved(http, station)
    station.lock.release(session_id)

    response = http.post(f"/api/sessions/{session_id}/planned")
    assert response.status_code == 409
    assert response.json()["code"] == "LOCK_NOT_HELD"
    assert station.gate.requests_for(session_id) == 0


def test_a_rejected_drive_leaves_the_item_in_the_queue(config, clock):
    """거부되면 큐가 줄지 않아 같은 문항으로 다시 부를 수 있다."""
    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
        llm=ReplayLlm(GOOD_RESPONSE),
    )
    with TestClient(create_app(station)) as http:
        plan_id, session_id = approved(http, station)
        stored = station.plans.get(plan_id)
        # 승인 뒤에 큐의 첫 문항을 지원되지 않는 글자로 바꾼다. 게이트가 막는다.
        stored.queue[0] = dataclasses.replace(stored.queue[0], letters="CAT")

        response = http.post(f"/api/sessions/{session_id}/planned")
        assert response.status_code == 400
        assert response.json()["code"] == "UNSUPPORTED_LETTERS"
        assert len(stored.queue) == 3
        assert station.controllers[session_id].index == 0


def test_ending_the_session_closes_the_planned_route(http, station):
    """끝난 세션으로는 계획을 더 집행할 수 없다.

    `/end` 가 락을 놓으므로 앞단 게이트 ① 이 먼저 잡는다 — 세션이 없으면 락도
    없다 (DECISIONS.md §46.3). 계획 큐가 비었는지 물어보기도 전에 막힌다.
    """
    _, session_id = approved(http, station)
    http.post(f"/api/sessions/{session_id}/planned")
    answer(http, station, session_id)
    assert http.post(f"/api/sessions/{session_id}/end").status_code == 200

    response = http.post(f"/api/sessions/{session_id}/planned")
    assert response.status_code == 409
    assert response.json()["code"] == "LOCK_NOT_HELD"
    # 계획과 세션의 연결도 끊겼다.
    from server.agent import PlanNotFoundError

    with pytest.raises(PlanNotFoundError):
        station.plans.for_session(session_id)


# --------------------------------------------------------------------------- #
# HITL 이 로그 패널에 남는가 (시연 ⑥ 라이브 정지)
# --------------------------------------------------------------------------- #

def log_events(http, count):
    """`/ws/mcp-log` 가 진단 뷰로 보낸 것을 그대로 읽는다."""
    with http.websocket_connect("/ws/mcp-log") as socket:
        return [socket.receive_json() for _ in range(count)]


def test_making_a_plan_announces_that_it_is_waiting(http):
    """승인 대기 자체가 로그에 남아야 한다.

    남지 않으면 패널에 아무것도 없다가 승인 뒤에 갑자기 구동 호출이 통과하는
    것처럼 보이고, "멈췄다 → 사람이 승인했다 → 그래서 움직인다" 가 끊긴다.
    """
    body = make_plan(http)
    event = log_events(http, 1)[0]
    assert event["type"] == "plan_ready"
    assert event["plan_id"] == body["plan_id"]
    assert event["status"] == "pending"
    assert event["session_id"] is None
    assert event["items"] == len(body["items"])
    assert event["estimated_seconds"] == body["estimated_seconds"]
    assert event["source"] == SOURCE_LLM


def test_approving_announces_the_session_it_created(http, station):
    plan_id = make_plan(http)["plan_id"]
    approved = http.post(f"/api/agent/plans/{plan_id}/approve").json()

    ready, decided = log_events(http, 2)
    assert ready["type"] == "plan_ready"
    assert decided["type"] == "plan_decided"
    assert decided["status"] == "approved"
    assert decided["session_id"] == approved["session_id"]


def test_rejecting_announces_that_nothing_was_created(http, station):
    """거부는 이것 말고는 아무 흔적도 남기지 않는다."""
    plan_id = make_plan(http)["plan_id"]
    http.post(f"/api/agent/plans/{plan_id}/reject")

    decided = log_events(http, 2)[1]
    assert decided["type"] == "plan_decided"
    assert decided["status"] == "rejected"
    assert decided["session_id"] is None
    assert station.controllers == {}


def test_a_refused_decision_announces_nothing(http, station):
    """이미 결정된 계획을 다시 결정하려 해도 로그가 늘지 않는다.

    소켓에서 "오지 않음" 을 기다릴 수는 없다 (`receive_json` 에 타임아웃이 없어
    영원히 막힌다). 그래서 표식을 하나 흘리고, 그것이 **바로 다음**에 오는 것으로
    사이에 아무것도 없었음을 증명한다.
    """
    plan_id = make_plan(http)["plan_id"]
    http.post(f"/api/agent/plans/{plan_id}/reject")
    assert http.post(f"/api/agent/plans/{plan_id}/approve").status_code == 409

    http.post("/api/mcp-log", json={"type": "tool_call", "name": "sentinel"})
    events = log_events(http, 3)
    assert [e["type"] for e in events] == ["plan_ready", "plan_decided", "tool_call"]
    assert events[2]["name"] == "sentinel"


def test_the_rule_fallback_says_so_in_the_log(config, clock):
    """규칙 폴백이 돌았는지가 계획 하나에 대해 가장 먼저 알아야 할 사실이다."""
    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
        llm=BrokenLlm(),
    )
    with TestClient(create_app(station)) as http:
        make_plan(http)
        event = log_events(http, 1)[0]
        assert event["source"] == SOURCE_RULE


def test_the_session_plan_matches_the_approved_item_count(http, station):
    """세션의 칸이 계획보다 넓으면 계획 밖의 문항이 들어갈 자리가 생긴다."""
    plan_id = make_plan(http, discrimination_items=2, word_items=1)["plan_id"]
    body = http.post(f"/api/agent/plans/{plan_id}/approve").json()
    controller = station.controllers[body["session_id"]]
    assert controller.total_items == len(body["items"]) == 3


# --------------------------------------------------------------------------- #
# 비용 — 기본 실행은 API 를 부르지 않는다
# --------------------------------------------------------------------------- #

def test_build_station_does_not_wire_a_real_model_by_default(config, clock):
    """개발자 셸에 API 키가 있어도 테스트가 돈을 쓰지 않는다."""
    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
    )
    assert station.agent._llm is None


class FakeSdk:
    """`client.beta.messages.create` 만 흉내낸 SDK.

    실제 SDK 를 부르지 않고 `AnthropicClient` 의 응답 처리를 고정한다. 호출
    인자를 붙잡아 두므로 어떤 파라미터를 보내는지도 검사할 수 있다.
    """

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.kwargs = None
        outer = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                outer.kwargs = kwargs
                if outer._raises is not None:
                    raise outer._raises
                return outer._response

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


class FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeBlock:
    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class FakeDetails:
    def __init__(self, category, explanation):
        self.category = category
        self.explanation = explanation


class FakeResponse:
    def __init__(
        self,
        *,
        content=(),
        stop_reason="end_turn",
        stop_details=None,
        model="claude-opus-5",
        usage=None,
    ):
        self.content = list(content)
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.model = model
        self.usage = usage or FakeUsage()


def test_the_anthropic_client_reports_a_missing_credential_as_a_fallback_signal():
    """자격증명이 없으면 예외가 아니라 폴백 신호다 (PRD 13.4)."""
    sdk = FakeSdk(raises=TypeError("Could not resolve authentication method."))
    client = AnthropicClient(client=sdk)
    with pytest.raises(LlmUnavailableError, match="Could not resolve"):
        client.complete("s", "u")


def test_the_client_asks_for_the_server_side_refusal_fallback():
    """Opus 5 요청에는 서버측 거절 폴백이 켜져 있어야 한다 (§47.8)."""
    from server.agent import FALLBACK_BETA

    sdk = FakeSdk(
        FakeResponse(content=[FakeBlock("text", GOOD_RESPONSE)],
                     usage=FakeUsage(output_tokens=300))
    )
    AnthropicClient(client=sdk).complete("s", "u")
    assert sdk.kwargs["fallbacks"] == "default"
    assert sdk.kwargs["betas"] == [FALLBACK_BETA]
    # 구조화 출력도 함께 나간다.
    assert sdk.kwargs["output_config"]["format"]["type"] == "json_schema"
    # 각도·flex 를 만들게 하는 파라미터는 없다 (R1).
    assert "tools" not in sdk.kwargs


def test_a_refusal_carries_its_category_into_the_fallback_reason():
    """**`content` 를 읽기 전에 `stop_reason` 을 본다.**

    거절은 HTTP 200 으로 오고 `content` 가 비어 있다. 이것을 "빈 응답" 으로
    처리하면 원인이 로그에서 사라진다 — 실제로 그렇게 됐다 (§47.8).
    """
    sdk = FakeSdk(
        FakeResponse(
            content=[],
            stop_reason="refusal",
            stop_details=FakeDetails("cyber", "blocked under the usage policy"),
        )
    )
    with pytest.raises(LlmUnavailableError) as raised:
        AnthropicClient(client=sdk).complete("s", "u")
    assert "declined" in str(raised.value)
    assert "'cyber'" in str(raised.value)
    assert "usage policy" in str(raised.value)


def test_a_response_without_text_reports_the_stop_reason():
    """사고 블록만 온 응답도 원인을 밝힌다."""
    sdk = FakeSdk(
        FakeResponse(content=[FakeBlock("thinking")], stop_reason="max_tokens")
    )
    with pytest.raises(LlmUnavailableError) as raised:
        AnthropicClient(client=sdk).complete("s", "u")
    assert "max_tokens" in str(raised.value)
    assert "thinking" in str(raised.value)


def test_a_thinking_block_before_the_text_does_not_break_extraction():
    """Opus 5 는 사고가 기본으로 켜져 있어 `content[0]` 이 text 가 아니다."""
    sdk = FakeSdk(
        FakeResponse(
            content=[FakeBlock("thinking"), FakeBlock("text", GOOD_RESPONSE)],
            usage=FakeUsage(input_tokens=2400, output_tokens=2200),
        )
    )
    reply = AnthropicClient(client=sdk).complete("s", "u")
    assert parse_plan_json(reply.text).items
    assert reply.input_tokens == 2400
    assert reply.output_tokens == 2200


def test_a_plan_served_by_the_fallback_model_reports_that_model():
    """거절을 다른 모델이 이어받으면 사용량에 그 모델이 남는다."""
    sdk = FakeSdk(
        FakeResponse(
            content=[FakeBlock("text", GOOD_RESPONSE)],
            model="claude-opus-4-8",
            usage=FakeUsage(output_tokens=300),
        )
    )
    reply = AnthropicClient(client=sdk).complete("s", "u")
    assert reply.model == "claude-opus-4-8"


@pytest.mark.skipif(
    not LIVE,
    reason="실제 API 호출은 SPELLHAND_LIVE_LLM=1 일 때만 돈다 (비용)",
)
def test_live_plan_generation(config, words, db, capsys):
    """실제 LLM 호출 1회. 결과와 토큰 사용량을 찍는다.

    기본 실행에서는 건너뛴다. 돌리려면:

        SPELLHAND_LIVE_LLM=1 .venv\\Scripts\\python.exe -m pytest \\
            tests/test_agent.py -k live -s
    """
    from server.env import load_env

    load_env()
    client = AnthropicClient()
    planner = PlanAgent(config, words, db, llm=client, rng=random.Random(1))
    plan, report, attempts = planner.plan(
        "haru",
        "V와 W가 계속 헷갈린다고 한다. 그 둘을 집중적으로 보고 단어로 마무리해 줘.",
        discrimination_items=8,
        word_items=4,
    )

    with capsys.disabled():
        print(f"\nmodel: {client.model}")
        for attempt in attempts:
            print(f"attempt {attempt.index}: usage={attempt.usage and attempt.usage.as_json()}")
            print(f"  error={attempt.error}")
        print(f"source: {plan.source}")
        print(f"rationale: {plan.rationale}")
        print(f"target_pairs: {[list(p) for p in plan.target_pairs]}")
        for item in plan.items:
            print(
                f"  {item.track:<14} {item.letters:<8} "
                f"hold={item.hold_ms:<5} {item.boundary}"
            )
        print(
            f"estimated_seconds: llm={plan.estimated_seconds} "
            f"station={estimate_seconds(config, plan.items)}"
        )
        for finding in report.findings:
            print(f"  {finding.action:<10} {finding.check:<22} {finding.detail[:80]}")

    assert plan.source == SOURCE_LLM, "실제 호출이 폴백으로 내려갔다"
    assert plan.items
    assert attempts[0].usage.input_tokens > 0
