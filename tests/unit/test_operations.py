import logging
from pathlib import Path

import pytest

from bybit_signal.operations import (
    ServiceAlreadyRunningError,
    ServiceInstanceLock,
    configure_logging,
)


def test_service_instance_lock_rejects_duplicate_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "state" / "service.lock"

    with (
        ServiceInstanceLock(lock_path),
        pytest.raises(ServiceAlreadyRunningError, match="another service instance"),
        ServiceInstanceLock(lock_path),
    ):
        pass

    with ServiceInstanceLock(lock_path):
        assert lock_path.is_file()


def test_file_logging_redacts_telegram_token_and_quiets_http_client(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs")
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghi"

    logging.getLogger("bybit_signal.test").error(
        "request failed https://api.telegram.org/bot%s/getMe",
        token,
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = path.read_text(encoding="utf-8")
    assert token not in content
    assert "<telegram-token-redacted>" in content
    assert logging.getLogger("httpx").level == logging.WARNING
