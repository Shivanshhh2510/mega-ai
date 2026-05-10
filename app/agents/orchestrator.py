import json
from uuid import UUID, uuid4
from typing import AsyncGenerator, Optional

from sqlalchemy import text

from app.agents import BaseAgent
from app.agents.decomposition import DecompositionAgent
from app.agents.retrieval import RetrievalAgent
from app.agents.critique import CritiqueAgent
from app.agents.synthesis import SynthesisAgent
from app.agents.compression import CompressionAgent
from app.core.context import SharedContext, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import parse_llm_json
from app.core import ExecutionLogger, Timer
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.schemas import SSEEventType

settings = get_settings()


class OrchestratorAgent(BaseAgent):
    agent_type = "orchestrator"
    agent_id = "orchestrator_0"

    def __init__(self):
        super().__init__()
        self.agents = {
            "decomposition": DecompositionAgent(),
            "retrieval": RetrievalAgent(),
            "critique": CritiqueAgent(),
            "synthesis": SynthesisAgent(),
            "compression": CompressionAgent(),
        }

    async def execute(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> dict:
        """Dynamically route the query through sub-agents."""
        # Step 1: Analyze query and decide routing
        routing_plan = await self._plan_routing(context, budget_manager, job_id, system_prompt)

        context.add_routing_decision(routing_plan)

        # Step 2: Load prompts from DB
        prompts = await self._load_active_prompts()

        # Step 3: Execute agents in dependency order
        executed = set()
        results = {}

        for step in routing_plan.get("routing_plan", []):
            agent_name = step["agent"]
            deps = step.get("depends_on", [])

            # Wait for dependencies
            if not all(d in executed for d in deps):
                await ExecutionLogger.log(
                    job_id=job_id, agent_id=self.agent_id,
                    event_type="routing_skip",
                    message=f"Skipping {agent_name}: unmet dependencies {deps}",
                )
                continue

            if agent_name not in self.agents:
                continue

            agent = self.agents[agent_name]
            agent_prompt = prompts.get(agent_name, "You are a helpful assistant. Respond with JSON.")

            # Register budget
            budget_amount = step.get("context_budget", getattr(settings, f"{agent_name}_budget", 2000))
            budget_manager.register_agent(agent.agent_id, budget_amount)

            # Execute
            with Timer() as t:
                try:
                    agent_result = await agent.execute(context, budget_manager, job_id, agent_prompt)
                    results[agent_name] = agent_result
                    executed.add(agent_name)
                except Exception as e:
                    await ExecutionLogger.log(
                        job_id=job_id, agent_id=agent.agent_id,
                        event_type="agent_error",
                        message=f"Agent {agent_name} failed: {e}",
                    )
                    results[agent_name] = {"error": str(e)}

            # Check if compression needed
            budget_status = budget_manager.get_all_status()
            needs_compression = any(
                a.get("utilization", 0) > 0.85
                for a in budget_status.get("agents", {}).values()
            )
            if needs_compression and "compression" not in executed and agent_name != "compression":
                comp_agent = self.agents["compression"]
                comp_prompt = prompts.get("compression", "Compress the context. Respond with JSON.")
                budget_manager.register_agent(comp_agent.agent_id, settings.compression_budget)
                try:
                    await comp_agent.execute(context, budget_manager, job_id, comp_prompt)
                    executed.add("compression")
                except Exception as e:
                    await ExecutionLogger.log(
                        job_id=job_id, agent_id=comp_agent.agent_id,
                        event_type="compression_error", message=str(e),
                    )

        # Extract final answer
        synthesis_result = context.get_latest_entry(ContextType.SYNTHESIS_RESULT)
        final_answer = ""
        if synthesis_result:
            final_answer = synthesis_result.payload.get("final_answer", "")

        return {
            "final_answer": final_answer,
            "routing_plan": routing_plan,
            "agent_results": {k: str(v)[:500] for k, v in results.items()},
            "budget_summary": budget_manager.get_all_status(),
            "context_summary": {
                "total_entries": len(context.entries),
                "tool_calls": len(context.tool_results),
            },
        }

    async def execute_streaming(
        self,
        context: SharedContext,
        budget_manager: ContextBudgetManager,
        job_id: UUID,
        system_prompt: str,
    ) -> AsyncGenerator[dict, None]:
        """Execute with SSE event streaming."""
        # Plan routing
        yield {"type": SSEEventType.ROUTING_DECISION, "data": {"status": "planning"}}

        routing_plan = await self._plan_routing(context, budget_manager, job_id, system_prompt)
        context.add_routing_decision(routing_plan)

        yield {"type": SSEEventType.ROUTING_DECISION, "data": routing_plan}

        prompts = await self._load_active_prompts()
        executed = set()

        for step in routing_plan.get("routing_plan", []):
            agent_name = step["agent"]
            deps = step.get("depends_on", [])

            if not all(d in executed for d in deps):
                continue
            if agent_name not in self.agents:
                continue

            agent = self.agents[agent_name]
            agent_prompt = prompts.get(agent_name, "You are a helpful assistant. Respond with JSON.")

            budget_amount = step.get("context_budget", getattr(settings, f"{agent_name}_budget", 2000))
            budget_manager.register_agent(agent.agent_id, budget_amount)

            yield {"type": SSEEventType.AGENT_START, "data": {"agent": agent_name}}

            with Timer() as t:
                try:
                    result = await agent.execute(context, budget_manager, job_id, agent_prompt)
                    executed.add(agent_name)
                    yield {
                        "type": SSEEventType.AGENT_COMPLETE,
                        "data": {"agent": agent_name, "latency_ms": t.elapsed_ms,
                                 "summary": str(result)[:300]},
                    }
                except Exception as e:
                    yield {
                        "type": SSEEventType.ERROR,
                        "data": {"agent": agent_name, "error": str(e)},
                    }

            yield {
                "type": SSEEventType.BUDGET_UPDATE,
                "data": budget_manager.get_all_status(),
            }

        # Final result — stream token by token
        synthesis_result = context.get_latest_entry(ContextType.SYNTHESIS_RESULT)
        final_answer = synthesis_result.payload.get("final_answer", "") if synthesis_result else ""

        # Stream final answer token by token
        if final_answer:
            words = final_answer.split(" ")
            for i, word in enumerate(words):
                token = word + (" " if i < len(words) - 1 else "")
                yield {
                    "type": SSEEventType.AGENT_TOKEN,
                    "data": {"agent": "synthesis", "token": token},
                }

        yield {
            "type": SSEEventType.JOB_COMPLETE,
            "data": {
                "final_answer": final_answer,
                "budget_summary": budget_manager.get_all_status(),
            },
        }

    async def _plan_routing(
        self, context: SharedContext, budget_manager: ContextBudgetManager,
        job_id: UUID, system_prompt: str,
    ) -> dict:
        """Use LLM to dynamically decide agent routing."""
        from app.tools.registry import ToolRegistry
        registry = ToolRegistry()

        user_prompt = json.dumps({
            "query": context.original_query,
            "available_agents": list(self.agents.keys()),
            "available_tools": registry.list_tools(),
            "context_budgets": {
                "orchestrator": settings.orchestrator_budget,
                "decomposition": settings.decomposition_budget,
                "retrieval": settings.retrieval_budget,
                "critique": settings.critique_budget,
                "synthesis": settings.synthesis_budget,
                "compression": settings.compression_budget,
            },
        })

        budget_manager.register_agent(self.agent_id, settings.orchestrator_budget)

        result = await self._call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            budget_manager=budget_manager,
            job_id=job_id,
        )

        parsed = parse_llm_json(result["content"])

        # Ensure critique always runs before synthesis
        plan = parsed.get("routing_plan", [])
        agent_names = [s["agent"] for s in plan]
        if "synthesis" in agent_names and "critique" not in agent_names:
            synth_idx = agent_names.index("synthesis")
            plan.insert(synth_idx, {
                "agent": "critique",
                "reason": "Auto-inserted: critique must run before synthesis",
                "context_budget": settings.critique_budget,
                "depends_on": [s["agent"] for s in plan[:synth_idx]],
            })
            parsed["routing_plan"] = plan

        return parsed

    async def _load_active_prompts(self) -> dict:
        """Load active prompt versions from DB."""
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("SELECT agent_type, prompt_text FROM prompt_versions WHERE is_active = true")
                )
                rows = result.fetchall()
                return {row[0]: row[1] for row in rows}
        except Exception:
            return {}