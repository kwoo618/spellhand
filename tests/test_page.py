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

def _motion(decl):
    """선언에 실제로 움직임을 거는 `animation` · `transition` 이 있는가. `none` 은 끄는 것이다."""
    return [value.strip() for value in
            re.findall(r"(?<![\w-])(?:animation|transition)(?:-[a-z]+)?\s*:\s*([^;]+)", decl)
            if not value.strip().startswith("none")]


def _selectors(sel):
    """쉼표로 묶인 선택자를 나눈다. `:is(...)` 안의 쉼표는 나누지 않는다."""
    parts, depth, current = [], 0, ""
    for ch in sel:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    return parts + [current.strip()]


def test_the_three_states_are_marked_without_motion(client):
    """움직이는 중 / 답할 차례 / 결과 가 `data-phase` 로 갈리고, **그 상태들에는** 전환
    효과가 없다.

    §57 에서는 파일 전체에 transition · animation · @keyframes 가 없음을 봤다. §59 에서
    범위를 좁혔다 — 근거인 PRD 14.1 "시선은 로봇손에" 는 손이 움직이는 동안의 말이다.
    그래서 이제는 (1) 움직임을 거는 규칙은 전부 홈 화면 안에 있고 (2) 손이 움직이는
    패널이 있는 동안과 훈련·글자 화면에서는 움직임을 끄는 규칙이 있으며 (3) 줄인 동작
    설정에서 전부 꺼지고 (4) 짧고 반복하지 않음을 본다."""
    body = _page(client)
    phases = _strings(_block(body, "PHASE_LABEL"))
    assert len(phases) >= 3
    for phase in ("moving", "answer", "result"):
        assert f'[data-phase="{phase}"]' in body, phase

    style = _style(body)
    rules = _rules(style)
    moving = [(sel, decl) for sel, decl in rules if _motion(decl)]
    assert moving, "움직임이 하나도 없다 — 홈 등장 효과가 빠졌다"
    for sel, decl in moving:
        # (1) 홈에만. 상태(`data-phase`)·훈련·글자 화면을 가리키는 선택자에는 없다.
        for part in _selectors(sel):
            assert part.startswith("#homeView"), sel
        assert "data-phase" not in sel and "trainView" not in sel and "studyView" not in sel, sel
        # (4) 0.3초 이하, 무한 반복 없음.
        for value in _motion(decl):
            assert "infinite" not in value, value
            for number in re.findall(r"(\d*\.?\d+)s\b", value):
                assert float(number) <= 0.3, value
    for sel, decl in rules:
        if "animation-delay" in decl:
            assert all(p.startswith("#homeView") for p in _selectors(sel)), sel

    # (2) 손이 움직이는 동안은 어느 화면에서든, 훈련·글자 화면은 늘 끈다.
    guards = [sel for sel, decl in rules
              if "animation: none" in decl and "transition: none" in decl]
    guard = next((sel for sel in guards if '[data-phase="moving"]' in sel), None)
    assert guard, "손이 움직이는 동안 움직임을 끄는 규칙이 없다"
    assert ':has([data-phase="moving"]:not([hidden]))' in guard, "보이는 화면만 막으면 홈 탭이 샌다"
    for view in ("#trainView", "#studyView"):
        assert f"{view} *" in guard, view

    # (3) 줄인 동작 설정.
    reduced = style[style.index("@media (prefers-reduced-motion: reduce)"):]
    reduced = reduced[:reduced.index("}\n")]
    assert "animation: none" in reduced and "transition: none" in reduced


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
    """스크롤바가 생기고 없어질 때 머리 줄·탭 줄이 좌우로 밀리지 않는다 — 스크롤바를 늘
    둔다. 자리만 비워 두는 `scrollbar-gutter` 는 스크롤 없는 화면에서 그 빈 자리가 밝은
    바탕으로 칠해져 어두운 화면 옆에 흰 띠가 섰다 (§58.4). 어두운 동안에는 스크롤바도
    어둡다."""
    style = _style(_page(client))
    assert "html { overflow-y: scroll; }" in style
    assert "scrollbar-gutter" not in style
    rules = _rules(style)
    assert any(sel.strip().startswith("html:has(") and '[data-phase="moving"]' in sel
               and "color-scheme: dark" in decl for sel, decl in rules)


