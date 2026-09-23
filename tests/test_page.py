"""화면 정리 (DECISIONS.md §57).

학습자 화면에는 개발자용 정보가 없다 — 영어, 파이썬 리스트 표기, 검사 이름,
ms 단위. §52 의 "혼동" 금지 테스트와 같은 방식으로 **마크업과 문구 표**를 본다.

교사 화면은 반대로 **코드를 남긴다** (§45). 읽는 문장을 덧붙일 뿐 바꿔치지 않는다.
"""

import re

from tests.test_routes import _block, _learner_markup, client, station  # noqa: F401


def _page(client):
    return client.get("/station.html").text.replace("\r\n", "\n")


def _visible_text(markup):
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
    return re.sub(r"<[^>]+>", " ", markup)


def _strings(block):
    """JS 블록 안의 문자열 리터럴. 주석과 템플릿의 `${...}` 자리는 뺀다."""
    block = re.sub(r"//[^\n]*", "", block)
    found = re.findall(r'"([^"]*)"|`([^`]*)`', block)
    return [re.sub(r"\$\{[^}]*\}", "", a or b) for a, b in found]


def _function(body, name):
    start = body.index(f"function {name}(")
    return body[start:body.index("\n}\n", start)]


# --------------------------------------------------------------------------- #
# 학습자 화면 — 개발자용 정보가 없다
# --------------------------------------------------------------------------- #

def test_learner_markup_shows_no_developer_text(client):
    """보이는 글자에 영어·코드·리스트 표기가 없다. `[정지]` 같은 버튼 이름만 된다."""
    text = _visible_text(_learner_markup(_page(client)))
    assert not re.findall(r"[A-Za-z_{}]", text), re.findall(r"[A-Za-z_{}]+", text)
    for bracket in re.findall(r"\[[^\]]*\]", text):
        assert re.fullmatch(r"\[[가-힣 ]+\]", bracket), bracket


def test_learner_message_tables_are_plain_korean(client):
    """거부 문구·상태 문구·결과 문구 표에 영어가 없다. 코드는 **키**에만 있다."""
    body = _page(client)
    for table in ("REJECTION_TEXT", "RESULT_LABEL", "PHASE_LABEL", "REFERENCE_LABEL"):
        for text in _strings(_block(body, table)):
            assert not re.search(r"[A-Za-z_]", text), (table, text)
    fallback = re.search(r'const FALLBACK_TEXT =\s*"([^"]*)"', body).group(1)
    assert not re.search(r"[A-Za-z]", fallback)


def test_learner_errors_never_show_a_raw_exception(client):
    """학습자 화면은 `err.message` 를 직접 쓰지 않는다. 코드 없는 예외(브라우저의
    "Failed to fetch", 화면 코드의 TypeError)는 영어 원문이다."""
    body = _page(client)
    said = _function(body, "said")
    # `said()` 안의 한 곳만 된다 — 코드가 있는(= 학습자 문구로 옮긴) 거부다.
    assert body.replace(said, "").count("err.message") == 0
    assert "FALLBACK_TEXT" in said and "err.code" in said
    # 서버에 닿지 못한 것도 학습자 문구로 옮긴다.
    call = _function(body, "call")
    assert 'learnerText("NETWORK")' in call


def test_learner_screens_never_print_the_server_detail(client):
    """서버의 `detail` 은 영어 원문이다. 학습자 쪽은 콘솔에만 남긴다."""
    body = _page(client)
    learner_script = body[body.index("<script>"):body.index("// 교사 모드 — teacher.html")]
    for line in learner_script.splitlines():
        if "detail" in line and "textContent" in line:
            raise AssertionError(line)


def test_the_learner_summary_speaks_in_seconds(client):
    summary = _function(_page(client), "renderSummary")
    assert "ms`" not in summary
    assert "seconds(v)" in summary


# --------------------------------------------------------------------------- #
# 안내 화면
# --------------------------------------------------------------------------- #

def test_the_page_opens_on_the_guide(client):
    body = _page(client)
    assert '<main id="homeView">' in body
    assert '<main id="trainView" hidden>' in body
    assert 'const DEFAULT_VIEW = { learner: "home", teacher: "plan" };' in body


def test_the_guide_explains_the_session_without_leaving_the_page(client):
    """PRD 의 핵심 흐름 넷이 안내 화면에 있다 — 읽기 훈련, 중립, 재생·시범, 입력 방법."""
    body = _page(client)
    guide = body[body.index('<main id="homeView">'):body.index('<main id="studyView"')]
    for phrase in ("읽는 법", "중립 자세", "천천히 한 번 더", "타자 · 버튼 · 끌어 놓기"):
        assert phrase in guide, phrase
    assert 'id="homeStudyBtn"' in guide and 'id="homeTrainBtn"' in guide


def test_the_guide_does_not_open_the_teacher_door(client):
    """교사 모드 진입은 사이드바 맨 아래 작은 글자 그대로다. 안내에 두지 않는다."""
    body = _page(client)
    guide = body[body.index('<main id="homeView">'):body.index('<main id="studyView"')]
    assert "교사" not in guide and "modeBtn" not in guide


# --------------------------------------------------------------------------- #
# 세 상태
# --------------------------------------------------------------------------- #

def test_the_three_states_are_marked_without_motion(client):
    """움직이는 중 / 답할 차례 / 결과 가 `data-phase` 로 갈리고, 전환 효과가 없다."""
    body = _page(client)
    phases = _strings(_block(body, "PHASE_LABEL"))
    assert len(phases) >= 3
    for phase in ("moving", "answer", "result"):
        assert f'[data-phase="{phase}"]' in body, phase
    style = re.sub(r"/\*.*?\*/", "", body[body.index("<style>"):body.index("</style>")], flags=re.S)
    for motion in ("transition", "animation", "@keyframes"):
        assert motion not in style, motion


# --------------------------------------------------------------------------- #
# 교사 화면 — 코드는 남는다
# --------------------------------------------------------------------------- #

def test_teacher_findings_keep_the_code_and_the_server_text(client):
    """읽는 문장을 덧붙일 뿐 코드와 원문을 바꿔치지 않는다 (§45)."""
    line = _function(_page(client), "findingLine")
    assert "finding.check} — ${finding.detail}" in line
    assert "CHECK_TEXT[finding.check] || finding.check" in line


def test_only_the_estimate_is_folded_away(client):
    """접는 것은 "읽지 않아도 된다" 는 단언이다. 좁게 둔다 (§57)."""
    body = _page(client)
    assert 'const INTERNAL_CHECKS = new Set(["estimated_seconds"]);' in body
