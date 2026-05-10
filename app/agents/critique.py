import json
from uuid import UUID

from app.agents import BaseAgent
from app.core.context import SharedContext, ContextEntry, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import parse_llm_json
from app.core import ExecutionLogger


class CritiqueAgent(BaseAgent):
    agent_type = "critique"
    agent_id = "critique_0"

    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        agent_input = context.to_agent_input("critique")

        user_prompt = json.dumps({
            "query": context.original_query,
            "outputs_to_review": agent_input.get("all_outputs", []),
            "tool_results": context.tool_results,
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
            context_type=ContextType.CRITIQUE_RESULT,
            payload=parsed,
            token_count=result["tokens_used"],
        ))

        flagged = len(parsed.get("flagged_spans", []))
        agreement = parsed.get("agreement_rate", "N/A")

        await ExecutionLogger.log(
            job_id=job_id, agent_id=self.agent_id,
            event_type="agent_complete",
            message=f"Critique done: {flagged} spans flagged, agreement={agreement}",
            token_count=result["tokens_used"],
        )

        return parsed
