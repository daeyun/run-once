"""
Client-side API for distributed task parallelism.

https://github.com/daeyun/run-once
"""

__license__ = "MIT"

import configparser
import contextlib
import enum
import functools
import logging
import os
import random
import socket
import sys
import time
from os import path
from typing import Dict, Sequence, Optional, Union, KeysView

import grpc
from google.protobuf import duration_pb2
from grpc._channel import _InactiveRpcError

rpc_timeout = 10


class DistributedIterator:
    def __init__(
        self,
        keys: Union[Sequence[Union[str, int]], KeysView, Dict],
        timeout: Union[int, float] = 0,
        shuffle=False,
        version=0,
        chunksize=10,
        random_seed=None,
    ):
        """Enables distributed iteration of unique string keys over network.

        Each key is iterated only once across all processes and machines.
        Progress is disk-backed and persistent between restarts. The intended
        purpose is to avoid computing the same thing twice in distributed
        pipeline jobs in a fault tolerant way.

        Args:
            keys: A list of unique string or integer keys to distribute. For
              now, we only support those two types. If you need to iterate over
              objects, consider serializing (not recommended) or passing URLs or
              filenames instead.
            timeout: Seconds to expiration. `notify_success(key)` must be called
              before the lock expires.
            shuffle: If False, the keys are guaranteed to be "queued" in the
              given order.
            version: An integer suffix appended to the key.
            chunksize: The number of keys to query at a time. Useful in low
              latency scenarios.
            random_seed: A random seed used for shuffling the keys.
        """
        assert keys and isinstance(keys, (KeysView, Dict, Sequence, range))
        self.keys = list(keys)
        if isinstance(self.keys[0], int):
            self.keys = [str(item) for item in self.keys]
        elif isinstance(self.keys[0], str):
            self.keys = list(self.keys)
        else:
            ValueError(f"Unrecognized key type: {type(self.keys[0])}")

        self.shuffle = shuffle
        self.version = version
        self.chunksize = chunksize
        self.expiration_seconds = timeout
        self.default_rpc_timeout = 30
        self.random_seed = random_seed
        self.random = random.Random(self.random_seed)

    def __iter__(self):
        self.current_index = 0
        if self.shuffle:
            random.shuffle(self.keys)
        return self

    def _acquire_next_available(self) -> Optional[str]:
        """Tries to acquire an exclusive lock and returns the corresponding
        string key.

        Other workers will not be able to acquire the same lock until it is
        released or expired.

        Side effects:
            Modifies `self.current_index` and sends a write request to the
            key-value lock server.

        Returns:
            A unique string key that corresponds to the acquired lock.
            None if there is no acquirable lock.
        """
        while True:
            client = lock_service_client()
            begin = self.current_index
            end = self.current_index + self.chunksize
            request = distlock_pb2.AcquireManyRequest(
                requests=[
                    distlock_pb2.AcquireLockRequest(
                        lock=make_lock(
                            key=make_versioned_key(key, version=self.version),
                            expiration_seconds=self.expiration_seconds,
                            owner_name=worker_name(),
                            force_ascii=True,
                        )
                    )
                    for key in self.keys[begin:end]
                ],
                max_acquired_locks=1,
            )
            responses: distlock_pb2.AcquireManyResponse = client.AcquireMany(
                request, timeout=self.default_rpc_timeout
            ).responses
            if len(responses) == 0:
                return None
            self.current_index += len(responses)
            if responses[-1].HasField("acquired_lock"):
                ret = responses[-1].acquired_lock.global_id
                assert ret
                return ret
            # If no lock is acquired within [begin, end), it will try the next
            # range.

    def __next__(self) -> str:
        next_key = _with_retry(
            lambda: self._acquire_next_available(),
            max_retry_count=20,
            sleep_seconds=5,
        )
        if next_key is None:
            raise StopIteration
        return next_key


