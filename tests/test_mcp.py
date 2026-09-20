"""MCP 서버 테스트 (PRD 12장).

**실제 라우트를 지난다.** `StationClient` 에 ASGI 테스트 클라이언트를 주입해
같은 프로세스 안에서 `SessionController` → `SafetyGate` 를 거치게 했다. 게이트를
우회할 수 없다는 것을 확인하는 테스트가 게이트를 흉내낸 것을 지나면 의미가 없다.

디바이스 동작은 인라인으로, 시간은 주입해서 돌린다 (`tests/test_routes.py` 와
같은 방식).
"""

import asyncio
import json
import random

import pytest
from fastapi.testclient import TestClient

from mcp_server.server import (
    REASON_INVALID_ARGUMENTS,
    REASON_UNREACHABLE,
    StationClient,
    create_server,
)
from server.db import TRACK_DISCRIMINATION, TRACK_WORD, Database
from server.device.mock import MockDevice
from server.routes import build_station, create_app
from server.routes.events import MCP_LOG_CHANNEL
from tests.conftest import REAL_POSES, REAL_WORDS

SMALL_PLAN = {"discrimination_items": 2, "word_items": 1}

# 각도·flex·전이 시간을 지정하는 길이 스키마에 없어야 한다 (R1, PRD 15.2).
FORBIDDEN_PARAMETER_HINTS = (
    "flex",
    "angle",
    "deg",
    "servo",
    "pin",
    "pwm",
    "permille",
    "transition",
)


@pytest.fixture
def station(config, clock):
    return build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        rng=random.Random(20260920),
        background=lambda work: work(),
    )


@pytest.fixture
def http(station):
    with TestClient(create_app(station)) as test_client:
        yield test_client


@pytest.fixture
def mcp(http):
    """도구가 등록된 MCP 서버. 스테이션 호출은 위의 ASGI 클라이언트로 나간다."""
    return create_server(StationClient(http=http))


def call(server, name, **arguments):
    """도구 하나를 부르고 구조화된 본문을 돌려준다."""
    result = asyncio.run(server.call_tool(name, arguments))
    assert result.structured_content is not None, result.content
    return result.structured_content


def tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def start(server, **overrides):
    body = call(
        server,
        "start_session",
        learner_name=overrides.pop("learner_name", "haru"),
        plan=dict(SMALL_PLAN, **overrides),
    )
    assert body["ok"] is True, body
    return body["session_id"]


def expected_answer(station, session_id):
    """서버가 들고 있는 정답. 도구 응답에는 들어 있지 않다."""
    return station.controllers[session_id]._active_answer()


# --------------------------------------------------------------------------- #
# 도구 목록과 스키마
# --------------------------------------------------------------------------- #

def test_the_three_layers_are_all_registered(mcp):
    assert set(tools(mcp)) == {
        # 1층 · 조회
        "get_capabilities",
        "get_learner_stats",
        "get_item_bank",
        "get_station_status",
        # 2층 · 세션
        "start_session",
        "submit_answer",
        "end_session",
        # 3층 · 구동
        "present_sequence",
        "demo_letter",
    }


def _property_names(schema):
    """스키마 전체(중첩 모델 `$defs` 포함)의 속성 이름."""
    names = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names.extend(value)
            names.extend(_property_names(value))
    elif isinstance(schema, list):
        for item in schema:
            names.extend(_property_names(item))
    return names


def test_no_tool_takes_a_flex_or_angle_or_transition_parameter(mcp):
    """LLM 이 각도를 지정하는 경로가 **스키마 수준에서** 없다 (R1, PRD 15.2).

    시연 ⑥ 의 "설명 슬라이드"가 이 테스트의 결과를 띄운다. 파라미터를 하나
    추가하려는 사람이 여기서 막히는 것이 요점이다.
    """
    offenders = []
    for name, tool in tools(mcp).items():
        for parameter in _property_names(tool.input_schema):
            lowered = parameter.lower()
            for hint in FORBIDDEN_PARAMETER_HINTS:
                if hint in lowered:
                    offenders.append(f"{name}({parameter})")
    assert offenders == []


def test_the_drive_tools_require_a_session_id(mcp):
    for name in ("present_sequence", "demo_letter"):
        assert "session_id" in tools(mcp)[name].input_schema["required"]


def test_the_track_and_boundary_enums_come_from_the_server_constants(mcp):
    schema = tools(mcp)["present_sequence"].input_schema["properties"]
    assert schema["track"]["enum"] == [TRACK_DISCRIMINATION, TRACK_WORD]
    assert schema["boundary"]["enum"] == ["explicit", "direct"]


# --------------------------------------------------------------------------- #
# 1층 · 조회
# --------------------------------------------------------------------------- #

