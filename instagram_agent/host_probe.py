"""Report what the host offers for video work: ffmpeg/ffprobe and the CPU, memory and disk limits it can read.

Run by the deploy (`deploy-dashboard.yml`) so the answer is in the job log. Read-only; prints no environment values.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_DIRS = ("/usr/bin", "/usr/local/bin", "/opt/ffmpeg/bin", str(Path.home() / "bin"))
CGROUP_FILES = {
    "cpu.max": "cgroup cpu.max",
    "memory.max": "cgroup memory.max",
    "pids.max": "cgroup pids.max",
    "cpu/cpu.cfs_quota_us": "cgroup v1 cpu quota (us)",
    "memory/memory.limit_in_bytes": "cgroup v1 memory limit",
}
RLIMITS = {"RLIMIT_AS": "address space", "RLIMIT_DATA": "data", "RLIMIT_NPROC": "processes", "RLIMIT_CPU": "CPU s"}


def find_tool(name: str) -> str | None:
    """`name` on PATH or in the usual install folders, as an executable file path."""
    found = shutil.which(name)
    if found:
        return found
    for folder in TOOL_DIRS:
        path = Path(folder) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def tool_version(path: str) -> str:
    try:
        result = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unusable ({type(exc).__name__})"
    first = (result.stdout or "").splitlines()[:1]
    return first[0][:120] if result.returncode == 0 and first else f"unusable (exit {result.returncode})"


def _limit(value: int) -> str:
    return "unlimited" if value == resource.RLIM_INFINITY else str(value)


def lines(app: Path) -> list[str]:
    out = []
    for name in ("ffmpeg", "ffprobe"):
        path = find_tool(name)
        out.append(f"{name}: {path} {tool_version(path)}" if path else f"{name}: not found")
    out.append(f"cpus: {os.cpu_count()} (usable: {len(os.sched_getaffinity(0))})")
    for key, label in RLIMITS.items():
        soft, _hard = resource.getrlimit(getattr(resource, key))
        out.append(f"rlimit {label}: {_limit(soft)}")
    for name, label in CGROUP_FILES.items():
        path = Path("/sys/fs/cgroup") / name
        try:
            out.append(f"{label}: {path.read_text().strip()[:60]}")
        except OSError:
            pass
    try:
        usage = shutil.disk_usage(app)
        out.append(f"disk free: {usage.free // 2**20} MiB of {usage.total // 2**20} MiB")
    except OSError:
        out.append("disk free: unreadable")
    return out


def main() -> int:
    for line in lines(Path.cwd()):
        print(f"video host: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
