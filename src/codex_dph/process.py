"""Bounded process supervision. No shell interpolation."""
import os
import signal
import subprocess
import time
import uuid
import psutil


class Stopped(Exception):
    pass


def stop(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Kill remaining descendants even if the group leader has already exited.
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait(timeout=3)


def owned_processes(token):
    result = []
    for process in psutil.process_iter(['pid','uids']):
        ids = None
        try:
            ids = process.uids()
            if ids.real == os.getuid() and process.environ().get('CODEX_DPH_PROCESS_TOKEN') == token:
                if process.status() != psutil.STATUS_ZOMBIE:
                    result.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as e:
            # macOS login inherits the user's real UID but runs as root and denies
            # environment inspection. It cannot be a DPH worker descendant.
            if ids is not None and ids.real == os.getuid() and getattr(ids, 'effective', None) == 0 and process.exe() == '/usr/bin/login':
                continue
            raise PermissionError('Cannot inspect a process to confirm task ownership') from e
    return result


def stop_owned(token):
    try:
        processes = owned_processes(token)
    except PermissionError as e:
        raise RuntimeError('Cannot confirm descendants stopped: process inspection denied') from e
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes,timeout=1)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive,timeout=2)
    try:
        remaining = owned_processes(token)
    except PermissionError as e:
        raise RuntimeError('Cannot confirm descendants stopped: process inspection denied') from e
    if remaining:
        raise RuntimeError('Cannot confirm execution descendants stopped')


def execute(argv, cwd, input_text, stdout_path, stderr_path, timeout, cancelled, heartbeat, env=None):
    env = dict(os.environ if env is None else env)
    token = env.setdefault('CODEX_DPH_PROCESS_TOKEN',uuid.uuid4().hex)
    with open(stdout_path, 'wb') as out, open(stderr_path, 'wb') as err:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err,
                             start_new_session=True, env=env)
        started = time.monotonic()
        try:
            # Feed stdin in nonblocking chunks. Repeated communicate() after a
            # timeout can leave a large prompt half-written and its pipe open.
            data = memoryview(input_text.encode())
            os.set_blocking(p.stdin.fileno(), False)
            while True:
                heartbeat(p.pid)
                if cancelled():
                    raise Stopped('cancelled')
                if time.monotonic() - started > timeout:
                    raise Stopped('timeout')
                if any(os.path.getsize(path) > 20*1024*1024 for path in (stdout_path, stderr_path)):
                    raise Stopped('log limit exceeded')
                if p.poll() is not None:
                    break
                if not p.stdin.closed:
                    if data:
                        try:
                            written = os.write(p.stdin.fileno(), data)
                            data = data[written:]
                        except BlockingIOError:
                            pass
                        except BrokenPipeError:
                            p.stdin.close()
                    else:
                        p.stdin.close()
                time.sleep(.1)
            # A finished parent must not leave a child continuing to modify source files.
            stop(p)
            return p.returncode
        finally:
            stop(p)
            try:
                stop_owned(token)
            except (PermissionError, psutil.AccessDenied) as e:
                raise RuntimeError('Cannot confirm execution descendants stopped') from e
