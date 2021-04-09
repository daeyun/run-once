"""
Some integration tests to make sure things work end to end.
"""

import atexit
import contextlib
import logging
import random
import signal
import subprocess
import sys
import threading
import time
from os import path

import grpc
import psutil
import pytest
from google.protobuf import duration_pb2

# TODO(daeyun): make these portable and add comments.
port = 22113
test_db = "/tmp/testdb_distlock"
server_process = None

log = logging.getLogger("test logger")
log.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
log.addHandler(ch)


@contextlib.contextmanager
def add_sys_path(p):
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        yield
    finally:
        if p in sys.path:
            sys.path.remove(p)


with add_sys_path(path.abspath(path.join(__file__, "../generated/proto"))):
    from generated.proto import distlock_pb2
    from generated.proto import distlock_pb2_grpc


def make_duration(seconds):
    return duration_pb2.Duration(
        seconds=int(seconds), nanos=int((seconds - int(seconds)) * 10 ** 9)
    )


def test_timestamp_consistent(stub):
    for i in range(10):
        stime = time.time()
        response = stub.GetCurrentServerTime(
            distlock_pb2.GetCurrentServerTimeRequest(), timeout=10
        )
        assert abs(stime - response.server_time.seconds) < 2
        assert abs(time.time() - response.server_time.seconds) < 2


def test_failed_lock_attempt(stub):
    for i in range(10):
        rand_id = str(random.random())
        key = f"{rand_id * 2} key_name {rand_id} {i}"
        key2 = f"key_name {rand_id} {i}"

        request = distlock_pb2.AcquireManyRequest(
            requests=[
                distlock_pb2.AcquireLockRequest(
                    lock=distlock_pb2.Lock(
                        global_id=key, expires_in=make_duration(1)
                    )
                ),
                distlock_pb2.AcquireLockRequest(
                    lock=distlock_pb2.Lock(
                        global_id=key2, expires_in=make_duration((2))
                    )
                ),
                distlock_pb2.AcquireLockRequest(
                    lock=distlock_pb2.Lock(
                        global_id=key, expires_in=make_duration(3)
                    )
                ),
            ]
        )

        response: distlock_pb2.AcquireManyResponse = stub.AcquireMany(
            request, timeout=10
        ).responses
        assert len(response) == 3
        for r in response:
            assert r.elapsed.nanos < 400000, r.elapsed
        assert not response[0].HasField("existing_lock")
        assert response[0].HasField("acquired_lock")
        assert not response[1].HasField("existing_lock")
        assert response[1].HasField("acquired_lock")
        assert response[2].HasField("existing_lock")
        assert not response[2].HasField("acquired_lock")
        assert response[2].existing_lock.global_id == key
        assert response[2].existing_lock.expires_in == duration_pb2.Duration(
            seconds=1, nanos=0
        )


def test_overwritten_locks(stub):
    for i in range(10):
        rand_id = str(random.random())
        key = f"{rand_id * 2} key_name {rand_id} {i}"

        request = distlock_pb2.AcquireLockRequest(
            lock=distlock_pb2.Lock(global_id=key, expires_in=make_duration(5))
        )
        response: distlock_pb2.AcquireLockResponse = stub.AcquireLock(
            request, timeout=10
        )

        assert not response.HasField("existing_lock")
        assert response.HasField("acquired_lock")

        for _ in range(5):
            should_overwrite = random.random() > 0.5

            request = distlock_pb2.AcquireLockRequest(
                lock=distlock_pb2.Lock(
                    global_id=key, expires_in=make_duration(5)
                ),
                overwrite=should_overwrite,
            )
            response: distlock_pb2.AcquireLockResponse = stub.AcquireLock(
                request, timeout=10
            )

            assert response.HasField("existing_lock")
            if should_overwrite:
                assert response.HasField("acquired_lock")
            else:
                assert not response.HasField("acquired_lock")


def test_expired_locks(stub):
    for i in range(10):
        rand_id = str(random.random())
        key = f"{rand_id * 2} key_name {rand_id} {i}"

        should_expire = random.random() > 0.5

        if should_expire:
            expiration = make_duration(0.000001)
        else:
            expiration = make_duration(0)  # 0 means no expiration.

        request = distlock_pb2.AcquireLockRequest(
            lock=distlock_pb2.Lock(global_id=key, expires_in=expiration)
        )
        response: distlock_pb2.AcquireLockResponse = stub.AcquireLock(
            request, timeout=10
        )

        # Not necessary to wait. Network latency should be enough. 1ms vs 1us
        time.sleep(0.05)
        assert not response.HasField("existing_lock")
        assert response.HasField("acquired_lock")

        request = distlock_pb2.AcquireLockRequest(
            lock=distlock_pb2.Lock(global_id=key, expires_in=make_duration(1))
        )
        response: distlock_pb2.AcquireLockResponse = stub.AcquireLock(
            request, timeout=10
        )

        assert response.HasField("existing_lock")
        if should_expire:
            assert response.HasField("acquired_lock")
        else:
            assert not response.HasField("acquired_lock")


