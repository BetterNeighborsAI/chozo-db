"""Safety parser: destructive detection on raw SQL."""

from chozo import safety


def test_clean_ddl_is_safe() -> None:
    sql = "CREATE TABLE users (id int PRIMARY KEY, email text NOT NULL);"
    assert safety.analyze(sql) == []


def test_drop_table_flagged() -> None:
    assert safety.is_destructive("DROP TABLE users;")


def test_drop_schema_and_database_flagged() -> None:
    assert safety.is_destructive("DROP SCHEMA public;")
    assert safety.is_destructive("DROP DATABASE app;")


def test_truncate_flagged() -> None:
    assert safety.is_destructive("TRUNCATE TABLE sessions;")
    assert safety.is_destructive("TRUNCATE sessions;")


def test_delete_with_where_is_safe() -> None:
    assert not safety.is_destructive("DELETE FROM users WHERE id = 1;")


def test_delete_without_where_is_destructive() -> None:
    findings = safety.analyze("DELETE FROM users;")
    assert any(f.label == "DELETE FROM without WHERE" for f in findings)


def test_destructive_inside_block_comment_still_flagged() -> None:
    assert safety.is_destructive("/* benign note */ DROP TABLE legacy;")


def test_destructive_inside_line_comment_ignored() -> None:
    assert not safety.is_destructive("-- DROP TABLE users;\nSELECT 1;")
