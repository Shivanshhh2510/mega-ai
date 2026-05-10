import json
from uuid import UUID, uuid4
from sqlalchemy import text

from app.core.llm import LLMClient, parse_llm_json
from app.core import logger
from app.database import AsyncSessionLocal


class MetaAgent:
    """
    Analyzes eval failures and proposes prompt rewrites.
    Rewrites require human approval before activation.
    """

    def __init__(self):
        self.llm = LLMClient()

    async def analyze_and_propose(self, eval_run_id: UUID) -> dict:
        """Analyze a completed eval run and propose prompt rewrites for the worst-performing agent."""
        # Load eval results
        async with AsyncSessionLocal() as session:
            results = await session.execute(
                text("""SELECT er.scores, er.passed, etc.category, etc.query, er.test_case_id
                        FROM eval_results er
                        JOIN eval_test_cases etc ON er.test_case_id = etc.id
                        WHERE er.run_id = :run_id"""),
                {"run_id": str(eval_run_id)},
            )
            rows = results.fetchall()

        if not rows:
            return {"error": "No eval results found for this run"}

        # Find worst dimension across all failing cases
        failing_cases = []
        dimension_scores = {}

        for row in rows:
            scores = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
            if not row[1]:  # not passed
                failing_cases.append({
                    "test_case_id": str(row[4]),
                    "category": row[2],
                    "query": row[3],
                    "scores": scores,
                })

            for dim, val in scores.items():
                if isinstance(val, dict) and "score" in val:
                    if dim not in dimension_scores:
                        dimension_scores[dim] = []
                    dimension_scores[dim].append(val["score"])

        if not failing_cases:
            return {"message": "All test cases passed. No rewrites needed."}

        # Find worst dimension
        avg_by_dim = {
            dim: sum(scores) / len(scores)
            for dim, scores in dimension_scores.items()
            if scores
        }
        worst_dim = min(avg_by_dim, key=avg_by_dim.get) if avg_by_dim else "correctness"

        # Map dimension to agent
        dim_to_agent = {
            "correctness": "retrieval",
            "citation_accuracy": "retrieval",
            "contradiction_resolution": "synthesis",
            "tool_efficiency": "orchestrator",
            "budget_compliance": "orchestrator",
            "critique_agreement": "critique",
        }
        target_agent = dim_to_agent.get(worst_dim, "orchestrator")

        # Load current prompt
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT id, prompt_text, version FROM prompt_versions WHERE agent_type = :at AND is_active = true LIMIT 1"),
                {"at": target_agent},
            )
            prompt_row = result.fetchone()

        if not prompt_row:
            return {"error": f"No active prompt found for {target_agent}"}

        # Load meta prompt
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT prompt_text FROM prompt_versions WHERE agent_type = 'meta' AND is_active = true LIMIT 1")
            )
            meta_row = result.fetchone()
            meta_prompt = meta_row[0] if meta_row else "You are a meta-agent. Analyze failures and propose prompt rewrites. Respond with JSON."

        # Call LLM to propose rewrite
        user_prompt = json.dumps({
            "target_agent": target_agent,
            "worst_dimension": worst_dim,
            "current_avg_score": round(avg_by_dim.get(worst_dim, 0), 3),
            "current_prompt": prompt_row[1],
            "failing_cases": failing_cases[:5],  # top 5 failures
            "all_dimension_averages": {k: round(v, 3) for k, v in avg_by_dim.items()},
        }, default=str)

        llm_result = await self.llm.generate(
            system_prompt=meta_prompt,
            user_prompt=user_prompt,
            max_tokens=3000,
        )

        parsed = parse_llm_json(llm_result["content"])

        # Store the proposed rewrite (pending approval)
        rewrite_id = uuid4()
        proposed_prompt = parsed.get("proposed_rewrite", {}).get("full_prompt", "")
        if not proposed_prompt:
            proposed_prompt = parsed.get("proposed_rewrite", {}).get("rewritten_excerpt", "")

        diff_text = json.dumps(parsed.get("proposed_rewrite", {}).get("changes_made", []))
        justification = parsed.get("proposed_rewrite", {}).get("expected_improvement", "")
        failing_ids = [fc["test_case_id"] for fc in failing_cases]

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""INSERT INTO prompt_rewrites
                        (id, prompt_version_id, proposed_prompt, diff_text, justification,
                         target_dimension, failing_test_cases, eval_run_id)
                        VALUES (:id, :pv_id, :prompt, :diff, :just, :dim, :cases, :run_id)"""),
                {
                    "id": str(rewrite_id), "pv_id": str(prompt_row[0]),
                    "prompt": proposed_prompt, "diff": diff_text,
                    "just": justification, "dim": worst_dim,
                    "cases": failing_ids, "run_id": str(eval_run_id),
                },
            )
            await session.commit()

        logger.info("meta_rewrite_proposed",
                     rewrite_id=str(rewrite_id), agent=target_agent, dimension=worst_dim)

        return {
            "rewrite_id": str(rewrite_id),
            "target_agent": target_agent,
            "target_dimension": worst_dim,
            "current_score": round(avg_by_dim.get(worst_dim, 0), 3),
            "proposed_changes": diff_text,
            "justification": justification,
            "status": "pending_approval",
            "failing_test_cases": len(failing_cases),
        }
