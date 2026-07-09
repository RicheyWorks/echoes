"""Tests for the echoes_agent built-in step type.

All tests mock subprocess.run so they don't require the echoes binary to be
built — the step type logic (arg construction, output parsing, error paths) is
fully covered without a Rust toolchain.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from automaton.steps import StepError, run_step


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec(**kwargs):
    base = {"type": "echoes_agent", "binary": "/fake/echoes"}
    base.update(kwargs)
    return base


def _mock_proc(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


_RUN_STDOUT = (
    "Creating new agent 'Echo'.\n"
    "Running 5 tick(s)...\n"
    "[Tick 01] Echo is observing → scanning environment  [hash: 9179..9c6c]\n"
    "[Tick 02] Echo is observing → scanning environment  [hash: 8675..f2ab]\n"
    "[Tick 03] Echo is exploring → working toward goal  [hash: 058d..99e6]\n"
    "[Tick 04] Echo is exploring → working toward goal  [hash: 4d0d..b8cc]\n"
    "[Tick 05] Echo is resting → conserving energy  [hash: d11a..274b]\n"
    "\nDone — 5 total memories | Merkle root: 5422..b989\n"
)

_VERIFY_STDOUT_OK = (
    "Agent 'Echo' — 5 entries loaded.\n"
    "Hash-chain integrity: PASSED ✓\n"
    "Merkle root:          5422..b989\n"
)

_VERIFY_STDOUT_FAIL = (
    "Hash-chain integrity: FAILED ✗\n"
    "  stored memory chain is CORRUPT — possible tamper or incomplete write.\n"
)

_REPORT_JSON = {
    "agent": "Echo",
    "goal": "map environment with cryptographic memory",
    "entries": 5,
    "integrity": "ok",
    "merkle_root": "5422e5a1" + "0" * 56,
    "memory": [],
}


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

class TestBinaryDiscovery:
    def test_missing_binary_raises_step_error(self):
        """Step raises StepError when binary is not found anywhere."""
        spec = {"type": "echoes_agent"}   # no 'binary', PATH won't have it
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(StepError, match="cannot find echoes binary"):
                run_step(spec, "key-001")

    def test_explicit_binary_used_when_provided(self):
        """Explicit 'binary' field is passed straight to subprocess."""
        with patch("subprocess.run", return_value=_mock_proc(stdout=_RUN_STDOUT)) as mock_run:
            run_step(_spec(action="run", ticks=5), "key-002")
        assert mock_run.call_args[0][0][0] == "/fake/echoes"


# ---------------------------------------------------------------------------
# Invalid action
# ---------------------------------------------------------------------------

class TestInvalidAction:
    def test_unknown_action_raises_step_error(self):
        with pytest.raises(StepError, match="invalid action"):
            run_step(_spec(action="launch"), "key-003")


# ---------------------------------------------------------------------------
# run action
# ---------------------------------------------------------------------------

class TestRunAction:
    def test_run_parses_summary_line(self):
        with patch("subprocess.run", return_value=_mock_proc(stdout=_RUN_STDOUT)):
            out = run_step(_spec(action="run", ticks=5), "key-010")
        assert out["agent"] == "Echo"
        assert out["ticks_run"] == 5
        assert out["total_memories"] == 5
        assert out["merkle_root"] == "5422..b989"
        assert "stdout" in out

    def test_run_passes_all_args_to_binary(self):
        with patch("subprocess.run", return_value=_mock_proc(stdout=_RUN_STDOUT)) as mock_run:
            run_step(_spec(action="run", db="mydb.db", ticks=3,
                           name="Agent7", goal="patrol"), "key-011")
        cmd = mock_run.call_args[0][0]
        assert cmd[1] == "run"
        assert "--db" in cmd and "mydb.db" in cmd
        assert "--ticks" in cmd and "3" in cmd
        assert "--name" in cmd and "Agent7" in cmd
        assert "--goal" in cmd and "patrol" in cmd

    def test_run_nonzero_exit_raises_step_error(self):
        proc = _mock_proc(returncode=1, stderr="DB locked")
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(StepError, match="failed.*exit 1"):
                run_step(_spec(action="run"), "key-012")

    def test_run_output_partial_when_summary_missing(self):
        """If the summary line is absent, totals are None but no crash."""
        with patch("subprocess.run", return_value=_mock_proc(stdout="partial output\n")):
            out = run_step(_spec(action="run"), "key-013")
        assert out["total_memories"] is None
        assert out["merkle_root"] is None

    def test_run_timeout_raises_step_error(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("echoes", 5)):
            with pytest.raises(StepError, match="timed out"):
                run_step(_spec(action="run", timeout=5), "key-014")


# ---------------------------------------------------------------------------
# verify action
# ---------------------------------------------------------------------------

class TestVerifyAction:
    def test_verify_passed_returns_ok(self):
        with patch("subprocess.run", return_value=_mock_proc(stdout=_VERIFY_STDOUT_OK)):
            out = run_step(_spec(action="verify", name="Echo"), "key-020")
        assert out["integrity"] == "ok"
        assert out["entries"] == 5
        assert out["merkle_root"] == "5422..b989"
        assert out["agent"] == "Echo"

    def test_verify_failed_raises_step_error(self):
        with patch("subprocess.run", return_value=_mock_proc(stdout=_VERIFY_STDOUT_FAIL)):
            with pytest.raises(StepError, match="integrity FAILED"):
                run_step(_spec(action="verify"), "key-021")

    def test_verify_passes_correct_args(self):
        with patch("subprocess.run", return_value=_mock_proc(stdout=_VERIFY_STDOUT_OK)) as mock_run:
            run_step(_spec(action="verify", db="prod.db", name="Sentinel"), "key-022")
        cmd = mock_run.call_args[0][0]
        assert cmd[1] == "verify"
        assert "--db" in cmd and "prod.db" in cmd
        assert "--name" in cmd and "Sentinel" in cmd

    def test_verify_nonzero_exit_raises_step_error(self):
        with patch("subprocess.run", return_value=_mock_proc(returncode=2)):
            with pytest.raises(StepError, match="failed.*exit 2"):
                run_step(_spec(action="verify"), "key-023")


# ---------------------------------------------------------------------------
# report action
# ---------------------------------------------------------------------------

class TestReportAction:
    def test_report_returns_parsed_json(self):
        with patch("subprocess.run",
                   return_value=_mock_proc(stdout=json.dumps(_REPORT_JSON))):
            out = run_step(_spec(action="report"), "key-030")
        assert out["agent"] == "Echo"
        assert out["entries"] == 5
        assert "memory" in out

    def test_report_bad_json_raises_step_error(self):
        with patch("subprocess.run", return_value=_mock_proc(stdout="not json{")):
            with pytest.raises(StepError, match="JSON parse failed"):
                run_step(_spec(action="report"), "key-031")

    def test_report_passes_json_flag(self):
        with patch("subprocess.run",
                   return_value=_mock_proc(stdout=json.dumps(_REPORT_JSON))) as mock_run:
            run_step(_spec(action="report"), "key-032")
        cmd = mock_run.call_args[0][0]
        assert cmd[1] == "report"
        assert "--json" in cmd

    def test_report_nonzero_exit_raises_step_error(self):
        with patch("subprocess.run", return_value=_mock_proc(returncode=1)):
            with pytest.raises(StepError, match="failed.*exit 1"):
                run_step(_spec(action="report"), "key-033")