def distributed_task(key, timeout, version=0, suppress_exception=True):
    """A decorator factory for functions that run only once across all processes
    and machines.

    Can be used to implement a single iteration of DistributedIterator.

    Args:
        key: A unique string id representing the wrapped function.
        timeout: Seconds to key expiration. The wrapped function should exit
          without raising an exception within this time limit.
        version: An integer suffix appended to the key.
        suppress_exception: If True, exceptions will be logged and cause the
          wrapped function to return None. The lock will be released.

    Returns:
        A decorator that invokes the wrapped function, which is prevented from
        running again after a successful run.
    """
    versioned_key = make_versioned_key(key=key, version=version)

    def decorator_distributed_task(func):
        @functools.wraps(func)
        def wrapper_distributed_task(*args, **kwargs):
            def try_lock_callable():
                return try_lock(
                    lock=make_lock(
                        key=versioned_key,
                        expiration_seconds=timeout,
                        owner_name=worker_name(),
                        force_ascii=True,
                    ),
                    force=False,
                )

            acquired_lock, existing_lock = _with_retry(
                try_lock_callable,
                max_retry_count=20,
                sleep_seconds=5,
            )
            if acquired_lock is None:

                # Sanity check. One of acquired or existing locks should exist.
                assert existing_lock is not None
                expires_in = existing_lock.expires_in

                # If the existing lock is a permanent lock, assume it was
                # confirmed successful.
                if expires_in.seconds == 0 and expires_in.nanos == 0:
                    ret_status = Status.SKIP_OK
                else:
                    ret_status = Status.SKIP_IN_PROGRESS

                return ret_status, None

            else:
                has_lock = False
                ret = None
                try:
                    ret = func(*args, **kwargs)
                    has_lock = True
                    ret_status = Status.COMPUTE_OK
                except Exception as ex:
                    ret_status = Status.COMPUTE_ERROR
                    if suppress_exception:
                        log.exception(
                            "Exception in function wrapped by "
                            f"@distributed_task. Key: {versioned_key}"
                        )
                    else:
                        release_lock_async(versioned_key)
                        raise ex

                if has_lock:
                    lock, _ = try_lock(
                        make_lock(versioned_key, expiration_seconds=0),
                        force=True,
                    )
                    assert lock is not None
                else:
                    release_lock_async(versioned_key)

                return ret_status, ret

        return wrapper_distributed_task

    return decorator_distributed_task


class Status(enum.Enum):
    COMPUTE_OK = 1
    COMPUTE_ERROR = 2
    SKIP_OK = 3
    SKIP_IN_PROGRESS = 4


def notify_success(key, assert_unique=True):
    """Prevents `key` from being processed by anyone until manually released.

    This function is intended to be called after task completion.

    Args:
        key: A string key to permanently lock.
    """
    acquired_lock, existing_lock = try_lock(
        make_lock(key, expiration_seconds=0), force=True
    )
    assert acquired_lock is not None
    assert existing_lock is not None

    if assert_unique:
        expires_in = existing_lock.expires_in
        if expires_in.seconds == 0 and expires_in.nanos == 0:
            raise ValueError("Completed more than once: {}".format(key))


def notify_failure(key):
    """Releases a lock, removing the key from the database.
    Does nothing if the key does not exist.

    Args:
        key: A string key to unlock.
    """
    release_lock_async(key)


def make_versioned_key(key: str, version: int) -> str:
    """Makes a new string key by appending a version number. The intended
    purpose is to easily create a new set of keys for when they need to be
    iterated again from scratch.

    Args:
        key: A string key.
        version: An integer suffix appended to the key. Consider the tuple
          (key, version) to be the actual key stored in the database.

    Returns:
        A new string key.
    """
    assert isinstance(key, str)
    assert isinstance(version, int)
    if version is None or version == 0:
        return key
    return f"{key}_v{version:03d}"


def make_duration(seconds: Union[int, float]):
    """Instantiates a Duration protobuf object.

    Args:
        seconds: An integer or floating point value to turn into a Duration,
          in seconds.

    Returns:
        A Duration protobuf object.
    """
    return duration_pb2.Duration(
        seconds=int(seconds), nanos=int((seconds - int(seconds)) * 1e9)
    )


def make_lock(
    key: str,
    expiration_seconds: Union[int, float] = 0,
    owner_name: Optional[str] = None,
    force_ascii=True,
):
    """Instantiates a Lock protobuf object.

    expiration_seconds=0 means no expiration.

    Args:
        key: A string key to query or store in the database.
          Usually a versioned key. See `make_versioned_key`.
        expiration_seconds: The lock will be acquirable again after this amount
          of time if not refreshed.
        owner_name: Mostly intended for debugging, e.g. to check which worker
          crashed and failed to release the lock. Currently does not affect
          functionality.
        force_ascii: Raises an AssertionError if True and `key` is not
          ascii-encodable.

    Returns:
        A Lock protobuf object.
    """
    if force_ascii:
        assert is_ascii(key), key
    return distlock_pb2.Lock(
        global_id=key,
        expires_in=make_duration(expiration_seconds),
        last_owner_name=owner_name,
    )


