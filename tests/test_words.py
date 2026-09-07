"""config/words.json 과 server.config.load_words() 검증 테스트.

poses.json 테스트와 성격이 다른 점에 주의: 문자 집합 필터는 실패가 아니라
경고다. 여기 테스트의 상당수는 "실패하지 않는다"를 확인한다.
"""

import copy
import json
import logging
from pathlib import Path

import pytest

from server.config import ConfigError, load_config, load_words

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"
REAL_WORDS = REPO_ROOT / "config" / "words.json"

# poses.json 이 제공하는 11자. 단어는 이 안에서만 만들어진다.
ALL_LETTERS = ("B", "D", "E", "F", "I", "L", "O", "S", "V", "W", "Y")


@pytest.fixture
def raw():
    """실제 words.json 의 내용. 테스트마다 새 deepcopy 를 준다."""
    with REAL_WORDS.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def written(tmp_path):
    """dict 를 임시 words.json 으로 저장하고 경로를 돌려주는 헬퍼."""

    def _write(data, name="words.json"):
        path = tmp_path / name
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    return _write


@pytest.fixture
def broken(raw, written):
    """raw 를 변형해 임시 파일로 저장한다."""

    def _write(mutate):
        data = copy.deepcopy(raw)
        mutate(data)
        return written(data)

    return _write


@pytest.fixture
def words():
    return load_words(REAL_WORDS, letters=ALL_LETTERS)


# --------------------------------------------------------------------------- #
# 정상 경로
# --------------------------------------------------------------------------- #

def test_real_word_list_loads_completely(words):
    """11자 전부가 살아 있으면 32개 단어가 하나도 빠지지 않는다."""
    assert len(words.words) == 32
    assert words.excluded == ()
    assert words.source_path == REAL_WORDS


def test_every_word_uses_only_available_letters(words):
    for word in words.words:
        assert set(word.letters) <= set(ALL_LETTERS), word.letters


def test_words_are_loadable_against_the_real_pose_config():
    """실제 poses.json 의 글자 집합으로도 전부 통과한다 (두 설정의 정합성)."""
    config = load_config(REAL_POSES)
    loaded = load_words(REAL_WORDS, letters=config.poses)
    assert len(loaded.words) == 32
    assert loaded.excluded == ()


def test_adjacent_repeat_is_flagged(words):
    """BLESS 의 SS 처럼 인접 중복이 있으면 has_repeat 가 True."""
    bless = next(w for w in words.words if w.letters == "BLESS")
    assert bless.has_repeat is True
    assert bless.length == 5


def test_separated_repeat_is_not_flagged(words):
    """WILDLIFE 의 L 은 떨어져 있으므로 has_repeat 가 아니다.

    연결 모드에서 문제가 되는 것은 손이 움직이지 않아 경계가 사라지는
    '인접' 중복뿐이다. 떨어진 중복은 사이에 다른 자세가 들어가므로 무관하다.
    """
    wildlife = next(w for w in words.words if w.letters == "WILDLIFE")
    assert wildlife.has_repeat is False
    assert wildlife.length == 8


def test_only_two_words_have_adjacent_repeats(words):
    assert {w.letters for w in words.words if w.has_repeat} == {"BLESS", "FOSSIL"}


def test_hook_word_is_wise_by_default(words):
    assert words.hook_word == "WISE"


def test_direct_mode_excludes_repeat_words(words):
    direct = words.direct_mode_words()
    assert len(direct) == 30
    assert all(not w.has_repeat for w in direct)


def test_of_length_filters_by_length(words):
    for word in words.of_length(3):
        assert word.length == 3
    assert {w.letters for w in words.of_length(8)} == {"WILDLIFE"}


# --------------------------------------------------------------------------- #
# 필터 동작 — 실패가 아니라 경고 후 계속
# --------------------------------------------------------------------------- #

def test_unsupported_word_is_dropped_with_a_warning(broken, caplog):
    """CAT 은 C·A·T 가 없어 빠지지만, 로딩 자체는 성공해야 한다."""

    def mutate(data):
        data["words"].append("CAT")

    path = broken(mutate)
    with caplog.at_level(logging.WARNING, logger="server.config"):
        loaded = load_words(path, letters=ALL_LETTERS)

    assert len(loaded.words) == 32
    assert "CAT" not in {w.letters for w in loaded.words}
    assert [e.word for e in loaded.excluded] == ["CAT"]
    assert loaded.excluded[0].unsupported == ("A", "C", "T")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "제외된 단어에 대한 경고가 있어야 한다"
    assert "CAT" in warnings[0].getMessage()


