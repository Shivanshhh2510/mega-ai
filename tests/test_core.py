"""
Tests for MEGA AI core components.
Run with: python -m pytest tests/ -v
"""
import asyncio
import pytest
import json
from uuid import uuid4


# ============================================================
# Test: Context Budget Manager
# ============================================================

class TestContextBudgetManager:
    """Tests for token budget tracking and violation detection."""

    def setup_method(self):
        from app.core.budget import ContextBudgetManager
        self.job_id = uuid4()
        self.manager = ContextBudgetManager(job_id=self.job_id)

    def test_register_agent(self):
        self.manager.register_agent("test_agent", 1000)
        status = self.manager.get_budget_status("test_agent")
        assert status["max_tokens"] == 1000
        assert status["used_tokens"] == 0
        assert status["remaining_tokens"] == 1000
        assert status["violated"] is False

    def test_check_remaining(self):
        self.manager.register_agent("test_agent", 500)
        remaining = self.manager.check_remaining("test_agent")
        assert remaining == 500

    def test_check_remaining_unregistered(self):
        remaining = self.manager.check_remaining("nonexistent")
        assert remaining == 0

    def test_consume_within_budget(self):
        self.manager.register_agent("test_agent", 10000)
        result = asyncio.get_event_loop().run_until_complete(
            self.manager.consume("test_agent", "hello world")
        )
        assert result["violated"] is False
        assert result["tokens_consumed"] > 0

    def test_consume_exceeds_budget(self):
        self.manager.register_agent("test_agent", 5)  # very small budget
        result = asyncio.get_event_loop().run_until_complete(
            self.manager.consume("test_agent", "This is a long sentence that will definitely exceed five tokens")
        )
        assert result["violated"] is True
        assert result["needs_compression"] is True
        assert "overflow_tokens" in result

    def test_compression_trigger_at_85_percent(self):
        self.manager.register_agent("test_agent", 100)
        # Manually set usage to 86%
        self.manager.budgets["test_agent"].used_tokens = 86
        result = asyncio.get_event_loop().run_until_complete(
            self.manager.consume("test_agent", "a")
        )
        assert result["needs_compression"] is True

    def test_get_all_status(self):
        self.manager.register_agent("agent_a", 1000)
        self.manager.register_agent("agent_b", 2000)
        status = self.manager.get_all_status()
        assert "total_tokens_used" in status
        assert "agent_a" in status["agents"]
        assert "agent_b" in status["agents"]

    def test_multiple_agents_independent(self):
        self.manager.register_agent("agent_a", 1000)
        self.manager.register_agent("agent_b", 500)
        asyncio.get_event_loop().run_until_complete(
            self.manager.consume("agent_a", "test input")
        )
        status_b = self.manager.get_budget_status("agent_b")
        assert status_b["used_tokens"] == 0  # agent_b untouched


# ============================================================
# Test: Web Search Tool
# ============================================================

class TestWebSearchTool:
    """Tests for web search tool including failure contracts."""

    def setup_method(self):
        from app.tools.web_search import WebSearchTool
        self.tool = WebSearchTool()

    def test_validate_input_valid(self):
        valid, error = self.tool.validate_input({"query": "capital of France"})
        assert valid is True
        assert error is None

    def test_validate_input_missing_query(self):
        valid, error = self.tool.validate_input({})
        assert valid is False
        assert "query" in error

    def test_validate_input_empty_query(self):
        valid, error = self.tool.validate_input({"query": ""})
        assert valid is False

    def test_validate_input_too_long(self):
        valid, error = self.tool.validate_input({"query": "x" * 501})
        assert valid is False
        assert "500" in error

    def test_execute_success(self):
        from app.tools import ToolStatus
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"query": "capital france"})
        )
        assert result.status == ToolStatus.SUCCESS
        assert result.data["total"] >= 1
        assert len(result.data["results"]) >= 1

    def test_execute_malformed_input(self):
        from app.tools import ToolStatus
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({})
        )
        assert result.status == ToolStatus.MALFORMED_INPUT
        assert result.retry_suggested is False

    def test_execute_empty_result(self):
        from app.tools import ToolStatus
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"query": "xyzzy_nonexistent_query_12345"})
        )
        # Should return low-relevance results
        assert result.status in (ToolStatus.SUCCESS, ToolStatus.EMPTY_RESULT)

    def test_failure_contract_exists(self):
        from app.tools import ToolStatus
        for status in [ToolStatus.TIMEOUT, ToolStatus.EMPTY_RESULT,
                       ToolStatus.MALFORMED_INPUT, ToolStatus.ERROR]:
            contract = self.tool.get_failure_contract(status)
            assert "action" in contract
            assert "retry" in contract
            assert "message" in contract

    def test_schema_output(self):
        schema = self.tool.get_schema()
        assert schema["name"] == "web_search"
        assert "failure_contracts" in schema


