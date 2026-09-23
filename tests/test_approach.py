"""경유 자세(approach) 테스트 (DECISIONS.md §55).

경유가 있는 글자는 경유 자세로 한 번 갔다가 목표로 간다 — P 명령 두 번이다.
경유가 없는 글자의 동작은 그대로여야 한다. 경유 글자는 설정에서 읽는다. 테스트도
글자를 직접 고르지 않고 `config.approaches` 에서 찾는다 — 전기팀이 경유를
옮기거나 지워도 이 테스트가 설정을 따라간다.

설정을 바꾼 경우는 실제 poses.json 을 복사해 임시 파일로 쓴다.
"""

import copy
import json
import random
import sqlite3
from datetime import timedelta

import pytest

from server.calibration import calibration_hash
from server.config import load_config, load_words
from server.db import (
    BOUNDARY_DIRECT,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    Database,
    Item,
    SchemaOutdatedError,
)
from server.device import Pose
from server.device.mock import MockDevice
from server.safety import PoseStep, SafetyGate, SessionLock
from server.session import SessionController
from tests.conftest import REAL_POSES, REAL_WORDS
from tests.test_serial import FakePort, build
from tools import migrate_db


# --------------------------------------------------------------------------- #
# 도구
# --------------------------------------------------------------------------- #

class SpyDevice(MockDevice):
    """MockDevice 그대로 움직이고, 무엇을 언제 보냈는지 적어 둔다."""

    def __init__(self, config, clock):
        super().__init__(config, sleep=clock.sleep)
        self.clock = clock
        self.moves = []  # (flex, transition_ms, hold_ms, arrived_at)

    def move_to(self, pose, transition_ms, hold_ms):
        super().move_to(pose, transition_ms, hold_ms)
        arrived_at = self.clock() - timedelta(milliseconds=hold_ms)
        self.moves.append((dict(pose.flex), transition_ms, hold_ms, arrived_at))


