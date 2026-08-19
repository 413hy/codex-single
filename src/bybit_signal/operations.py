from __future__ import annotations

import importlib
import logging
import logging.handlers
import os
import re
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, cast


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, length: int) -> None: ...


_TELEGRAM_SECRET = re.compile(r"(?<![A-Za-z0-9_-])\d{6,12}:[A-Za-z0-9_-]{20,}")


class _SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TELEGRAM_SECRET.sub("<telegram-token-redacted>", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TELEGRAM_SECRET.sub("<telegram-token-redacted>", value)
                if isinstance(value, str)
                else value
                for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: (
                    _TELEGRAM_SECRET.sub("<telegram-token-redacted>", value)
                    if isinstance(value, str)
                    else value
                )
                for key, value in record.args.items()
            }
        return True


class ServiceAlreadyRunningError(RuntimeError):
    pass


class ServiceInstanceLock:
    """Cross-platform advisory lock preventing duplicate schedulers."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._handle: BinaryIO | None = None

    def __enter__(self) -> ServiceInstanceLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = cast(_FcntlModule, importlib.import_module("fcntl"))
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise ServiceAlreadyRunningError(
                f"another service instance owns {self._path}"
            ) from error
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = cast(_FcntlModule, importlib.import_module("fcntl"))
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def configure_logging(log_root: Path) -> Path:
    root = log_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "service.log"
    root_logger = logging.getLogger()
    if not any(
        isinstance(handler, logging.handlers.RotatingFileHandler)
        and Path(handler.baseFilename) == path
        for handler in root_logger.handlers
    ):
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)sZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        handler.addFilter(_SecretRedactingFilter())
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return path
