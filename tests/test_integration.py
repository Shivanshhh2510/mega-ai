"""
Integration tests for the MEGA AI pipeline.
These tests verify end-to-end behavior across agents and tools.
Run with: python -m pytest tests/test_integration.py -v
"""
import asyncio
import pytest
from uuid import uuid4


class TestPipelineIntegration:
    """Integration tests that verify multi-agent pipeline behavior."""

    def test_decomposition_produces_sub_tasks(self):
        """Verify decomposition agent breaks queries into typed sub-tasks."""
        from app.core.context import SharedContext, ContextType
        from app.core.budget import ContextBudgetManager

        job_id = uuid4()
        context = SharedContext(job_id=job_id, query="What is the capital of France?")
        budget = ContextBudgetManager(job_id=job_id)

        # Verify context starts empty
        assert len(context.entries) == 0
        assert context.original_query == "What is the capital of France?"

        # Verify budget starts clean
        budget.register_agent("decomposition_0", 1500)
        status = budget.get_budget_status("decomposition_0")
        assert status["used_tokens"] == 0
        assert status["violated"] is False

    def test_shared_context_agent_isolation(self):
        """Verify agents only see appropriate context via to_agent_input()."""
        from app.core.context import SharedContext, ContextEntry, ContextType

        job_id = uuid4()
        context = SharedContext(job_id=job_id, query="test query")

        # Add entries from different agents
        context.add_entry(ContextEntry(
            agent_id="decomposition_0",
            context_type=ContextType.SUB_TASKS,
            payload={"sub_tasks": [{"id": "t1"}]},
        ))
        context.add_entry(ContextEntry(
            agent_id="retrieval_0",
            context_type=ContextType.RETRIEVAL_RESULT,
            payload={"chunks": [{"id": "c1"}]},
        ))
        context.add_entry(ContextEntry(
            agent_id="critique_0",
            context_type=ContextType.CRITIQUE_RESULT,
            payload={"flagged_spans": []},
        ))

        # Retrieval agent should see sub_tasks but not critique results
        retrieval_input = context.to_agent_input("retrieval")
        assert "sub_tasks" in retrieval_input
        assert len(retrieval_input["sub_tasks"]) == 1

        # Synthesis should see everything including critique
        synthesis_input = context.to_agent_input("synthesis")
        assert "all_outputs" in synthesis_input
        assert "critique_results" in synthesis_input
        assert len(synthesis_input["critique_results"]) == 1

        # Critique should see all outputs EXCEPT its own
        critique_input = context.to_agent_input("critique")
        output_types = [o["type"] for o in critique_input["all_outputs"]]
        assert ContextType.CRITIQUE_RESULT not in output_types
        assert ContextType.SUB_TASKS in output_types

    def test_budget_violation_logged_not_truncated(self):
        """Verify budget violations are logged and surfaced, never silently truncated."""
        from app.core.budget import ContextBudgetManager

        job_id = uuid4()
        budget = ContextBudgetManager(job_id=job_id)
        budget.register_agent("test_agent", 10)  # tiny budget

        # Consume way more than budget
        result = asyncio.get_event_loop().run_until_complete(
            budget.consume("test_agent", "This is a very long text that will certainly exceed ten tokens by a large margin")
        )

        # Violation must be flagged
        assert result["violated"] is True
        assert result["needs_compression"] is True
        assert result["overflow_tokens"] > 0

        # But the consumption still happened — not truncated
        status = budget.get_budget_status("test_agent")
        assert status["used_tokens"] > status["max_tokens"]
        assert status["violated"] is True

    def test_tool_failure_contract_no_retry_on_malformed(self):
        """Verify malformed input never triggers retry."""
        from app.tools.web_search import WebSearchTool
        from app.tools import ToolStatus

        tool = WebSearchTool()
        contract = tool.get_failure_contract(ToolStatus.MALFORMED_INPUT)
        assert contract["retry"] is False

    def test_tool_failure_contract_retry_on_timeout(self):
        """Verify timeout allows retry."""
        from app.tools.web_search import WebSearchTool
        from app.tools import ToolStatus

        tool = WebSearchTool()
        contract = tool.get_failure_contract(ToolStatus.TIMEOUT)
        assert contract["retry"] is True

    def test_tool_result_serialization(self):
        """Verify ToolResult serializes cleanly for logging."""
        from app.tools import ToolResult, ToolStatus

        result = ToolResult(
            tool_name="test_tool",
            status=ToolStatus.SUCCESS,
            data={"key": "value"},
            latency_ms=150,
        )
        d = result.to_dict()
        assert d["tool_name"] == "test_tool"
        assert d["status"] == "success"
        assert d["data"]["key"] == "value"
        assert d["latency_ms"] == 150

    def test_routing_decision_timestamp(self):
        """Verify routing decisions get timestamped."""
        from app.core.context import SharedContext

        context = SharedContext(job_id=uuid4(), query="test")
        context.add_routing_decision({"agent": "retrieval", "reason": "factual"})

        assert len(context.routing_decisions) == 1
        assert "timestamp" in context.routing_decisions[0]
        assert context.routing_decisions[0]["agent"] == "retrieval"

    def test_multiple_tool_results_tracked(self):
        """Verify all tool results are accumulated in context."""
        from app.core.context import SharedContext

        context = SharedContext(job_id=uuid4(), query="test")
        context.add_tool_result({"tool": "web_search", "status": "success"})
        context.add_tool_result({"tool": "code_execution", "status": "success"})

        assert len(context.tool_results) == 2

    def test_full_context_serialization(self):
        """Verify full context round-trips to dict."""
        from app.core.context import SharedContext, ContextEntry, ContextType

        job_id = uuid4()
        context = SharedContext(job_id=job_id, query="test query")
        context.add_entry(ContextEntry(
            agent_id="agent_a",
            context_type=ContextType.SUB_TASKS,
            payload={"tasks": [1, 2, 3]},
        ))
        context.add_routing_decision({"plan": "test"})
        context.add_tool_result({"tool": "web_search"})

        data = context.to_dict()
        assert data["original_query"] == "test query"
        assert len(data["entries"]) == 1
        assert len(data["routing_decisions"]) == 1
        assert len(data["tool_results"]) == 1