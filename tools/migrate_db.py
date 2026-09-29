"""`sessions.learner_id` 를 NULL 허용으로 바꾼다 (DECISIONS.md §63).

```
python tools/migrate_db.py --check              # 필요한지 확인만 (쓰지 않음)
python tools/migrate_db.py --apply              # 백업을 떠 두고 바꾼다
python tools/migrate_db.py --apply --db other.db
```

공부 세션은 이름을 받지 않으므로 학습자가 없는 `sessions` 행이다. §63 이전의
DB 는 `learner_id` 가 `NOT NULL` 이라 그 행을 받지 못한다. 서버는 그래도 뜨고
훈련도 돈다 — 공부 시작만 `SCHEMA_OUTDATED` 로 막힌다.

**기본은 아무것도 쓰지 않는다.** `--apply` 를 줘야 쓴다. `station.db` 에 돌리는
것은 승인이 필요한 작업이다 (CLAUDE.md). 쓰기 전에 같은 폴더에
`<이름>.before-migrate-<시각>` 으로 복사본을 둔다.

SQLite 는 컬럼 제약을 바꾸는 ALTER 가 없어서 표를 다시 만든다 (새 표 → 복사 →
옛 표 삭제 → 이름 바꾸기). 행과 id 는 그대로다. `items` · `device_lock` 이
`sessions(id)` 를 가리키는 것도 그대로다. 끝에 `foreign_key_check` 로 확인한다.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.schema import CreateTable  # noqa: E402

from server.db import Database, TrainingSession  # noqa: E402

DEFAULT_DB_PATH = REPO_ROOT / "station.db"

COLUMNS = ("id", "learner_id", "status", "source", "plan_json", "started_at", "ended_at")


class MigrationError(Exception):
    """옮기지 않았다. DB 는 손대기 전 그대로다."""


def _database(path: Path) -> Database:
    return Database(f"sqlite+pysqlite:///{path.as_posix()}")


def _new_sessions_ddl(database: Database) -> str:
    """지금 모델의 `sessions` DDL 을 `sessions_new` 이름으로. 손으로 옮겨 적지 않는다."""
    ddl = str(CreateTable(TrainingSession.__table__).compile(database.engine)).strip()
    head = "CREATE TABLE sessions "
    if not ddl.startswith(head):
        raise MigrationError(f"unexpected DDL for sessions: {ddl[:40]!r}")
    return "CREATE TABLE sessions_new " + ddl[len(head):]


def needs_migration(path: Path) -> bool:
    return not _database(path).sessions_allow_no_learner()


def migrate(path: Path) -> bool:
    """바꿨으면 True, 이미 바뀌어 있으면 False."""
    database = _database(path)
    if database.sessions_allow_no_learner():
        return False
    ddl = _new_sessions_ddl(database)
    database.engine.dispose()
    columns = ", ".join(COLUMNS)
    # 드라이버의 암묵적 트랜잭션에 맡기지 않는다 — pysqlite 는 CREATE 를
    # 트랜잭션 밖에서 바로 커밋해서, 중간에 실패하면 반쯤 바뀐 DB 가 남는다.
    # BEGIN 을 직접 걸고, 실패하면 전부 되돌린다.
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        found = tuple(row[1] for row in connection.execute("PRAGMA table_info(sessions)"))
        if found != COLUMNS:
            raise MigrationError(f"sessions has columns {list(found)}, expected {list(COLUMNS)}")
        # 외래키 검사는 트랜잭션 밖에서만 끌 수 있다. 옛 표를 지우는 사이
        # `items` 의 참조가 잠깐 비는 것을 허용하고, 끝에서 한 번에 확인한다.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(ddl)
            connection.execute(
                f"INSERT INTO sessions_new ({columns}) SELECT {columns} FROM sessions"
            )
            connection.execute("DROP TABLE sessions")
            connection.execute("ALTER TABLE sessions_new RENAME TO sessions")
            broken = connection.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise MigrationError(f"foreign key check failed: {broken}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()
    if not database.sessions_allow_no_learner():
        raise MigrationError("sessions.learner_id is still NOT NULL after the rebuild")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="필요한지 확인만 한다 (쓰지 않음)")
    group.add_argument("--apply", action="store_true", help="백업을 떠 두고 바꾼다")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2

    if args.check:
        if needs_migration(args.db):
            print(f"needs migration: {args.db} (sessions.learner_id is NOT NULL)")
            return 1
        print(f"ok: {args.db} is up to date")
        return 0

    if not needs_migration(args.db):
        print(f"ok: {args.db} is up to date; nothing written")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.db.with_name(f"{args.db.name}.before-migrate-{stamp}")
    shutil.copy2(args.db, backup)
    print(f"backup: {backup}")
    try:
        migrate(args.db)
    except MigrationError as exc:
        print(f"not migrated: {exc}", file=sys.stderr)
        return 1
    print(f"migrated: {args.db} (sessions.learner_id now allows NULL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
