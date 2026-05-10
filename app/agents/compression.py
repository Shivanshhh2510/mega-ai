import json
from uuid import UUID

from app.agents import BaseAgent
from app.core.context import SharedContext, ContextEntry, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import parse_llm_json
from app.core import ExecutionLogger


class CompressionAgent(BaseAgent):
    agent_type = "compression"
    agent_id = "compression_0"

    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        agent_input = context.to_agent_input("compression")

        user_prompt = json.dumps({
            "entries_to_compress": agent_input.get("entries_to_compress", []),
            "budget_status": budget_manager.get_all_status(),
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
            context_type=ContextType.COMPRESSION,
            payload=parsed,
            token_count=result["tokens_used"],
        ))

        ratio = parsed.get("compression_ratio", "N/A")
        await ExecutionLogger.log(
            job_id=job_id, agent_id=self.agent_id,
            event_type="agent_complete",
            message=f"Compression done: ratio={ratio}",
            token_count=result["tokens_used"],
        )

        return parsed
