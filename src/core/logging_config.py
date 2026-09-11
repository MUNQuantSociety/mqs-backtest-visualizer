"""Visible, idempotent terminal logging for the API and spawned workers."""

from copy import copy
import logging
from logging.handlers import QueueHandler, QueueListener
from queue import Full, Queue
import sys


class _ConsoleQueueHandler(QueueHandler):
    """A stalled terminal must not hold the API event loop or grow memory forever."""

    def __init__(self, target: logging.StreamHandler) -> None:
        super().__init__(Queue(maxsize=2_000))
        self.target = target
        self.set_name(target.name)
        self.setLevel(target.level)
        self.dropped = 0
        self.listener = QueueListener(self.queue, target, respect_handler_level=True)
        self.listener.start()

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # This is an in-process queue. Preserve args/tracebacks for Uvicorn's
        # AccessFormatter and do formatting on the writer thread as well.
        return copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except Full:
            # QueueHandler's default handleError writes to stderr, which would
            # recreate the blocked-terminal problem when this queue is full.
            self.dropped += 1


def _stream_handler(handler: logging.Handler) -> logging.Handler:
    return handler.target if isinstance(handler, _ConsoleQueueHandler) else handler


def configure_logging(level: str = "INFO", *, non_blocking: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    # basicConfig silently does nothing if a launcher/library installed a root
    # handler first. Keep existing handlers, but guarantee a console at our level.
    consoles = [
        _stream_handler(handler) for handler in root.handlers
        if isinstance(_stream_handler(handler), logging.StreamHandler)
        and (handler.name == "mqs.console"
             or getattr(_stream_handler(handler), "stream", None) in (sys.stdout, sys.stderr))
    ]
    if not consoles:
        console = logging.StreamHandler(sys.stdout)
        console.set_name("mqs.console")
        root.addHandler(console)
        consoles = [console]
    for console in consoles:
        if console.name == "mqs.console":
            # Test capture/embedded launchers can replace or close stdout.
            console.stream = sys.stdout
        console.setLevel(level)
        console.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s pid=%(process)d %(name)s | %(message)s"
        ))

    if non_blocking:
        # Uvicorn's access log also writes during response.send(), separately
        # from the application root logger. Queue both console paths.
        for owner in (root, logging.getLogger("uvicorn"), logging.getLogger("uvicorn.access")):
            for handler in list(owner.handlers):
                if isinstance(handler, _ConsoleQueueHandler):
                    handler.setLevel(handler.target.level)
                elif isinstance(handler, logging.StreamHandler) and handler.stream in (sys.stdout, sys.stderr):
                    owner.removeHandler(handler)
                    owner.addHandler(_ConsoleQueueHandler(handler))
