"""Report what the host offers for video work: ffmpeg/ffprobe and the CPU, memory and disk limits it can read.

Run by the deploy (`deploy-dashboard.yml`) so the answer is in the job log. Read-only; prints no environment values.
"""

from __future__ import annotations

import os
import re
import resource
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_DIRS = ("/usr/bin", "/usr/local/bin", "/opt/ffmpeg/bin", str(Path.home() / "bin"))
CGROUP_ROOT = Path("/sys/fs/cgroup")
CGROUP_V2_FILES = {"cpu.max": "cgroup cpu.max", "memory.max": "cgroup memory.max", "pids.max": "cgroup pids.max"}
CGROUP_V1_FILES = {
    "cpu": ("cpu.cfs_quota_us", "cgroup v1 cpu quota (us)"),
    "memory": ("memory.limit_in_bytes", "cgroup v1 memory limit"),
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


def _unescape(field: str) -> str:
    """mountinfo writes a space as \\040 (and tab, newline, backslash as octal too)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _mounts(mountinfo: str) -> tuple[list[tuple[str, Path]], dict[str, list[tuple[str, Path]]]]:
    """Every mount of the cgroup v2 hierarchy and of each v1 controller, with the cgroup at that mount's root."""
    v2: list[tuple[str, Path]] = []
    v1: dict[str, list[tuple[str, Path]]] = {}
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields, fs = left.split(), right.split()
        if not sep or len(fields) < 5 or len(fs) < 3:
            continue
        mount = (_unescape(fields[3]), Path(_unescape(fields[4])))
        if fs[0] == "cgroup2":
            v2.append(mount)
        elif fs[0] == "cgroup":
            for opt in fs[2].split(","):
                if opt in CGROUP_V1_FILES:
                    v1.setdefault(opt, []).append(mount)
    return v2, v1


def _inside(where: str, mount_root: str) -> bool:
    return mount_root == "/" or where == mount_root or where.startswith(mount_root.rstrip("/") + "/")


def _chain(where: str, mounts: list[tuple[str, Path]]) -> list[Path]:
    """The process's cgroup folder and its ancestors up to the mount point, innermost first.

    Uses the mount that shows the most of the path (the shortest root containing it); unrelated bind mounts are ignored.
    """
    usable = [m for m in mounts if _inside(where, m[0])]
    if not usable:
        return []
    mount_root, point = min(usable, key=lambda m: len(m[0]))
    rest = where[len(mount_root) :] if mount_root != "/" else where
    parts = [part for part in rest.split("/") if part]
    return [point.joinpath(*parts[:n]) for n in range(len(parts), -1, -1)]


def cgroup_files(membership: str, mountinfo: str = "", root: Path = CGROUP_ROOT) -> list[tuple[Path, str]]:
    """This process's own cgroup limit files (from /proc/self/cgroup and mountinfo), then its ancestors'."""
    v2_mounts, v1_mounts = _mounts(mountinfo)
    found = []
    for line in membership.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[2].startswith("/"):
            continue
        _, controllers, where = parts
        if controllers == "":
            chain = _chain(where, v2_mounts or [("/", root)])
            for name, label in CGROUP_V2_FILES.items() if chain else ():
                found += [(chain[0] / name, label)] + [(up / name, f"{label} (parent {up.name})") for up in chain[1:]]
        for controller in controllers.split(","):
            if controller in CGROUP_V1_FILES:
                name, label = CGROUP_V1_FILES[controller]
                chain = _chain(where, v1_mounts.get(controller) or [("/", root / controller)])
                if chain:
                    found += [(chain[0] / name, label)] + [
                        (up / name, f"{label} (parent {up.name})") for up in chain[1:]
                    ]
    return found


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


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
    membership, mountinfo = (_read("/proc/self/cgroup"), _read("/proc/self/mountinfo"))
    for path, label in cgroup_files(membership, mountinfo):
        try:
            value = path.read_text().strip()[:60]
        except OSError:
            continue
        if "(parent " not in label or not value.startswith(("max", "-1", "9223372036854")):  # skip "no limit" parents
            out.append(f"{label}: {value}")
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
