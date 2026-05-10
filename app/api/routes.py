import json
from uuid import UUID, uuid4
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sse_starlette.sse import EventSourceResponse

from app.models.schemas import (
    QueryRequest, PromptReviewRequest, JobResponse, ExecutionTrace,
    ExecutionStep, EvalRunSummary, PromptRewriteResponse, ReEvalResponse,
    ErrorResponse, SSEEventType,
)
from app.agents.orchestrator import OrchestratorAgent
from app.core.context import SharedContext
from app.core.budget import ContextBudgetManager
from app.core import ExecutionLogger
from app.database import AsyncSessionLocal
from app.config import get_settings

settings = get_settings()
router = APIRouter()


# ─────────────────────────────────────────────
# Endpoint 1: POST /query — Submit query + SSE stream
# ─────────────────────────────────────────────

@router.post("/query", response_model=JobResponse)
async def submit_query(req: QueryRequest):
    """Submit a query and get a job ID. Results stream via SSE at /query/{job_id}/stream."""
    job_id = uuid4()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("INSERT INTO jobs (id, query, status) VALUES (:id, :query, 'pending')"),
            {"id": str(job_id), "query": req.query},
        )
        await session.commit()

    return JobResponse(job_id=str(job_id), status="pending", message="Job created. Stream at /query/{job_id}/stream")


