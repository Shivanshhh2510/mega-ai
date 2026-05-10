import json
from uuid import UUID

from app.agents import BaseAgent
from app.core.context import SharedContext, ContextEntry, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import parse_llm_json
from app.core import ExecutionLogger


class SynthesisAgent(BaseAgent):
    agent_type = "synthesis"
    agent_id = "synthesis_0"

    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        agent_input = context.to_agent_input("synthesis")

        user_prompt = json.dumps({
            "query": context.original_query,
            "all_outputs": agent_input.get("all_outputs", []),
            "critique_results": agent_input.get("critique_results", []),
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
            context_type=ContextType.SYNTHESIS_RESULT,
            payload=parsed,
            token_count=result["tokens_used"],
        ))

        contradictions = len(parsed.get("contradictions_resolved", []))
        await ExecutionLogger.log(
            job_id=job_id, agent_id=self.agent_id,
            event_type="agent_complete",
            message=f"Synthesis done: {contradictions} contradictions resolved",
            token_count=result["tokens_used"],
        )

        return parsed