def test_the_letter_screen_cards_share_one_grid(client):
    """글자가 아닌 자세 카드도 글자 카드와 같은 6열 격자에 두 칸씩 앉는다.
    끝 안내 칸은 기본 여백 없이 이웃 카드와 높이가 같다."""
    rules = _rules(_style(_page(client)))
    assert any(".poses .lcard" in sel and "grid-column: span 2" in decl for sel, decl in rules)
    assert any(".lcard.end" in sel and "margin: 0" in decl for sel, decl in rules)


# --------------------------------------------------------------------------- #
# 판단 항목 반영 (§58.3)
# --------------------------------------------------------------------------- #

def _learner_style(body):
    style = body[body.index("<style>"):body.index("/* ==== 교사 모드")]
    return re.sub(r"/\*.*?\*/", "", style, flags=re.S)


def test_learner_spacing_uses_the_seven_steps(client):
    """학습자 쪽 여백(margin·padding·gap)은 --sp-* 일곱 단계만 쓴다. 예외는 본문 폭
    (60rem)과 잔디 높이(62px) 둘이다 — 여백이 아니라 크기다."""
    style = _learner_style(_page(client))
    steps = dict(re.findall(r"--sp-(\d+): (\d+)px;", style))
    assert sorted(int(v) for v in steps.values()) == [4, 8, 12, 16, 24, 32, 48]
    decls = re.findall(
        r"(?<![\w-])(?:margin|padding)(?:-(?:top|bottom|left|right))?\s*:\s*([^;{}]+);"
        r"|(?<![\w-])(?:row-|column-)?gap\s*:\s*([^;{}]+);", style)
    assert decls
    for a, b in decls:
        value = (a or b).replace("calc((100% - 60rem) / 2)", "").replace("62px", "")
        literals = re.findall(r"\d*\.?\d+(?:rem|px|em)", value)
        assert not literals, (a or b)


def test_the_approved_design_adjustments_stay(client):
    body = _page(client)
    rules = _rules(_learner_style(body))
    # 구름이 없다.
    assert 'class="cloud' not in body
    # [보기] 는 흰 바탕.
    see = [decl for sel, decl in rules if sel.strip().endswith("button.see")]
    assert see and "background: #fff" in see[0]
    # 결과에서는 판정이 제목 — 상태 줄의 큰 글자를 숨긴다.
    assert any('[data-phase="result"] .status .phase' in sel and "display: none" in decl
               for sel, decl in rules)
    # 놓인 글자는 파랑.
    assert any(".composed .tile" in sel and "background: var(--blue)" in decl
               for sel, decl in rules)
    # 손이 쉬는 동안 글자 화면의 상태는 한 줄.
    assert any('#studyPanel:not([data-phase="moving"])' in sel and "display: none" in decl
               for sel, decl in rules)


# --------------------------------------------------------------------------- #
# 홈과 교사 화면의 설명 (§59)
# --------------------------------------------------------------------------- #

def _home(body):
    return body[body.index('<main id="homeView">'):body.index('<main id="studyView"')]


def test_the_home_says_what_this_is_and_where_to_start(client):
    """무엇인가(읽는 연습), 메뉴마다 무엇·언제, 처음 오는 사람의 순서."""
    home = _visible_text(_home(_page(client)))
    assert "로봇손" in home and "읽어 답합니다" in home and "읽는 법" in home
    assert re.search(r"글자\s*→\s*훈련\s*→\s*내 진행", home)
    for when in ("처음이거나", "본 뒤에", "마친 뒤에"):
        assert when in home, when


def _teacher_markup(body):
    return body[body.index('<div id="teacherMode" hidden>'):body.index("<script>")]


def test_the_station_card_speaks_to_the_teacher(client):
    """장치 상태는 교사가 읽을 문장이 위, 원래 값은 접힌 상세다. 값을 지우지 않는다 (§35, §59)."""
    body = _page(client)
    station = _function(body, "loadStation")
    # 예전 문구가 그대로 찍히지 않는다.
    for raw in ('"Mock"', '"락 없음"', '"실기 (serial)"', "`해시 ${"):
        assert raw not in station, raw
    # 원래 값은 접힌 상세에 전부 남는다.
    for field in ("status.mode", "status.calibration_hash", "state", "status.lock_holder"):
        assert f"rawRow(" in station and field in station, field
    markup = _teacher_markup(body)
    details = markup[markup.index('<details class="internal">'):markup.index("</details>")]
    assert 'id="stationRaw"' in details
    # 불일치일 때만 크게, 교사가 할 일을 말한다. `null` 은 불일치가 아니다 (§48.3).
    assert ("로봇손 펌웨어가 지금 설정과 다릅니다. 전기팀에 펌웨어 재업로드를 요청하세요."
            in body)
    assert "alarm.hidden = status.calibration_hash_ok !== false;" in station


