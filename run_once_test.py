"""
Some integration tests to make sure things work end to end.
"""

import multiprocessing as mp
import random
import time
import typing
import uuid

import run_once


def worker_routine(
    worker_id: int,
    all_indices: typing.Sequence[int],
    global_prefix: str,
    shared_queue,
    fail_chance,
    max_retries=1,
):
    processed_indices = []
    rand = random.Random(worker_id)
    constant_delay = rand.random() * 0.01

    for _ in range(max_retries):
        for index in all_indices:
            key = f"{global_prefix}_{index}"

            # Temp lock index.
            lock, _ = run_once.try_lock(
                run_once.make_lock(key, expiration_seconds=0.15)
            )

            # If this index is already taken, try the next one.
            if lock is None:
                continue

            # Sleep to simulate work
            time.sleep(constant_delay + rand.random() * 0.002)

            if random.random() < fail_chance:
                # If failure happens, the lock will time out eventually.
                # So it's OK to not release.
                if random.random() < 0.5:
                    run_once.release_lock_async(key)
                continue

            # Mark processed
            processed_indices.append(index)

            # If latency is a problem, try force_lock_async
            lock, _ = run_once.try_lock(
                run_once.make_lock(key, expiration_seconds=0), force=True
            )

            assert lock is not None

        time.sleep(0.2)
        expired_keys = run_once.search_keys_by_prefix(
            global_prefix, is_expired=True
        )
        if len(expired_keys) == 0:
            break

    for index in processed_indices:
        if isinstance(shared_queue, list):
            shared_queue.append(index)
        else:
            shared_queue.put(index)

    return 0


def worker_routine_ranged(
    worker_id: int,
    all_indices: typing.Sequence[int],
    global_prefix: str,
    shared_queue,
    fail_chance,
    max_retries=1,
    shuffle_indices=False,
):
    processed_indices = []
    rand = random.Random(worker_id)
    constant_delay = rand.random() * 0.01

    keys = [f"{global_prefix}_{index}" for index in all_indices]

    distributed_tasks = run_once.DistributedIterator(
        keys=keys,
        timeout=0.15,
        shuffle=shuffle_indices,
        chunksize=10,
    )

    for _ in range(max_retries):
        for key in distributed_tasks:
            assert key is not None

            # Sleep to simulate work
            time.sleep(constant_delay + rand.random() * 0.002)

            if random.random() < fail_chance:
                # If failure happens, the lock will time out eventually.
                # So it's OK to not release.
                if random.random() < 0.5:
                    run_once.notify_failure(key)
                continue

            # Mark processed
            processed_indices.append(int(key.split("_")[-1]))

            run_once.notify_success(key)

        time.sleep(0.2)
        expired_keys = run_once.search_keys_by_prefix(
            global_prefix, is_expired=True
        )
        non_expired_keys = run_once.search_keys_by_prefix(
            global_prefix, is_expired=False
        )
        if len(expired_keys) == 0 and len(non_expired_keys) == len(keys):
            break

    for index in processed_indices:
        if isinstance(shared_queue, list):
            shared_queue.append(index)
        else:
            shared_queue.put(index)

    return 0


def test_no_duplicates():
    num_integers = 1000
    num_processes = 20
    fail_chance = 0.0

    indices_to_process = list(range(num_integers))
    global_prefix: str = "100k__" + uuid.uuid4().hex
    manager = mp.Manager()
    queue = manager.Queue()
    processes = [
        mp.Process(
            target=worker_routine,
            args=(i, indices_to_process, global_prefix, queue, fail_chance),
        )
        for i in range(num_processes)
    ]

    stime = time.time()
    for process in processes:
        process.start()

    for process in processes:
        process.join()

    elapsed = time.time() - stime
    print(f"Elapsed {elapsed:.2f}")

    processed_indices = []
    while not queue.empty():
        processed_indices.append(queue.get())
    processed_indices.sort()

    assert len(processed_indices) == num_integers
    assert len(set(processed_indices)) == num_integers


def test_retry_failed():
    num_integers = 1000
    num_processes = 20
    fail_chance = 0.4
    max_retries = 100

    indices_to_process = list(range(num_integers))
    global_prefix: str = "100k__" + uuid.uuid4().hex
    manager = mp.Manager()
    queue = manager.Queue()
    processes = [
        mp.Process(
            target=worker_routine,
            args=(
                i,
                indices_to_process,
                global_prefix,
                queue,
                fail_chance,
                max_retries,
            ),
        )
        for i in range(num_processes)
    ]

    stime = time.time()
    for process in processes:
        process.start()

    for process in processes:
        process.join()

    elapsed = time.time() - stime
    print(f"Elapsed {elapsed:.2f}")

    processed_indices = []
    while not queue.empty():
        processed_indices.append(queue.get())
    processed_indices.sort()

    assert len(processed_indices) == num_integers
    assert len(set(processed_indices)) == num_integers


