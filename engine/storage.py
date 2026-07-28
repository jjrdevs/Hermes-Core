from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Artifact, Event, Tool, WorkflowDefinition


class SQLiteEventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
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
        self.connection.execute(
            "INSERT INTO events (event_id, event_type, timestamp, workflow_id, execution_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (event.event_id, event.event_type, event.timestamp, event.workflow_id, event.execution_id, json.dumps(event.payload, sort_keys=True)),
        )
        self.connection.commit()

    def all_events(self) -> List[Event]:
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
            "INSERT INTO tools (tool_id, name, description, actions, allowed_roles, metadata, tool_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
        self.connection.execute(
            "INSERT INTO runs (run_id, payload) VALUES (?, ?)",
            (run_id, json.dumps(payload, sort_keys=True)),
        )
        self.connection.commit()

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.connection.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def list(self) -> List[Dict[str, Any]]:
        cursor = self.connection.execute("SELECT payload FROM runs ORDER BY run_id ASC")
        return [json.loads(row["payload"]) for row in cursor]

    def update(self, run_id: str, payload: Dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE runs SET payload = ? WHERE run_id = ?",
            (json.dumps(payload, sort_keys=True), run_id),
        )
        self.connection.commit()

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
