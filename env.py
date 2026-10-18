"""HUD environment for GDPval-style knowledge-work tasks.

The harness stages a bundle of reference files into the solver workspace, hands
the agent a brief, and grades the native deliverable (.xlsx / .docx / .pptx /
.pdf / code) with a transparent, readable grader.

The agent works in an ssh Workspace whose shell runs as a non-root uid with every
secret stripped, so it can neither read the grading key nor call the judge; the
grader runs host-side as root, where it holds the key for the LLM judge. The
grader and rubric arrive as plaintext task args — the author-local _hidden/ rubric
is never baked into the image.
"""

# Keep this module free of `from __future__ import annotations`: the @env.template
# params (e.g. `rubric: dict | None`) must stay real types or hud's arg coercion
# and manifest generation break.

import glob
import importlib.util
import inspect
import math
import os
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, AsyncGenerator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hud import Environment
from hud.environment import Workspace
from hud.graders import EvaluationResult

APP_ROOT = Path(__file__).resolve().parent


def _ensure_app_root_on_path() -> None:
    # The SDK loader puts the env dir on sys.path only during import and strips it
    # after, and `hud serve` has no cwd on the path — so graders importing siblings
    # (deliverable_io, native_grading) at grade time need it re-asserted then.
    if str(APP_ROOT) not in sys.path:
        sys.path.insert(0, str(APP_ROOT))


_ensure_app_root_on_path()


WORKSPACE_DIR = Path(
    os.environ.get("WORKSPACE_DIR", "/workspace/target" if Path("/workspace").exists() else "/tmp/gdpval_workspace")
).resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Dropped from the solver shell so the agent can't reach the judge or any key.
SOLVER_ENV_DROP_NAMES = {
    "HUD_API_KEY", "HUD_API_URL", "HUD_GATEWAY_URL",
    "GDPVAL_JUDGE_API_KEY", "GDPVAL_JUDGE_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
}
SOLVER_ENV_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

# A literal name is required — `hud deploy` static-parses `Environment("literal")`.
env = Environment(name="gdpval-template")


# ---------------------------------------------------------------------------
# Task clock: an in-world "now" for the solver shell. Always on.
#
# Knowledge-work tasks are often set at a specific moment (a Q3 close, a reply due
# before Friday's call, an email archive frozen at a cutoff). Without a clock the
# agent's `date` and every library it calls report the container's real time, so a
# memo "dated today" carries the wrong date. Every task gets a clock: the prompt
# states the current time and the shell gets TZ. A task sets its moment with
# `current_time` (ISO 8601) and optionally `timezone` (IANA name); then — where
# libfaketime is installed (the image) — every process in the shell sees the task's
# time, advancing at real speed from task start. Without `current_time` the clock
# reads the real time, so nothing is faked.
# ---------------------------------------------------------------------------

# The active task's clock; set at task start, cleared when the task finishes.
_TASK_CLOCK: dict[str, Any] | None = None

LIBFAKETIME_GLOBS = (
    "/usr/lib/*-linux-gnu/faketime/libfaketime.so.1",
    "/usr/lib/faketime/libfaketime.so.1",
    "/usr/local/lib/faketime/libfaketime.so.1",
)


def _find_libfaketime() -> str | None:
    """Path to libfaketime's shared object, or None (e.g. macOS / a bare dev box)."""
    for pattern in LIBFAKETIME_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def _parse_task_clock(current_time: str, tz_name: str) -> dict[str, Any] | None:
    """Validate the clock args. Without `current_time` the clock is the real time now.

    A naive `current_time` is read in `tz_name`; one with an offset is converted
    to it. The offset is fixed at task start, so the shell clock then advances in
    real time and stays consistent across commands.
    """
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(f"unknown timezone {tz_name!r}") from exc
    if not (current_time or "").strip():
        return {"start": datetime.now(zone), "timezone": zone.key, "offset_seconds": 0, "set_by_task": False}
    try:
        moment = datetime.fromisoformat(current_time.strip())
    except ValueError as exc:
        raise RuntimeError(f"current_time must be ISO 8601, got {current_time!r}") from exc
    moment = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    return {
        "start": moment,
        "timezone": zone.key,
        # task time minus wall time, in whole seconds (libfaketime's relative form).
        # Rounded up, so the shell clock never reads earlier than current_time.
        "offset_seconds": math.ceil(moment.timestamp() - time.time()),
        "set_by_task": True,
    }