def test_the_confusion_view_explains_what_it_actually_draws(client):
    """읽는 법은 화면이 **실제로 그리는 것**을 설명한다. §59 에서는 방향 없는 쌍 목록이라
    격자 말을 쓰지 않았고, §60 에서 격자가 되었으므로 이제 격자를 말한다. 문구만 보지 않고
    그리는 코드가 그 방향으로 그리는지도 본다 — 세로(행) = 보여 준 글자, 가로(열) = 고른 답,
    같은 글자 칸 = 대각선. 트랙은 여전히 따로다."""
    body = _page(client)
    markup = _teacher_markup(body)
    view = markup[markup.index('id="teacherConfusionView"'):]
    text = _visible_text(view)
    assert "읽는 법" in text
    for said in ("세로", "보여 준 글자", "가로", "고른 답", "대각선", "맞힌 횟수",
                 "대각선 밖 숫자가 큰 칸", "예상한 짝"):
        assert said in text, said
    # 예전 쌍 목록의 읽는 법은 없다 — 이제 화면에 없는 것을 설명한다.
    assert "나누지 않습니다" not in text and "목록에 없음" not in text
    assert "따로 봅니다" in text

    draw = _function(body, "renderMatrix")
    rows = draw.index("for (const shown of payload.letters)")
    columns = draw.index("for (const given of payload.letters)", rows)
    assert rows < columns, "바깥 줄(세로)이 보여 준 글자여야 한다"
    assert 'count.set(key(cell.expected, cell.given)' in draw
    assert 'if (shown === given) classes.push("diag")' in draw
    assert 'classes.push("listed")' in draw
    # 쌍 목록 응답을 읽는 코드가 남아 있지 않다.
    assert "payload.pairs" not in body


def test_the_confusion_view_says_so_when_there_is_no_record(client):
    """기록이 없으면 빈 격자가 아니라 문장이다. 0 으로 채운 격자는 "기록 없음" 과
    "전부 0" 을 가르지 못한다 (§60)."""
    load = _function(_page(client), "loadStats")
    empty = load.index("if (!payload.items)")
    assert "아직 기록이 없습니다" in load[empty:load.index("renderMatrix(", empty)]


def test_the_confusion_matrix_is_reached_only_from_teacher_mode(client):
    """혼동 행렬을 부르는 곳은 교사 쪽 `loadStats()` 하나다. 학습자 모드는 부르지도,
    그릴 자리를 갖지도 않는다 (§52)."""
    body = _page(client)
    assert body.count("/api/stats/confusion?") == 1
    assert "/api/stats/confusion?" in _function(body, "loadStats")
    callers = [line.strip() for line in body.splitlines()
               if "loadStats" in line and "function loadStats" not in line]
    assert sorted(callers) == sorted([
        "loadStats();",                                                          # setTrack · startTeacher
        "loadStats();",
        'el("statsRefresh").addEventListener("click", loadStats);',
        'if (ui.mode === "teacher" && view === "confusion") { loadStats(); }',
    ]), callers
    assert "loadStats();" in _function(body, "setTrack")
    assert "loadStats();" in _function(body, "startTeacher")
    learner = _learner_markup(body)
    for forbidden in ("matrix", "대각선", "statsView", "혼동"):
        assert forbidden not in learner, forbidden


# --------------------------------------------------------------------------- #
# [판단 필요] J2 ~ J9 (§60)
# --------------------------------------------------------------------------- #

def test_progress_numbers_sessions_by_their_turn_in_the_track(client):
    """세션별 정확도는 세션 번호가 아니라 트랙 안 차례다. 공부 세션이 번호를 먹어
    첫 훈련이 "세션 2" 로 나오던 것을 고쳤다 (J2)."""
    trend = _function(_page(client), "renderTrend")
    assert "point.ordinal" in trend and "번째" in trend
    assert "point.session_id" not in trend
    assert "sessionWhen(point.started_at)" in trend


def test_the_depth_stage_is_explained_without_naming_letters(client):
    """깊이쌍은 개념만 말한다. 어느 글자인지는 적지 않는다 (§52, J3)."""
    learner = _learner_markup(_page(client))
    section = learner[learner.index("<h2>깊이쌍 단계</h2>"):]
    section = section[:section.index("</section>")]
    text = _visible_text(section)
    assert "비슷한 손 모양이 앞뒤로 달라지는 글자 짝이 문항에 나오기 시작하는 단계" in text
    assert not re.search(r"[A-Za-z]", text), text