def search_keys_by_prefix(key_prefix: str, is_expired=None) -> Sequence[str]:
    """Scans the database to find a list of keys that start with `key_prefix`.

    Does not include released keys, since they were removed from the database.

    Args:
        key_prefix: A string prefix to match.
        is_expired: An optional boolean specifying whether we want to list
          expired or unexpired keys.

    Returns:
        A list of matching string keys.
    """
    client = lock_service_client()

    # `end_key` is exclusive. The suffix makes sure it comes after other keys.
    end_key = key_prefix + "\U0010fffe"  # 244, 143, 191, 191
    start_key = key_prefix

    ret = []

    if is_expired is None:
        includes = None
    else:
        includes = [
            distlock_pb2.LockMatchExpression(
                global_id_regex=r".*", is_expired=is_expired
            )
        ]

    # Pagination.
    while True:
        request = distlock_pb2.ListLocksRequest(
            start_key=start_key,
            end_key=end_key,
            includes=includes,
        )
        response: distlock_pb2.ListLocksResponse = client.ListLocks(
            request, timeout=rpc_timeout
        )
        if len(response.locks) == 0:
            break
        elif len(response.locks) == 1:
            ret.append(response.locks[0].global_id)
            break
        else:
            start_key = response.locks[-1].global_id
            ret.extend([item.global_id for item in response.locks[:-1]])
    return ret


def count_keys_by_prefix(key_prefix: str) -> Dict[str, int]:
    """Scans the database to count keys that start with `key_prefix`.

    Does not include released keys, since they were removed from the database.

    Args:
        key_prefix: A string prefix to match.
        is_expired: An optional boolean specifying whether we want to list
          expired or unexpired keys.

    Returns:
        A dict contain,
            expired -> Number of expired locks
            unexpired -> Number of locks that may expire eventually.
            no_expiration -> Number of locks do not expire.
        They are mutually exclusive.
    """
    client = lock_service_client()

    # `end_key` is exclusive. The suffix makes sure it comes after other keys.
    end_key = key_prefix + "\U0010fffe"  # 244, 143, 191, 191
    start_key = key_prefix

    request = distlock_pb2.CountLocksRequest(
        start_key=start_key,
        end_key=end_key,
    )

    response: distlock_pb2.ListLocksResponse = client.CountLocks(
        request, timeout=rpc_timeout
    )

    assert response.HasField("expired")
    assert response.HasField("unexpired")
    assert response.HasField("no_expiration")

    ret = {
        "expired": response.expired,
        "unexpired": response.unexpired,
        "no_expiration": response.no_expiration,
    }

    return ret


class GlogFormatter(logging.Formatter):
    # Based on https://github.com/benley/python-glog/blob/master/glog.py
    LEVEL_MAP = {
        logging.FATAL: "F",
        logging.ERROR: "E",
        logging.WARN: "W",
        logging.INFO: "I",
        logging.DEBUG: "D",
    }

    def __init__(self):
        logging.Formatter.__init__(self)

    def format(self, record):
        level = GlogFormatter.LEVEL_MAP.get(record.levelno, "?")

        date = time.localtime(record.created)
        date_usec = (record.created - int(record.created)) * 1e6
        record_message = "%c%04d%02d%02d %02d:%02d:%02d.%06d %s %s:%d] %s" % (
            level,
            date.tm_year,
            date.tm_mon,
            date.tm_mday,
            date.tm_hour,
            date.tm_min,
            date.tm_sec,
            date_usec,
            record.process if record.process is not None else "?????",
            record.filename,
            record.lineno,
            self._format_message(record),
        )
        record.getMessage = lambda: record_message
        return logging.Formatter.format(self, record)

    def _format_message(self, record):
        try:
            record_message = "%s" % (record.msg % record.args)
        except TypeError:
            record_message = record.msg
        return record_message


def set_logger_level(level: Union[int, str], logger: logging.Logger = None):
    global log
    if logger is None:
        logger = log
    for handler in logger.handlers:
        handler.setLevel(level)
    logger.setLevel(level)


def make_logger(name: str, level: Union[int, str]) -> logging.Logger:
    ret = logging.getLogger(name)
    ret.setLevel(logging.DEBUG)
    ret.handlers.clear()
    ret.propagate = False

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(GlogFormatter())
    ret.addHandler(handler)

    set_logger_level(level=level, logger=ret)

    return ret


log = make_logger(name="run_once", level="INFO")


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


