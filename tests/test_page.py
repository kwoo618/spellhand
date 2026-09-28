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


def _style(body):
    """`<style>` 안쪽. 주석은 뺀다."""
    return re.sub(r"/\*.*?\*/", "", body[body.index("<style>"):body.index("</style>")], flags=re.S)


def _rules(style):
    """`(선택자, 선언)` 쌍. 중첩은 `@font-face`·`@media` 한 겹뿐이라 안쪽 규칙만 본다."""
    return re.findall(r"([^{}]+)\{([^{}]*)\}", style)


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
    for table in ("REJECTION_TEXT", "RESULT_LABEL", "PHASE_LABEL", "PHASE_BADGE",
                  "REFERENCE_LABEL"):
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
    style = _style(body)
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


# --------------------------------------------------------------------------- #
# 손이 움직이는 동안 — 본문이 어두워진다 (§57 → §58)
# --------------------------------------------------------------------------- #

def test_the_screen_goes_dark_while_the_hand_moves(client):
    """공부·훈련 패널이 `moving` 이면 본문 전체가 밤색이 되고 상태 하나만 남는다.
    탭 줄의 비활성 항목도 회색으로 내린다."""
    body = _page(client)
    style = _style(body)
    assert "--night: #1B2331;" in style
    rules = _rules(style)

    dark = [sel for sel, decl in rules
            if '[data-phase="moving"]' in sel and "background: var(--night)" in decl]
    assert any(".content" in sel for sel in dark), "본문 영역이 어두워지지 않는다"

    hidden = [sel for sel, decl in rules
              if '[data-phase="moving"]' in sel and "display: none" in decl]
    assert any(":not(.status)" in sel for sel in hidden), "움직이는 동안 입력이 남는다"

    grey = [sel for sel, decl in rules
            if '[data-phase="moving"]' in sel and ".navItem" in sel and "var(--text-3)" in decl]
    assert grey, "탭 줄이 그대로 밝다"

    # 두 패널이 실제로 그 상태에 들어간다.
    assert 'setPhase("moving")' in _function(body, "setMoving")
    assert 'dataset.phase = busy ? "moving"' in _function(body, "setStudyBusy")


def test_the_dark_screen_never_shows_the_training_letter(client):
    """훈련의 큰 자리에는 몇 번째인지만 적는다. 글자를 적으면 정답이다."""
    body = _page(client)
    learner_script = body[body.index("<script>"):body.index("// 교사 모드 — teacher.html")]
    writes = [line.strip() for line in learner_script.splitlines()
              if 'el("glyph")' in line]
    assert writes
    for line in writes:
        assert line in ('el("glyph").textContent = "";',
                        'el("glyph").textContent = `${event.letter_pos + 1}번째`;'), line


def test_the_learner_design_uses_edges_not_shadows(client):
    """입체감은 아래 테두리로 낸다. 그림자·그라데이션·흐림이 없다 (§57, §58)."""
    style = _style(_page(client))
    for effect in ("box-shadow", "text-shadow", "gradient", "blur", "filter:"):
        assert effect not in style, effect
    assert "border-bottom: 6px solid var(--letter-edge)" in style


# --------------------------------------------------------------------------- #
# 글꼴과 글자 수 — 밖에서 받지 않고, 박지 않는다
# --------------------------------------------------------------------------- #

def test_the_fonts_are_served_by_the_station(client):
    """발표장에 인터넷이 없어도 같은 화면이다. 외부 주소를 부르지 않는다 (§58)."""
    body = _page(client)
    assert "http://" not in _style(body) and "https://" not in _style(body)
    assert "<link" not in body
    fonts = re.findall(r'url\("(fonts/[^"]+\.woff2)"\)', body)
    assert len(fonts) >= 7
    for path in fonts:
        response = client.get(f"/{path}")
        assert response.status_code == 200, path
        assert response.content[:4] == b"wOF2", path


def test_the_letter_count_comes_from_the_server(client, config):
    """머리 줄의 "N자" 와 글자 칸 끝의 안내는 서버 목록의 길이다 (§56, §57)."""
    body = _page(client)
    assert f"{len(config.poses)}자" not in body
    assert "${letters.length}자" in _function(body, "renderLetterSet")
    assert "${letters.length}자입니다" in _function(body, "renderStudyPads")


