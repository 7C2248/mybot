"""Start or reuse the local API, then run its interactive CLI client."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys


_ROOT = Path(__file__).resolve().parent


@contextmanager
def _windows_job():
    """Kill only our children if the launcher window closes or crashes."""
    if os.name != "nt":
        yield lambda process: None
        return

    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

        def attach(process):
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE; never search by process name.
            handle = kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
            if not handle:
                if process.poll() is not None:
                    return
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(job, handle):
                    error = ctypes.get_last_error()
                    if process.poll() is None:
                        raise ctypes.WinError(error)
            finally:
                kernel.CloseHandle(handle)

        yield attach
    finally:
        kernel.CloseHandle(job)


def _stop_process(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_cli(cli_args: list[str], *, project_root: Path = _ROOT) -> int:
    from cli.client import parse_args
    import httpx
    import time
    from urllib.parse import urlsplit
    from uuid import uuid4
    args = parse_args(cli_args)
    base = args.api_url
    owner = str(uuid4())
    processes = []
    service = None
    log_path = project_root / 'data' / 'log' / 'api-service-console.log'

    def healthy():
        try:
            result = httpx.get(base + '/api/health', timeout=1, trust_env=False)
            return result.is_success and result.json().get('service') == 'mybot'
        except (httpx.HTTPError, ValueError):
            return False

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as output, _windows_job() as attach:
            try:
                if not healthy():
                    environment = dict(os.environ, PYTHONIOENCODING='utf-8', MYBOT_SERVICE_OWNER_TOKEN=owner)
                    service = subprocess.Popen(
                        [sys.executable, '-u', '-m', 'server', '--port', str(urlsplit(base).port)],
                        cwd=project_root, env=environment, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    processes.append(service)
                    attach(service)
                    deadline = time.monotonic() + 35
                    while not healthy():
                        if service.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('本机 API 启动失败，请检查 data/log/api-service-console.log')
                        time.sleep(0.2)
                    print('本机 API 已启动。', flush=True)
                else:
                    print('复用已有本机 API。', flush=True)
                cli = subprocess.Popen([sys.executable, '-u', str(project_root / 'main.py'), *cli_args],
                                       cwd=project_root, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
                processes.append(cli)
                attach(cli)
                while True:
                    try:
                        return cli.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if service is not None and service.poll() is None:
                    try:
                        httpx.post(base + '/api/service/shutdown', headers={
                            'X-Mybot-Client': 'mybot-desktop', 'X-Mybot-Owner': owner}, timeout=3, trust_env=False)
                        service.wait(timeout=10)
                    except (httpx.HTTPError, subprocess.TimeoutExpired):
                        pass
                for process in reversed(processes):
                    _stop_process(process)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


def main(argv=None):
    return run_cli(sys.argv[1:] if argv is None else argv)


if __name__ == '__main__':
    raise SystemExit(main())