class TimedPort(FakePort):
    """RDY 는 전이 시간 뒤에, DON 은 유지 시간 뒤에 오는 가짜 펌웨어.

    FakePort 는 응답을 즉시 내놓아 RDY 시각이 전부 같다. 여기서는 ready_at 이
    어느 RDY 인지 가려야 하므로 시간이 흐르게 한다.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = {}
        self.rdy_at = []

    def write(self, data):
        parts = data.decode("ascii").split()
        if parts[:1] == ["P"]:
            self.timings[parts[1]] = (int(parts[-2]), int(parts[-1]))
        return super().write(data)

    def readline(self):
        if self.pending:
            head, _, seq = self.pending[0].partition(" ")
            trans, hold = self.timings.get(seq, (0, 0))
            if head == "RDY":
                self.clock.advance(trans / 1000)
                self.rdy_at.append(self.clock())
            elif head == "DON":
                self.clock.advance(hold / 1000)
        return super().readline()


def approached_letter(config):
    """경유가 있는 글자 하나. 없으면 이 파일의 실기 설정 테스트는 의미가 없다."""
    if not config.approaches:
        pytest.skip("config/poses.json has no approach; nothing to exercise")
    return sorted(config.approaches)[0]


def plain_letters(config):
    """경유가 없고 경유 대상도 아닌 글자들."""
    targets = set(config.approaches.values())
    return [
        letter
        for letter in sorted(config.poses)
        if letter not in config.approaches and letter not in targets
    ]


def write_config(tmp_path, mutate):
    with REAL_POSES.open(encoding="utf-8") as fh:
        data = json.load(fh)
    data = copy.deepcopy(data)
    mutate(data)
    path = tmp_path / "poses.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config(path)


def make_controller(config, db, device, clock):
    words = load_words(REAL_WORDS, letters=config.poses)
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    controller = SessionController(
        config, words, db, device, gate, lock, clock=clock, rng=random.Random(1)
    )
    controller.start("haru")
    return controller


def present(controller, config, letters, *, boundary=BOUNDARY_DIRECT):
    return controller.present_letters(
        letters,
        track=TRACK_DISCRIMINATION,
        hold_ms=config.timing.hold_default_ms,
        boundary=boundary,
    )


def only_item(db):
    with db.session() as session:
        (item,) = session.query(Item).all()
        return item


# --------------------------------------------------------------------------- #
# 명령 수 — Mock
# --------------------------------------------------------------------------- #

def test_an_approached_letter_moves_twice_through_its_approach(config, db, clock):
    letter = approached_letter(config)
    via = config.approach_of(letter)
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letter)

    timing = config.timing
    assert [(flex, t, h) for flex, t, h, _ in device.moves] == [
        (dict(config.poses[via]), timing.approach_transition_ms, timing.approach_hold_ms),
        (dict(config.poses[letter]), timing.transition_ms, timing.hold_default_ms),
    ]


def test_a_letter_without_approach_moves_once_as_before(config, db, clock):
    letters = "".join(plain_letters(config)[:3])
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letters)

    assert [(flex, t, h) for flex, t, h, _ in device.moves] == [
        (dict(config.poses[letter]), config.timing.transition_ms,
         config.timing.hold_default_ms)
        for letter in letters
    ]


def test_without_any_approach_every_letter_moves_once(tmp_path, db, clock):
    """approach 를 지우면 경유가 사라진다 — 하드웨어가 고쳐졌을 때의 상태다."""

    def strip(data):
        for pose in data["poses"].values():
            pose.pop("approach", None)

    config = write_config(tmp_path, strip)
    letters = "".join(sorted(load_config(REAL_POSES).approaches)) or "B"
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letters)

    assert [flex for flex, *_ in device.moves] == [
        dict(config.poses[letter]) for letter in letters
    ]


def test_the_boundary_neutral_comes_before_the_approach(config, db, clock):
    """또박또박 모드: 중립 -> 경유 -> 목표. 경유가 중립을 대신하지 않는다."""
    letter = approached_letter(config)
    first = plain_letters(config)[0]
    device = SpyDevice(config, clock)
    device.connect()
    order = []
    device.neutral = lambda: order.append("neutral")  # type: ignore[method-assign]
    original = device.move_to

    def spy(pose, transition_ms, hold_ms):
        original(pose, transition_ms, hold_ms)
        order.append(dict(pose.flex))

    device.move_to = spy  # type: ignore[method-assign]
    controller = make_controller(config, db, device, clock)

    present(controller, config, first + letter, boundary=BOUNDARY_EXPLICIT)

    assert order == [
        dict(config.poses[first]),
        "neutral",
        dict(config.poses[config.approach_of(letter)]),
        dict(config.poses[letter]),
    ]


# --------------------------------------------------------------------------- #
# 경유 시간은 설정을 따른다
# --------------------------------------------------------------------------- #

def test_approach_timing_comes_from_the_config(tmp_path, db, clock):
    real = load_config(REAL_POSES)
    letter = approached_letter(real)

    def retime(data):
        data["timing"]["approach_transition_ms"] = 140
        data["timing"]["approach_hold_ms"] = 60

    config = write_config(tmp_path, retime)
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letter)

    (_, t, h, _), _ = device.moves
    assert (t, h) == (140, 60)


def test_slow_replay_does_not_slow_the_approach(config, db, clock):
    """오답 재생은 본 전이만 늦춘다. 경유는 보여 주는 자세가 아니다."""
    letter = approached_letter(config)
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letter)
    controller.submit(plain_letters(config)[0])  # 1회 오답 -> 재생 대기
    device.moves.clear()
    present(controller, config, letter)

    (_, approach_t, approach_h, _), (_, t, _, _) = device.moves
    assert t == config.timing.transition_slow_ms
    assert (approach_t, approach_h) == (
        config.timing.approach_transition_ms,
        config.timing.approach_hold_ms,
    )


def test_the_confusion_demo_also_goes_through_the_approach(config, db, clock):
    """시범도 같은 경로다. 시범에서만 약지가 덜 접히면 시범이 틀린 자세를 보인다."""
    letter = approached_letter(config)
    other = plain_letters(config)[0]
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letter)
    controller.submit(other)
    present(controller, config, letter)
    device.moves.clear()
    controller.submit(other)  # 2회 오답 -> 시범

    flexes = [flex for flex, *_ in device.moves]
    via = dict(config.poses[config.approach_of(letter)])
    assert flexes[:2] == [via, dict(config.poses[letter])]
    assert flexes[2:] == [dict(config.poses[other])]


# --------------------------------------------------------------------------- #
# ready_at 과 기록
# --------------------------------------------------------------------------- #

def test_ready_at_is_the_final_arrival_not_the_approach(config, db, clock):
    letter = approached_letter(config)
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letter)
    controller.submit(letter)

    item = only_item(db)
    (_, _, _, approach_arrival), (_, _, _, final_arrival) = device.moves
    assert item.ready_at == final_arrival
    assert item.ready_at != approach_arrival
    timing = config.timing
    assert item.ready_at - item.presented_at == timedelta(
        milliseconds=timing.approach_transition_ms
        + timing.approach_hold_ms
        + timing.transition_ms
    )


def test_items_record_the_time_spent_on_approaches(config, db, clock):
    letter = approached_letter(config)
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letter)
    controller.submit(letter)

    timing = config.timing
    assert only_item(db).approach_ms == (
        timing.approach_transition_ms + timing.approach_hold_ms
    )


def test_items_without_approach_record_zero(config, db, clock):
    letters = "".join(plain_letters(config)[:3])
    device = SpyDevice(config, clock)
    device.connect()
    controller = make_controller(config, db, device, clock)

    present(controller, config, letters)
    controller.submit(letters)

    # 0 은 "경유 없음" 이다. NULL(컬럼 이전 기록, 모름)과 구별된다.
    assert only_item(db).approach_ms == 0


# --------------------------------------------------------------------------- #
# 안전 게이트
# --------------------------------------------------------------------------- #

@pytest.fixture
def gate_setup(config, db, clock):
    device = MockDevice(config, sleep=clock.sleep)
    device.connect()
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    controller = make_controller(config, db, device, clock)
    return gate, controller.session_id


def test_the_gate_returns_the_approach_pose_with_the_target(config, gate_setup):
    gate, session_id = gate_setup
    letter = approached_letter(config)
    plain = plain_letters(config)[0]

    steps = gate.authorize_steps(
        session_id, plain + letter, TRACK_DISCRIMINATION, depth_unlocked=True
    )

    assert steps == (
        PoseStep(pose=Pose.from_config(config, config.poses[plain])),
        PoseStep(
            pose=Pose.from_config(config, config.poses[letter]),
            approach=Pose.from_config(
                config, config.poses[config.approach_of(letter)]
            ),
        ),
    )
    # 경유는 같은 요청 안의 동작이다. 요청은 한 건이다.
    assert gate.requests_for(session_id) == 1


def test_authorize_sequence_still_returns_only_target_poses(config, gate_setup):
    gate, session_id = gate_setup
    letter = approached_letter(config)

    (pose,) = gate.authorize_sequence(
        session_id, letter, TRACK_DISCRIMINATION, depth_unlocked=True
    )

    assert pose == Pose.from_config(config, config.poses[letter])


# --------------------------------------------------------------------------- #
# 시리얼 — P 명령 수와 RDY
# --------------------------------------------------------------------------- #

def serial_controller(config, db, clock):
    port = TimedPort(clock, hash_value=calibration_hash(config))
    device = build(config, clock, port)
    device.connect()
    return port, make_controller(config, db, device, clock)


def p_commands(port):
    return [c for c in port.drive_commands if c.startswith("P ")]


def test_serial_sends_two_p_commands_for_an_approached_letter(config, db, clock):
    letter = approached_letter(config)
    port, controller = serial_controller(config, db, clock)

    present(controller, config, letter)

    timing = config.timing
    first, second = p_commands(port)
    assert first.split()[-2:] == [
        str(timing.approach_transition_ms), str(timing.approach_hold_ms)
    ]
    assert second.split()[-2:] == [
        str(timing.transition_ms), str(timing.hold_default_ms)
    ]


def test_serial_sends_one_p_command_per_plain_letter(config, db, clock):
    letters = "".join(plain_letters(config)[:3])
    port, controller = serial_controller(config, db, clock)

    present(controller, config, letters)

    assert len(p_commands(port)) == len(letters)


def test_serial_ready_at_is_the_last_rdy(config, db, clock):
    letter = approached_letter(config)
    port, controller = serial_controller(config, db, clock)

    present(controller, config, letter)
    controller.submit(letter)

    approach_rdy, final_rdy = port.rdy_at
    item = only_item(db)
    assert item.ready_at == final_rdy
    assert item.ready_at != approach_rdy


# --------------------------------------------------------------------------- #
# 마이그레이션 — items.approach_ms
# --------------------------------------------------------------------------- #

def old_schema_db(path):
    """approach_ms 가 생기기 전의 station.db 를 흉내낸다. 기록 한 줄이 있다."""
    Database(f"sqlite+pysqlite:///{path.as_posix()}").create_all()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("ALTER TABLE items DROP COLUMN approach_ms")
        conn.execute("INSERT INTO learners (id, name, created_at) VALUES (1, 'a', '2026-09-01')")
        conn.execute(
            "INSERT INTO sessions (id, learner_id, status, source) "
            "VALUES (1, 1, 'done', 'rule')"
        )
        conn.execute(
            "INSERT INTO items (session_id, seq, track, letters, hold_ms, "
            "transition_ms, boundary, attempt) "
            "VALUES (1, 1, 'discrimination', 'VWB', 3000, 900, 'explicit', 1)"
        )
    conn.close()


def test_an_outdated_database_refuses_to_start(tmp_path):
    path = tmp_path / "station.db"
    old_schema_db(path)

    with pytest.raises(SchemaOutdatedError) as exc:
        Database(f"sqlite+pysqlite:///{path.as_posix()}").create_all()
    assert exc.value.missing == {"items": ("approach_ms",)}


def test_migration_adds_the_column_and_leaves_old_rows_unknown(tmp_path):
    path = tmp_path / "station.db"
    old_schema_db(path)

    assert migrate_db.main(["--check", "--db", str(path)]) == 1
    assert migrate_db.main(["--db", str(path)]) == 0
    assert migrate_db.main(["--check", "--db", str(path)]) == 0
    assert migrate_db.main(["--db", str(path)]) == 0  # 다시 돌려도 같다

    db = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    db.create_all()
    with db.session() as session:
        (item,) = session.query(Item).all()
    # 이전 기록은 추정해 채우지 않는다 (R5).
    assert item.approach_ms is None


def test_migration_does_not_create_a_missing_database(tmp_path):
    path = tmp_path / "nowhere.db"
    assert migrate_db.main(["--check", "--db", str(path)]) == 1
    assert not path.exists()
