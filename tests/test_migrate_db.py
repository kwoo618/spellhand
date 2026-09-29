"""`tools/migrate_db.py` — `sessions.learner_id` 를 NULL 허용으로 (DECISIONS.md §63).

`station.db` 에는 돌리지 않는다. 임시 폴더의 예전 스키마 DB 로만 본다.
"""

import sqlite3

from server.db import Database
from tests.test_study import old_database
from tools import migrate_db


def _seed(path):
    """학습자 하나, 훈련 세션 하나, 답한 문항 하나. 락은 그 세션이 쥐고 있다."""
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("INSERT INTO learners (id, name, created_at) VALUES (1, 'haru', '2026-09-01')")
        connection.execute(
            "INSERT INTO sessions (id, learner_id, status, source, plan_json, started_at, ended_at) "
            "VALUES (7, 1, 'done', 'manual', '{\"word_items\": 1}', '2026-09-01', '2026-09-01')"
        )
        connection.execute(
            "INSERT INTO items (id, session_id, seq, track, letters, hold_ms, transition_ms, boundary, attempt) "
            "VALUES (1, 7, 1, 'word', 'WEB', 3000, 900, 'explicit', 1)"
        )
        connection.execute("UPDATE device_lock SET session_id = 7 WHERE id = 1")
    connection.close()


def _rows(path):
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("learners", "sessions", "items", "device_lock")
        }
    finally:
        connection.close()


def test_the_migration_keeps_every_row_and_allows_a_study_row(tmp_path):
    path = tmp_path / "old.db"
    old_database(path).engine.dispose()
    _seed(path)
    before = _rows(path)

    assert migrate_db.migrate(path) is True

    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    assert database.sessions_allow_no_learner()
    assert _rows(path) == before
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # 학습자 없는 행이 들어간다 — 공부 세션이다.
        connection.execute(
            "INSERT INTO sessions (learner_id, status, source) VALUES (NULL, 'done', 'study')"
        )
    finally:
        connection.close()


def test_the_migration_runs_once(tmp_path):
    path = tmp_path / "old.db"
    old_database(path).engine.dispose()
    assert migrate_db.migrate(path) is True
    assert migrate_db.migrate(path) is False


def test_check_writes_nothing_and_apply_leaves_a_backup(tmp_path):
    path = tmp_path / "old.db"
    old_database(path).engine.dispose()
    original = path.read_bytes()

    assert migrate_db.main(["--check", "--db", str(path)]) == 1
    assert path.read_bytes() == original

    assert migrate_db.main(["--apply", "--db", str(path)]) == 0
    backups = list(tmp_path.glob("old.db.before-migrate-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert migrate_db.main(["--check", "--db", str(path)]) == 0


def test_a_fresh_database_needs_nothing(tmp_path):
    path = tmp_path / "new.db"
    Database(f"sqlite+pysqlite:///{path.as_posix()}").create_all()
    assert migrate_db.main(["--check", "--db", str(path)]) == 0
    assert migrate_db.main(["--apply", "--db", str(path)]) == 0
    assert list(tmp_path.glob("new.db.before-migrate-*")) == []


def test_a_failed_migration_leaves_the_database_as_it_was(tmp_path):
    """끝의 외래키 검사가 실패하면 전부 되돌린다. 반쯤 바뀐 DB 가 남지 않는다."""
    path = tmp_path / "old.db"
    old_database(path).engine.dispose()
    connection = sqlite3.connect(path)
    with connection:  # 없는 세션을 가리키는 문항 — 외래키 검사가 잡는다
        connection.execute(
            "INSERT INTO items (id, session_id, seq, track, letters, hold_ms, transition_ms, boundary, attempt) "
            "VALUES (1, 99, 1, 'word', 'WEB', 3000, 900, 'explicit', 1)"
        )
    connection.close()
    before = _rows(path)

    try:
        migrate_db.migrate(path)
    except migrate_db.MigrationError as exc:
        assert "foreign key" in str(exc)
    else:
        raise AssertionError("migration should have refused")

    assert not Database(f"sqlite+pysqlite:///{path.as_posix()}").sessions_allow_no_learner()
    assert _rows(path) == before
    connection = sqlite3.connect(path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert "sessions_new" not in tables
