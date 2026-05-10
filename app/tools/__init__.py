from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass
from enum import Enum


class ToolStatus(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    EMPTY_RESULT = "empty_result"
    MALFORMED_INPUT = "malformed_input"


@dataclass
class ToolResult:
    """Standard result from any tool, including failure information."""
    tool_name: str
    status: ToolStatus
    data: Any = None
    error_message: Optional[str] = None
    latency_ms: int = 0
    retry_suggested: bool = False
    retry_hint: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "status": self.status.value,
            "data": self.data,
            "error_message": self.error_message,
            "latency_ms": self.latency_ms,
            "retry_suggested": self.retry_suggested,
            "retry_hint": self.retry_hint,
        }


class BaseTool(ABC):
    """
    Base class for all tools.
    Every tool defines its failure contract via the FAILURE_CONTRACTS dict.
    """
    name: str = "base_tool"
    description: str = "Base tool"

    # Failure contracts: what happens on each failure mode
    FAILURE_CONTRACTS: Dict[ToolStatus, dict] = {
        ToolStatus.TIMEOUT: {
            "action": "return_partial_or_empty",
            "retry": True,
            "message": "Tool timed out",
        },
        ToolStatus.EMPTY_RESULT: {
            "action": "return_empty_with_suggestion",
            "retry": True,
            "message": "No results found",
        },
        ToolStatus.MALFORMED_INPUT: {
            "action": "return_error_with_schema",
            "retry": False,
            "message": "Input validation failed",
        },
        ToolStatus.ERROR: {
            "action": "return_error",
            "retry": True,
            "message": "Internal tool error",
        },
    }

    @abstractmethod
    async def execute(self, input_data: dict) -> ToolResult:
        """Execute the tool with given input."""
        pass

    @abstractmethod
    def validate_input(self, input_data: dict) -> tuple[bool, Optional[str]]:
        """Validate input before execution. Returns (is_valid, error_message)."""
        pass

    def get_failure_contract(self, status: ToolStatus) -> dict:
        """Return the failure contract for a given status."""
        return self.FAILURE_CONTRACTS.get(status, self.FAILURE_CONTRACTS[ToolStatus.ERROR])

    def get_schema(self) -> dict:
        """Return tool schema for LLM consumption."""
        return {
            "name": self.name,
            "description": self.description,
            "failure_contracts": {
                k.value: v for k, v in self.FAILURE_CONTRACTS.items()
            }
        }