def _task_now(clock: dict[str, Any]) -> datetime:
    """The task clock's current reading."""
    return datetime.fromtimestamp(time.time() + clock["offset_seconds"], ZoneInfo(clock["timezone"]))


def _clock_env(clock: dict[str, Any] | None) -> dict[str, str]:
    """Env vars that put a shell on the task clock (empty when no clock is set)."""
    if clock is None:
        return {}
    out = {
        "TZ": clock["timezone"],
        "TASK_START_TIME": clock["start"].isoformat(),
    }
    lib = _find_libfaketime() if clock.get("set_by_task") else None   # real time needs no faking
    if lib:
        out.update({
            "LD_PRELOAD": lib,
            "FAKETIME": f"{clock['offset_seconds']:+d}",
            # Leave monotonic clocks real so sleeps and timeouts behave normally.
            "FAKETIME_DONT_FAKE_MONOTONIC": "1",
            "DONT_FAKE_MONOTONIC": "1",
        })
    return out


def _solver_clean_env() -> dict[str, str]:
    """The solver shell's env: the container env minus every secret, with a non-root HOME/USER."""
    clean = dict(os.environ)
    for name in list(clean):
        if name in SOLVER_ENV_DROP_NAMES or any(marker in name for marker in SOLVER_ENV_SECRET_MARKERS):
            clean.pop(name, None)
    clean.update(
        {
            "HOME": "/home/solver" if Path("/home/solver").exists() else str(WORKSPACE_DIR),
            "USER": "solver",
            "LOGNAME": "solver",
            "XDG_CACHE_HOME": str(WORKSPACE_DIR / ".cache"),
            "TMPDIR": "/tmp",
        }
    )
    clean.update(_clock_env(_TASK_CLOCK))
    return clean


class _SolverWorkspace(Workspace):
    """The agent's ssh shell: every command re-exec'd under a secret-free env
    (`env -i`) and, when serving as root, dropped to uid 1000 (`setpriv`).

    network=False is not an air-gap without bubblewrap, so offline is a
    platform-layer concern; the secret strip is what keeps a networked shell safe.
    """

    def shell_argv(self, command=None, *, cwd=None, env=None):
        argv = super().shell_argv(command, cwd=cwd, env=env)
        if sys.platform == "win32":
            return argv
        clean = _solver_clean_env()
        wrapper = ["env", "-i", *(f"{k}={v}" for k, v in clean.items())]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            wrapper = ["setpriv", "--reuid", "1000", "--regid", "1000", "--clear-groups", "--", *wrapper]
        return [*wrapper, *argv]


# guest_path is the real workspace path so the absolute paths in the prompt resolve
# identically inside the shell.
_ws = _SolverWorkspace(WORKSPACE_DIR, guest_path=str(WORKSPACE_DIR), network=False, user="solver")


