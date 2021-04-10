Easy distributed task parallelism in Python. Inspired by [Joblib](https://joblib.readthedocs.io/en/latest/) but supports multi-machine distribution.

## run-once

The package name is a bit of a misnomer.
The idea is that you can run the same code multiple times without re-computing everything from scratch.
This library supports persistent progress tracking in distributed systems.


```python
pip install run-once
```

Instantly turn your loops into distributed queues: just wrap any iterable with `run_once.DistributedIterator`.

```python
task_keys = ['task_001', 'task_002', ...]
for key in DistributedIterator(task_keys):
    ...
```

You can run this script concurrently on multiple machines (e.g. over ssh, as kubernetes jobs),
and the workload will be distributed evenly across all processes.
Each iteration is computed exactly **once** globally regardless of how many times or where the code runs.


## Tutorial

Suppose you have a list of string arguments.
This could be the names of model checkpoints to evaluate or data you want to preprocess.

```python
task_keys = [f'unique_task_name_{i}' for i in range(100000)]
```

A typical for-loop:

```python
for key in task_keys:
    ...  # Independent computation. 
```

We want to 1. run this code on multiple machines simultaneously, 2. evenly distribute the tasks, 3. avoid duplicate work,
and 4. easily recover from failure. `DistributedIterator` achieves (2) and (3).
(1) is up to you; run the code as you would normally. 
(4) happens in-place. Progress is persistent between restarts, so you can simply rerun the script to
pick up where you left off. 

```python
for key in DistributedIterator(task_keys):
    ... # Keys are claimed on a first-come, first-served basis. Other workers will skip this iteration.
```

Documentation is available as comments: [run_once.py](./run_once.py), [distlock.proto](./distlock.proto)


### Repeating failed iterations

To be able to recover from catastrophic failures, set a timeout to let unconfirmed keys expire.
Expired keys will be picked up in the next iteration attempt.

```python
for key in DistributedIterator(task_keys, timeout=60):  
    # Exclusive access to the key is granted for 60 seconds.
    try:
        ...  # This may fail.

        # Claim permanently.
        notify_success(key)
    except:
        # Optional. Request immediate expiration on failure.
        notify_failure(key)
    
    # If neither is called, the key will time out eventually.
```

There is a decorator version that wraps the try-except block.
This is more flexible but makes more RPC calls than the iterator version.

```python
for key in task_keys:
    @run_once(key, timeout=3)
    def work():
        ...  # Throw on failure.

    work()  # The wrapper decides if work() should be skipped or not.
```

See [Usage](#usage) for configuration.
The server needs to listen on a port accessible from where the Python code runs.


## Design

The backend is implemented as a key-value lock using LevelDB. Each string key represents a lock.
The Python API will send RPC requests for exclusive access to a key.
`notify_success` disables expiration, permanently locking the key. Locked keys stay in the database to serve as "cache-hit" lookup tables.
`notify_failure` removes the key from the database to start over.

- Implicit: No consumer-producer queue pattern. No worker pool.
- Fault-tolerant: Reschedule failed tasks in-place.

### When do you need this package?

- Ad-hoc scaling across multiple arbitrary machines.
- Running unstable code that may fail: fix bug, sync code, rerun the same script and continue where you left off,
  skipping successful runs.
- Avoiding duplicate work in distributed, cached pipeline jobs. The keys could be unique identifiers like S3 object URLs.

#### Caching example

Serialization & deserialization need to be implemented separately.
There are a lot of options.
This package only provides functionality for task distribution.

```python
# Your own serialization and upload function.
def upload(output, url: str):
    ...

# Your own download and deserialization function.
def download(url: str):
    return ...

for url in s3_urls:
    # Increment the version if you ever need to recompute from scratch.
    @run_once(url, timeout=60, version=0)
    def work():
        output = ...
        upload(output, url)  
        return output
    status, output = work()
    
    if status == Status.SKIP_OK:
        # Cache hit.
        output = download(url)  
    elif status == Status.COMPUTE_OK:
        # Cache miss. `ret` is already the output value of `work()`.
        pass  
    elif status in (Status.SKIP_IN_PROGRESS, Status.COMPUTE_ERROR):
        # Try again later when it is available. Skip for now.
        continue
    else:
        raise NotImplementedError(f'Unrecognized status: {status}')
    
    # Do something with `output`.
    ...
```

For single-machine task distribution and caching, you can try [joblib](https://joblib.readthedocs.io/en/latest/).
You can use both.


### Disadvantages

- Not ideal for short-running (<1s) tasks. Non-local network latency seems to be the bottleneck.

## Usage

Install via pip.

```shell
pip install run-once
```

### Server

```shell
distlock --db=/tmp/testdb 
```

See `--help` for all options.

```
$ ./distlock --help
Distributed lock service.
Usage:
  distlock [OPTION...]

      --db arg          Path to LevelDB database
      --cache_size arg  LRU cache size in MB (default: 200)
      --port arg        HTTP service port (default: 22113)
      --host arg        Hostname (default: 127.0.0.1)
  -h, --help            Print usage
```

### Client

Create `~/.run_once.ini` as follows. This step is optional if the server is accessible at `127.0.0.1:22113`.

```ini
[DEFAULT]
address = <server ip address>
port = <server port>
```

Optionally, if the server running on a remote machine, consider forwarding a local port.

```shell
ssh -N -L 22113:localhost:22113 <host>
```

Alternatively via [mutagen](http://mutagen.io/),

```shell
mutagen forward create --name=run-once tcp:localhost:22113 <host>:tcp::22113
```

## Development

### Build

Install dependencies via [python-poetry](https://python-poetry.org/).

```shell
conda create -n run-once python=3.6
conda activate run-once
poetry update
```

Build and run server.

```shell
bash ./build.sh
./cmake-build-release/distlock --db=/tmp/testdb --port=22113
```

Run tests

```shell
pytest -s distlock_test.py
pytest -s run_once_test.py
```
