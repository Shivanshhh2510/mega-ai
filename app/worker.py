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
                # Find running eval runs that need processing
                result = await session.execute(
                    text("""SELECT er.id, el.metadata
                            FROM eval_runs er
                            JOIN execution_logs el ON el.job_id = er.id
                            WHERE er.status = 'running'
                            AND el.event_type = 'eval_run_requested'
                            AND NOT EXISTS (
                                SELECT 1 FROM eval_results WHERE run_id = er.id
                            )
                            ORDER BY er.started_at ASC LIMIT 1""")
                )
                row = result.fetchone()

            if row:
                run_id = row[0]
                meta_raw = row[1]
                metadata = meta_raw if isinstance(meta_raw, dict) else json.loads(meta_raw) if meta_raw else {}

                logger.info("worker_processing_eval", run_id=str(run_id))

                if metadata.get("run_type") == "targeted" and metadata.get("test_case_ids"):
                    await harness.run_targeted_eval(UUID(str(run_id)), metadata["test_case_ids"])
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
