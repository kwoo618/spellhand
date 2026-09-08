"""펌웨어 헤더 생성기 테스트 (PRD 5.1).

두 가지를 지킨다.
1. **헤더에 자세 테이블이 없다** — 펌웨어는 글자를 모른다 (R2).
2. **해시가 헤더 내용을 정확히 반영한다** — 핀이든 각도든 바뀌면 해시가 바뀌고,
   서버가 접속 직후 대조해 재업로드를 잊은 상태를 잡아낸다.
"""

import copy
import json
import re
import subprocess
import sys

import pytest

from server.config import load_config
from tests.conftest import REAL_POSES, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))
from tools.gen_calibration import (  # noqa: E402
    calibration_hash,
    canonical_payload,
    main,
    read_header_hash,
    render_header,
)

LETTERS = ("B", "D", "E", "F", "I", "L", "O", "S", "V", "W", "Y")

# 헤더가 내보내도 되는 배열 전부. 여기 없는 배열이 생기면 테스트가 깨진다.
ALLOWED_ARRAYS = {
    "SERVO_PIN",
    "SERVO_OPEN",
    "SERVO_CLOSED",
    "POSE_SAFE_PERMILLE",
    "POSE_NEUTRAL_PERMILLE",
}


@pytest.fixture
def header(config):
    return render_header(config)


@pytest.fixture
def written(tmp_path):
    def _write(data):
        path = tmp_path / "poses.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    return _write


@pytest.fixture
def raw():
    with REAL_POSES.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 헤더 내용
# --------------------------------------------------------------------------- #

def test_pins_are_exported(header, config):
    """핀 번호는 펌웨어 소스가 아니라 생성 헤더로 간다 (R1)."""
    assert "static const int SERVO_PIN[FINGER_COUNT] = { 2, 3, 6, 8, 10 };" in header
    assert list(config.pins.values()) == [2, 3, 6, 8, 10]


def test_angles_are_exported_in_finger_order(header, config):
    opens = ", ".join(str(config.calibration[f].open) for f in config.fingers)
    closes = ", ".join(str(config.calibration[f].closed) for f in config.fingers)
    assert f"SERVO_OPEN[FINGER_COUNT] = {{ {opens} }}" in header
    assert f"SERVO_CLOSED[FINGER_COUNT] = {{ {closes} }}" in header


def test_the_header_has_no_pose_table(header):
    """완료 확인 3: 11자 자세 테이블이 헤더에 없다 (R2).

    허용되는 배열은 다섯 개뿐이다. 새 배열이 생기면 여기서 걸린다 — 그때
    "그것이 자세 테이블인가"를 사람이 판단해야 한다.
    """
    arrays = set(re.findall(r"static const int (\w+)\[FINGER_COUNT\]", header))
    assert arrays == ALLOWED_ARRAYS


def test_the_header_names_no_letters(header):
    """글자 이름이 식별자로 등장하지 않는다.

    `POSE_SAFE_PERMILLE` 처럼 우연히 글자로 시작하는 이름과 섞이지 않도록
    식별자 전체를 본다.
    """
    identifiers = set(re.findall(r"\b[A-Z][A-Z0-9_]*\b", header))
    for letter in LETTERS:
        assert f"POSE_{letter}_PERMILLE" not in identifiers
        assert letter not in identifiers


def test_neutral_and_safe_are_both_present_and_different(header, config):
    """중립과 안전은 별개 자세다 (R3). 둘 다 있어야 하고 값이 달라야 한다."""
    safe = re.search(r"POSE_SAFE_PERMILLE\[FINGER_COUNT\] = \{ ([^}]+) \}", header)
    neutral = re.search(
        r"POSE_NEUTRAL_PERMILLE\[FINGER_COUNT\] = \{ ([^}]+) \}", header
    )
    assert safe and neutral
    assert safe.group(1) != neutral.group(1)
    assert safe.group(1).strip() == "0, 0, 0, 0, 0"


def test_the_header_carries_a_hash(header, config):
    assert f'#define CAL_HASH "{calibration_hash(config)}"' in header


