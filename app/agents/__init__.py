from abc import ABC, abstractmethod
from typing import Optional, AsyncGenerator
from uuid import UUID

from app.core.context import SharedContext
from app.core.budget import ContextBudgetManager
from app.core.llm import LLMClient, count_tokens, parse_llm_json
from app.core import ExecutionLogger, compute_hash, Timer
from app.config import get_settings

import json

settings = get_settings()


class BaseAgent(ABC):
    """Base class for all agents in the multi-agent pipeline."""

    agent_type: str = "base"
    agent_id: str = "base_0"

    def __init__(self):
        self.llm = LLMClient()

    @abstractmethod
    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        """Execute the agent's logic. Returns output dict."""
        pass

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        json_mode: bool = True,
    ) -> dict:
        """Call LLM with budget tracking and logging."""
        # Check budget before calling
        input_tokens = count_tokens(system_prompt + user_prompt)
        remaining = budget_manager.check_remaining(self.agent_id)

        if input_tokens > remaining:
            await ExecutionLogger.log(
                job_id=job_id,
                agent_id=self.agent_id,
                event_type="budget_warning",
                message=f"Input ({input_tokens} tokens) exceeds remaining budget ({remaining} tokens)",
                token_count=input_tokens,
            )

        with Timer() as t:
            result = await self.llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=json_mode,
            )

        # Track consumption
        full_text = system_prompt + user_prompt + result["content"]
        await budget_manager.consume(self.agent_id, full_text)

        # Log execution
        await ExecutionLogger.log(
            job_id=job_id,
            agent_id=self.agent_id,
            event_type="llm_call",
            input_hash=compute_hash(user_prompt),
            output_hash=compute_hash(result["content"]),
            latency_ms=t.elapsed_ms,
            token_count=result["tokens_used"],
        )

        return result
