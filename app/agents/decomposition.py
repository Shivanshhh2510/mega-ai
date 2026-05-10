import json
from uuid import UUID

from app.agents import BaseAgent
from app.core.context import SharedContext, ContextEntry, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import parse_llm_json
from app.core import ExecutionLogger


class DecompositionAgent(BaseAgent):
    agent_type = "decomposition"
    agent_id = "decomposition_0"

    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        """Break the query into typed sub-tasks with dependency graphs."""

        agent_input = context.to_agent_input("decomposition")
        user_prompt = json.dumps({
            "query": context.original_query,
            "previous_decompositions": agent_input.get("previous_decompositions", []),
        })

        result = await self._call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            budget_manager=budget_manager,
            job_id=job_id,
        )

        parsed = parse_llm_json(result["content"])

        # Validate dependency graph
        if "sub_tasks" in parsed:
            task_ids = {t["id"] for t in parsed["sub_tasks"]}
            for task in parsed["sub_tasks"]:
                for dep in task.get("depends_on", []):
                    if dep not in task_ids:
                        await ExecutionLogger.log(
                            job_id=job_id,
                            agent_id=self.agent_id,
                            event_type="validation_warning",
                            message=f"Task {task['id']} depends on non-existent task {dep}",
                        )

        # Add to shared context
        context.add_entry(ContextEntry(
            agent_id=self.agent_id,
            context_type=ContextType.SUB_TASKS,
            payload=parsed,
            token_count=result["tokens_used"],
        ))

        await ExecutionLogger.log(
            job_id=job_id,
            agent_id=self.agent_id,
            event_type="agent_complete",
            message=f"Decomposed into {len(parsed.get('sub_tasks', []))} sub-tasks",
            token_count=result["tokens_used"],
        )

        return parsed
