"""테스트 전반에서 쓰는 픽스처.

여기 있는 것은 **여러 파일이 똑같이 쓰던 것**만이다. 한 파일에서만 쓰는
픽스처는 그 파일에 둔다 — 어디서 온 값인지 찾아다니게 만들지 않는다.

시간은 항상 주입한다. `clock` 을 `MockDevice(sleep=clock.sleep)` 과 게이트·세션에
함께 물리면, 가짜 sleep 이 가짜 시계를 밀어 rate limit·락 만료가 실제 운용과 같은
조건에서 검사된다. 그러면서도 스위트는 초 단위로 걸리지 않는다.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from server.config import load_config
from server.db import Database

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"
REAL_WORDS = REPO_ROOT / "config" / "words.json"


class FakeClock:
    """주입식 시계. 가짜 sleep 이 이 시계를 밀어 준다."""

    def __init__(self, start=datetime(2026, 9, 7, 12, 0, 0)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.advance(seconds)


@pytest.fixture
def config():
    """실제 config/poses.json. 테스트는 이 파일을 절대 수정하지 않는다."""
    return load_config(REAL_POSES)


@pytest.fixture
def db():
    return Database.in_memory()


@pytest.fixture
def clock():
    return FakeClock()
