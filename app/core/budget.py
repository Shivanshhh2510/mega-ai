from dataclasses import dataclass, field
from typing import Dict, Optional
from uuid import UUID

from app.core import ExecutionLogger, logger
from app.core.llm import count_tokens


@dataclass
class AgentBudget:
    agent_id: str
    max_tokens: int
    used_tokens: int = 0
    violated: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    @property
    def utilization(self) -> float:
        return self.used_tokens / self.max_tokens if self.max_tokens > 0 else 0.0


class ContextBudgetManager:
    """
    Tracks token consumption per agent per turn.
    Agents must check remaining budget before adding to context.
    Budget violations are caught and logged, never silently truncated.
    """

    def __init__(self, job_id: UUID):
        self.job_id = job_id
        self.budgets: Dict[str, AgentBudget] = {}
        self.total_tokens_used: int = 0

    def register_agent(self, agent_id: str, max_tokens: int):
        """Agent declares its maximum context budget before execution."""
        self.budgets[agent_id] = AgentBudget(agent_id=agent_id, max_tokens=max_tokens)
        logger.info("budget_registered", agent_id=agent_id, max_tokens=max_tokens)

    def check_remaining(self, agent_id: str) -> int:
        """Any agent can call this to check remaining budget."""
        if agent_id not in self.budgets:
            return 0
        return self.budgets[agent_id].remaining

    def get_budget_status(self, agent_id: str) -> dict:
        """Return full budget status for an agent."""
        if agent_id not in self.budgets:
            return {"error": "agent not registered"}
        b = self.budgets[agent_id]
        return {
            "agent_id": agent_id,
            "max_tokens": b.max_tokens,
            "used_tokens": b.used_tokens,
            "remaining_tokens": b.remaining,
            "utilization": round(b.utilization, 3),
            "violated": b.violated,
        }

    async def consume(self, agent_id: str, text: str) -> dict:
        """
        Record token consumption. Returns status.
        If budget exceeded, logs a policy violation — does NOT silently truncate.
        """
        tokens = count_tokens(text)
        if agent_id not in self.budgets:
            self.register_agent(agent_id, 4000)  # default fallback

        budget = self.budgets[agent_id]
        budget.used_tokens += tokens
        self.total_tokens_used += tokens

        result = {
            "tokens_consumed": tokens,
            "remaining": budget.remaining,
            "violated": False,
            "needs_compression": False,
        }

        if budget.used_tokens > budget.max_tokens:
            budget.violated = True
            result["violated"] = True
            result["needs_compression"] = True
            result["overflow_tokens"] = budget.used_tokens - budget.max_tokens

            await ExecutionLogger.log(
                job_id=self.job_id,
                agent_id=agent_id,
                event_type="budget_violation",
                message=f"Agent {agent_id} exceeded budget: {budget.used_tokens}/{budget.max_tokens} tokens",
                token_count=tokens,
                policy_violation=f"context_overflow:{budget.used_tokens - budget.max_tokens}_tokens",
            )
        elif budget.utilization > 0.85:
            result["needs_compression"] = True

        return result

    def get_all_status(self) -> dict:
        """Return budget status for all agents."""
        return {
            "total_tokens_used": self.total_tokens_used,
            "agents": {
                agent_id: self.get_budget_status(agent_id)
                for agent_id in self.budgets
            }
        }