def _chown_solver_workspace() -> None:
    if os.getuid() != 0:
        return
    try:
        subprocess.run(["chown", "-R", "1000:1000", str(WORKSPACE_DIR)], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        print(f"[gdpval-env] warning: chown failed: {exc}", file=sys.stderr)


@env.initialize
async def _up() -> None:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    _chown_solver_workspace()
    await _ws.start()
    env.add_capability(_ws.capability("shell"))


@env.shutdown
async def _down() -> None:
    await _ws.stop()


def _deliverable_path(path_text: str) -> Path:
    rel = Path(path_text or "deliverable/report.md")
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError(f"unsafe deliverable path: {path_text!r}")
    return WORKSPACE_DIR / rel


# Scratch dirs a confused agent might use instead of the workspace; surfaced in
# diagnostics only, never graded.
SCRATCH_ROOTS = ("/tmp", "/home/solver", "/root")
_WALK_SKIP_DIRS = {".git", "__pycache__", ".cache", ".config", ".ipython",
                   "node_modules", ".npm", "reference_files"}


def _within(root: Path, path: Path) -> bool:
    try:
        rp, rr = path.resolve(), root.resolve()
    except OSError:
        return False
    return rp == rr or rr in rp.parents


def _display(path: Path) -> str:
    """Workspace-relative path when possible, else the absolute path."""
    try:
        return str(path.resolve().relative_to(WORKSPACE_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def _solver_files(root: Path, limit: int = 4000) -> list[Path]:
    """Non-reference files under ``root`` (the solver's outputs)."""
    reference_root = (WORKSPACE_DIR / "reference_files").resolve()
    out: list[Path] = []
    seen: set[Path] = set()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _WALK_SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    if not path.is_file():  # skip sockets, fifos, broken symlinks
                        continue
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                if resolved == reference_root or reference_root in resolved.parents:
                    continue
                seen.add(resolved)
                out.append(path)
                if len(out) >= limit:
                    return out
    except OSError:
        pass
    return out


def _misplaced_files(suffix: str) -> list[str]:
    """Deliverable-type files an agent wrote outside the workspace (diagnostics only)."""
    suffix = suffix.lower()
    found: list[str] = []
    for candidate in SCRATCH_ROOTS:
        rp = Path(candidate)
        if not rp.exists() or _within(WORKSPACE_DIR, rp):
            continue
        found.extend(
            str(p) for p in _solver_files(rp, limit=200)
            if (not suffix) or p.suffix.lower() == suffix
        )
    return found[:40]


def _resolve_deliverable(expected: Path) -> tuple[Path | None, dict[str, Any]]:
    """Find the agent's deliverable in the workspace, tolerating small naming slips
    but not missing work. Only the workspace is graded; scratch dirs are diagnostics."""
    if expected.is_file():
        return expected, {}

    expected_name = expected.name.lower()
    expected_stem = expected.stem.lower()
    expected_suffix = expected.suffix.lower()
    files = _solver_files(WORKSPACE_DIR)
    same_suffix = [path for path in files if path.suffix.lower() == expected_suffix]

    def pick(matches: list[Path]) -> Path | None:
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return None
        try:
            return max(matches, key=lambda p: p.stat().st_mtime)
        except OSError:
            return matches[0]

    def found(path: Path) -> tuple[Path, dict[str, Any]]:
        return path, {"deliverable_resolved_from": _display(path), "expected_deliverable": _display(expected)}

    name_match = pick([p for p in same_suffix if p.name.lower() == expected_name])
    if name_match is not None:
        return found(name_match)

    stem_match = pick([
        p for p in same_suffix
        if expected_stem in p.stem.lower() or p.stem.lower() in expected_stem
    ])
    if stem_match is not None:
        return found(stem_match)

    if len(same_suffix) == 1:
        return found(same_suffix[0])

    return None, {
        "expected_deliverable": _display(expected),
        "candidate_deliverables": [_display(path) for path in same_suffix[:20]],
        "workspace_files": [_display(path) for path in files[:80]],
        "misplaced_files_outside_workspace": _misplaced_files(expected_suffix),
    }


def _zero(status: str, **info: Any) -> EvaluationResult:
    return EvaluationResult(
        reward=0.0,
        content=f"status={status} reward=0.000",
        info={"status": status, **info},
        isError=status in ("grader_error", "invalid_grader_result"),
    )


def _workspace_preamble(deliverable_rel: str, clock: dict[str, Any] | None = None) -> str:
    """The environment contract prepended to every prompt: working dir, staged
    inputs, the deliverable path, and the task's date and time when it sets one.
    Deliberately no navigation or anti-fabrication coaching — ignoring the paths
    or inventing data is a real failure."""
    ws = str(WORKSPACE_DIR)
    deliverable_abs = str((WORKSPACE_DIR / deliverable_rel))
    when = ""
    if clock is not None:
        start = clock["start"]
        when = (f"- Current date and time: {start.strftime('%A, %B')} {start.day}, "
                f"{start.strftime('%Y %H:%M %Z')} ({clock['timezone']})\n")
    return (
        "[Environment]\n"
        f"- Working directory: {ws}\n"
        f"- Reference files for this task are staged at: {ws}/reference_files/\n"
        f"- Save your deliverable to: {deliverable_abs}\n"
        f"{when}\n"
    )


def _timing_info(clock: dict[str, Any] | None, started: float, ended: float) -> dict[str, Any]:
    """Wall-clock span of the agent's turn (prompt sent -> answer back), plus the
    task-clock span when one is set. Recorded on every grade, including zeros."""
    def iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, dt_timezone.utc).isoformat()

    info: dict[str, Any] = {
        "agent_started_at": iso(started),
        "agent_ended_at": iso(ended),
        "agent_seconds": round(ended - started, 3),
    }
    if clock is not None:
        zone = ZoneInfo(clock["timezone"])
        info["task_clock_started_at"] = datetime.fromtimestamp(started + clock["offset_seconds"], zone).isoformat()
        info["task_clock_ended_at"] = datetime.fromtimestamp(ended + clock["offset_seconds"], zone).isoformat()
        info["task_clock_set_by_task"] = clock.get("set_by_task", False)
        info["task_clock_faked_in_shell"] = clock.get("set_by_task", False) and _find_libfaketime() is not None
    return info


def _with_timing(result: EvaluationResult, timing: dict[str, Any]) -> EvaluationResult:
    return result.model_copy(update={"info": {**(result.info or {}), "timing": timing}})


def _stage_bundle(task_slug: str) -> None:
    """Copy tasks/<slug>/reference_files/ into the workspace (never the _hidden/ key)."""
    source = APP_ROOT / "tasks" / task_slug / "reference_files"
    if not source.is_dir():
        raise RuntimeError(f"no reference files staged for task {task_slug!r} at {source}")
    target = WORKSPACE_DIR / "reference_files"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".gitkeep"))


