import json
from uuid import UUID, uuid4
from datetime import datetime, timezone

from sqlalchemy import text

from app.agents.orchestrator import OrchestratorAgent
from app.core.context import SharedContext, ContextType
from app.core.budget import ContextBudgetManager
from app.core.llm import LLMClient, parse_llm_json
from app.core import ExecutionLogger, logger
from app.database import AsyncSessionLocal


SCORING_PROMPT = """You are an evaluation judge. Score the system's output on these dimensions.
Each score is 0.0 to 1.0 with a brief justification.

Dimensions:
1. correctness — Is the answer factually correct?
2. citation_accuracy — Are claims properly attributed to sources?
3. contradiction_resolution — Were contradictions between sources handled?
4. tool_efficiency — Were the right tools used without unnecessary calls?
5. budget_compliance — Did agents stay within token budgets?
6. critique_agreement — Did the final answer incorporate critique feedback?

Respond with JSON:
{
  "correctness": {"score": float, "justification": "..."},
  "citation_accuracy": {"score": float, "justification": "..."},
  "contradiction_resolution": {"score": float, "justification": "..."},
  "tool_efficiency": {"score": float, "justification": "..."},
  "budget_compliance": {"score": float, "justification": "..."},
  "critique_agreement": {"score": float, "justification": "..."},
  "passed": true/false,
  "overall_notes": "..."
}

A test case passes if average score >= 0.6 and no single dimension is below 0.3.
"""


class EvalHarness:
    """Runs eval test cases through the full pipeline and scores results."""

    def __init__(self):
        self.llm = LLMClient()

    async def run_full_eval(self, run_id: UUID):
        """Execute all 15 test cases and score them."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT id, category, query, expected_answer, metadata FROM eval_test_cases"))
            test_cases = result.fetchall()

        logger.info("eval_run_start", run_id=str(run_id), test_cases=len(test_cases))

        for tc in test_cases:
            tc_id, category, query, expected, metadata = tc
            await self._run_single_case(run_id, tc_id, category, query, expected, metadata)

        # Update run status
        await self._finalize_run(run_id)

    async def run_targeted_eval(self, run_id: UUID, test_case_ids: list[str]):
        """Re-run only specific failing test cases."""
        async with AsyncSessionLocal() as session:
            placeholders = ", ".join(f"'{tc}'" for tc in test_case_ids)
            result = await session.execute(
                text(f"SELECT id, category, query, expected_answer, metadata FROM eval_test_cases WHERE id IN ({placeholders})")
            )
            test_cases = result.fetchall()

        for tc in test_cases:
            tc_id, category, query, expected, metadata = tc
            await self._run_single_case(run_id, tc_id, category, query, expected, metadata)

        await self._finalize_run(run_id)

    async def _run_single_case(self, run_id: UUID, tc_id, category, query, expected, metadata):
        """Run a single test case through the pipeline and score it."""
        job_id = uuid4()

        # Create job
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("INSERT INTO jobs (id, query, status) VALUES (:id, :query, 'running')"),
                {"id": str(job_id), "query": query},
            )
            await session.commit()

        try:
            # Execute through orchestrator
            context = SharedContext(job_id=job_id, query=query)
            budget_manager = ContextBudgetManager(job_id=job_id)
            orchestrator = OrchestratorAgent()

            # Load orchestrator prompt
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("SELECT prompt_text FROM prompt_versions WHERE agent_type = 'orchestrator' AND is_active = true LIMIT 1")
                )
                row = result.fetchone()
                orch_prompt = row[0] if row else "You are an orchestrator. Respond with JSON."

            pipeline_result = await orchestrator.execute(context, budget_manager, job_id, orch_prompt)

            # Score the result
            scores = await self._score_output(
                query=query,
                expected=expected,
                actual=pipeline_result,
                context_data=context.to_dict(),
                budget_data=budget_manager.get_all_status(),
                category=category,
                metadata=metadata,
            )

            # Determine pass/fail
            score_values = [v.get("score", 0) for v in scores.values() if isinstance(v, dict) and "score" in v]
            avg_score = sum(score_values) / len(score_values) if score_values else 0
            min_score = min(score_values) if score_values else 0
            passed = avg_score >= 0.6 and min_score >= 0.3

            # Persist
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""INSERT INTO eval_results (id, run_id, test_case_id, job_id, scores, passed, raw_outputs)
                            VALUES (:id, :run_id, :tc_id, :job_id, :scores::jsonb, :passed, :raw::jsonb)"""),
                    {
                        "id": str(uuid4()), "run_id": str(run_id), "tc_id": str(tc_id),
                        "job_id": str(job_id), "scores": json.dumps(scores, default=str),
                        "passed": passed, "raw": json.dumps(pipeline_result, default=str),
                    },
                )
                await session.execute(
                    text("UPDATE jobs SET status = 'completed', completed_at = NOW() WHERE id = :id"),
                    {"id": str(job_id)},
                )
                await session.commit()

            logger.info("eval_case_done", tc_id=str(tc_id), category=category, passed=passed, avg_score=round(avg_score, 3))

        except Exception as e:
            logger.error("eval_case_failed", tc_id=str(tc_id), error=str(e))
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""INSERT INTO eval_results (id, run_id, test_case_id, job_id, scores, passed)
                            VALUES (:id, :run_id, :tc_id, :job_id, :scores::jsonb, false)"""),
                    {
                        "id": str(uuid4()), "run_id": str(run_id), "tc_id": str(tc_id),
                        "job_id": str(job_id),
                        "scores": json.dumps({"error": {"score": 0, "justification": str(e)}}),
                    },
                )
                await session.execute(
                    text("UPDATE jobs SET status = 'failed', error = :error WHERE id = :id"),
                    {"id": str(job_id), "error": str(e)},
                )
                await session.commit()

    async def _score_output(self, query, expected, actual, context_data, budget_data, category, metadata) -> dict:
        """Use LLM-as-judge to score the output across dimensions."""
        user_prompt = json.dumps({
            "query": query,
            "category": category,
            "expected_answer": expected,
            "actual_output": actual,
            "budget_data": budget_data,
            "metadata": metadata if isinstance(metadata, dict) else json.loads(metadata) if metadata else {},
        }, default=str)

        result = await self.llm.generate(
            system_prompt=SCORING_PROMPT,
            user_prompt=user_prompt,
            max_tokens=2000,
        )

        return parse_llm_json(result["content"])

    async def _finalize_run(self, run_id: UUID):
        """Update eval run status and compute summary."""
        async with AsyncSessionLocal() as session:
            results = await session.execute(
                text("SELECT passed, scores FROM eval_results WHERE run_id = :id"),
                {"id": str(run_id)},
            )
            rows = results.fetchall()

            total = len(rows)
            passed = sum(1 for r in rows if r[0])

            summary = {
                "total": total,
                "passed": passed,
                "pass_rate": round(passed / total, 3) if total else 0,
            }

            await session.execute(
                text("""UPDATE eval_runs SET status = 'completed', completed_at = NOW(),
                        summary = :summary::jsonb WHERE id = :id"""),
                {"id": str(run_id), "summary": json.dumps(summary)},
            )
            await session.commit()

        logger.info("eval_run_complete", run_id=str(run_id), pass_rate=summary["pass_rate"])
