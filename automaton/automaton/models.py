"""Typed dataclasses for the five entities. Plain data — persistence lives in db.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class WorkflowDef:
    id: Optional[int]
    name: str
    version: int
    spec: dict[str, Any]  # parsed JSON/YAML spec
    created_at: Optional[str] = None


@dataclass
class Run:
    id: Optional[int]
    workflow_def_id: int
    status: str  # pending | running | completed | failed | cancelled
    trigger_kind: str  # manual | cron | webhook
    trigger_payload: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass
class Step:
    id: Optional[int]
    run_id: int
    name: str
    attempt: int
    status: str  # pending | running | completed | failed | skipped
    idempotency_key: str
    input: Optional[dict[str, Any]] = None
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass
class QueueEntry:
    step_id: int
    ready_at: str
    leased_by: Optional[str] = None
    leased_until: Optional[str] = None


@dataclass
class Event:
    id: Optional[int]
    run_id: int
    ts: Optional[str]
    kind: str
    payload: Optional[dict[str, Any]] = None
