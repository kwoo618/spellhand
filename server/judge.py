"""응답 판정 (PRD 9.3).

순수 함수 하나다. DB 도 설정도 보지 않는다.

네 가지 결과를 구분하는 이유는 각각 **다른 하위 기술의 실패**이기 때문이다.

- `correct` — 성공
- `length`  — 글자 수를 잘못 셈. 위치 대응이 불확실하므로 혼동을 기록하지 않는다
- `order`   — 글자는 다 읽었고 순서만 틀림 (애너그램). 판별은 성공했고 작업기억에서
              실패한 것이므로 `wrong` 과 같은 표에 넣지 않는다
- `wrong`   — 글자를 잘못 읽음. 이때만 위치별 혼동을 기록한다
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from server.db import RESULT_CORRECT, RESULT_LENGTH, RESULT_ORDER, RESULT_WRONG

__all__ = ["Confusion", "Judgement", "judge", "normalize_answer"]


@dataclass(frozen=True)
class Confusion:
    """한 위치에서 기대한 글자와 실제로 읽은 글자."""

    position: int
    expected: str
    given: str


@dataclass(frozen=True)
class Judgement:
    result: str
    confusions: Tuple[Confusion, ...]

    @property
    def is_correct(self) -> bool:
        return self.result == RESULT_CORRECT


def normalize_answer(text: str) -> str:
    """대소문자와 공백을 없앤다. 학습자가 "v w b" 라고 써도 통과한다."""
    return "".join(text.split()).upper()


def judge(expected: str, answer: str) -> Judgement:
    """기대한 글자열과 학습자의 응답을 비교한다."""
    target = normalize_answer(expected)
    given = normalize_answer(answer)

    if given == target:
        return Judgement(result=RESULT_CORRECT, confusions=())

    if len(given) != len(target):
        # 위치가 밀렸으므로 어느 글자가 어느 글자로 잘못 읽혔는지 알 수 없다.
        return Judgement(result=RESULT_LENGTH, confusions=())

    if sorted(given) == sorted(target):
        # 글자는 전부 맞게 읽었고 순서만 틀렸다. 판별 실패가 아니다.
        return Judgement(result=RESULT_ORDER, confusions=())

    confusions = tuple(
        Confusion(position=i, expected=e, given=g)
        for i, (e, g) in enumerate(zip(target, given))
        if e != g
    )
    return Judgement(result=RESULT_WRONG, confusions=confusions)
