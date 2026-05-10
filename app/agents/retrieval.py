import json
from uuid import UUID

from app.agents import BaseAgent
from app.core.context import SharedContext, ContextEntry, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import parse_llm_json
from app.core import ExecutionLogger
from app.tools.registry import ToolRegistry


class RetrievalAgent(BaseAgent):
    agent_type = "retrieval"
    agent_id = "retrieval_0"

    def __init__(self):
        super().__init__()
        self.tool_registry = ToolRegistry()

    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        agent_input = context.to_agent_input("retrieval")
        sub_tasks = agent_input.get("sub_tasks", [])

        # Determine which tools to call based on sub-tasks
        tool_calls_needed = self._plan_tool_calls(sub_tasks, context.original_query)

        # Execute tools
        tool_outputs = []
        for tc in tool_calls_needed:
            result = await self.tool_registry.execute_with_retry(
                tool_name=tc["tool"],
                input_data=tc["input"],
                job_id=job_id,
                execution_id=job_id,
            )
            tool_output = {
                "tool": tc["tool"],
                "task_ref": tc.get("task_ref", ""),
                "result": result.to_dict(),
            }
            tool_outputs.append(tool_output)
            context.add_tool_result(tool_output)

        # Call LLM to do multi-hop reasoning over retrieved chunks
        user_prompt = json.dumps({
            "query": context.original_query,
            "sub_tasks": [e.payload for e in context.get_entries_by_type(ContextType.SUB_TASKS)],
            "tool_outputs": tool_outputs,
        })

        result = await self._call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            budget_manager=budget_manager,
            job_id=job_id,
        )

        parsed = parse_llm_json(result["content"])

        context.add_entry(ContextEntry(
            agent_id=self.agent_id,
            context_type=ContextType.RETRIEVAL_RESULT,
            payload=parsed,
            token_count=result["tokens_used"],
        ))

        await ExecutionLogger.log(
            job_id=job_id, agent_id=self.agent_id,
            event_type="agent_complete",
            message=f"Retrieved {len(parsed.get('chunks_used', []))} chunks, {len(parsed.get('reasoning_chain', []))} reasoning steps",
            token_count=result["tokens_used"],
        )

        return parsed

    def _plan_tool_calls(self, sub_tasks: list, query: str) -> list:
        """Decide which tools to call based on sub-task types."""
        calls = []
        if sub_tasks:
            for st_group in sub_tasks:
                for task in st_group.get("sub_tasks", []):
                    for tool in task.get("required_tools", []):
                        calls.append({
                            "tool": tool,
                            "input": {"query": task.get("description", query)},
                            "task_ref": task.get("id", ""),
                        })
        if not calls:
            # Default: search with the original query
            calls.append({
                "tool": "web_search",
                "input": {"query": query},
                "task_ref": "default",
            })
        return calls
