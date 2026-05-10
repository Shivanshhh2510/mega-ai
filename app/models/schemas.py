from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from enum import Enum


# ── Request Models ──

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000, description="The query to process")


class PromptReviewRequest(BaseModel):
    rewrite_id: str = Field(..., description="ID of the prompt rewrite to review")
    action: str = Field(..., pattern="^(approve|reject)$", description="approve or reject")
    reviewer_notes: Optional[str] = None


# ── Response Models ──

class ErrorResponse(BaseModel):
    error_code: str
    message: str
    job_id: Optional[str] = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class ExecutionStep(BaseModel):
    timestamp: str
    agent_id: str
    event_type: str
    input_hash: Optional[str]
    output_hash: Optional[str]
    latency_ms: Optional[int]
    token_count: Optional[int]
    policy_violation: Optional[str]
    details: Optional[Dict[str, Any]] = None


class ExecutionTrace(BaseModel):
    job_id: str
    query: str
    status: str
    total_tokens: int
    steps: List[ExecutionStep]
    tool_calls: List[Dict[str, Any]]
    context_budgets: Dict[str, Any]
    created_at: str
    completed_at: Optional[str]


class DimensionScore(BaseModel):
    score: float
    justification: str


class TestCaseResult(BaseModel):
    test_case_id: str
    category: str
    query: str
    passed: bool
    scores: Dict[str, DimensionScore]


class EvalRunSummary(BaseModel):
    run_id: str
    run_type: str
    status: str
    started_at: str
    completed_at: Optional[str]
    summary_by_category: Dict[str, Dict[str, float]]
    summary_by_dimension: Dict[str, float]
    results: List[TestCaseResult]


class PromptRewriteResponse(BaseModel):
    rewrite_id: str
    agent_type: str
    status: str
    proposed_changes: str
    justification: str
    target_dimension: str
    performance_delta: Optional[Dict[str, Any]]


class ReEvalResponse(BaseModel):
    eval_run_id: str
    status: str
    message: str
    targeted_test_cases: int


# ── SSE Event Types ──

class SSEEventType(str, Enum):
    AGENT_START = "agent_start"
    AGENT_TOKEN = "agent_token"
    AGENT_COMPLETE = "agent_complete"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    BUDGET_UPDATE = "budget_update"
    ROUTING_DECISION = "routing_decision"
    JOB_COMPLETE = "job_complete"
    ERROR = "error"