# ============================================================
# Test: Code Execution Tool
# ============================================================

class TestCodeExecutionTool:
    """Tests for sandboxed code execution."""

    def setup_method(self):
        from app.tools.code_execution import CodeExecutionTool
        self.tool = CodeExecutionTool()

    def test_validate_valid_code(self):
        valid, error = self.tool.validate_input({"code": "print('hello')"})
        assert valid is True

    def test_validate_blocked_import_os(self):
        valid, error = self.tool.validate_input({"code": "import os\nos.system('ls')"})
        assert valid is False
        assert "Blocked" in error

    def test_validate_blocked_import_subprocess(self):
        valid, error = self.tool.validate_input({"code": "import subprocess"})
        assert valid is False

    def test_validate_syntax_error(self):
        valid, error = self.tool.validate_input({"code": "def foo(:"})
        assert valid is False
        assert "Syntax" in error

    def test_execute_simple_code(self):
        from app.tools import ToolStatus
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"code": "print(2 + 2)"})
        )
        assert result.status == ToolStatus.SUCCESS
        assert "4" in result.data["stdout"]

    def test_execute_fibonacci(self):
        from app.tools import ToolStatus
        code = """
def fib(n):
    a, b = 0, 1
    result = []
    for _ in range(n):
        result.append(a)
        a, b = b, a + b
    return result

print(fib(10))
"""
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"code": code})
        )
        assert result.status == ToolStatus.SUCCESS
        assert "0, 1, 1, 2, 3, 5" in result.data["stdout"]

    def test_execute_runtime_error(self):
        from app.tools import ToolStatus
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"code": "print(1/0)"})
        )
        assert result.status == ToolStatus.ERROR
        assert "ZeroDivisionError" in result.data["stderr"]

    def test_execute_no_output(self):
        from app.tools import ToolStatus
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"code": "x = 42"})
        )
        assert result.status == ToolStatus.EMPTY_RESULT


# ============================================================
# Test: Tool Registry
# ============================================================

class TestToolRegistry:
    """Tests for tool registry and retry logic."""

    def setup_method(self):
        from app.tools.registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_all_tools_registered(self):
        tools = self.registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "web_search" in tool_names
        assert "code_execution" in tool_names
        assert "database_lookup" in tool_names
        assert "self_reflection" in tool_names

    def test_get_existing_tool(self):
        tool = self.registry.get_tool("web_search")
        assert tool is not None
        assert tool.name == "web_search"

    def test_get_nonexistent_tool(self):
        tool = self.registry.get_tool("nonexistent")
        assert tool is None


# ============================================================
# Test: Shared Context
# ============================================================

