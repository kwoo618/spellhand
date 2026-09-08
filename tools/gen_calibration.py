"""`config/poses.json` 에서 펌웨어 헤더를 생성한다 (PRD 5.1).

```
python tools/gen_calibration.py            # 생성
python tools/gen_calibration.py --check    # 최신인지 확인 (생성하지 않음)
```

생성물은 `firmware/hand_controller/calibration.h` 이며 **커밋하지 않는다**
(`.gitignore`). 커밋해 두면 `poses.json` 을 고치고 재생성을 잊은 채로 빌드가
통과해 버린다 (DECISIONS.md §1).

**헤더에 자세 테이블은 없다.** 11자 flex 값도 글자 이름도 들어가지 않는다 —
펌웨어는 글자를 모른다 (R2). 들어가는 것은 손가락별 각도 양 끝점, 핀 번호,
타이밍, 그리고 중립·안전 자세뿐이다.

`CAL_HASH` 는 **헤더에 실제로 들어가는 값 전부**의 SHA-256 앞 8자리다. 해시가
같으면 헤더 내용도 같다는 뜻이고, 다르면 펌웨어를 다시 올려야 한다는 뜻이다.
서버는 시리얼 접속 직후 `H` 로 펌웨어 해시를 받아 이 값과 대조한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.calibration import (  # noqa: E402  - sys.path 를 먼저 세워야 한다
    calibration_hash,
    canonical_payload,
)
from server.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    Config,
    load_config,
)

DEFAULT_HEADER_PATH = (
    REPO_ROOT / "firmware" / "hand_controller" / "calibration.h"
)

# 해시 정의는 server/calibration.py 에 있다. 서버가 시리얼 접속 직후 같은 값을
# 계산해 펌웨어와 대조하므로, 정의가 두 벌이면 안 된다.
__all__ = ["canonical_payload", "calibration_hash", "render_header", "main"]


def _int_array(name: str, values: Mapping[str, int], fingers) -> str:
    joined = ", ".join(str(values[finger]) for finger in fingers)
    return f"static const int {name}[FINGER_COUNT] = {{ {joined} }};"


def _permille_array(name: str, pose: Mapping[str, float], fingers) -> str:
    """자세를 ‰ 정수로 내보낸다. 펌웨어는 실수 연산을 하지 않는다."""
    joined = ", ".join(str(round(pose[finger] * 1000)) for finger in fingers)
    return f"static const int {name}[FINGER_COUNT] = {{ {joined} }};"


def render_header(config: Config) -> str:
    fingers = config.fingers
    digest = calibration_hash(config)
    order = ", ".join(fingers)

    pins = _int_array("SERVO_PIN", config.pins, fingers)
    opens = _int_array(
        "SERVO_OPEN",
        {finger: config.calibration[finger].open for finger in fingers},
        fingers,
    )
    closes = _int_array(
        "SERVO_CLOSED",
        {finger: config.calibration[finger].closed for finger in fingers},
        fingers,
    )
    safe = _permille_array("POSE_SAFE_PERMILLE", config.safe, fingers)
    neutral = _permille_array("POSE_NEUTRAL_PERMILLE", config.neutral, fingers)

    return f"""// 생성 파일 — 손으로 고치지 말 것.
//   python tools/gen_calibration.py
//
// 출처: {config.source_path.name} (device_id={config.device_id})
// 배열 순서: {order}
//
// 자세 테이블은 여기 없다. 11자 flex 값도 글자 이름도 들어가지 않는다 —
// 펌웨어는 글자를 모른다 (R2). 중립과 안전은 자세 테이블이 아니라 펌웨어가
// 직접 명령으로 받는 두 자세이므로 예외다 (DECISIONS.md §31).

#ifndef SPELLHAND_CALIBRATION_H
#define SPELLHAND_CALIBRATION_H

#define CAL_HASH "{digest}"

static const int FINGER_COUNT = {len(fingers)};

// 서보 기종의 물리 가동범위. 손가락별 양 끝점 바깥은 어떤 경우에도 쓰지 않는다.
static const int SERVO_RANGE_MIN = {config.servo_range.min};
static const int SERVO_RANGE_MAX = {config.servo_range.max};

{pins}
{opens}
{closes}

// 자세는 ‰(0~1000) 정수다. 서버가 보내는 것과 같은 단위다.
{safe}
{neutral}

static const unsigned long INTERPOLATION_TICK_MS = {config.timing.interpolation_tick_ms};
static const unsigned long IDLE_TIMEOUT_MS = {config.timing.idle_timeout_ms};
static const unsigned long DEFAULT_TRANSITION_MS = {config.timing.transition_ms};

// 받아들일 수 있는 시간 값의 상한. 서버가 실제로 보내는 최대치다.
static const unsigned long MAX_TRANSITION_MS = {config.timing.transition_slow_ms};
static const unsigned long MAX_HOLD_MS = {config.timing.hold_max_ms};

static const int PERMILLE_MAX = 1000;

#endif  // SPELLHAND_CALIBRATION_H
"""


def read_header_hash(path: Path) -> str | None:
    """헤더에 박힌 `CAL_HASH`. 파일이 없거나 못 찾으면 None."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#define CAL_HASH"):
            return line.split('"')[1]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="생성하지 않고 헤더가 최신인지만 확인한다.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_HEADER_PATH)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    expected = calibration_hash(config)

    if args.check:
        found = read_header_hash(args.out)
        if found is None:
            # 헤더는 생성 산출물이고 커밋되지 않는다. 아직 만든 적이 없다면
            # 낡았다고 할 대상이 없으므로 실패가 아니다.
            print(f"skip: {args.out} has not been generated yet")
            return 0
        if found != expected:
            print(
                f"stale: {args.out} has CAL_HASH {found}, "
                f"but {args.config.name} produces {expected}.\n"
                f"       Run: python tools/gen_calibration.py  "
                f"(then re-upload the firmware)"
            )
            return 1
        print(f"ok: CAL_HASH {found}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_header(config), encoding="utf-8")
    print(f"wrote {args.out} (CAL_HASH {expected})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
