import asyncio
import json
from uuid import UUID

from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.evaluation.harness import EvalHarness
from app.evaluation.meta_agent import MetaAgent
from app.core import logger


async def process_eval_runs():
    """Poll for pending eval runs and process them."""
    harness = EvalHarness()
    meta = MetaAgent()

    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Find eval runs that are still 'running' and have no results yet
                result = await session.execute(
                    text("""SELECT id, run_type FROM eval_runs
                            WHERE status = 'running'
                            AND NOT EXISTS (
                                SELECT 1 FROM eval_results WHERE run_id = eval_runs.id
                            )
                            ORDER BY started_at ASC LIMIT 1""")
                )
                row = result.fetchone()

            if row:
                run_id = row[0]
                run_type = row[1]

                logger.info("worker_processing_eval", run_id=str(run_id), run_type=run_type)

                if run_type == "targeted":
                    # Look up targeted test case IDs from the log
                    async with AsyncSessionLocal() as session:
                        log_result = await session.execute(
                            text("""SELECT metadata FROM execution_logs
                                    WHERE event_type = 'eval_run_requested'
                                    AND message LIKE :pattern
                                    ORDER BY timestamp DESC LIMIT 1"""),
                            {"pattern": f"%{str(run_id)}%"},
                        )
                        log_row = log_result.fetchone()
                        meta_data = json.loads(log_row[0]) if log_row and log_row[0] else {}
                        test_case_ids = meta_data.get("test_case_ids", [])

                    if test_case_ids:
                        await harness.run_targeted_eval(UUID(str(run_id)), test_case_ids)
                    else:
                        await harness.run_full_eval(UUID(str(run_id)))
                else:
                    await harness.run_full_eval(UUID(str(run_id)))

                # After eval, run meta-agent to propose improvements
                logger.info("worker_running_meta_agent", run_id=str(run_id))
                proposal = await meta.analyze_and_propose(UUID(str(run_id)))
                logger.info("worker_meta_proposal", proposal=str(proposal)[:200])

        except Exception as e:
            logger.error("worker_error", error=str(e))

        await asyncio.sleep(5)


async def main():
    logger.info("worker_started")
    await process_eval_runs()


if __name__ == "__main__":
    asyncio.run(main())