def test_released_locks(stub):
    for i in range(10):
        rand_id = str(random.random())
        key = f"{rand_id * 2} key_name {rand_id} {i}"
        key2 = f"name {rand_id} {i}"

        should_expire = random.random() > 0.5

        if should_expire:
            expiration = make_duration(0.000001)
        else:
            expiration = make_duration(0)

        request = distlock_pb2.AcquireLockRequest(
            lock=distlock_pb2.Lock(global_id=key, expires_in=expiration)
        )
        response: distlock_pb2.AcquireLockResponse = stub.AcquireLock(
            request, timeout=10
        )

        # Not necessary to wait. Network latency should be enough. 1ms vs 1us
        time.sleep(0.05)
        assert not response.HasField("existing_lock")
        assert response.HasField("acquired_lock")

        request = distlock_pb2.ReleaseLockRequest(
            lock=distlock_pb2.Lock(global_id=key, expires_in=make_duration(1)),
            return_released_lock=True,
        )
        response: distlock_pb2.ReleaseLockResponse = stub.ReleaseLock(
            request, timeout=10
        )
        assert response.HasField("released_lock")
        assert response.released_lock.global_id == key
        assert response.released_lock.expires_in == expiration

        request = distlock_pb2.ReleaseLockRequest(
            lock=distlock_pb2.Lock(
                global_id=key2, expires_in=make_duration(1)
            ),
            return_released_lock=True,
        )
        response: distlock_pb2.ReleaseLockResponse = stub.ReleaseLock(
            request, timeout=10
        )
        assert not response.HasField("released_lock")  # Non-existing lock.


def test_list_locks(stub):
    # LevelDB maintains keys in dictionary order.
    keys = [
        "!!0001",
        "!!0002",
        "!!0003",
        "!!00030",
        "!!00039",
        "!!0004",
    ]

    for key in keys:
        request = distlock_pb2.AcquireLockRequest(
            lock=distlock_pb2.Lock(global_id=key, expires_in=make_duration(0))
        )
        response: distlock_pb2.AcquireLockResponse = stub.AcquireLock(
            request, timeout=10
        )

    request = distlock_pb2.ListLocksRequest(
        start_key="!!0001", end_key="!!0002"
    )
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 1
    assert response.locks[0].global_id == "!!0001"

    request = distlock_pb2.ListLocksRequest(
        start_key="!!0002", end_key="!!0003"
    )
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 1
    assert response.locks[0].global_id == "!!0002"

    request = distlock_pb2.ListLocksRequest(start_key="!!", end_key="!!0003")
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 2
    assert response.locks[0].global_id == "!!0001"
    assert response.locks[1].global_id == "!!0002"

    request = distlock_pb2.ListLocksRequest(start_key="!!", end_key="!!00039")
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 4
    assert response.locks[0].global_id == "!!0001"
    assert response.locks[1].global_id == "!!0002"
    assert response.locks[2].global_id == "!!0003"
    assert response.locks[3].global_id == "!!00030"

    request = distlock_pb2.ListLocksRequest(start_key="!!", end_key="!!~~~~")
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 6
    assert response.locks[0].global_id == "!!0001"
    assert response.locks[1].global_id == "!!0002"
    assert response.locks[2].global_id == "!!0003"
    assert response.locks[3].global_id == "!!00030"
    assert response.locks[4].global_id == "!!00039"
    assert response.locks[5].global_id == "!!0004"

    # Regex must be a full match.
    request = distlock_pb2.ListLocksRequest(
        start_key="!!",
        end_key="!!~~~~",
        includes=[
            distlock_pb2.LockMatchExpression(global_id_regex=r"!!\d{4}")
        ],
    )
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 4
    assert response.locks[0].global_id == "!!0001"
    assert response.locks[1].global_id == "!!0002"
    assert response.locks[2].global_id == "!!0003"
    assert response.locks[3].global_id == "!!0004"

    request = distlock_pb2.ListLocksRequest(
        start_key="!!",
        end_key="!!~~~~",
        excludes=[
            distlock_pb2.LockMatchExpression(global_id_regex=r"!!\d{4}")
        ],
    )
    response: distlock_pb2.ListLocksResponse = stub.ListLocks(
        request, timeout=10
    )
    assert len(response.locks) == 2
    assert response.locks[0].global_id == "!!00030"
    assert response.locks[1].global_id == "!!00039"


@pytest.fixture(scope="function")
def stub():
    host = "127.0.0.1:{}".format(port)

    channel = grpc.insecure_channel(
        host,
        options=(
            ("grpc.keepalive_time_ms", 10000),
            ("grpc.keepalive_timeout_ms", 5000),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.bdp_probe", True),
        ),
    )
    stub = distlock_pb2_grpc.LockManagerServiceStub(channel)
    return stub


def cleanup():
    global server_process
    if isinstance(server_process, subprocess.Popen):
        log.info("Cleaning up")
        server_process.terminate()
        server_process = None


@pytest.fixture(scope="session", autouse=True)
def server():
    atexit.register(cleanup)
    for process in psutil.process_iter():
        try:
            connections = process.connections(kind="inet")
        except psutil.AccessDenied:
            continue

        for connection in connections:
            if connection.laddr.port == port:
                log.info("Process found: {}".format(process))
                assert (
                        process.name() == "distlock"
                ), f"Another process is using port {port}"
                log.info("Using port: {}".format(connection.laddr.port))
                process.send_signal(signal.SIGTERM)
                log.info("Terminated.")
                break

    global server_process
    server_process = subprocess.Popen(
        [
            "./cmake-build-release/distlock",
            "--db={}".format(test_db),
            "--port={}".format(port),
        ],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    should_terminate = True

    def read_timeout(timeout: int):
        global server_process
        start_time = time.time()
        while should_terminate:
            time.sleep(0.05)
            if time.time() - start_time > timeout:
                cleanup()
                return

    timeout_seconds = 5
    timeout_thread = threading.Thread(
        target=read_timeout, args=(timeout_seconds,)
    )
    timeout_thread.start()

    for _ in range(30):
        if server_process is None:
            raise RuntimeError("Could not start server")
        line = server_process.stdout.readline()
        if line is None:
            continue
        line = line.decode()
        if "Server listening on" in line:
            assert str(port) in line, line
            break
    should_terminate = False
    log.info("Server started.")
    yield
    log.info("End of tests.")
    should_terminate = True
    cleanup()