def test_get_capabilities_reads_everything_from_the_config(mcp, station):
    body = call(mcp, "get_capabilities")
    assert body["ok"] is True
    assert body["letters"] == sorted(station.config.poses)
    assert body["tracks"] == [TRACK_DISCRIMINATION, TRACK_WORD]
    assert body["transition_ms"] == station.config.timing.transition_ms
    assert body["word_count"] == len(station.words.words)
    assert body["difficulty_axes"]["hold_ms"]["min"] == (
        station.config.timing.hold_min_ms
    )
    assert body["difficulty_axes"]["boundary"] == ["explicit", "direct"]
    # 이력이 없는 학습자는 깊이쌍이 잠겨 있다.
    assert body["depth_pairs_unlocked"] is False
    assert any(pair["depth"] for pair in body["confusion_pairs"])


def test_get_station_status_reports_the_mode_and_the_lock(mcp, station):
    body = call(mcp, "get_station_status")
    assert body == {
        "ok": True,
        "state": "idle",
        "locked_by": None,
        "device_mode": "mock",
    }

    session_id = start(mcp)
    assert call(mcp, "get_station_status")["locked_by"] == session_id


def test_get_item_bank_returns_candidates_that_pass_the_constraints(mcp):
    body = call(
        mcp, "get_item_bank", track=TRACK_DISCRIMINATION, length=3, count=4
    )
    assert body["ok"] is True
    assert len(body["candidates"]) == 4
    for letters in body["candidates"]:
        assert len(letters) == 3
        # 같은 글자가 연속되지 않는다 (기획서 4.1).
        assert all(a != b for a, b in zip(letters, letters[1:]))


def test_get_item_bank_draws_words_from_the_filtered_pool(mcp, station):
    body = call(mcp, "get_item_bank", track=TRACK_WORD, length=4, count=3)
    pool = {word.letters for word in station.words.of_length(4)}
    assert body["candidates"]
    assert set(body["candidates"]) <= pool


def test_get_item_bank_leaves_out_locked_depth_pairs(mcp):
    """깊이쌍이 잠긴 상태에서 E·I 만으로는 통과하는 조합이 없다.

    후보 0개가 정상적인 답이다 — 추측으로 채우지 않는다 (R5).
    """
    body = call(
        mcp,
        "get_item_bank",
        track=TRACK_DISCRIMINATION,
        length=2,
        count=5,
        letters=["E", "I"],
    )
    assert body["ok"] is True
    assert body["depth_pairs_unlocked"] is False
    assert body["candidates"] == []


def test_get_learner_stats_never_reports_a_combined_figure(mcp, station):
    session_id = start(mcp)
    call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="BDE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    call(mcp, "submit_answer", session_id=session_id, answer="BDE")
    call(mcp, "end_session", session_id=session_id)

    body = call(mcp, "get_learner_stats", learner_name="haru")
    assert body["ok"] is True
    assert set(body["by_track"]) == {TRACK_DISCRIMINATION, TRACK_WORD}
    # 트랙별 수치는 트랙 아래에만 있다. 바깥에 합친 숫자가 없다.
    serialized = json.dumps(body, ensure_ascii=False)
    for forbidden in ("overall", "combined", "total_accuracy"):
        assert forbidden not in serialized
    assert "accuracy" not in body
    assert body["by_track"][TRACK_DISCRIMINATION]["answered"] == 1
    assert body["by_track"][TRACK_WORD]["answered"] == 0
    assert body["by_track"][TRACK_DISCRIMINATION]["accuracy_by_hold_ms"]
    assert body["by_track"][TRACK_DISCRIMINATION]["recent_items"]


def test_get_learner_stats_accepts_a_name_with_no_history(mcp, http):
    body = call(mcp, "get_learner_stats", learner_name="아무도")
    assert body["ok"] is True
    assert body["learner_id"] is None
    assert body["by_track"][TRACK_DISCRIMINATION]["answered"] == 0
    # 조회가 학습자를 만들지 않는다. 오타 한 번이 목록에 남으면 안 된다.
    assert http.get("/api/learners").json()["learners"] == []


# --------------------------------------------------------------------------- #
# 2층·3층 · 정상 경로
# --------------------------------------------------------------------------- #

def test_a_session_runs_from_start_to_summary_through_mcp(mcp, station):
    session_id = start(mcp)

    presented = call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="BDE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert presented["ok"] is True
    assert presented["status"] == "presented"
    assert presented["length"] == 3
    # 표현 응답에도 정답 문자열은 없다.
    assert "BDE" not in json.dumps(presented)

    # 채점 대상은 **표현한 그 글자열**이다.
    assert expected_answer(station, session_id) == "BDE"
    judged = call(mcp, "submit_answer", session_id=session_id, answer="BDE")
    assert judged["result"] == "correct"

    summary = call(mcp, "end_session", session_id=session_id)
    assert summary["ok"] is True
    assert summary["by_track"][TRACK_DISCRIMINATION]["answered"] == 1
    assert station.lock.holder() is None