# --------------------------------------------------------------------------- #
# 상태 줄은 단계를 따른다 (§58.1)
# --------------------------------------------------------------------------- #

def _learner_script(body):
    return body[body.index("<script>"):body.index("// 교사 모드 — teacher.html")]


def test_every_phase_change_rewrites_the_status_line(client):
    """단계를 바꾸는 곳은 상태 줄도 바꾼다. 그러지 않으면 앞 단계 문구가 남는다 —
    결과 화면에 제출 때의 "틀렸다면 …" 이 남아 있었다."""
    script = _learner_script(_page(client))
    names = re.findall(r"^(?:async )?function (\w+)\(", script, re.M)
    changing = [
        name for name in names
        if name not in ("setPhase", "setMoving")
        and re.search(r'setPhase\("|setMoving\(true\)', _function(script, name))
    ]
    assert {"setAnswering", "backToResult", "submit", "showStopped", "showStalled",
            "present", "presentPlanned", "replayDemo"} <= set(changing), changing
    for name in changing:
        assert 'el("status").textContent' in _function(script, name), name


def test_the_result_phase_has_its_own_status_line(client):
    """결과는 다음에 누를 것을 말한다. 제출 때 문구를 들고 있지 않는다."""
    body = _page(client)
    result = re.sub(r"//[^\n]*", "", _function(body, "renderResult"))
    assert "backToResult();" in result
    assert "[다음 문항]" in result and "[요약 보기]" in result
    assert "틀렸다면" not in result
    back = _function(body, "backToResult")
    assert 'setPhase("result");' in back
    assert 'el("status").textContent = state.resultLine;' in back


def test_a_replayed_demo_returns_to_the_result(client):
    """[시범 다시 보기] 는 손을 움직이므로 화면이 어두워진다. 끝나면(시범 이벤트),
    멈추면, 거부되면 결과로 돌아온다 — 결과가 어두운 화면 뒤에 숨은 채 남지 않는다."""
    body = _page(client)
    handle = _function(body, "handleEvent")
    demo = handle[handle.index('case "demo":'):handle.index('case "error":')]
    assert "if (state.resultLine !== null) { backToResult(); break; }" in demo
    stopped = handle[handle.index('if (event.code === "STOPPED")'):]
    assert stopped.index("backToResult()") < stopped.index("showStopped()")
    replay = _function(body, "replayDemo")
    catch = replay[replay.index("catch"):]
    assert catch.index("backToResult();") < catch.index("setError(")
    # 결과가 없는 동안에는 되돌아갈 곳이 없다 — 새 문항·새 제출에서 비운다.
    assert "state.resultLine = null;" in _function(body, "nextItem")
    assert "state.resultLine = null;" in _function(body, "submit")


# --------------------------------------------------------------------------- #
# 마감 점검에서 고친 것 (§58.2)
# --------------------------------------------------------------------------- #

def test_the_outlined_title_keeps_the_hangul_counters_open(client):
    """외곽선이 두꺼우면 검은고딕의 속공간(ㅇ·ㅁ)이 막힌다. 7px 에서 막혔다."""
    style = _style(_page(client))
    width = int(re.search(r"-webkit-text-stroke: (\d+)px", style).group(1))
    assert width <= 4, width
    assert "paint-order: stroke fill" in style


def test_switching_screens_does_not_shift_the_bars(client):
    """스크롤바가 생기고 없어질 때 머리 줄·탭 줄이 좌우로 밀리지 않는다."""
    assert "scrollbar-gutter: stable" in _style(_page(client))


def test_the_letter_screen_cards_share_one_grid(client):
    """글자가 아닌 자세 카드도 글자 카드와 같은 6열 격자에 두 칸씩 앉는다.
    끝 안내 칸은 기본 여백 없이 이웃 카드와 높이가 같다."""
    rules = _rules(_style(_page(client)))
    assert any(".poses .lcard" in sel and "grid-column: span 2" in decl for sel, decl in rules)
    assert any(".lcard.end" in sel and "margin: 0" in decl for sel, decl in rules)
