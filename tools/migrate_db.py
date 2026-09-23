"""기존 DB 파일에 컬럼을 더한다. `create_all()` 이 하지 않는 일이다.

```
python tools/migrate_db.py --check                # 남은 마이그레이션이 있는가 (쓰지 않음)
python tools/migrate_db.py                        # station.db 에 적용
python tools/migrate_db.py --db path/to/other.db  # 다른 파일에 적용
```

**`station.db` 에 적용하는 것은 승인 대상이다** (CLAUDE.md). 적용 전에 파일을
복사해 두고 `--check` 로 무엇이 붙을지 먼저 본다.

컬럼 추가만 한다. 기존 행은 새 컬럼이 NULL 이 되고 — "그때는 기록하지 않았다"
는 뜻이다 — 값을 추정해 채우지 않는다 (R5). 이미 붙은 컬럼은 건너뛰므로 여러
번 실행해도 같다. 테이블이 아예 없으면 건너뛴다 — 그 테이블은 서버 기동 때
`create_all()` 이 새 스키마로 만든다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "station.db"

# (테이블, 컬럼, 타입). 순서대로 적용한다. 지우지 않고 뒤에 더하기만 한다.
MIGRATIONS: Tuple[Tuple[str, str, str], ...] = (
    # 경유 자세에 쓴 시간 (DECISIONS.md §55)
    ("items", "approach_ms", "INTEGER"),
)


def pending(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """아직 붙지 않은 마이그레이션."""
    out = []
    for table, column, sql_type in MIGRATIONS:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # 테이블이 없다 — create_all() 이 새 스키마로 만든다
        if column not in {row[1] for row in rows}:
            out.append((table, column, sql_type))
    return out


def apply(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """남은 마이그레이션을 한 트랜잭션으로 적용하고, 적용한 것을 돌려준다."""
    todo = pending(conn)
    with conn:
        for table, column, sql_type in todo:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    return todo


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="적용하지 않고 남은 것만 본다. 남았으면 exit 1",
    )
    args = parser.parse_args(list(argv))

    if not args.db.exists():
        # 없는 파일에 connect 하면 빈 DB 가 생긴다. 만들지 않는다.
        print(f"{args.db}: not found", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        if args.check:
            todo = pending(conn)
            for table, column, sql_type in todo:
                print(f"pending: {table}.{column} {sql_type}")
            if not todo:
                print("up to date")
            return 1 if todo else 0

        done = apply(conn)
        for table, column, sql_type in done:
            print(f"applied: {table}.{column} {sql_type}")
        if not done:
            print("up to date")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