def test_present_sequence_is_scored_against_what_the_hand_showed(mcp, station):
    session_id = start(mcp)
    call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="VWB",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    judged = call(mcp, "submit_answer", session_id=session_id, answer="VWD")
    assert judged["result"] == "wrong"
    # 오답 1회째에는 정답도 혼동 내역도 나가지 않는다 (기획서 5.2).
    assert judged["retry"] is True
    assert judged["expected"] is None
    assert judged["confusions"] == []


def test_demo_letter_drives_the_hand_without_recording(mcp, station):
    session_id = start(mcp)
    for letter in ("V", "W"):
        body = call(
            mcp, "demo_letter", session_id=session_id, letter=letter, hold_ms=1200
        )
        assert body["ok"] is True
        assert body["letter"] == letter
        assert body["hold_ms"] == 1200

    # 시범은 문항이 아니다. 요약에 남지 않는다.
    summary = call(mcp, "end_session", session_id=session_id)
    assert summary["by_track"][TRACK_DISCRIMINATION]["answered"] == 0


def test_hold_ms_out_of_range_is_clamped_and_reported(mcp, station):
    session_id = start(mcp)
    body = call(
        mcp, "demo_letter", session_id=session_id, letter="V", hold_ms=99999
    )
    assert body["hold_ms"] == station.config.timing.hold_max_ms


# --------------------------------------------------------------------------- #
# 구조화된 실패 3종 (PRD 12.2)
# --------------------------------------------------------------------------- #

def test_unsupported_letters_comes_back_structured(mcp, station):
    session_id = start(mcp)
    body = call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="CAT",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert body["ok"] is False
    assert body["reason"] == "unsupported_letters"
    assert body["requested"] == "CAT"
    assert body["unsupported"] == ["A", "C", "T"]
    assert body["supported"] == sorted(station.config.poses)
    assert body["next_step"] == "call get_item_bank() with supported letters only"
    assert body["detail"]