@router.get("/query/{job_id}/stream")
async def stream_query(job_id: str):
    """SSE endpoint — streams agent execution events in real time."""
    async def event_generator():
        try:
            uid = UUID(job_id)
        except ValueError:
            yield {"event": SSEEventType.ERROR, "data": json.dumps({"error": "Invalid job ID"})}
            return

        # Load job
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT query, status FROM jobs WHERE id = :id"), {"id": job_id}
            )
            row = result.fetchone()
            if not row:
                yield {"event": SSEEventType.ERROR, "data": json.dumps({"error": "Job not found"})}
                return

            query_text, status = row[0], row[1]

            if status == "completed":
                result2 = await session.execute(
                    text("SELECT result FROM jobs WHERE id = :id"), {"id": job_id}
                )
                cached = result2.fetchone()
                yield {"event": SSEEventType.JOB_COMPLETE, "data": json.dumps(cached[0] if cached and cached[0] else {"message": "Already completed"})}
                return

            # Mark running
            await session.execute(
                text("UPDATE jobs SET status = 'running' WHERE id = :id"), {"id": job_id}
            )
            await session.commit()

        # Execute
        context = SharedContext(job_id=uid, query=query_text)
        budget_manager = ContextBudgetManager(job_id=uid)
        orchestrator = OrchestratorAgent()

        # Load orchestrator prompt
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT prompt_text FROM prompt_versions WHERE agent_type = 'orchestrator' AND is_active = true LIMIT 1")
            )
            row = result.fetchone()
            orch_prompt = row[0] if row else "You are an orchestrator. Route queries to sub-agents. Respond with JSON."

        try:
            async for event in orchestrator.execute_streaming(context, budget_manager, uid, orch_prompt):
                yield {
                    "event": event["type"].value if hasattr(event["type"], "value") else event["type"],
                    "data": json.dumps(event["data"], default=str),
                }

            # Persist result
            final_data = context.to_dict()
            synthesis = context.get_latest_entry(
                __import__("app.core.context", fromlist=["ContextType"]).ContextType.SYNTHESIS_RESULT
            )
            final_result = {
                "final_answer": synthesis.payload.get("final_answer", "") if synthesis else "",
                "budget_summary": budget_manager.get_all_status(),
                "total_entries": len(context.entries),
                "tool_calls": len(context.tool_results),
            }

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""UPDATE jobs SET status = 'completed', result = cast(:result as jsonb),
                            total_tokens_used = :tokens, completed_at = NOW() WHERE id = :id"""),
                    {"id": job_id, "result": json.dumps(final_result, default=str),
                     "tokens": budget_manager.total_tokens_used},
                )
                await session.commit()

        except Exception as e:
            yield {"event": SSEEventType.ERROR.value, "data": json.dumps({"error": str(e)})}

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("UPDATE jobs SET status = 'failed', error = :error WHERE id = :id"),
                    {"id": job_id, "error": str(e)},
                )
                await session.commit()

    return EventSourceResponse(event_generator())


# ─────────────────────────────────────────────
# Endpoint 2: GET /executions/{job_id} — Full execution trace
# ─────────────────────────────────────────────

@router.get("/executions/{job_id}", response_model=ExecutionTrace)
async def get_execution_trace(job_id: str):
    """Return the full execution trace with all agent steps, tool calls, and budget info."""
    async with AsyncSessionLocal() as session:
        # Get job
        job = await session.execute(
            text("SELECT id, query, status, total_tokens_used, created_at, completed_at FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
        job_row = job.fetchone()
        if not job_row:
            raise HTTPException(status_code=404, detail="Job not found")

        # Get logs
        logs = await session.execute(
            text("""SELECT timestamp, agent_id, event_type, input_hash, output_hash,
                    latency_ms, token_count, policy_violation, metadata, message
                    FROM execution_logs WHERE job_id = :id ORDER BY timestamp"""),
            {"id": job_id},
        )

        steps = []
        for row in logs.fetchall():
            steps.append(ExecutionStep(
                timestamp=row[0].isoformat() if row[0] else "",
                agent_id=row[1] or "",
                event_type=row[2],
                input_hash=row[3],
                output_hash=row[4],
                latency_ms=row[5],
                token_count=row[6],
                policy_violation=row[7],
                details=json.loads(row[8]) if row[8] and row[8] != '{}' else None,
            ))

        # Get tool calls
        tools = await session.execute(
            text("""SELECT tool_name, tool_input, tool_output, status, latency_ms, retry_count
                    FROM tool_calls WHERE job_id = :id ORDER BY created_at"""),
            {"id": job_id},
        )
        tool_calls = [
            {"tool": r[0], "input": r[1], "output": r[2], "status": r[3],
             "latency_ms": r[4], "retries": r[5]}
            for r in tools.fetchall()
        ]

        return ExecutionTrace(
            job_id=job_id,
            query=job_row[1],
            status=job_row[2],
            total_tokens=job_row[3] or 0,
            steps=steps,
            tool_calls=tool_calls,
            context_budgets={},
            created_at=job_row[4].isoformat() if job_row[4] else "",
            completed_at=job_row[5].isoformat() if job_row[5] else None,
        )


# ─────────────────────────────────────────────
# Endpoint 3: GET /evals/summary — Eval run results
# ─────────────────────────────────────────────

@router.get("/evals/summary")
async def get_eval_summary():
    """Return summary of the latest eval run with scores by category and dimension."""
    async with AsyncSessionLocal() as session:
        run = await session.execute(
            text("SELECT id, run_type, status, summary, started_at, completed_at FROM eval_runs ORDER BY started_at DESC LIMIT 1")
        )
        run_row = run.fetchone()
        if not run_row:
            return {"message": "No eval runs found. POST /evals/run to trigger one."}

        run_id = str(run_row[0])

        results = await session.execute(
            text("""SELECT er.id, er.test_case_id, er.scores, er.passed,
                    etc.category, etc.query
                    FROM eval_results er
                    JOIN eval_test_cases etc ON er.test_case_id = etc.id
                    WHERE er.run_id = :run_id"""),
            {"run_id": run_id},
        )

        test_results = []
        by_category = {}
        by_dimension = {}

        for r in results.fetchall():
            scores = r[2] if isinstance(r[2], dict) else json.loads(r[2]) if r[2] else {}
            cat = r[4]

            test_results.append({
                "test_case_id": str(r[1]),
                "category": cat,
                "query": r[5],
                "passed": r[3],
                "scores": scores,
            })

            if cat not in by_category:
                by_category[cat] = {"count": 0, "passed": 0, "total_score": 0}
            by_category[cat]["count"] += 1
            if r[3]:
                by_category[cat]["passed"] += 1

            for dim, val in scores.items():
                score_val = val.get("score", 0) if isinstance(val, dict) else 0
                if dim not in by_dimension:
                    by_dimension[dim] = {"total": 0, "count": 0}
                by_dimension[dim]["total"] += score_val
                by_dimension[dim]["count"] += 1

        summary_cat = {
            k: {"pass_rate": v["passed"] / v["count"] if v["count"] else 0, "count": v["count"]}
            for k, v in by_category.items()
        }
        summary_dim = {
            k: round(v["total"] / v["count"], 3) if v["count"] else 0
            for k, v in by_dimension.items()
        }

        return {
            "run_id": run_id,
            "run_type": run_row[1],
            "status": run_row[2],
            "started_at": run_row[4].isoformat() if run_row[4] else "",
            "completed_at": run_row[5].isoformat() if run_row[5] else None,
            "summary_by_category": summary_cat,
            "summary_by_dimension": summary_dim,
            "results": test_results,
        }


# ─────────────────────────────────────────────
# Endpoint 4: POST /prompts/review — Approve/reject prompt rewrites
# ─────────────────────────────────────────────

@router.post("/prompts/review")
async def review_prompt_rewrite(req: PromptReviewRequest):
    """Approve or reject a proposed prompt rewrite from the meta-agent."""
    async with AsyncSessionLocal() as session:
        # Find the rewrite
        result = await session.execute(
            text("SELECT id, prompt_version_id, proposed_prompt, status FROM prompt_rewrites WHERE id = :id"),
            {"id": req.rewrite_id},
        )
        row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Rewrite not found")

        if row[3] != "pending":
            raise HTTPException(status_code=400, detail=f"Rewrite already {row[3]}")

        if req.action == "approve":
            # Create new prompt version
            pv_result = await session.execute(
                text("SELECT agent_type, version FROM prompt_versions WHERE id = :id"),
                {"id": str(row[1])},
            )
            pv_row = pv_result.fetchone()

            # Deactivate old
            await session.execute(
                text("UPDATE prompt_versions SET is_active = false WHERE agent_type = :at AND is_active = true"),
                {"at": pv_row[0]},
            )

            # Insert new active version
            new_id = str(uuid4())
            await session.execute(
                text("""INSERT INTO prompt_versions (id, agent_type, prompt_text, version, is_active, parent_id)
                        VALUES (:id, :at, :text, :ver, true, :parent)"""),
                {"id": new_id, "at": pv_row[0], "text": row[2],
                 "ver": pv_row[1] + 1, "parent": str(row[1])},
            )

        # Update rewrite status
        await session.execute(
            text("UPDATE prompt_rewrites SET status = :status, reviewed_at = NOW() WHERE id = :id"),
            {"status": req.action + "d", "id": req.rewrite_id},
        )
        await session.commit()

        return {"rewrite_id": req.rewrite_id, "action": req.action, "status": f"{req.action}d"}


# ─────────────────────────────────────────────
# Endpoint 5: POST /evals/run — Trigger eval or re-eval
# ─────────────────────────────────────────────

@router.post("/evals/run")
async def trigger_eval(targeted: bool = False, test_case_ids: list[str] = None):
    """Trigger a full eval run or targeted re-eval on specific failing test cases."""
    run_id = uuid4()
    run_type = "targeted" if targeted and test_case_ids else "full"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("INSERT INTO eval_runs (id, run_type, status) VALUES (:id, :type, 'running')"),
            {"id": str(run_id), "type": run_type},
        )
        await session.commit()

    # The actual eval execution happens in the worker
    # Store the request for the worker to pick up
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""INSERT INTO execution_logs (job_id, event_type, message, metadata)
                    VALUES (:id, 'eval_run_requested', :msg, :meta::jsonb)"""),
            {
                "id": str(run_id),
                "msg": f"Eval run {run_type} requested",
                "meta": json.dumps({"test_case_ids": test_case_ids or [], "run_type": run_type}),
            },
        )
        await session.commit()

    return ReEvalResponse(
        eval_run_id=str(run_id),
        status="running",
        message=f"{'Targeted' if targeted else 'Full'} eval run started",
        targeted_test_cases=len(test_case_ids) if test_case_ids else 15,
    )
