from typing import Dict, Optional
from uuid import UUID

from app.tools import BaseTool, ToolResult, ToolStatus
from app.tools.web_search import WebSearchTool
from app.tools.code_execution import CodeExecutionTool
from app.tools.database_lookup import DatabaseLookupTool
from app.tools.self_reflection import SelfReflectionTool
from app.core import ExecutionLogger, compute_hash, Timer
from app.config import get_settings

import json

settings = get_settings()


class ToolRegistry:
    """
    Manages all tools, handles execution with retry logic, and logs everything.
    Fallback logic is explicit in code, not in prompts.
    """

    def __init__(self):
        self.tools: Dict[str, BaseTool] = {
            "web_search": WebSearchTool(),
            "code_execution": CodeExecutionTool(),
            "database_lookup": DatabaseLookupTool(),
            "self_reflection": SelfReflectionTool(),
        }

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self.tools.get(name)

    def list_tools(self) -> list:
        return [tool.get_schema() for tool in self.tools.values()]

    async def execute_with_retry(
        self,
        tool_name: str,
        input_data: dict,
        job_id: UUID,
        execution_id: UUID,
        max_retries: int = None,
    ) -> ToolResult:
        """
        Execute a tool with explicit retry logic.
        - On timeout: retry with same input
        - On empty_result: retry with modified hint if available
        - On malformed_input: NO retry
        - On error: retry up to max
        Each retry is logged separately.
        """
        if max_retries is None:
            max_retries = settings.tool_max_retries

        tool = self.get_tool(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                status=ToolStatus.ERROR,
                error_message=f"Unknown tool: {tool_name}",
            )

        last_result = None
        retry_of_id = None

        for attempt in range(max_retries + 1):
            with Timer() as t:
                result = await tool.execute(input_data)
                result.latency_ms = t.elapsed_ms

            # Log the tool call
            input_str = json.dumps(input_data, default=str)
            output_str = json.dumps(result.to_dict(), default=str)

            await ExecutionLogger.log(
                job_id=job_id,
                agent_id=f"tool:{tool_name}",
                event_type="tool_call",
                message=f"Attempt {attempt + 1}/{max_retries + 1}: {result.status.value}",
                input_hash=compute_hash(input_str),
                output_hash=compute_hash(output_str),
                latency_ms=result.latency_ms,
                metadata={
                    "tool_name": tool_name,
                    "attempt": attempt + 1,
                    "status": result.status.value,
                    "input": input_data,
                    "retry_of": str(retry_of_id) if retry_of_id else None,
                },
            )

            last_result = result

            # Decide whether to retry based on failure contract
            if result.status == ToolStatus.SUCCESS:
                break

            if result.status == ToolStatus.MALFORMED_INPUT:
                # Never retry malformed input
                break

            if attempt >= max_retries:
                break

            contract = tool.get_failure_contract(result.status)
            if not contract.get("retry", False):
                break

            # Modify input for retry if hint available
            if result.retry_hint and result.status == ToolStatus.EMPTY_RESULT:
                # Apply hint to modify query
                if "query" in input_data:
                    input_data = {**input_data, "query": f"{input_data['query']} (refined)"}

            retry_of_id = execution_id

        return last_result