def test_the_progress_bar_waits_for_a_session(client):
    """세션 전(문항 패널이 숨은 동안)에는 진행 막대가 없다 (J4)."""
    rules = _rules(_style(_page(client)))
    assert any("#trainView:has(#trainPanel[hidden]) .bar" in sel and "display: none" in decl
               for sel, decl in rules)


def test_the_top_bar_darkens_with_the_body_while_the_hand_moves(client):
    """손이 움직이는 동안 맨 위 머리 줄도 남색이다. 움직임은 없다 (J5, §59)."""
    rules = _rules(_style(_page(client)))
    top = [(sel, decl) for sel, decl in rules
           if '[data-phase="moving"]' in sel and sel.strip().endswith(".topbar")]
    assert top, "머리 줄을 어둡게 하는 규칙이 없다"
    for _sel, decl in top:
        assert "background: var(--navy)" in decl
        assert not _motion(decl), decl


def test_study_keeps_the_recovery_button_and_says_when_to_use_it(client):
    """[글자 보기 마치기] 는 정상 종료, [세션 끝내고 손 놓기] 는 복구다. 복구를 숨기지
    않고 문구로 가른다 (§45, §51, J6)."""
    body = _page(client)
    learner = _learner_markup(body)
    row = learner[learner.index('id="studyRecoveryRow"'):]
    row = row[:row.index("</div>")]
    assert 'id="studyResetBtn"' in row
    why = _visible_text(row)
    assert "막혔을 때만" in why and "[글자 보기 마치기]" in why
    # 공부가 락을 쥐는 동안 복구 줄이 보인다 — 숨기는 경로를 만들지 않았다.
    assert "renderStudyRecovery(true);" in _function(body, "startStudy")
    # 멈추기와 모양이 다르다 — 복구 버튼은 여전히 `.recovery` 안의 밑줄 글자다 (§45).
    assert 'class="recovery" id="studyRecoveryRow"' in learner


def test_study_buttons_follow_the_tab_name(client):
    """탭 이름이 "글자" 이므로 버튼도 글자 보기다. `data-view` 는 그대로다 (J7)."""
    body = _page(client)
    learner = _learner_markup(body)
    assert '<button class="primary" id="studyStartBtn" type="button">글자 보기 시작</button>' in learner
    assert '<button id="studyEndBtn" type="button">글자 보기 마치기</button>' in learner
    assert 'data-view="study">글자</button>' in learner
    for old in ("공부 시작", "공부 마치기", "공부가 끝났습니다"):
        assert old not in body, old


def test_the_plan_table_shows_hold_in_seconds_per_letter(client):
    """계획 표의 유지 시간은 초다. 글자 하나를 보여 주는 시간이라 "글자당" 이다 (J8)."""
    plan = _function(_page(client), "renderPlan")
    assert "`글자당 ${seconds(item.hold_ms)}`" in plan
    assert "}ms`" not in plan


def test_the_station_card_is_read_again_after_approval_and_release(client):
    """승인 · 거부 · 손 놓기 뒤에 한 번 더 읽는다. 주기적으로 묻지는 않는다 (§48, J9)."""
    body = _page(client)
    decide = _function(body, "decide")
    reset = _function(body, "resetLock")
    # try/catch 밖 — 성공이든 실패든 읽는다.
    assert decide.rstrip().endswith("loadStation();")
    assert reset.rstrip().endswith("loadStation();")
    for timer in re.findall(r"setInterval\(([^,]+)", body):
        assert "loadStation" not in timer, timer
    assert "setTimeout(loadStation" not in body
    note = _visible_text(_teacher_markup(body))
    assert "들어올 때 읽은 상태입니다" not in note
    assert "[세션 끝내고 손 놓기] 를 누른 뒤에" in note


def test_teacher_screen_shows_no_document_references(client):
    """PRD · DECISIONS · 시연 번호는 주석의 일이다. 교사 화면 글자에 없다 (§59)."""
    body = _page(client)
    text = _visible_text(_teacher_markup(body))
    assert not re.search(r"PRD|DECISIONS|§|시연|기획서", text), text
    script = body[body.index("// 교사 모드 — teacher.html"):body.index("// 모드와 화면")]
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    for literal in _strings(script):
        assert not re.search(r"PRD|DECISIONS|§|기획서", literal), literal