class TestSharedContext:
    """Tests for inter-agent communication schema."""

    def setup_method(self):
        from app.core.context import SharedContext, ContextEntry, ContextType
        self.job_id = uuid4()
        self.context = SharedContext(job_id=self.job_id, query="test query")

    def test_add_and_retrieve_entry(self):
        from app.core.context import ContextEntry, ContextType
        entry = ContextEntry(
            agent_id="test_agent",
            context_type=ContextType.SUB_TASKS,
            payload={"sub_tasks": [{"id": "t1", "type": "factual"}]},
        )
        self.context.add_entry(entry)
        entries = self.context.get_entries_by_type(ContextType.SUB_TASKS)
        assert len(entries) == 1
        assert entries[0].agent_id == "test_agent"

    def test_get_entries_by_agent(self):
        from app.core.context import ContextEntry, ContextType
        self.context.add_entry(ContextEntry(
            agent_id="agent_a", context_type=ContextType.SUB_TASKS, payload={}
        ))
        self.context.add_entry(ContextEntry(
            agent_id="agent_b", context_type=ContextType.RETRIEVAL_RESULT, payload={}
        ))
        entries = self.context.get_entries_by_agent("agent_a")
        assert len(entries) == 1

    def test_get_latest_entry(self):
        from app.core.context import ContextEntry, ContextType
        self.context.add_entry(ContextEntry(
            agent_id="agent_a", context_type=ContextType.SUB_TASKS, payload={"v": 1}
        ))
        self.context.add_entry(ContextEntry(
            agent_id="agent_a", context_type=ContextType.SUB_TASKS, payload={"v": 2}
        ))
        latest = self.context.get_latest_entry(ContextType.SUB_TASKS)
        assert latest.payload["v"] == 2

    def test_to_agent_input_decomposition(self):
        agent_input = self.context.to_agent_input("decomposition")
        assert "original_query" in agent_input
        assert agent_input["original_query"] == "test query"

    def test_to_agent_input_critique_excludes_own_results(self):
        from app.core.context import ContextEntry, ContextType
        self.context.add_entry(ContextEntry(
            agent_id="retrieval_0", context_type=ContextType.RETRIEVAL_RESULT, payload={"data": "test"}
        ))
        self.context.add_entry(ContextEntry(
            agent_id="critique_0", context_type=ContextType.CRITIQUE_RESULT, payload={"review": "ok"}
        ))
        agent_input = self.context.to_agent_input("critique")
        # Critique should see retrieval but not its own previous results
        output_types = [o["type"] for o in agent_input["all_outputs"]]
        assert ContextType.CRITIQUE_RESULT not in output_types

    def test_to_dict_serialization(self):
        data = self.context.to_dict()
        assert data["original_query"] == "test query"
        assert str(self.job_id) == data["job_id"]

    def test_add_routing_decision(self):
        self.context.add_routing_decision({"agent": "retrieval", "reason": "factual"})
        assert len(self.context.routing_decisions) == 1
        assert "timestamp" in self.context.routing_decisions[0]


# ============================================================
# Test: LLM Utilities
# ============================================================

class TestLLMUtilities:
    """Tests for token counting and JSON parsing."""

    def test_count_tokens(self):
        from app.core.llm import count_tokens
        count = count_tokens("Hello, world!")
        assert count > 0
        assert isinstance(count, int)

    def test_count_tokens_empty(self):
        from app.core.llm import count_tokens
        count = count_tokens("")
        assert count == 0

    def test_parse_llm_json_valid(self):
        from app.core.llm import parse_llm_json
        result = parse_llm_json('{"key": "value"}')
        assert result["key"] == "value"

    def test_parse_llm_json_with_fences(self):
        from app.core.llm import parse_llm_json
        result = parse_llm_json('```json\n{"key": "value"}\n```')
        assert result["key"] == "value"

    def test_parse_llm_json_with_preamble(self):
        from app.core.llm import parse_llm_json
        result = parse_llm_json('Here is the result: {"key": "value"} done.')
        assert result["key"] == "value"

    def test_parse_llm_json_invalid(self):
        from app.core.llm import parse_llm_json
        result = parse_llm_json("not json at all")
        assert "parse_error" in result or "raw_content" in result


# ============================================================
# Test: Hashing
# ============================================================

class TestHashing:
    """Tests for deterministic input/output hashing."""

    def test_compute_hash_deterministic(self):
        from app.core import compute_hash
        h1 = compute_hash("test input")
        h2 = compute_hash("test input")
        assert h1 == h2

    def test_compute_hash_different_inputs(self):
        from app.core import compute_hash
        h1 = compute_hash("input a")
        h2 = compute_hash("input b")
        assert h1 != h2

    def test_compute_hash_length(self):
        from app.core import compute_hash
        h = compute_hash("test")
        assert len(h) == 16  # truncated to 16 chars