"""Task clock + timing info: parsing, the solver shell env, the prompt line, the
grade's `timing` block, and (Linux with libfaketime only) a real shell reading the
task's time.
"""

import subprocess
import time
from datetime import datetime

import pytest
from hud import Run, connect

import env as env_mod
from tasks import tasks as ALL_TASKS

BY_SLUG = {t.slug: t for t in ALL_TASKS}
SLUG = "acct-afc-audit-sampling"


# ── parsing ────────────────────────────────────────────────────────────────

def test_clock_defaults_to_real_time():
    for blank, zone in (("", "UTC"), ("   ", "America/Chicago")):
        clock = env_mod._parse_task_clock(blank, zone)
        assert clock["set_by_task"] is False and clock["offset_seconds"] == 0
        assert clock["timezone"] == zone
        env = env_mod._clock_env(clock)
        assert env["TZ"] == zone and "LD_PRELOAD" not in env and "FAKETIME" not in env   # real time: nothing faked


def test_naive_time_is_read_in_the_given_zone():
    clock = env_mod._parse_task_clock("2001-03-05T14:00:00", "America/Chicago")
    assert clock["start"].isoformat() == "2001-03-05T14:00:00-06:00"
    assert clock["timezone"] == "America/Chicago"


def test_offset_time_is_converted_to_the_given_zone():
    clock = env_mod._parse_task_clock("2001-03-05T20:00:00+00:00", "America/Chicago")
    assert clock["start"].isoformat() == "2001-03-05T14:00:00-06:00"


def test_offset_seconds_points_the_clock_at_the_task_time():
    clock = env_mod._parse_task_clock("2001-03-05T14:00:00", "America/Chicago")
    lag = (env_mod._task_now(clock) - clock["start"]).total_seconds()
    assert 0 <= lag < 2  # never before current_time, at most ~1s after


@pytest.mark.parametrize("bad_time, bad_tz", [("next tuesday", "UTC"), ("2001-03-05T14:00:00", "Mars/Olympus")])
def test_bad_clock_args_fail_loudly(bad_time, bad_tz):
    with pytest.raises(RuntimeError):
        env_mod._parse_task_clock(bad_time, bad_tz)


# ── solver shell env ───────────────────────────────────────────────────────

def test_clock_env_with_libfaketime(monkeypatch):
    monkeypatch.setattr(env_mod, "_find_libfaketime", lambda: "/usr/lib/x/libfaketime.so.1")
    clock = env_mod._parse_task_clock("2001-03-05T14:00:00", "America/Chicago")
    out = env_mod._clock_env(clock)
    assert out["TZ"] == "America/Chicago"
    assert out["LD_PRELOAD"] == "/usr/lib/x/libfaketime.so.1"
    assert out["FAKETIME"] == f"{clock['offset_seconds']:+d}"
    assert out["FAKETIME"].startswith("-")  # 2001 is in the past
    assert out["FAKETIME_DONT_FAKE_MONOTONIC"] == "1"


def test_clock_env_without_libfaketime_still_sets_tz(monkeypatch):
    monkeypatch.setattr(env_mod, "_find_libfaketime", lambda: None)
    out = env_mod._clock_env(env_mod._parse_task_clock("2001-03-05T14:00:00", "America/Chicago"))
    assert out["TZ"] == "America/Chicago"
    assert "LD_PRELOAD" not in out and "FAKETIME" not in out


def test_solver_env_follows_the_active_clock(monkeypatch):
    monkeypatch.setattr(env_mod, "_TASK_CLOCK", None)
    assert "TASK_START_TIME" not in env_mod._solver_clean_env()
    monkeypatch.setattr(env_mod, "_TASK_CLOCK", env_mod._parse_task_clock("2001-03-05T14:00:00", "UTC"))
    assert env_mod._solver_clean_env()["TASK_START_TIME"] == "2001-03-05T14:00:00+00:00"


# ── one task end to end, in process ────────────────────────────────────────

def _args(**extra):
    a = dict(BY_SLUG[SLUG].args)
    return dict(task_slug=a["task_slug"], prompt=a["prompt"], deliverable_rel=a["deliverable"],
                grader_source=a["grader_source"], rubric_arg=a["rubric"], **extra)


async def test_run_with_clock(monkeypatch, tmp_path):
    monkeypatch.setattr(env_mod, "WORKSPACE_DIR", tmp_path)
    gen = env_mod._run(**_args(current_time="2001-03-05T14:00:00", tz_name="America/Chicago"))

    prompt = await gen.__anext__()
    assert "- Current date and time: Monday, March 5, 2001 14:00 CST (America/Chicago)" in prompt
    assert env_mod._TASK_CLOCK is not None  # the shell is on the task clock while the agent works

    time.sleep(1.1)
    result = await gen.asend("done")  # no deliverable written -> a zero, still with timing
    assert env_mod._TASK_CLOCK is None  # cleared once the agent's turn ends
    assert result.info["status"] == "missing_deliverable"
    timing = result.info["timing"]
    assert timing["agent_seconds"] >= 1.0
    assert timing["task_clock_started_at"].startswith("2001-03-05T14:00:0")
    assert datetime.fromisoformat(timing["task_clock_ended_at"]) > datetime.fromisoformat(timing["task_clock_started_at"])


async def test_run_without_current_time_uses_real_time(monkeypatch, tmp_path):
    monkeypatch.setattr(env_mod, "WORKSPACE_DIR", tmp_path)
    gen = env_mod._run(**_args())
    prompt = await gen.__anext__()
    assert "Current date and time" in prompt
    result = await gen.asend("done")
    timing = result.info["timing"]
    assert timing["task_clock_set_by_task"] is False and timing["task_clock_faked_in_shell"] is False
    assert {"agent_started_at", "agent_ended_at", "agent_seconds", "task_clock_started_at"} <= set(timing)


# ── over the wire (LocalRuntime): the new args survive manifest + coercion ──

@pytest.mark.asyncio(loop_scope="session")
async def test_served_task_with_clock(runtime):
    base = BY_SLUG[SLUG]
    task = env_mod.gdpval_task(**base.args, current_time="2001-03-05T14:00:00", timezone="America/Chicago")
    async with runtime(task) as addr, connect(addr) as client:
        async with Run(client, task.id, task.args) as run:
            assert "Monday, March 5, 2001 14:00 CST" in run.prompt_text
    assert run.reward == 0.0
    assert run.evaluation["info"]["timing"]["task_clock_started_at"].startswith("2001-03-05T14:00")


# ── real shell under libfaketime (the image; skipped where it isn't installed) ──

@pytest.mark.skipif(env_mod._find_libfaketime() is None, reason="libfaketime not installed (Linux image only)")
def test_real_shell_sees_task_time(monkeypatch, tmp_path):
    monkeypatch.setattr(env_mod, "_TASK_CLOCK", env_mod._parse_task_clock("2001-03-05T14:00:00", "America/Chicago"))
    ws = env_mod._SolverWorkspace(tmp_path, guest_path=str(tmp_path), network=False, user="solver")
    cmd = "date +%Y-%m-%dT%H:%M%z; python3 -c 'import datetime; print(datetime.datetime.now().year)'"
    out = subprocess.run(ws.shell_argv(cmd), capture_output=True, text=True, timeout=30)
    lines = out.stdout.split()
    assert lines[0].startswith("2001-03-05T14:0") and lines[0].endswith("-0600"), out
    assert lines[1] == "2001", out
