# spellhand

지문자(fingerspelling) 수용 훈련용 로봇손 스테이션.

로봇손이 지문자 한 글자를 만들어 보이면 훈련생이 그것을 읽어 답한다.
훈련생이 손을 만드는 것이 아니라 **읽는 것**을 훈련한다.

## 현재 상태

초기 개발 단계다. 지금 구현된 것은 자세 설정 로딩·검증뿐이다.
로봇손 실기는 확보되어 있으나, Mock 디바이스로 먼저 완성한 뒤 어댑터를
교체해 연결한다.

| 구성 요소 | 상태 |
|---|---|
| `config/poses.json` (schema v2) | 구현됨 |
| `server/config.py` — 로딩·검증 | 구현됨 |
| `tests/test_config.py` | 구현됨 |
| 그 외 (`server/device/`, `mcp_server/`, `web/`, `firmware/`, `tools/`) | 비어 있음 |

## 요구 환경

- Python 3.11 이상
- 이 단계에서 필요한 외부 패키지: `pytest` 뿐

이후 단계에서 `fastapi`, `uvicorn`, `sqlalchemy`, `pyserial`, `mcp`,
`anthropic` 을 쓴다. `pyproject.toml` 에 미리 적혀 있지만 지금 설치할 필요는
없다.

의존성 파일이 두 개인 이유: `pyproject.toml` 은 **선언**(무엇이 필요한가),
`requirements.txt` 는 `.venv` 를 `pip freeze` 한 **고정본**(무슨 버전이 깔려
있는가)이다.

`.env` 에 `ANTHROPIC_API_KEY`, `SERIAL_PORT` 등 로컬 설정이 들어간다.
`.gitignore` 에 있으니 커밋되지 않는다.

## 실행

**이 프로젝트는 `.venv` (Python 3.12.10) 안에서 동작한다.** 전역 `python` 은
3.10.11 이고 의존성이 설치되어 있지 않으므로 쓰지 않는다.

```powershell
# 방법 1 — 직접 호출 (매번 새 셸에서 명령을 실행할 때 안전하다)
.\.venv\Scripts\python.exe -m pytest tests/test_config.py -v

# 방법 2 — 셸을 활성화한 뒤 평소대로
.\.venv\Scripts\Activate.ps1
pytest tests/test_config.py -v
```

설정을 직접 확인하려면:

```powershell
.\.venv\Scripts\python.exe -c "from server.config import load_config; c = load_config('config/poses.json'); print(sorted(c.poses))"
```

## 자세 테이블

11자(B D E F I L O S V W Y)의 자세, 손가락별 서보 각도, 타이밍, 혼동쌍은
**전부 `config/poses.json` 한 곳에만** 있다. 코드에도 펌웨어에도 복제본을 두지
않는다. flex 값은 0.0(완전히 폄) ~ 1.0(안전 최대 굽힘) 정규화 값이며 서보
각도가 아니다 — 각도로의 변환은 `calibration` 블록이 담당한다.

설정 파일이 스키마를 만족하지 않으면 `server.config.load_config()` 가
`ConfigError` 를 던지고 서버는 뜨지 않는다. 경고 로그로 넘어가지 않는다.

배경과 근거는 [DECISIONS.md](DECISIONS.md) 참조.