def test_timing_limits_come_from_the_config(header, config):
    assert f"INTERPOLATION_TICK_MS = {config.timing.interpolation_tick_ms}" in header
    assert f"IDLE_TIMEOUT_MS = {config.timing.idle_timeout_ms}" in header
    assert f"MAX_TRANSITION_MS = {config.timing.transition_slow_ms}" in header
    assert f"MAX_HOLD_MS = {config.timing.hold_max_ms}" in header


# --------------------------------------------------------------------------- #
# 해시
# --------------------------------------------------------------------------- #

def test_the_hash_is_deterministic(config):
    assert calibration_hash(config) == calibration_hash(config)
    assert len(calibration_hash(config)) == 8


def test_changing_a_pin_changes_the_hash(config, raw, written):
    """핀이 바뀌면 펌웨어를 다시 올려야 한다 (DECISIONS.md §30)."""
    before = calibration_hash(config)
    data = copy.deepcopy(raw)
    data["pins"]["fingers"]["thumb"] = 4
    after = calibration_hash(load_config(written(data)))
    assert after != before


def test_changing_an_angle_changes_the_hash(config, raw, written):
    before = calibration_hash(config)
    data = copy.deepcopy(raw)
    data["calibration"]["fingers"]["index"]["closed"] = 119
    after = calibration_hash(load_config(written(data)))
    assert after != before


def test_changing_the_neutral_pose_changes_the_hash(config, raw, written):
    """중립도 헤더에 들어가므로 해시 대상이다."""
    before = calibration_hash(config)
    data = copy.deepcopy(raw)
    data["neutral"]["middle"] = 0.60
    after = calibration_hash(load_config(written(data)))
    assert after != before


def test_changing_a_letter_pose_does_not_change_the_hash(config, raw, written):
    """자세 테이블은 헤더에 없으므로 해시에도 없다.

    B 를 고쳤다고 펌웨어를 다시 올릴 이유는 없다 — 펌웨어는 B 를 모른다.
    """
    before = calibration_hash(config)
    data = copy.deepcopy(raw)
    data["poses"]["B"]["thumb"] = 0.60
    after = calibration_hash(load_config(written(data)))
    assert after == before


def test_the_hash_covers_exactly_what_the_header_emits(config):
    """해시 대상과 헤더 내용이 어긋나면 '해시 같음 = 헤더 같음'이 깨진다."""
    payload = canonical_payload(config)
    assert set(payload) == {
        "device_id",
        "fingers",
        "servo_range_deg",
        "calibration",
        "pins",
        "neutral",
        "safe",
        "timing",
    }
    assert set(payload["timing"]) == {
        "interpolation_tick_ms",
        "idle_timeout_ms",
        "transition_ms",
        "max_transition_ms",
        "max_hold_ms",
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_generate_then_check_passes(tmp_path):
    out = tmp_path / "calibration.h"
    assert main(["--config", str(REAL_POSES), "--out", str(out)]) == 0
    assert out.is_file()
    assert main(["--check", "--config", str(REAL_POSES), "--out", str(out)]) == 0


def test_check_detects_a_stale_header(tmp_path, raw, written, capsys):
    """poses.json 을 고치고 재생성을 잊은 상태를 잡아낸다 — 이것이 이 검사의 목적이다."""
    out = tmp_path / "calibration.h"
    main(["--config", str(REAL_POSES), "--out", str(out)])

    data = copy.deepcopy(raw)
    data["pins"]["fingers"]["pinky"] = 11
    changed = written(data)

    assert main(["--check", "--config", str(changed), "--out", str(out)]) == 1
    assert "stale" in capsys.readouterr().out


def test_check_skips_when_the_header_was_never_generated(tmp_path, capsys):
    """헤더는 커밋되지 않는 생성 산출물이다. 없으면 낡았다고 할 대상도 없다."""
    out = tmp_path / "never" / "calibration.h"
    assert main(["--check", "--config", str(REAL_POSES), "--out", str(out)]) == 0
    assert "skip" in capsys.readouterr().out


def test_read_header_hash_round_trips(tmp_path, config):
    out = tmp_path / "calibration.h"
    main(["--config", str(REAL_POSES), "--out", str(out)])
    assert read_header_hash(out) == calibration_hash(config)


def test_the_generator_runs_as_a_script(tmp_path):
    """verify.ps1 이 부르는 형태 그대로 돌아가는지 확인한다."""
    out = tmp_path / "calibration.h"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "gen_calibration.py"),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert out.is_file()
