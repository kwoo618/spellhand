"""공부 모드와 입력 방법 (DECISIONS.md §56).

공부는 훈련이 아니다. 손은 움직이지만(락·게이트는 그대로) 기록은 남지 않는다 —
`items` 에도, 난이도에도, 깊이쌍 해금 진척에도.

글자 버튼은 `poses.json` 을 따른다. 훈련의 버튼 입력도 같은 목록을 쓰므로
문항과 무관해야 한다 — 그래야 정답이 새지 않는다.

입력 방법은 세션마다 하나이고 첫 문항 전에만 정한다.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from server.config import load_config
from server.db import (
    Confusion,
    Database,
    Item,
    LearnerDifficulty,
    TrainingSession,
)
from server.device.mock import MockDevice
from server.routes import build_station, create_app
from server.routes.api import router as api_router
from server.safety import PURPOSE_DEMO
from server.study import REFERENCE_POSES, STUDY_SOURCE
from tests.conftest import REAL_POSES, REAL_WORDS
from tests.test_approach import SpyDevice
from tests.test_routes import SMALL_PLAN, answer_correctly, start


def _build(config, clock, *, config_path=REAL_POSES, device=None):
    return build_station(
        config_path=config_path,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=device or MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
    )


@pytest.fixture
def station(config, clock):
    return _build(config, clock)


@pytest.fixture
def client(station):
    with TestClient(create_app(station)) as test_client:
        yield test_client


def start_study(client, name="haru"):
    response = client.post("/api/study-sessions", json={"learner_name": name})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def show(client, clock, study_id, **body):
    """버튼 하나. 사람이 다음 버튼을 누르기 전의 간격을 둔다."""
    response = client.post(f"/api/study-sessions/{study_id}/show", json=body)
    clock.advance(1)
    return response


def counts(station):
    with station.database.session() as db:
        return {
            "items": db.scalar(select(func.count(Item.id))),
            "confusions": db.scalar(select(func.count(Confusion.id))),
            "difficulty": db.scalar(select(func.count(LearnerDifficulty.id))),
        }


# --------------------------------------------------------------------------- #
# 공부는 기록하지 않는다
# --------------------------------------------------------------------------- #

def test_studying_every_letter_writes_no_item(client, station, clock, config):
    study_id = start_study(client)
    for letter in sorted(config.poses):
        assert show(client, clock, study_id, letter=letter).status_code == 200
    for name in REFERENCE_POSES:
        assert show(client, clock, study_id, pose=name).status_code == 200
    assert client.post(f"/api/study-sessions/{study_id}/end").status_code == 200

    assert counts(station) == {"items": 0, "confusions": 0, "difficulty": 0}


def test_the_study_row_is_marked_and_never_counts_as_a_session(
    client, station, clock
):
    study_id = start_study(client)
    show(client, clock, study_id, letter="B")
    client.post(f"/api/study-sessions/{study_id}/end")

    with station.database.session() as db:
        row = db.get(TrainingSession, study_id)
        assert (row.source, row.status, row.plan_json) == (STUDY_SOURCE, "done", None)

    progress = client.get("/api/learners/haru/progress").json()
    assert progress["sessions"] == 0


def _progress_without_ids(client, name):
    body = client.get(f"/api/learners/{name}/progress").json()
    body.pop("learner_id")
    return body


def test_studying_does_not_move_difficulty_or_depth_unlock(client, station, clock, config):
    """훈련 기록이 있는 학습자가 공부를 해도 진행이 한 칸도 움직이지 않는다."""
    session = start(client)
    sid = session["session_id"]
    for _ in range(SMALL_PLAN["discrimination_items"] + SMALL_PLAN["word_items"]):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        answer_correctly(client, station, sid)
    client.post(f"/api/sessions/{sid}/end")

    before = _progress_without_ids(client, "haru")
    before_counts = counts(station)

    study_id = start_study(client)
    for letter in sorted(config.poses):
        show(client, clock, study_id, letter=letter)
    client.post(f"/api/study-sessions/{study_id}/end")

    assert _progress_without_ids(client, "haru") == before
    assert counts(station) == before_counts


def test_a_first_time_learner_still_starts_on_the_first_rung_after_study(
    client, clock
):
    fresh = _progress_without_ids(client, "nobody-yet")

    study_id = start_study(client, "newcomer")
    show(client, clock, study_id, letter="V")
    client.post(f"/api/study-sessions/{study_id}/end")

    after = _progress_without_ids(client, "newcomer")
    after["learner_name"] = fresh["learner_name"]
    assert after == fresh


def test_the_study_module_cannot_reach_the_records():
    """공부 컨트롤러에는 기록하는 길이 **없다.** 막아 둔 것이 아니다."""
    source = Path("server/study.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]  # 머리말(설명)은 빼고 본다
    for forbidden in (
        "Item", "Confusion", "save_difficulty", "LearnerDifficulty",
        "submit", "_record",
    ):
        assert forbidden not in code, forbidden


# --------------------------------------------------------------------------- #
# 공부도 손을 움직인다 — 락과 게이트
# --------------------------------------------------------------------------- #

def test_study_holds_the_lock_so_training_cannot_start(client):
    study_id = start_study(client)
    blocked = client.post("/api/sessions", json={"learner_name": "mina", **SMALL_PLAN})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "LOCK_UNAVAILABLE"


def test_a_study_is_never_offered_as_a_session_to_join(client):
    """참여 조회가 공부를 훈련 세션으로 내주면 `/next` 가 공부 세션으로 간다."""
    study_id = start_study(client)
    open_ = client.get("/api/sessions/open").json()
    assert open_["session_id"] is None
    assert open_["lock_holder"] == study_id


def test_study_cannot_start_while_a_training_session_holds_the_hand(client, station):
    start(client)
    response = client.post("/api/study-sessions", json={"learner_name": "mina"})
    assert response.status_code == 409
    assert response.json()["code"] == "LOCK_UNAVAILABLE"
    assert station.studies == {}


def test_ending_a_study_releases_the_lock(client, station):
    study_id = start_study(client)
    client.post(f"/api/study-sessions/{study_id}/end")
    assert station.lock.holder() is None
    start(client)


def test_reset_ends_a_study(client, station):
    study_id = start_study(client)
    result = client.post("/api/device/reset").json()
    assert result["ended_sessions"] == [study_id]
    assert station.lock.holder() is None
    with station.database.session() as db:
        assert db.get(TrainingSession, study_id).status == "aborted"


def test_a_study_without_the_lock_cannot_drive(client, clock):
    study_id = start_study(client)
    client.post("/api/device/reset")
    response = show(client, clock, study_id, letter="B")
    assert response.status_code == 409
    assert response.json()["code"] == "LOCK_NOT_HELD"


def test_every_study_press_is_a_gated_drive_request(client, station, clock):
    study_id = start_study(client)
    show(client, clock, study_id, letter="B")
    show(client, clock, study_id, pose="neutral")
    assert station.gate.requests_for(study_id) == 2


def test_a_press_right_after_another_request_hits_the_rate_limit(client, station):
    """표현 중에 누른 두 번째 버튼은 게이트 ⑥ 에 걸린다."""
    study_id = start_study(client)
    station.gate.authorize_steps(
        study_id, "B", "discrimination", depth_unlocked=True, purpose=PURPOSE_DEMO
    )
    response = client.post(f"/api/study-sessions/{study_id}/show", json={"letter": "W"})
    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMIT"


def test_a_letter_the_hand_cannot_make_is_refused_with_alternatives(client, clock, config):
    study_id = start_study(client)
    response = show(client, clock, study_id, letter="C")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "UNSUPPORTED_LETTERS"
    assert body["alternatives"] == sorted(config.poses)


def test_study_shows_one_letter_at_a_time(client, clock):
    study_id = start_study(client)
    response = show(client, clock, study_id, letter="BW")
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


def test_only_neutral_and_safe_are_reference_poses(client, clock):
    study_id = start_study(client)
    response = show(client, clock, study_id, pose="fist")
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


@pytest.mark.parametrize("name", REFERENCE_POSES)
def test_a_reference_pose_lands_on_the_config_value(client, station, clock, config, name):
    study_id = start_study(client)
    show(client, clock, study_id, pose=name)
    expected = getattr(config, name)
    assert station.device.current_flex() == pytest.approx(dict(expected))


def test_study_passes_through_the_approach_like_training(config, clock):
    """공부에서 본 E 가 훈련에서 본 E 와 같은 자세여야 한다 (§55)."""
    if not config.approaches:
        pytest.skip("config/poses.json has no approach")
    letter = sorted(config.approaches)[0]
    device = SpyDevice(config, clock)
    station = _build(config, clock, device=device)
    with TestClient(create_app(station)) as client:
        study_id = start_study(client)
        device.moves.clear()
        show(client, clock, study_id, letter=letter)
    flexes = [move[0] for move in device.moves]
    assert flexes == [
        pytest.approx(dict(config.poses[config.approaches[letter]])),
        pytest.approx(dict(config.poses[letter])),
    ]


# --------------------------------------------------------------------------- #
# 글자 목록은 poses.json 을 따른다
# --------------------------------------------------------------------------- #

def test_the_letter_list_is_the_pose_table(client, config):
    body = client.get("/api/letters").json()
    assert body == {"letters": sorted(config.poses), "references": list(REFERENCE_POSES)}


def _config_without(tmp_path, letter):
    """실제 poses.json 에서 글자 하나와 그 글자를 쓰는 혼동쌍을 뺀 사본."""
    raw = json.loads(REAL_POSES.read_text(encoding="utf-8"))
    del raw["poses"][letter]
    raw["confusion_pairs"] = [
        pair for pair in raw["confusion_pairs"] if letter not in pair["pair"]
    ]
    for pose in raw["poses"].values():
        assert pose.get("approach") != letter, "고른 글자가 경유 대상이다"
    raw["curriculum"]["heavy_letters"].pop(letter, None)
    path = tmp_path / "poses.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_removing_a_letter_from_the_config_removes_its_button(tmp_path, config, clock):
    letter = "Y"
    assert letter in config.poses
    path = _config_without(tmp_path, letter)
    smaller = load_config(path)
    station = _build(smaller, clock, config_path=path)
    with TestClient(create_app(station)) as client:
        letters = client.get("/api/letters").json()["letters"]
        study = client.post("/api/study-sessions", json={"learner_name": "haru"}).json()
        refused = client.post(
            f"/api/study-sessions/{study['session_id']}/show", json={"letter": letter}
        )
    assert letter not in letters
    assert letters == sorted(smaller.poses)
    assert letter not in study["letters"]
    assert refused.json()["code"] == "UNSUPPORTED_LETTERS"


def test_the_page_draws_letter_buttons_from_the_server(client, config):
    """화면에 글자 목록이 박혀 있지 않다. `/api/letters` 에서 받는다."""
    body = client.get("/station.html").text
    assert '"/api/letters"' in body
    script = body[body.index("<script>"):]
    ordered = sorted(config.poses)
    for spelled in (
        "".join(ordered),
        json.dumps(ordered),
        ", ".join(f'"{c}"' for c in ordered[:3]),
    ):
        assert spelled not in script, spelled


# --------------------------------------------------------------------------- #
# 버튼 입력으로 정답이 새지 않는다
# --------------------------------------------------------------------------- #

def test_the_letter_list_does_not_depend_on_the_item(client, station):
    before = client.get("/api/letters").json()
    sid = start(client, input_mode="buttons")["session_id"]
    seen = []
    for _ in range(SMALL_PLAN["discrimination_items"] + SMALL_PLAN["word_items"]):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        seen.append(client.get("/api/letters").json())
        answer_correctly(client, station, sid)
    assert all(body == before for body in seen)


def test_the_letter_route_takes_no_session():
    """세션을 받는 자리가 없으면 문항에 맞춘 목록을 낼 길도 없다."""
    routes = {route.path for route in api_router.routes}
    assert "/api/letters" in routes
    assert not any(
        path.startswith("/api/sessions/") and "letters" in path for path in routes
    )


def test_the_button_pad_is_built_without_looking_at_the_item(client):
    """버튼을 그리는 함수가 문항 표현을 읽지 않는다."""
    body = client.get("/station.html").text.replace("\r\n", "\n")
    start_at = body.index("function renderAnswerPad(")
    end_at = body.index("\n}\n", start_at)
    pad = body[start_at:end_at]
    assert "prompt" not in pad
    assert "state.letters" in pad


# --------------------------------------------------------------------------- #
# 입력 방법은 세션 중에 바뀌지 않는다
# --------------------------------------------------------------------------- #

def _plan_json(station, session_id):
    with station.database.session() as db:
        return json.loads(db.get(TrainingSession, session_id).plan_json)


def test_the_input_mode_chosen_at_start_is_recorded(client, station):
    session = start(client, input_mode="drag")
    assert session["input_mode"] == "drag"
    assert _plan_json(station, session["session_id"])["input_mode"] == "drag"


def test_the_input_mode_cannot_change_during_a_session(client, station):
    sid = start(client, input_mode="typing")["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    response = client.post(
        f"/api/sessions/{sid}/input-mode", json={"input_mode": "buttons"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"
    assert _plan_json(station, sid)["input_mode"] == "typing"


def test_a_different_mode_is_refused_even_before_the_first_item(client, station):
    sid = start(client, input_mode="typing")["session_id"]
    response = client.post(
        f"/api/sessions/{sid}/input-mode", json={"input_mode": "drag"}
    )
    assert response.json()["code"] == "SESSION_STATE"
    assert station.controllers[sid].input_mode == "typing"


def test_repeating_the_same_mode_is_accepted(client):
    """화면을 새로 고쳐 다시 참여하는 경로다."""
    sid = start(client, input_mode="buttons")["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    response = client.post(
        f"/api/sessions/{sid}/input-mode", json={"input_mode": "buttons"}
    )
    assert response.status_code == 200


def test_an_approved_session_gets_its_mode_when_the_learner_joins(client, station):
    plan = client.post(
        "/api/agent/plan",
        json={"learner_name": "haru", "request": "기본", **SMALL_PLAN},
    ).json()
    approved = client.post(f"/api/agent/plans/{plan['plan_id']}/approve")
    assert approved.status_code == 201, approved.text
    sid = approved.json()["session_id"]

    open_ = client.get("/api/sessions/open").json()
    assert open_["session_id"] == sid and open_["input_mode"] is None
    assert "input_mode" not in _plan_json(station, sid)

    chosen = client.post(f"/api/sessions/{sid}/input-mode", json={"input_mode": "buttons"})
    assert chosen.status_code == 200
    assert client.get("/api/sessions/open").json()["input_mode"] == "buttons"
    assert _plan_json(station, sid)["input_mode"] == "buttons"


def test_a_mode_cannot_be_chosen_after_the_first_item(client, station):
    sid = start(client)["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    response = client.post(
        f"/api/sessions/{sid}/input-mode", json={"input_mode": "buttons"}
    )
    assert response.json()["code"] == "SESSION_STATE"
    assert station.controllers[sid].input_mode is None


def test_an_unknown_mode_opens_no_session(client, station):
    response = client.post(
        "/api/sessions", json={"learner_name": "haru", "input_mode": "voice"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"
    assert station.lock.holder() is None
    with station.database.session() as db:
        assert db.scalar(select(func.count(TrainingSession.id))) == 0


def test_sessions_without_a_mode_keep_their_old_plan_json(client, station):
    """MCP·에이전트 세션은 화면을 거치지 않는다. 칸이 없어야 한다."""
    sid = start(client)["session_id"]
    assert _plan_json(station, sid) == SMALL_PLAN
