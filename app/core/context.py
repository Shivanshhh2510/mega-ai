from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel


class ContextType(str, Enum):
    SUB_TASKS = "sub_tasks"
    RETRIEVAL_RESULT = "retrieval_result"
    CRITIQUE_RESULT = "critique_result"
    SYNTHESIS_RESULT = "synthesis_result"
    TOOL_OUTPUT = "tool_output"
    COMPRESSION = "compression"


class ContextEntry(BaseModel):
    id: str = ""
    agent_id: str
    context_type: ContextType
    payload: dict
    token_count: int = 0
    version: int = 1
    created_at: str = ""

    def __init__(self, **data):
        super().__init__(**data)
        if not self.id:
            self.id = str(uuid4())
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


class SharedContext:
    """
    Shared context object with defined schema.
    All inter-agent communication passes through this.
    Agents must NOT call each other directly.
    The orchestrator mediates all handoffs.
    """

    def __init__(self, job_id: UUID, query: str):
        self.job_id = job_id
        self.original_query = query
        self.entries: List[ContextEntry] = []
        self.routing_decisions: List[dict] = []
        self.tool_results: List[dict] = []
        self._metadata: Dict[str, Any] = {}

    def add_entry(self, entry: ContextEntry):
        """Add an entry to shared context."""
        self.entries.append(entry)

    def get_entries_by_type(self, context_type: ContextType) -> List[ContextEntry]:
        """Retrieve all entries of a given type."""
        return [e for e in self.entries if e.context_type == context_type]

    def get_entries_by_agent(self, agent_id: str) -> List[ContextEntry]:
        """Retrieve all entries from a given agent."""
        return [e for e in self.entries if e.agent_id == agent_id]

    def get_latest_entry(self, context_type: ContextType) -> Optional[ContextEntry]:
        """Get the most recent entry of a type."""
        entries = self.get_entries_by_type(context_type)
        return entries[-1] if entries else None

    def add_routing_decision(self, decision: dict):
        """Record an orchestrator routing decision."""
        self.routing_decisions.append({
            **decision,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    def add_tool_result(self, result: dict):
        """Record a tool call result."""
        self.tool_results.append(result)

    def to_agent_input(self, for_agent: str) -> dict:
        """
        Assemble the context that should be passed to a given agent.
        Each agent gets only what it needs.
        """
        base = {
            "job_id": str(self.job_id),
            "original_query": self.original_query,
            "routing_decisions": self.routing_decisions,
        }

        if for_agent == "decomposition":
            base["previous_decompositions"] = [
                e.payload for e in self.get_entries_by_type(ContextType.SUB_TASKS)
            ]

        elif for_agent == "retrieval":
            base["sub_tasks"] = [
                e.payload for e in self.get_entries_by_type(ContextType.SUB_TASKS)
            ]
            base["tool_results"] = self.tool_results

        elif for_agent == "critique":
            base["all_outputs"] = [
                {"agent": e.agent_id, "type": e.context_type, "payload": e.payload}
                for e in self.entries
                if e.context_type != ContextType.CRITIQUE_RESULT
            ]

        elif for_agent == "synthesis":
            base["all_outputs"] = [
                {"agent": e.agent_id, "type": e.context_type, "payload": e.payload}
                for e in self.entries
            ]
            base["critique_results"] = [
                e.payload for e in self.get_entries_by_type(ContextType.CRITIQUE_RESULT)
            ]

        elif for_agent == "compression":
            base["entries_to_compress"] = [
                {"agent": e.agent_id, "type": e.context_type, "payload": e.payload,
                 "token_count": e.token_count}
                for e in self.entries
            ]

        return base

    def to_dict(self) -> dict:
        """Full serialization for storage."""
        return {
            "job_id": str(self.job_id),
            "original_query": self.original_query,
            "entries": [e.model_dump() for e in self.entries],
            "routing_decisions": self.routing_decisions,
            "tool_results": self.tool_results,
            "metadata": self._metadata,
        }