def test_no_duplicates_range_query():
    num_integers = 1000
    num_processes = 20
    fail_chance = 0.0

    indices_to_process = list(range(num_integers))
    global_prefix: str = "100k__" + uuid.uuid4().hex
    manager = mp.Manager()
    queue = manager.Queue()
    processes = [
        mp.Process(
            target=worker_routine_ranged,
            args=(i, indices_to_process, global_prefix, queue, fail_chance),
        )
        for i in range(num_processes)
    ]

    stime = time.time()
    for process in processes:
        process.start()

    for process in processes:
        process.join()

    elapsed = time.time() - stime
    print(f"Elapsed {elapsed:.2f}")

    processed_indices = []
    while not queue.empty():
        processed_indices.append(queue.get())
    processed_indices.sort()

    assert len(processed_indices) == num_integers
    assert len(set(processed_indices)) == num_integers


def test_retry_failed_range_query():
    num_integers = 1000
    num_processes = 20
    fail_chance = 0.4
    max_retries = 100

    indices_to_process = list(range(num_integers))
    global_prefix: str = "100k__" + uuid.uuid4().hex
    manager = mp.Manager()
    queue = manager.Queue()
    processes = [
        mp.Process(
            target=worker_routine_ranged,
            args=(
                i,
                indices_to_process,
                global_prefix,
                queue,
                fail_chance,
                max_retries,
            ),
        )
        for i in range(num_processes)
    ]

    stime = time.time()
    for process in processes:
        process.start()

    for process in processes:
        process.join()

    elapsed = time.time() - stime
    print(f"Elapsed {elapsed:.2f}")

    processed_indices = []
    while not queue.empty():
        processed_indices.append(queue.get())
    processed_indices.sort()

    assert len(processed_indices) == num_integers
    assert len(set(processed_indices)) == num_integers


def test_retry_failed_range_query_shuffled():
    num_integers = 1000
    num_processes = 20
    fail_chance = 0.4
    max_retries = 100

    indices_to_process = list(range(num_integers))
    global_prefix: str = "100k__" + uuid.uuid4().hex
    manager = mp.Manager()
    queue = manager.Queue()
    processes = [
        mp.Process(
            target=worker_routine_ranged,
            args=(
                i,
                indices_to_process,
                global_prefix,
                queue,
                fail_chance,
                max_retries,
                True,
            ),
        )
        for i in range(num_processes)
    ]

    stime = time.time()
    for process in processes:
        process.start()

    for process in processes:
        process.join()

    elapsed = time.time() - stime
    print(f"Elapsed {elapsed:.2f}")

    processed_indices = []
    while not queue.empty():
        processed_indices.append(queue.get())
    processed_indices.sort()

    assert len(processed_indices) == num_integers
    assert len(set(processed_indices)) == num_integers


def test_search_by_prefix():
    prefix = uuid.uuid4().hex

    inserted_keys = []
    for _ in range(10):
        key = prefix + uuid.uuid4().hex
        inserted_keys.append(key)
        run_once.try_lock(
            run_once.make_lock(key, expiration_seconds=0, force_ascii=False)
        )
    found_keys = run_once.search_keys_by_prefix(prefix)
    assert len(found_keys) == 10
    assert set(found_keys) == set(inserted_keys)


def test_search_by_prefix_nonascii():
    for delim in [
        "\U00100000",
        "ÿ",
        "~",
        chr(127),
        chr(255),
        chr(256),
        chr(1000),
        "ø",
    ]:
        prefix = uuid.uuid4().hex

        inserted_keys = []
        for _ in range(10):
            key = prefix + delim + uuid.uuid4().hex
            inserted_keys.append(key)
            run_once.try_lock(
                run_once.make_lock(
                    key, expiration_seconds=0, force_ascii=False
                )
            )
        found_keys = run_once.search_keys_by_prefix(prefix)
        assert len(found_keys) == 10
        assert set(found_keys) == set(inserted_keys)


def test_search_expired():
    prefix = uuid.uuid4().hex

    inserted_keys = []
    for i in range(10):
        key = prefix + uuid.uuid4().hex
        inserted_keys.append(key)
        if i in [0, 2, 8]:
            expiration_seconds = 0.01
        else:
            expiration_seconds = 0
        run_once.try_lock(
            run_once.make_lock(
                key, expiration_seconds=expiration_seconds, force_ascii=False
            )
        )
    time.sleep(0.1)

    found_keys = run_once.search_keys_by_prefix(prefix)
    assert len(found_keys) == 10
    assert set(found_keys) == set(inserted_keys)

    found_keys = run_once.search_keys_by_prefix(prefix, is_expired=True)
    assert len(found_keys) == 3

    found_keys = run_once.search_keys_by_prefix(prefix, is_expired=False)
    assert len(found_keys) == 7