def _load_grader(task_slug: str, grader_source: str):
    """Load the grader from the grader_source task arg, or fall back to the image
    file tasks/<slug>/grader.py."""
    _ensure_app_root_on_path()  # graders import repo-root siblings at grade time
    if grader_source.strip():
        module = types.ModuleType(f"gdpval_grader_arg_{abs(hash(task_slug))}")
        module.__dict__["__file__"] = str(APP_ROOT / "tasks" / task_slug / "grader.py")
        exec(compile(grader_source, "<grader_source>", "exec"), module.__dict__)  # noqa: S102 - task-author code
        return module
    grader_path = APP_ROOT / "tasks" / task_slug / "grader.py"
    spec = importlib.util.spec_from_file_location(f"gdpval_grader_{abs(hash(task_slug))}", grader_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load grader at {grader_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _grade(
    task_slug: str,
    deliverable: Path,
    grader_source: str,
    rubric_arg: Any,
) -> EvaluationResult:
    grader = _load_grader(task_slug, grader_source)
    if isinstance(rubric_arg, dict) and rubric_arg:
        rubric = rubric_arg
    else:
        return _zero("missing_rubric_arg")
    result = grader.grade(WORKSPACE_DIR, deliverable, rubric)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, EvaluationResult) else _zero("invalid_grader_result")


async def _run(task_slug: str, prompt: str, deliverable_rel: str,
               grader_source: str, rubric_arg: Any,
               current_time: str = "", tz_name: str = "UTC") -> AsyncGenerator[Any, None]:
    global _TASK_CLOCK
    if not task_slug:
        raise RuntimeError("task args must include task_slug")
    clock = _parse_task_clock(current_time, tz_name)
    deliverable = _deliverable_path(deliverable_rel)
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    _stage_bundle(task_slug)
    _chown_solver_workspace()

    _TASK_CLOCK = clock  # read by the solver shell's env on every command
    try:
        started = time.time()
        # Grading reads the deliverable FILE, not the text answer, so the sent value is ignored.
        _ = yield _workspace_preamble(deliverable_rel, clock) + prompt
        timing = _timing_info(clock, started, time.time())
    finally:
        _TASK_CLOCK = None

    resolved_deliverable, resolution_info = _resolve_deliverable(deliverable)
    if resolved_deliverable is None:
        yield _with_timing(_zero("missing_deliverable", **resolution_info), timing)
        return
    try:
        result = await _grade(task_slug, resolved_deliverable, grader_source, rubric_arg)
        if resolution_info:
            result = result.model_copy(update={"info": {**result.info, **resolution_info}})
        yield _with_timing(result, timing)
    except Exception as exc:  # pragma: no cover - fail closed: any grader error scores 0
        yield _with_timing(_zero("grader_error", reason=repr(exc)[:500]), timing)


@env.template(
    id="gdpval_task",
    description="Read a staged reference bundle and produce the requested native deliverable; "
    "graded by a transparent deterministic + LLM-judge grader with a fabrication cap.",
)
async def gdpval_task(
    prompt: str = "Read the staged reference_files and produce the requested deliverable.",
    task_slug: str = "",
    deliverable: str = "deliverable/report.md",
    grader_source: str = "",
    rubric: dict[str, Any] | None = None,
    current_time: str = "",
    timezone: str = "UTC",
) -> AsyncGenerator[Any, None]:
    # grader_source and rubric arrive as task args; the rubric is never in the image.
    # current_time / timezone set the task clock (real time if current_time is empty; see "Task clock" above).
    async for item in _run(task_slug, prompt, deliverable, grader_source, rubric,
                           current_time, timezone):
        yield item
