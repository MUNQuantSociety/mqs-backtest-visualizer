from unittest.mock import MagicMock

import pytest

from src.auth import db, repository


@pytest.mark.parametrize("fail", [False, True])
def test_transaction_closes_and_propagates_errors(monkeypatch, fail):
    connection = MagicMock()
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kwargs: connection)
    for name in ("HOST", "DB", "USER", "PASSWORD"):
        monkeypatch.setenv(f"POSTGRES_{name}", "test")

    def operation():
        with db.transaction() as actual:
            assert actual is connection
            if fail:
                raise RuntimeError("insert failed")

    if fail:
        with pytest.raises(RuntimeError, match="insert failed"):
            operation()
        assert connection.__exit__.call_args.args[0] is RuntimeError
    else:
        operation()
        assert connection.__exit__.call_args.args == (None, None, None)
    connection.close.assert_called_once()


def test_email_is_bound_as_a_parameter():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    repository.get_user_via_email(connection, "USER'@example.com")
    sql, values = cursor.execute.call_args.args
    assert "USER'" not in sql
    assert values == ("user'@example.com",)


@pytest.mark.parametrize("row, expected", [({"jti": "token"}, True), (None, False)])
def test_refresh_consumption_reports_conflict(row, expected):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = row
    assert repository.consume_refresh_token(connection, "token", "expiry") is expected
    assert "ON CONFLICT (jti) DO NOTHING" in cursor.execute.call_args.args[0]
