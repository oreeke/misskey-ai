import atexit
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from ..shared.exceptions import ConfigurationError
from . import main as app_main


def _pid_file_path() -> Path:
    if value := os.environ.get("TWIPSYBOT_PID_FILE"):
        return Path(value)
    return Path("data") / "twipsybot.pid"


def _read_pid(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _remove_pid_file(pid_file: Path, *, expected_pid: int | None = None) -> None:
    try:
        if expected_pid is not None:
            current = _read_pid(pid_file)
            if current != expected_pid:
                return
        pid_file.unlink(missing_ok=True)
    except OSError:
        return


def _print_help() -> None:
    help_text = (
        "Usage: twipsybot <command>\n\n"
        "Commands:\n"
        "  up      Run bot\n"
        "  status  Show runtime status\n"
        "  down    Stop bot\n"
        "  restart Restart bot\n"
        "  help    Show help"
    )
    print(help_text, file=sys.stdout)


def _should_daemonize() -> bool:
    if os.environ.get("TWIPSYBOT_UP_CHILD") == "1":
        return False
    return os.environ.get("TWIPSYBOT_UP_MODE") != "foreground"


def _spawn_detached(argv: list[str], *, env: dict[str, str]) -> subprocess.Popen:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": os.getcwd(),
        "env": env,
    }
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _run_up_foreground(pid_file: Path) -> int:
    pid = os.getpid()
    pid_file.write_text(str(pid), encoding="utf-8")
    atexit.register(_remove_pid_file, pid_file, expected_pid=pid)
    try:
        return app_main.main()
    except ConfigurationError as e:
        print(f"Startup error: {e}", file=sys.stderr)
        return 2
    finally:
        _remove_pid_file(pid_file, expected_pid=pid)


def _run_up_daemon(pid_file: Path) -> int:
    env = dict(os.environ)
    env["TWIPSYBOT_UP_CHILD"] = "1"
    proc = _spawn_detached([sys.executable, "-m", "twipsybot.app.cli", "up"], env=env)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        pid = _read_pid(pid_file)
        if pid is not None and pid == proc.pid and psutil.pid_exists(pid):
            return 0
        time.sleep(0.05)
    print("failed to start twipsybot", file=sys.stderr)
    return 1


def _cmd_up() -> int:
    pid_file = _pid_file_path()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        pid = _read_pid(pid_file)
        if pid and psutil.pid_exists(pid):
            print(f"twipsybot is already running (pid={pid})", file=sys.stderr)
            return 2
        _remove_pid_file(pid_file)

    if _should_daemonize():
        code = _run_up_daemon(pid_file)
        if code == 0:
            pid = _read_pid(pid_file)
            pid_text = str(pid) if pid is not None else "unknown"
            print(
                f"twipsybot started (pid={pid_text})\n"
                f"pid_file={pid_file}\n"
                "next:\n"
                "  twipsybot status    watch\n"
                "  twipsybot down      stop\n"
                "  twipsybot restart   restart",
                file=sys.stdout,
            )
        return code
    print(
        f"twipsybot running (pid={os.getpid()})\n"
        f"pid_file={pid_file}\n"
        "press Ctrl+C to stop",
        file=sys.stdout,
    )
    return _run_up_foreground(pid_file)


def _cmd_down() -> int:
    pid_file = _pid_file_path()
    if not pid_file.exists():
        print("twipsybot is not running", file=sys.stderr)
        return 2

    pid = _read_pid(pid_file)
    if not pid or not psutil.pid_exists(pid):
        _remove_pid_file(pid_file)
        print("twipsybot is not running", file=sys.stderr)
        return 2

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
        _remove_pid_file(pid_file)
        print(f"twipsybot stopped (pid={pid})", file=sys.stdout)
        return 0
    except psutil.NoSuchProcess:
        _remove_pid_file(pid_file)
        print("twipsybot stopped", file=sys.stdout)
        return 0
    except Exception as e:
        print(f"failed to stop twipsybot: {e}", file=sys.stderr)
        return 1


def _cmd_restart() -> int:
    pid_file = _pid_file_path()
    print("twipsybot restarting...", file=sys.stdout)
    if pid_file.exists():
        pid = _read_pid(pid_file)
        if pid and psutil.pid_exists(pid):
            print(f"stopping twipsybot (pid={pid})...", file=sys.stdout)
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except psutil.TimeoutExpired:
                    proc.kill()
            except psutil.NoSuchProcess:
                pass
            except Exception as e:
                print(f"failed to stop twipsybot: {e}", file=sys.stderr)
                return 1
            print(f"twipsybot stopped (pid={pid})", file=sys.stdout)
        _remove_pid_file(pid_file)
    else:
        print("twipsybot is not running; starting...", file=sys.stdout)
    return _cmd_up()


def _format_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{ss:02d}"
    return f"{m}:{ss:02d}"


def _cmd_status() -> int:
    pid_file = _pid_file_path()
    if not pid_file.exists():
        print("stopped", file=sys.stdout)
        return 2

    pid = _read_pid(pid_file)
    if not pid or not psutil.pid_exists(pid):
        _remove_pid_file(pid_file)
        print("stopped", file=sys.stdout)
        return 2

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        _remove_pid_file(pid_file)
        print("stopped", file=sys.stdout)
        return 2

    is_tty = sys.stdout.isatty()
    try:
        while True:
            if not proc.is_running():
                print("stopped", file=sys.stdout)
                return 2
            try:
                mem = proc.memory_info().rss / (1024 * 1024)
                cpu = proc.cpu_percent(interval=None)
                uptime = _format_duration(time.time() - proc.create_time())
                line = (
                    f"running pid={pid} uptime={uptime} cpu={cpu:.1f}% rss={mem:.1f}MB"
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                print("stopped", file=sys.stdout)
                return 2

            if is_tty:
                sys.stdout.write("\r" + line + " " * 10)
                sys.stdout.flush()
            else:
                print(line, file=sys.stdout)

            time.sleep(1.0)
    except KeyboardInterrupt:
        if is_tty:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return 130


def _dispatch(argv: list[str]) -> int:
    if not argv:
        _print_help()
        return 0

    cmd = argv[0].strip().lower()
    if cmd in {"help", "-h", "--help"}:
        _print_help()
        return 0
    if cmd == "up":
        return _cmd_up()
    if cmd == "down":
        return _cmd_down()
    if cmd == "restart":
        return _cmd_restart()
    if cmd == "status":
        return _cmd_status()

    print(f"unknown command: {argv[0]}", file=sys.stderr)
    _print_help()
    return 2


def main() -> int:
    return _dispatch(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
