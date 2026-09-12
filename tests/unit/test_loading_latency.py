"""API responses must not wait on console output or duplicate SDK startup."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import pickle
import threading
from types import SimpleNamespace

import httpx

from src.core import logging_config
from src.integrations.strategy_store import S3StrategyStore


def test_health_responds_while_console_is_blocked(monkeypatch):
    from server import app

    entered = threading.Event()
    release = threading.Event()

    class BlockedConsole(io.StringIO):
        def write(self, text):
            entered.set()
            release.wait(10)
            return super().write(text)

    output = BlockedConsole()
    root = logging.getLogger()
    owners = [root, logging.getLogger("uvicorn"), logging.getLogger("uvicorn.access")]
    for owner in owners:
        monkeypatch.setattr(owner, "handlers", [])
    monkeypatch.setattr(root, "level", logging.INFO)
    monkeypatch.setattr(logging_config.sys, "stdout", output)
    logging_config.configure_logging("INFO", non_blocking=True)
    logging_config.configure_logging("INFO", non_blocking=True)
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging_config._ConsoleQueueHandler)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            return await client.get("/api/health")

    try:
        root.info("occupy the terminal writer")
        assert entered.wait(3)
        handler.queue.maxsize = 2
        for index in range(10):
            root.info("queued line %s", index)
        assert handler.dropped > 0
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, request())
            try:
                response = future.result(timeout=3)
                assert response.status_code == 200
                assert not release.is_set()
            finally:
                release.set()
    finally:
        release.set()
        handler.queue.join()
        handler.listener.stop()


def test_concurrent_s3_requests_keep_sessions_and_clients_in_their_own_threads(monkeypatch):
    import boto3

    sessions = []
    start = threading.Barrier(8)
    requests_overlap = threading.Barrier(8)

    def make_session():
        session = SimpleNamespace(owner=threading.get_ident(), clients=[])
        sessions.append(session)

        def make_client(service, **kwargs):
            assert session.owner == threading.get_ident()
            assert service == "s3"
            assert kwargs.get("verify", True) is not False
            assert kwargs["config"].connect_timeout == 5
            assert kwargs["config"].read_timeout == 10
            client = SimpleNamespace(owner=session.owner)

            def get_object(**request):
                assert client.owner == threading.get_ident(), "S3 client crossed threads"
                # All eight requests must be able to run concurrently, rather
                # than removing the TLS race by serializing catalogue reads.
                requests_overlap.wait(timeout=5)
                return {"Body": io.BytesIO(b"source")}

            client.get_object = get_object
            session.clients.append(client)
            return client

        session.client = make_client
        return session

    def shared_default_session(*args, **kwargs):
        raise AssertionError("must not use boto3's shared default Session")

    monkeypatch.setattr(boto3, "client", shared_default_session)
    monkeypatch.setattr(boto3.session, "Session", make_session)
    store = S3StrategyStore("test-bucket")

    def read_client(_):
        start.wait(timeout=5)
        first = store._s3
        assert store.get("strategies/example/", "strategy.py") == "source"
        assert store.get("strategies/example/", "config.json") == "source"
        assert store._s3 is first
        return first

    with ThreadPoolExecutor(max_workers=8) as executor:
        clients = list(executor.map(read_client, range(8)))
    assert len(sessions) == 8
    assert len({id(client) for client in clients}) == 8
    assert all(len(session.clients) == 1 for session in sessions)
    assert store._client is None  # Worker clients never leak into this thread.
    main_client = store._s3
    assert store._s3 is main_client
    assert all(main_client is not client for client in clients)
    restored = pickle.loads(pickle.dumps(store))
    assert restored._client is None
    assert restored._s3 is not main_client
    assert len(sessions) == 10


def test_queued_access_logs_preserve_uvicorn_formatter_arguments():
    from uvicorn.logging import AccessFormatter

    output = io.StringIO()
    target = logging.StreamHandler(output)
    target.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    handler = logging_config._ConsoleQueueHandler(target)
    logger = logging.Logger("isolated-access", level=logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1234", "GET", "/api/health", "1.1", 200)
        handler.queue.join()
        assert 'GET /api/health HTTP/1.1' in output.getvalue()
        assert '200 OK' in output.getvalue()
    finally:
        handler.listener.stop()
