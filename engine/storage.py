from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Artifact, Event, Tool, WorkflowDefinition
from .scheduler import SQLiteJobStore


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class SQLiteEventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                execution_id TEXT,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def append(self, event: Event) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO events (event_id, event_type, timestamp, workflow_id, execution_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (event.event_id, event.event_type, event.timestamp, event.workflow_id, event.execution_id, json.dumps(event.payload, sort_keys=True)),
            )
            self.connection.commit()

    def all_events(self) -> List[Event]:
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM events ORDER BY sequence ASC")
            return [
                Event(
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    timestamp=row["timestamp"],
                    workflow_id=row["workflow_id"],
                    execution_id=row["execution_id"],
                    payload=json.loads(row["payload"]),
                )
                for row in cursor
            ]

    def get(self, event_id: str) -> Optional[Event]:
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return Event(
            event_id=row["event_id"],
            event_type=row["event_type"],
            timestamp=row["timestamp"],
            workflow_id=row["workflow_id"],
            execution_id=row["execution_id"],
            payload=json.loads(row["payload"]),
        )

    def close(self) -> None:
        self.connection.close()


class SQLiteWorkflowDefinitionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_definitions (
                workflow_definition_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                definition_version INTEGER NOT NULL,
                definition_hash TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def add(self, workflow_definition: WorkflowDefinition) -> None:
        self.connection.execute(
            "INSERT INTO workflow_definitions (workflow_definition_id, workflow_id, name, created_at, definition_version, definition_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                workflow_definition.workflow_definition_id,
                workflow_definition.workflow_id,
                workflow_definition.name,
                workflow_definition.created_at,
                workflow_definition.definition_version,
                workflow_definition.compute_definition_hash(),
                json.dumps(workflow_definition.to_dict(), sort_keys=True),
            ),
        )
        self.connection.commit()

    def get(self, workflow_definition_id: str) -> Optional[WorkflowDefinition]:
        cursor = self.connection.execute("SELECT payload FROM workflow_definitions WHERE workflow_definition_id = ?", (workflow_definition_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return WorkflowDefinition.from_dict(payload)

    def list(self) -> List[WorkflowDefinition]:
        cursor = self.connection.execute("SELECT payload FROM workflow_definitions ORDER BY created_at ASC, workflow_id ASC")
        return [WorkflowDefinition.from_dict(json.loads(row["payload"])) for row in cursor]

    def close(self) -> None:
        self.connection.close()


class SQLiteToolStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tools (
                tool_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                actions TEXT NOT NULL,
                allowed_roles TEXT NOT NULL,
                metadata TEXT NOT NULL,
                tool_hash TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def add(self, tool: Tool) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO tools (tool_id, name, description, actions, allowed_roles, metadata, tool_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tool.tool_id,
                tool.name,
                tool.description,
                json.dumps(tool.actions, sort_keys=True),
                json.dumps(tool.allowed_roles, sort_keys=True),
                json.dumps(tool.metadata, sort_keys=True),
                tool.compute_tool_hash(),
                json.dumps(tool.to_dict(), sort_keys=True),
            ),
        )
        self.connection.commit()

    def get(self, tool_id: str) -> Optional[Tool]:
        cursor = self.connection.execute("SELECT payload FROM tools WHERE tool_id = ?", (tool_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return Tool.from_dict(payload)

    def list(self) -> List[Tool]:
        cursor = self.connection.execute("SELECT payload FROM tools ORDER BY tool_id ASC")
        return [Tool.from_dict(json.loads(row["payload"])) for row in cursor]

    def close(self) -> None:
        self.connection.close()


class SQLiteRunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def create(self, run_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO runs (run_id, payload) VALUES (?, ?)",
                (run_id, json.dumps(payload, sort_keys=True)),
            )
            self.connection.commit()

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self.connection.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return json.loads(row["payload"])

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.connection.execute("SELECT payload FROM runs ORDER BY run_id ASC")
            return [json.loads(row["payload"]) for row in cursor]

    def update(self, run_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET payload = ? WHERE run_id = ?",
                (json.dumps(payload, sort_keys=True), run_id),
            )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()


class SQLiteMemoryStore:
    def __init__(self, path: Path, max_entries: Optional[int] = None) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.max_entries = max_entries or 100
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL,
                topic TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    @staticmethod
    def _sanitize_content(content: str) -> str:
        sanitized = content
        for pattern in [r"sk-[A-Za-z0-9_-]+", r"api[_-]?key[=:][A-Za-z0-9._-]+", r"token[=:][A-Za-z0-9._-]+", r"secret[=:][A-Za-z0-9._-]+"]:
            sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
        return sanitized

    @staticmethod
    def _normalise_metadata(metadata: Optional[Dict[str, Any]], source: str) -> Dict[str, Any]:
        normalised = dict(metadata or {})
        if not isinstance(normalised, dict):
            normalised = {}
        if "provenance" not in normalised:
            normalised["provenance"] = source
        if "confidence" in normalised and normalised["confidence"] is not None:
            try:
                normalised["confidence"] = float(normalised["confidence"])
            except (TypeError, ValueError):
                normalised.pop("confidence", None)
        return normalised

    @staticmethod
    def _parse_expiry(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                try:
                    return datetime.fromtimestamp(float(candidate), tz=timezone.utc)
                except ValueError:
                    return None
        return None

    @classmethod
    def _is_expired(cls, metadata: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(metadata, dict):
            return False
        expires_at = metadata.get("expires_at")
        parsed = cls._parse_expiry(expires_at)
        if parsed is None:
            return False
        return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def _prune_if_needed(self) -> None:
        count = self.connection.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        if count <= self.max_entries:
            return
        rows = self.connection.execute(
            "SELECT memory_id FROM memories ORDER BY created_at ASC LIMIT ?",
            (count - self.max_entries,),
        ).fetchall()
        if not rows:
            return
        self.connection.executemany("DELETE FROM memories WHERE memory_id = ?", [(row["memory_id"],) for row in rows])
        self.connection.commit()

    def add(self, entry_type: str, content: str, topic: str, source: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sanitized_content = self._sanitize_content(content)
        metadata_payload = self._normalise_metadata(metadata, source)
        payload = {
            "memory_id": str(uuid.uuid4()),
            "entry_type": entry_type,
            "content": sanitized_content,
            "topic": topic,
            "source": source,
            "created_at": _now_iso(),
            "metadata": metadata_payload,
        }
        self.connection.execute(
            "INSERT INTO memories (memory_id, entry_type, content, topic, source, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload["memory_id"],
                payload["entry_type"],
                payload["content"],
                payload["topic"],
                payload["source"],
                payload["created_at"],
                json.dumps(payload["metadata"], sort_keys=True),
            ),
        )
        self.connection.commit()
        self._prune_if_needed()
        return payload

    def list(self, topic: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        query = "SELECT * FROM memories"
        params: List[Any] = []
        if topic:
            query += " WHERE topic = ?"
            params.append(topic)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        valid: List[Dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            if self._is_expired(metadata):
                continue
            valid.append(
                {
                    "memory_id": row["memory_id"],
                    "entry_type": row["entry_type"],
                    "content": row["content"],
                    "topic": row["topic"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "metadata": metadata,
                }
            )
        return valid

    def record_usage(self, memory_id: str, outcome: Optional[str] = None) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            "SELECT memory_id, entry_type, content, topic, source, created_at, metadata FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None

        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        if not isinstance(metadata, dict):
            metadata = {}

        usage_count = int(metadata.get("usage_count", 0) or 0) + 1
        metadata["usage_count"] = usage_count
        metadata["last_used_at"] = _now_iso()
        if outcome in {"helpful", "neutral", "harmful"}:
            existing_outcome = str(metadata.get("outcome") or "").lower()
            if existing_outcome not in {"helpful", "neutral", "harmful"}:
                metadata["outcome"] = outcome
            elif outcome == "helpful" and existing_outcome != "helpful":
                metadata["outcome"] = "helpful"
            elif outcome == "harmful" and existing_outcome == "helpful":
                metadata["outcome"] = "helpful"
            elif outcome == "neutral" and existing_outcome == "harmful":
                metadata["outcome"] = "neutral"

        self.connection.execute(
            "UPDATE memories SET metadata = ? WHERE memory_id = ?",
            (json.dumps(metadata, sort_keys=True), memory_id),
        )
        self.connection.commit()

        return {
            "memory_id": row["memory_id"],
            "entry_type": row["entry_type"],
            "content": row["content"],
            "topic": row["topic"],
            "source": row["source"],
            "created_at": row["created_at"],
            "metadata": metadata,
        }

    def close(self) -> None:
        self.connection.close()


class SQLiteArtifactStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                artifact_hash TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                inputs TEXT NOT NULL,
                parent_version INTEGER,
                status TEXT NOT NULL,
                decision_record TEXT NOT NULL,
                metadata TEXT NOT NULL,
                PRIMARY KEY (artifact_id, version)
            )
            """
        )
        self.connection.commit()

    def add(self, artifact: Artifact) -> None:
        self.connection.execute(
            "INSERT INTO artifacts (artifact_id, artifact_type, version, title, content, content_hash, artifact_hash, created_by, created_at, inputs, parent_version, status, decision_record, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.artifact_type,
                artifact.version,
                artifact.title,
                json.dumps(artifact.content, sort_keys=True),
                artifact.content_hash,
                artifact.artifact_hash,
                artifact.created_by,
                artifact.created_at,
                json.dumps(artifact.inputs, sort_keys=True),
                artifact.parent_version,
                artifact.status,
                json.dumps(artifact.decision_record, sort_keys=True),
                json.dumps(artifact.metadata, sort_keys=True),
            ),
        )
        self.connection.commit()

    def get(self, artifact_id: str, version: Optional[int] = None) -> Optional[Artifact]:
        if version is None:
            cursor = self.connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY version DESC LIMIT 1",
                (artifact_id,),
            )
        else:
            cursor = self.connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            )
        row = cursor.fetchone()
        if row is None:
            return None
        return Artifact(
            artifact_id=row["artifact_id"],
            artifact_type=row["artifact_type"],
            version=row["version"],
            title=row["title"],
            content=json.loads(row["content"]),
            content_hash=row["content_hash"],
            artifact_hash=row["artifact_hash"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            inputs=json.loads(row["inputs"]),
            parent_version=row["parent_version"],
            status=row["status"],
            decision_record=json.loads(row["decision_record"]),
            metadata=json.loads(row["metadata"]),
        )

    def list(self) -> List[Artifact]:
        cursor = self.connection.execute("SELECT * FROM artifacts ORDER BY created_at ASC, artifact_id ASC")
        return [
            Artifact(
                artifact_id=row["artifact_id"],
                artifact_type=row["artifact_type"],
                version=row["version"],
                title=row["title"],
                content=json.loads(row["content"]),
                content_hash=row["content_hash"],
                artifact_hash=row["artifact_hash"],
                created_by=row["created_by"],
                created_at=row["created_at"],
                inputs=json.loads(row["inputs"]),
                parent_version=row["parent_version"],
                status=row["status"],
                decision_record=json.loads(row["decision_record"]),
                metadata=json.loads(row["metadata"]),
            )
            for row in cursor
        ]

    def close(self) -> None:
        self.connection.close()