def test_a_refused_approval_still_shows_the_code(client):
    """안내가 첫 줄이 되어도 코드와 원문은 화면에 남는다 — 자리만 옮긴다 (§45, §59)."""
    failure = _function(_page(client), "showPlanFailure")
    assert 'el("planError").textContent = hint ? "" : error.message;' in failure
    assert 'el("planHintCode").textContent = hint ? error.message : "";' in failure


def test_teacher_spacing_uses_the_seven_steps(client):
    """교사 쪽 여백도 --sp-* 일곱 단계만 쓴다 (§58.3 → §59)."""
    body = _page(client)
    style = re.sub(r"/\*.*?\*/", "", body[body.index("/* ==== 교사 모드"):body.index("</style>")],
                   flags=re.S)
    decls = re.findall(
        r"(?<![\w-])(?:margin|padding)(?:-(?:top|bottom|left|right))?\s*:\s*([^;{}]+);"
        r"|(?<![\w-])(?:row-|column-)?gap\s*:\s*([^;{}]+);", style)
    assert decls
    for a, b in decls:
        literals = re.findall(r"\d*\.?\d+(?:rem|px|em)", a or b)
        assert not literals, (a or b)
    assert "box-shadow" not in style and "gradient" not in style
    assert "border-bottom: 6px solid var(--letter-edge)" in style


# --------------------------------------------------------------------------- #
# 교사 모드 — MCP 설명 (접힌 영역)
# --------------------------------------------------------------------------- #

def _mcp_about(body):
    markup = _teacher_markup(body)
    start = markup.index('<details class="mcpAbout">')
    return markup[start:markup.index("</details>", start)]


def test_the_mcp_explanation_is_folded_and_teacher_only(client):
    """교사가 매번 읽을 것이 아니다 — 기본은 접힘. 학습자 화면에는 없다."""
    body = _page(client)
    about = _mcp_about(body)
    assert "<details class=\"mcpAbout\">" in about and " open" not in about.split(">", 1)[0]
    assert "mcpAbout" not in _learner_markup(client.get("/station.html").text)
    # 절차는 README 에 둔다. 화면은 가리키기만 한다.
    assert "README 4장 (다)" in _visible_text(about)
    assert "claude_desktop_config.json" not in about


def test_the_mcp_explanation_lists_exactly_the_registered_tools(client):
    """화면의 도구 목록은 MCP 서버가 실제로 등록한 9개와 같다. 지어낸 도구가 없다."""
    import asyncio

    from mcp_server.server import create_server

    registered = {tool.name for tool in asyncio.run(create_server().list_tools())}
    shown = set(re.findall(r"<dd><code>(\w+)</code>", _mcp_about(_page(client))))
    assert shown == registered


def test_the_mcp_explanation_says_what_the_ai_cannot_do(client):
    """제일 중요한 절이다. 각도 없음 · 글자 거부 · 깊이쌍 · 계획 세션 · 속도·횟수·동시 구동."""
    text = _visible_text(_mcp_about(_page(client)))
    for phrase in ("글자 이름까지만", "각도나 손가락 값은 도구에 아예 없습니다",
                   "없는 글자는 거부됩니다", "해금되지 않은 깊이쌍은 거부됩니다",
                   "계획대로만 움직입니다", "속도 제한", "구동 횟수 상한", "동시 구동 차단"):
        assert phrase in text, phrase


# --------------------------------------------------------------------------- #
# 글자(공부) — 이름을 받지 않는다 (§63)
# --------------------------------------------------------------------------- #

def test_the_study_screen_asks_for_no_name(client):
    body = _page(client)
    markup = body[body.index('<section id="studyStart"'):body.index('<section id="studyPanel"')]
    assert "<input" not in markup and "이름" not in _visible_text(markup)
    # 원래 문구는 그대로다.
    assert "채점하지 않고, 기록도 남지 않습니다." in markup
    start = _function(body, "startStudy")
    assert "learner_name" not in start and 'call("POST", "/api/study-sessions", {})' in start
    # 공부가 훈련의 이름 칸을 채우지 않는다 — 누가 공부했는지 모른다.
    assert 'el("name")' not in start and "learnerName" not in start


def test_the_training_screen_still_asks_for_a_name(client):
    body = _page(client)
    learner = _learner_markup(body)
    assert 'id="name"' in learner
    assert "learner_name: name" in body
