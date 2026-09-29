"""제3자 판별 파일럿 — 11자가 사람 눈에 구별되는지 계측한다.

```
.venv\\Scripts\\python.exe tools\\pilot.py
```

**임시 도구다.** 제품 코드가 아니고 파일럿이 끝나면 지워도 된다.

- `poses.json` 의 글자를 각 `REPEATS` 회씩, 무작위 순서로 표현한다.
- 표현 하나는 중립 → 글자(유지 `hold_default_ms`) → 중립.
- 콘솔에는 **번호만** 찍는다. 진행자가 화면을 보므로 글자 이름이 새면 안 된다.
- 엔터를 눌러야 다음으로 넘어간다. 자동 진행하지 않는다.
- Ctrl+C 로 끊어도 손은 안전 자세로 간다.
- 끝나면(끊겨도) 정답 순서를 `tools/pilot_key_<timestamp>.txt` 로 남긴다.

세션·DB·안전 게이트·통계를 거치지 않는다. `station.db` 에 아무것도 남기지
않는다. 경유(approach)는 훈련과 **같은 함수**(`server.session.move_step`)로
적용한다 — 파일럿에서 본 E 와 훈련에서 볼 E 가 같은 동작이어야 한다.
"""

from __future__ import annotations

import logging
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.config import Config, load_config  # noqa: E402  - sys.path 를 먼저 세워야 한다
from server.device import (  # noqa: E402
    DeviceAdapter,
    Pose,
    create_device,
    resolve_device_mode,
)
from server.env import load_env  # noqa: E402
from server.safety import PoseStep  # noqa: E402
from server.session import move_step  # noqa: E402

REPEATS = 3
KEY_DIR = Path(__file__).resolve().parent


def _step_for(config: Config, letter: str) -> PoseStep:
    approach = config.approach_of(letter)
    return PoseStep(
        pose=Pose.from_config(config, config.poses[letter]),
        approach=None
        if approach is None
        else Pose.from_config(config, config.poses[approach]),
    )


def _write_key(order: List[str], shown: int, started: datetime) -> Path:
    path = KEY_DIR / f"pilot_key_{started:%Y%m%d_%H%M%S}.txt"
    lines = [
        f"# 파일럿 정답 순서 — 시작 {started:%Y-%m-%d %H:%M:%S}",
        f"# 표현 {shown}/{len(order)}"
        + ("" if shown == len(order) else " (중단됨)"),
    ]
    lines += [f"{n}\t{letter}" for n, letter in enumerate(order, start=1)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    # 글자 이름이 로그로 새지 않도록 경고 이상만 보인다.
    logging.basicConfig(level=logging.WARNING)
    load_env()

    config = load_config()
    order = [letter for letter in config.poses for _ in range(REPEATS)]
    random.SystemRandom().shuffle(order)
    steps = {letter: _step_for(config, letter) for letter in config.poses}
    hold_ms = config.timing.hold_default_ms
    transition_ms = config.timing.transition_ms

    device: DeviceAdapter = create_device(config)
    print(f"장치: {resolve_device_mode()} · 표현 {len(order)}회 · 유지 {hold_ms}ms")
    device.connect()

    started = datetime.now()
    shown = 0
    completed = False
    interrupted = False
    try:
        for n, letter in enumerate(order, start=1):
            input(f"\n[{n}/{len(order)}] 엔터를 누르면 표현합니다 ")
            print(f"  {n}번 표현 중...")
            device.neutral()
            move_step(device, config, steps[letter], transition_ms, hold_ms)
            device.neutral()
            shown = n
        completed = True
        print("\n끝.")
    except KeyboardInterrupt:
        interrupted = True
        print("\n중단 — 안전 자세로 돌아갑니다.")
    finally:
        try:
            # 끝까지 갔으면 천천히 안전 자세로, 도중에 끊겼으면(Ctrl+C 든 장치
            # 오류든) 진행 중인 동작을 버리고 즉시 안전 자세로.
            if completed:
                device.safe()
            else:
                device.stop()
        finally:
            device.disconnect()
            key = _write_key(order, shown, started)
            print(f"정답 순서: {key}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
