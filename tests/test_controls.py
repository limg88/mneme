import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from work_memory.budget import Budget, BudgetUnavailable, SpendingLimitExceeded
from work_memory.retention import prune_logs
from work_memory.worker import CorporateLoginRequired, DockerClaude


def test_expired_corporate_login_is_explicit_and_zero_cost_is_preserved(tmp_path, monkeypatch):
    budget = Budget(tmp_path / "budget.db")
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 10, budget=budget)

    def run(cmd, **kwargs):
        if cmd[1] == "info":
            return subprocess.CompletedProcess(cmd, 0, "test-runtime", "")
        event = {
            "type": "result",
            "is_error": True,
            "total_cost_usd": 0,
            "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        }
        return subprocess.CompletedProcess(cmd, 1, json.dumps(event), "")

    monkeypatch.setattr("work_memory.worker.subprocess.run", run)
    with pytest.raises(CorporateLoginRequired, match="corporate Claude login"):
        client.call("Synthetic local test")
    assert budget.status()["total_accounted"] == 0
    assert budget.status()["reserved_or_unknown_calls"] == 0


def test_concurrent_reservations_cannot_spend_the_same_daily_balance(tmp_path):
    budget = Budget(tmp_path / "budget.db", daily_usd=0.1)

    def reserve(_):
        try:
            return budget.reserve("haiku")[0]
        except SpendingLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(reserve, range(8)))
    assert len([r for r in reservations if r]) == 2
    assert budget.status()["daily_accounted"] == 0.1
    assert not budget.status()["can_start_call"]


def test_actual_cost_releases_unused_reservation_and_restart_preserves_limits(tmp_path):
    path = tmp_path / "budget.db"
    budget = Budget(path, total_usd=0.1, daily_usd=0.1)
    rid, _ = budget.reserve("haiku")
    budget.settle(rid, 0.003)
    other = Budget(path, total_usd=100, daily_usd=100)
    assert other.status()["total_limit"] == 0.1
    assert other.status()["total_accounted"] == 0.003
    assert other.status()["reserved_or_unknown_calls"] == 0
    with pytest.raises(ValueError, match="already settled"):
        other.settle(rid, 0)


def test_daily_reset_uses_rome_and_never_resets_total_budget(tmp_path):
    budget = Budget(tmp_path / "budget.db", total_usd=0.1, daily_usd=0.05)
    before = datetime(2026, 11, 6, 22, 59, tzinfo=UTC)
    after = datetime(2026, 11, 6, 23, 1, tzinfo=UTC)
    rid, _ = budget.reserve("haiku", now=before)
    budget.settle(rid, 0.05)
    with pytest.raises(SpendingLimitExceeded):
        budget.reserve("haiku", now=before)
    assert budget.status(now=after)["day"] == "2026-11-07"
    rid, _ = budget.reserve("haiku", now=after)
    budget.settle(rid, 0.05)
    with pytest.raises(SpendingLimitExceeded):
        budget.reserve("haiku", now=after + timedelta(days=1))


def test_unknown_or_interrupted_cost_remains_reserved(tmp_path):
    budget = Budget(tmp_path / "budget.db", daily_usd=0.05)
    rid, _ = budget.reserve("haiku")
    budget.settle(rid, None)
    assert budget.status()["daily_accounted"] == 0.05
    with pytest.raises(SpendingLimitExceeded):
        budget.reserve("haiku")


def test_missing_or_truncated_ledger_never_silently_reinitializes_authorization(tmp_path):
    path = tmp_path / "budget.db"
    budget = Budget(path)
    rid, _ = budget.reserve("haiku")
    budget.settle(rid, 0.01)
    path.unlink()
    with pytest.raises(BudgetUnavailable):
        Budget(path)
    assert not path.exists()
    path.write_bytes(b"")
    with pytest.raises(BudgetUnavailable):
        Budget(path)


