"""Core-seam tests: HermesApi.handle dispatch for P3 spec-schedule actions.

Contract (from engine/api.py + engine/spec_scheduler.py):
  spec_list   -> {sheets: [filename,...], sheets_dir}
  spec_show   -> {name: source_path, jobs:[...], chains:[...]}
  spec_add    -> {sheet, path, added_job, job_count}
  spec_run    -> JobOutcome (job) or ChainOutcome (chain); sheet=source_path
  spec_status -> {sheets: [filenames], last_run, next_due, sheets_dir}
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from engine import spec_scheduler as ss
from engine.api import HermesApi


def _mk_sheet(base, name, jobs=1, chain=False):
    d = os.path.join(base, "spec-sheets")
    os.makedirs(d, exist_ok=True)
    body = {
        "name": name,
        "jobs": [{"name": "j" + str(i), "workflow": "stub.py", "schedule": "manual"}
                 for i in range(jobs)],
        "chains": ([{"name": "all",
                     "jobs": ["j" + str(i) for i in range(jobs)],
                     "fail_strategy": "halt"}]
                   if (chain and jobs >= 2) else []),
    }
    p = os.path.join(d, name + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return p


def _stub_executor(fail=False):
    def _ex(spec):
        if fail:
            raise RuntimeError("stub boom")
        return "run-" + getattr(spec, "name", "x")
    return _ex


@pytest.fixture()
def data_dir(tmp_path):
    d = str(tmp_path / "data")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture(autouse=True)
def _stub_core(monkeypatch):
    monkeypatch.setattr(ss, "_default_executor_for",
                        lambda rs=None: _stub_executor(fail=False))


# ---- spec_list ------------------------------------------------------------
def test_spec_list_returns_filenames_and_dir(data_dir):
    _mk_sheet(data_dir, "daily")
    _mk_sheet(data_dir, "nightly")
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_list"})
    assert isinstance(out["sheets"], list)
    assert set(out["sheets"]) >= {"daily.json", "nightly.json"}
    assert out["sheets_dir"].endswith("spec-sheets")


def test_spec_list_empty_dir(data_dir):
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_list"})
    assert out["sheets"] == []


# ---- spec_add -------------------------------------------------------------
def test_spec_add_creates_sheet_with_one_job(data_dir):
    api = HermesApi(data_dir=data_dir)
    out = api.handle({
        "action": "spec_add",
        "name": "brand-new",
        "job": "j1",
        "workflow": "w.py",
        "schedule": "interval:60",
        "goal": "keep-alive",
        "provider": "stub",
        "model_name": "m1",
        "require_approval": False,
    })
    # real api.py line 171: {"sheet": name, "path": str(path), "added_job": job_name, "job_count": ...}
    assert out["sheet"] == "brand-new"
    assert out["path"].endswith("brand-new.json")
    assert out["added_job"] == "j1"
    assert out["job_count"] == 1
    p = os.path.join(data_dir, "spec-sheets", "brand-new.json")
    assert os.path.isfile(p), f"missing {p}"
    body = json.loads(open(p, encoding="utf-8").read())
    assert "j1" in [j["name"] for j in body["jobs"]]
    j1 = next(j for j in body["jobs"] if j["name"] == "j1")
    assert j1["workflow"] == "w.py"
    assert j1["schedule"] == "interval:60"
    assert j1.get("goal") == "keep-alive"


def test_spec_add_replaces_same_job_name(data_dir):
    _mk_sheet(data_dir, "daily", jobs=1)
    api = HermesApi(data_dir=data_dir)
    out1 = api.handle({"action": "spec_add", "name": "daily", "job": "j0",
                       "workflow": "one.py", "schedule": "manual"})
    out2 = api.handle({"action": "spec_add", "name": "daily", "job": "j0",
                       "workflow": "two.py", "schedule": "manual"})
    assert out1["job_count"] == out2["job_count"]
    p = os.path.join(data_dir, "spec-sheets", "daily.json")
    body = json.loads(open(p).read())
    j0 = next(j for j in body["jobs"] if j["name"] == "j0")
    assert j0["workflow"] == "two.py"


# ---- spec_show ------------------------------------------------------------
def test_spec_show_returns_source_path_and_jobs(data_dir):
    _mk_sheet(data_dir, "showme", jobs=2, chain=True)
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_show", "name": "showme"})
    assert out["name"].endswith("showme.json")
    assert len(out["jobs"]) == 2
    assert out["chains"][0]["name"] == "all"
    assert out["chains"][0]["jobs"] == ["j0", "j1"]


def test_spec_show_missing_raises(data_dir):
    api = HermesApi(data_dir=data_dir)
    with pytest.raises(Exception):
        api.handle({"action": "spec_show", "name": "does-not-exist"})


# ---- spec_run (job) -------------------------------------------------------
def test_spec_run_single_job_succeeds(data_dir):
    _mk_sheet(data_dir, "runme", jobs=1)
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_run", "sheet": "runme", "job": "j0"})
    assert out["job"] == "j0"
    assert out["status"] == "COMPLETED"
    assert out["run_id"].startswith("run-")
    assert out["sheet"].endswith("runme.json")


def test_spec_run_missing_job_is_invalid(data_dir):
    _mk_sheet(data_dir, "nobody", jobs=1)
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_run", "sheet": "nobody", "job": "ghost"})
    assert out["status"] == "INVALID"
    assert "unknown job" in (out.get("reason") or "")


# ---- spec_run (chain) -----------------------------------------------------
def test_spec_run_chain_succeeds(data_dir):
    _mk_sheet(data_dir, "chainme", jobs=2, chain=True)
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_run", "sheet": "chainme", "chain": "all"})
    assert out["chain"] == "all"
    assert out["halted_at"] is None
    assert len(out["jobs"]) == 2
    assert all(j["status"] == "COMPLETED" for j in out["jobs"])
    assert out["sheet"].endswith("chainme.json")


def test_spec_run_chain_missing_raises(data_dir):
    _mk_sheet(data_dir, "c", jobs=2)
    api = HermesApi(data_dir=data_dir)
    with pytest.raises(Exception):
        api.handle({"action": "spec_run", "sheet": "c", "chain": "nope"})


# ---- spec_status ----------------------------------------------------------
def test_spec_status_shape(data_dir):
    _mk_sheet(data_dir, "s1")
    _mk_sheet(data_dir, "s2")
    api = HermesApi(data_dir=data_dir)
    out = api.handle({"action": "spec_status"})
    assert set(out["sheets"]) == {"s1.json", "s2.json"}
    assert "last_run" in out
    assert "next_due" in out
    assert out["sheets_dir"].endswith("spec-sheets")