def test_lock_not_held_comes_back_structured(mcp):
    """`start_session` 없이 `present_sequence` (PRD 15.2 라이브 차단)."""
    body = call(
        mcp,
        "present_sequence",
        session_id=1,
        letters="BDE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert body["ok"] is False
    assert body["reason"] == "lock_not_held"
    assert body["next_step"] == "call start_session() first"
    assert body["detail"]


def test_curriculum_violation_comes_back_structured(mcp):
    """신규 학습자에게 `E-I-E` (PRD 15.2 라이브 차단)."""
    session_id = start(mcp)
    body = call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="EIE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert body["ok"] is False
    assert body["reason"] == "curriculum_violation"
    assert body["blocked_pairs"] == [["E", "I"]]
    assert body["next_step"] == (
        "use demo_letter for these, or choose non-depth pairs"
    )
    assert body["detail"]


def test_a_depth_pair_is_refused_even_as_part_of_a_longer_item(mcp):
    session_id = start(mcp)
    body = call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="BOSV",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert body["reason"] == "curriculum_violation"
    assert body["blocked_pairs"] == [["O", "S"]]


def test_a_demo_of_one_letter_is_allowed_where_the_pair_is_not(mcp):
    """시범은 커리큘럼에 걸리지 않는다 — 글자 하나는 깊이쌍이 될 수 없다."""
    session_id = start(mcp)
    for letter in ("E", "I"):
        assert call(
            mcp, "demo_letter", session_id=session_id, letter=letter
        )["ok"] is True


# --------------------------------------------------------------------------- #
# 게이트 우회 불가 (R4)
# --------------------------------------------------------------------------- #

def test_no_drive_tool_moves_the_hand_without_a_session(mcp, station):
    """세션이 없으면 손은 움직이지 않는다. 게이트가 통과시킨 요청이 0건이다."""
    for name, arguments in (
        (
            "present_sequence",
            {
                "letters": "BDE",
                "track": TRACK_DISCRIMINATION,
                "boundary": "explicit",
            },
        ),
        ("demo_letter", {"letter": "V"}),
    ):
        body = call(mcp, name, session_id=1, **arguments)
        assert body["ok"] is False, name
        assert body["reason"] == "lock_not_held", name

    assert station.gate.request_count == 0
    assert station.lock.holder() is None


def test_a_session_that_lost_the_lock_cannot_drive(mcp, station):
    """락이 만료되면 진행 중이던 세션도 구동할 수 없다 (게이트 ①)."""
    session_id = start(mcp)
    station.lock.release(session_id)

    body = call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="BDE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert body["reason"] == "lock_not_held"
    assert station.gate.requests_for(session_id) == 0


def test_a_rejected_drive_leaves_the_session_where_it_was(mcp, station):
    """거부된 요청이 문항 번호를 올려 두지 않는다."""
    session_id = start(mcp)
    controller = station.controllers[session_id]

    call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="CAT",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert controller.index == 0
    assert controller.state == "awaiting_item"

    ok = call(
        mcp,
        "present_sequence",
        session_id=session_id,
        letters="BDE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )
    assert ok["index"] == 1


def test_a_boundary_the_schema_would_reject_is_still_refused_by_the_server(
    mcp, station, http
):
    """스키마가 막지만 스키마만 믿지 않는다.

    도구 스키마의 enum 을 지나오지 않은 값 — 즉 REST 를 직접 두드린 경우 — 도
    거부되어야 한다. 게이트는 경계 모드를 보지 않으므로 이 검사가 유일한 방어선이고,
    막히지 않으면 `_present()` 가 모르는 값을 조용히 direct 로 취급한다.
    """
    session_id = start(mcp)
    response = http.post(
        f"/api/sessions/{session_id}/sequence",
        json={
            "letters": "BDE",
            "track": TRACK_DISCRIMINATION,
            "boundary": "sideways",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"
    assert station.gate.requests_for(session_id) == 0


# --------------------------------------------------------------------------- #
# /ws/mcp-log
# --------------------------------------------------------------------------- #

def test_the_log_carries_tool_calls_and_blocked_reasons(mcp, station, http):
    """호출 로그가 실시간으로 나간다 (시연 ⑥).

    `EventHub` 는 늦게 붙은 구독자에게 최근 이벤트를 되돌려 주므로, 호출을
    먼저 하고 나중에 구독해도 보인다.
    """
    call(mcp, "get_capabilities")
    call(
        mcp,
        "present_sequence",
        session_id=1,
        letters="BDE",
        track=TRACK_DISCRIMINATION,
        boundary="explicit",
    )

    with http.websocket_connect("/ws/mcp-log") as socket:
        events = [socket.receive_json() for _ in range(4)]

    assert events[0] == {
        "type": "tool_call",
        "name": "get_capabilities",
        "summary": "능력 선언 조회",
    }
    assert events[1]["type"] == "tool_result"
    assert events[1]["name"] == "get_capabilities"
    assert events[2] == {
        "type": "tool_call",
        "name": "present_sequence",
        "summary": "세션 1 에 3글자 표현",
    }
    blocked = events[3]
    assert blocked["type"] == "blocked"
    assert blocked["reason"] == "lock_not_held"
    assert blocked["detail"]


def test_the_log_socket_does_not_take_commands(mcp, station, http):
    """단방향이다. 이 소켓으로 보낸 것은 읽지 않는다."""
    with http.websocket_connect("/ws/mcp-log") as socket:
        socket.send_json({"type": "present_sequence", "letters": "BDE"})
        call(mcp, "get_capabilities")
        assert socket.receive_json()["type"] == "tool_call"

    assert station.gate.request_count == 0


def test_the_log_endpoint_refuses_an_unknown_event_type(http):
    assert http.post("/api/mcp-log", json={"type": "tool_call"}).status_code == 202
    response = http.post("/api/mcp-log", json={"type": "drive"})
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


def test_the_log_channel_is_not_a_session_channel(station):
    """세션 id 는 1부터이므로 로그 채널(0)과 겹치지 않는다."""
    assert MCP_LOG_CHANNEL == 0


# --------------------------------------------------------------------------- #
# 스테이션이 없을 때
# --------------------------------------------------------------------------- #

def test_every_tool_answers_when_the_station_is_not_up():
    """서버가 안 떠 있으면 예외가 아니라 구조화된 실패로 답한다.

    Claude Desktop 은 MCP 서버를 먼저 띄운다. 그때 스테이션이 없으면 도구가
    터지는 대신 "서버를 띄우라" 고 말해야 한다.
    """
    # 아무것도 듣고 있지 않은 포트. 연결 자체가 실패한다.
    server = create_server(StationClient("http://127.0.0.1:1", timeout=0.5))
    body = call(server, "get_capabilities")
    assert body["ok"] is False
    assert body["reason"] == REASON_UNREACHABLE
    assert "uvicorn" in body["next_step"]


def test_a_bad_argument_that_the_schema_lets_through_is_reported(mcp):
    """스테이션이 거부 코드를 붙이지 않는 경우(인자 검증)도 사유가 있다."""
    body = call(mcp, "get_item_bank", track=TRACK_DISCRIMINATION, length=0, count=1)
    assert body["ok"] is False
    # length 0 은 라우트가 SESSION_STATE 로 거부한다. 어댑터가 사유를 지어내지
    # 않고 코드를 소문자로 옮긴다.
    assert body["reason"] == "session_state"
    assert REASON_INVALID_ARGUMENTS not in body["reason"]