def test_retention_removes_only_expired_run_logs_not_evidence_or_memory(tmp_path):
    now = datetime(2026, 11, 6, tzinfo=UTC)
    old = tmp_path / (str(uuid.uuid4()) + ".jsonl")
    materialized = tmp_path / (str(uuid.uuid4()) + ".materialized.json")
    fresh = tmp_path / (str(uuid.uuid4()) + ".jsonl")
    evidence = tmp_path / "sample.evidence.json"
    memory = tmp_path / "memory.sqlite3"
    outside = tmp_path / "outside.txt"
    for p in (old, fresh, evidence, memory, outside, materialized):
        p.write_text("preserve unless expired runtime log")
        os.utime(p, (now.timestamp(), now.timestamp()))
    for p in (old, evidence, memory, outside, materialized):
        os.utime(p, ((now - timedelta(days=8)).timestamp(),) * 2)
    link = tmp_path / (str(uuid.uuid4()) + ".jsonl")
    link.symlink_to(outside)
    assert prune_logs(tmp_path, now=now)["raw_files_removed"] == 2
    assert not old.exists() and not materialized.exists()
    assert all(p.exists() for p in (fresh, evidence, memory, outside, link))


def test_worker_budget_stops_before_process_and_accounts_real_cost(tmp_path, monkeypatch):
    budget = Budget(tmp_path / "budget.db", daily_usd=0.05)
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 10, budget=budget)
    commands = []

    def run(cmd, **kwargs):
        if cmd[1] == "info":
            return subprocess.CompletedProcess(cmd, 0, "test-runtime", "")
        commands.append(cmd)
        event = {"type": "result", "is_error": False, "total_cost_usd": 0.05}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(event) + "\n", "")

    monkeypatch.setattr("work_memory.worker.subprocess.run", run)
    client.call("Synthetic local test; no model called")
    assert "--max-budget-usd" in commands[0]
    assert client.cloud_calls == 1
    with pytest.raises(SpendingLimitExceeded):
        client.call("Must not start")
    assert len(commands) == 1


def test_timeout_retains_budget_even_if_no_cost_was_reported(tmp_path, monkeypatch):
    budget = Budget(tmp_path / "budget.db", daily_usd=0.05)
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 10, budget=budget)

    def timeout(cmd, **kwargs):
        if cmd[1] in {"rm", "info"}:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr("work_memory.worker.subprocess.run", timeout)
    with pytest.raises(TimeoutError):
        client.call("No actual model call")
    assert budget.status()["reserved_or_unknown_calls"] == 1
    assert not budget.status()["can_start_call"]


def test_unavailable_docker_does_not_reserve_cloud_budget(tmp_path, monkeypatch):
    budget = Budget(tmp_path / "budget.db")
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 10, budget=budget)

    def unavailable(cmd, **kwargs):
        assert cmd[1] == "info"
        return subprocess.CompletedProcess(cmd, 1, "", "Socket unavailable")

    monkeypatch.setattr("work_memory.worker.subprocess.run", unavailable)
    with pytest.raises(RuntimeError, match="no cloud budget reserved"):
        client.call("No cloud call")
    assert client.cloud_calls == 0 and budget.status()["total_accounted"] == 0


def test_explicit_daily_limit_change_preserves_charges_and_total_cap(tmp_path):
    budget = Budget(tmp_path / "budget.db")
    rid, _ = budget.reserve("haiku")
    budget.settle(rid, 0.1)
    before = budget.status()
    budget.change_daily_limit(2, reason="Explicit user authorization")
    after = Budget(tmp_path / "budget.db").status()
    assert after["daily_limit"] == 2 and after["total_limit"] == before["total_limit"] == 5
    assert after["authorized_at"] == before["authorized_at"]
    assert after["total_accounted"] == before["total_accounted"]
    with budget.connect() as db:
        row = db.execute("SELECT * FROM policy_changes").fetchone()
    assert row["old_daily_limit"] == 500000 and row["new_daily_limit"] == 2000000
