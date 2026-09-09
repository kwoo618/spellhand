"""`.env` 로딩.

서버를 띄우는 경로에서 **한 번만** 부른다. 라이브러리 코드나 테스트는 이것을
부르지 않는다 — 개발자 기계의 `.env` 가 테스트에 새어 들어가면 `DEVICE_MODE=serial`
로 테스트가 실기를 잡으려 든다.

**명시적으로 설정된 환경변수가 파일보다 우선한다.** 셸에서
`DEVICE_MODE=mock` 을 주고 띄웠는데 `.env` 가 그것을 `serial` 로 덮으면,
사람이 지정한 것과 다른 것이 뜬다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import dotenv_values

__all__ = ["load_env", "EnvLoad", "DEFAULT_ENV_PATH"]

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class EnvLoad:
    """무엇을 올렸고 무엇을 건너뛰었는지. **값은 담지 않는다.**

    `.env` 에는 `ANTHROPIC_API_KEY` 같은 것이 들어 있다. 이름만 남긴다.
    """

    path: Path
    found: bool
    applied: Tuple[str, ...]
    skipped: Tuple[str, ...]


def load_env(path: "str | Path" = DEFAULT_ENV_PATH) -> EnvLoad:
    """`.env` 를 환경에 올린다. 파일이 없으면 아무것도 하지 않는다."""
    source = Path(path)
    if not source.is_file():
        logger.info("no .env at %s; using the environment as it is", source)
        return EnvLoad(path=source, found=False, applied=(), skipped=())

    applied, skipped = [], []
    for key, value in dotenv_values(source).items():
        if value is None:
            continue
        if key in os.environ:
            # 명시적 설정이 파일보다 우선한다.
            skipped.append(key)
            continue
        os.environ[key] = value
        applied.append(key)

    logger.info(
        "loaded %s: applied %s%s",
        source,
        sorted(applied) or "nothing",
        f", kept existing {sorted(skipped)}" if skipped else "",
    )
    return EnvLoad(
        path=source, found=True, applied=tuple(applied), skipped=tuple(skipped)
    )