def test_dropping_a_letter_shrinks_the_pool_but_still_starts(caplog):
    """E 가 탈락해도 서버는 뜬다 — 3주차 판별 테스트에서 예상되는 시나리오.

    이때 훅 단어 WISE 가 표현 불가해져 폴백 WILD 로 넘어간다. WILD 는
    $.words 에 없지만 그것은 실패가 아니다 (훅은 시연용 단독 표현).
    """
    letters = tuple(letter for letter in ALL_LETTERS if letter != "E")

    with caplog.at_level(logging.INFO, logger="server.config"):
        loaded = load_words(REAL_WORDS, letters=letters)

    kept = {w.letters for w in loaded.words}
    assert kept, "E 를 빼도 남는 단어가 있어야 한다"
    assert all("E" not in word for word in kept)
    assert "WISE" not in kept and "YES" not in kept
    assert "SOLID" in kept and "OWL" in kept
    assert {e.word for e in loaded.excluded} == {
        w for w in _all_raw_words() if "E" in w
    }

    # 훅은 폴백으로 자동 대체된다.
    assert loaded.hook_word == "WILD"
    assert loaded.hook_word not in kept

    messages = [r.getMessage() for r in caplog.records]
    assert any("WISE" in m for m in messages), "WISE 불가 경고가 있어야 한다"
    assert any(
        "WILD" in m and "not in the question pool" in m for m in messages
    ), "폴백이 출제 풀에 없다는 정보 로그가 있어야 한다"


def test_single_letter_set_leaves_no_words_and_fails():
    """B 하나만 남으면 통글자 트랙이 성립하지 않으므로 기동 실패."""
    with pytest.raises(ConfigError) as exc:
        load_words(REAL_WORDS, letters=("B",))
    assert "$.words" in str(exc.value)
    assert "0 of 32" in str(exc.value)


def test_lowercase_words_are_normalized(written):
    loaded = load_words(
        written({"words": ["wise", "Old"], "hook_word": "wise"}),
        letters=ALL_LETTERS,
    )
    assert {w.letters for w in loaded.words} == {"WISE", "OLD"}
    assert loaded.hook_word == "WISE"


def test_hook_word_need_not_be_in_the_pool(written, caplog):
    """훅이 $.words 에 없어도 실패가 아니다. 정보 로그만 남는다."""
    with caplog.at_level(logging.INFO, logger="server.config"):
        loaded = load_words(
            written({"words": ["OLD"], "hook_word": "WISE"}), letters=ALL_LETTERS
        )
    assert loaded.hook_word == "WISE"
    assert any("not in the question pool" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# 실패 경로
# --------------------------------------------------------------------------- #

def test_empty_word_array_is_rejected(broken):
    def mutate(data):
        data["words"] = []

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "$.words" in str(exc.value)


def test_missing_word_array_is_rejected(broken):
    def mutate(data):
        del data["words"]

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "words" in str(exc.value)


def test_word_with_a_digit_is_rejected(broken):
    def mutate(data):
        data["words"].append("W1SE")

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "W1SE" in str(exc.value)


def test_word_with_a_space_is_rejected(broken):
    def mutate(data):
        data["words"].append("OLD OWL")

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "OLD OWL" in str(exc.value)


def test_word_with_punctuation_is_rejected(broken):
    def mutate(data):
        data["words"].append("WOLF-DOG")

    with pytest.raises(ConfigError):
        load_words(broken(mutate), letters=ALL_LETTERS)


def test_duplicate_word_is_rejected(broken):
    def mutate(data):
        data["words"].append("WISE")

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "WISE" in str(exc.value)


def test_case_insensitive_duplicate_is_rejected(broken):
    def mutate(data):
        data["words"].append("wise")

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "WISE" in str(exc.value)


def test_missing_hook_word_is_rejected(broken):
    def mutate(data):
        del data["hook_word"]

    with pytest.raises(ConfigError) as exc:
        load_words(broken(mutate), letters=ALL_LETTERS)
    assert "hook_word" in str(exc.value)


def test_hook_word_with_every_fallback_unavailable_is_rejected(caplog):
    """WISE·WILD·BODY 가 전부 불가하면 시연이 성립하지 않으므로 실패.

    O·L·D 만 남기면 OLD 는 살아남아 출제 풀은 비지 않는다. 즉 이 실패는
    풀이 비어서가 아니라 훅 단어 때문임이 분명하다.
    """
    with pytest.raises(ConfigError) as exc:
        load_words(REAL_WORDS, letters=("O", "L", "D"))
    assert "$.hook_word" in str(exc.value)
    assert "WISE" in str(exc.value)


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_words(tmp_path / "nope.json", letters=ALL_LETTERS)


# --------------------------------------------------------------------------- #

def _all_raw_words():
    with REAL_WORDS.open(encoding="utf-8") as fh:
        return [w.upper() for w in json.load(fh)["words"]]