def worker_name():
    return "{}_{:05d}_{:05d}".format(
        socket.gethostname(),
        os.getpid(),
        os.getppid(),
    )


class RetryTimeoutError(TimeoutError):
    pass


def _with_retry(fn, max_retry_count=20, sleep_seconds=5):
    retry_count = 0
    while True:
        try:
            return fn()
        except _InactiveRpcError as ex:
            if retry_count > max_retry_count:
                raise RetryTimeoutError("Max retries exceeded. {}".format(ex))
            retry_count += 1
            # lock_service_client() is expected to be called from `fn()`.
            _prepare_client.cache_clear()
            log.warning(
                f"Could not connect to server. "
                f"Retrying in {sleep_seconds} seconds"
            )
            time.sleep(sleep_seconds)


def try_lock(
    lock: distlock_pb2.Lock, force=False
) -> Optional[distlock_pb2.Lock]:
    client = lock_service_client()
    request = distlock_pb2.AcquireLockRequest(
        lock=lock,
        overwrite=force,
    )
    response: distlock_pb2.AcquireLockResponse = client.AcquireLock(
        request, timeout=rpc_timeout
    )
    has_acquired_lock = response.HasField("acquired_lock")
    has_existing_lock = response.HasField("existing_lock")

    ret_acquired = response.acquired_lock if has_acquired_lock else None
    ret_existing = response.existing_lock if has_existing_lock else None
    return ret_acquired, ret_existing


def force_lock_async(lock: distlock_pb2.Lock, callback=None):
    client = lock_service_client()
    request = distlock_pb2.AcquireLockRequest(
        lock=lock,
        overwrite=True,
    )
    future: grpc.Future = client.AcquireLock.future(request)

    if callback is not None:
        future.add_done_callback(callback)

    assert isinstance(future, grpc.Future)
    return future


def release_lock_async(key: str, callback=None):
    assert isinstance(key, str), key
    client = lock_service_client()
    request = distlock_pb2.ReleaseLockRequest(
        lock=make_lock(key=key),
        return_released_lock=False,
    )
    future: grpc.Future = client.ReleaseLock.future(request)

    if callback is not None:
        future.add_done_callback(callback)

    assert isinstance(future, grpc.Future)
    return future


def is_ascii(s):
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        return False
    else:
        return True


def at_most_every(_func=None, *, seconds, key):
    def decorator_at_most_every(func):
        @functools.wraps(func)
        def wrapper_at_most_every(*args, **kwargs):
            acquired_lock, _ = try_lock(
                lock=distlock_pb2.Lock(
                    global_id=key,
                    expires_in=seconds,
                    last_owner_name=worker_name(),
                )
            )
            if acquired_lock is not None:
                ret = _with_retry(
                    lambda: func(*args, **kwargs),
                    max_retry_count=20,
                    sleep_seconds=5,
                )
                return ret
            else:
                return

        return wrapper_at_most_every

    if _func is None:
        return decorator_at_most_every
    else:
        return decorator_at_most_every(_func)


def lock_service_client(address: str = None, port: int = None):
    config = configparser.ConfigParser(
        defaults={"address": "127.0.0.1", "port": "22113"}
    )
    config.read(path.expanduser("~/.run_once.ini"))

    if address is None:
        address = config["DEFAULT"]["address"]

    if port is None:
        port = int(config["DEFAULT"]["port"])

    assert isinstance(address, str), address
    assert isinstance(port, int), port

    ret = _prepare_client(address=address, port=port)
    return ret


@functools.lru_cache(1)
def _prepare_client(address: str, port: int):
    hostname = f"{address}:{port}"

    channel = grpc.insecure_channel(
        hostname,
        options=(
            ("grpc.keepalive_time_ms", 10000),
            ("grpc.keepalive_timeout_ms", 5000),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.bdp_probe", True),
        ),
    )
    client = distlock_pb2_grpc.LockManagerServiceStub(channel)
    return client


def _run_server():
    """The installer will generate a script that runs this function.

    Returns:
        Return code of the executable.
    """
    executable = path.abspath(
        path.join(__file__, "../cmake-build-release/distlock")
    )
    if not path.isfile(executable):
        raise FileNotFoundError(
            "Precompiled binary not found: {}".format(executable)
        )

    import subprocess
    import shlex

    args = " ".join(map(shlex.quote, sys.argv[1:]))
    command = f"{executable} {args}"
    log.info(f"Running command: {command}")
    return subprocess.Popen(command, shell=True).wait()
