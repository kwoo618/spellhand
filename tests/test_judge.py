"""응답 판정 테스트 (PRD 9.3, 17장 필수 항목).

네 결과가 서로 다른 하위 기술의 실패라는 것이 이 파일의 요지다. 특히
애너그램이 `wrong` 이 아니라 `order` 로 나오는 것을 고정한다.
"""

import pytest

from server.judge import Confusion, Judgement, judge, normalize_answer


# --------------------------------------------------------------------------- #
# 정답
# --------------------------------------------------------------------------- #

def test_exact_match_is_correct():
    result = judge("VWB", "VWB")
    assert result.result == "correct"
    assert result.confusions == ()
    assert result.is_correct


def test_lowercase_answer_is_correct():
    assert judge("WISE", "wise").result == "correct"


def test_spaces_in_the_answer_are_ignored():
    assert judge("VWB", "v w b").result == "correct"
    assert judge("VWB", "  VWB  ").result == "correct"
    assert judge("VWB", "V\tW\nB").result == "correct"


def test_normalize_answer_strips_and_uppercases():
    assert normalize_answer("  v w b ") == "VWB"


# --------------------------------------------------------------------------- #
# 길이 오류
# --------------------------------------------------------------------------- #

def test_short_answer_is_a_length_error():
    result = judge("VWB", "VW")
    assert result.result == "length"


def test_long_answer_is_a_length_error():
    result = judge("VWB", "VWBS")
    assert result.result == "length"


def test_empty_answer_is_a_length_error():
    assert judge("VWB", "").result == "length"


def test_length_errors_record_no_confusions():
    """위치가 밀렸으므로 어느 글자가 어느 글자로 읽혔는지 알 수 없다."""
    assert judge("VWB", "VW").confusions == ()
    assert judge("WISE", "WIS").confusions == ()


# --------------------------------------------------------------------------- #
# 순서 오류 (애너그램)
# --------------------------------------------------------------------------- #

def test_anagram_is_an_order_error_not_a_wrong_answer():
    """WISE -> WIES. 글자는 다 읽었고 순서만 틀렸다.

    이것을 `wrong` 으로 기록하면 판별력 지표가 오염된다. 판별은 성공했고
    작업기억에서 실패한 것이므로 별도 결과다.
    """
    result = judge("WISE", "WIES")
    assert result.result == "order"
    assert result.confusions == ()


def test_full_reversal_is_an_order_error():
    assert judge("VWB", "BWV").result == "order"


def test_order_error_with_repeated_letters():
    assert judge("EIE", "IEE").result == "order"


def test_same_letters_different_order_is_never_wrong():
    for expected, answer in (("SOLID", "SLOID"), ("BOLD", "BLOD")):
        assert judge(expected, answer).result == "order", (expected, answer)


# --------------------------------------------------------------------------- #
# 글자 오류
# --------------------------------------------------------------------------- #

def test_single_substitution_records_one_confusion():
    result = judge("VWB", "VWS")
    assert result.result == "wrong"
    assert result.confusions == (Confusion(position=2, expected="B", given="S"),)


def test_multiple_substitutions_record_each_position():
    result = judge("VWB", "WVS")
    assert result.result == "wrong"
    assert result.confusions == (
        Confusion(position=0, expected="V", given="W"),
        Confusion(position=1, expected="W", given="V"),
        Confusion(position=2, expected="B", given="S"),
    )


def test_matching_positions_are_not_recorded():
    result = judge("WISE", "WOSE")
    assert result.result == "wrong"
    assert [c.position for c in result.confusions] == [1]


def test_judgement_is_immutable():
    result = judge("VWB", "VWS")
    with pytest.raises(Exception):
        result.result = "correct"
    assert isinstance(result, Judgement)
