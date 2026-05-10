import hashlib
import time
import structlog
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from app.database import AsyncSessionLocal

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
)

logger = structlog.get_logger()


def compute_hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]


class ExecutionLogger:
    """Structured logging with DB persistence for full audit trail."""

    @staticmethod
    async def log(
        job_id: Optional[UUID],
        agent_id: Optional[str],
        event_type: str,
        message: str = "",
        input_hash: Optional[str] = None,
        output_hash: Optional[str] = None,
        latency_ms: Optional[int] = None,
        token_count: Optional[int] = None,
        policy_violation: Optional[str] = None,
        metadata: dict = None,
    ):
        logger.info(
            event_type,
            job_id=str(job_id) if job_id else None,
            agent_id=agent_id,
            message=message,
            latency_ms=latency_ms,
            token_count=token_count,
            policy_violation=policy_violation,
        )

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO execution_logs
                        (job_id, agent_id, event_type, input_hash, output_hash,
                         latency_ms, token_count, policy_violation, metadata, message)
                        VALUES (cast(:job_id as uuid), :agent_id, :event_type, :input_hash, :output_hash,
                                :latency_ms, :token_count, :policy_violation, cast(:metadata as jsonb), :message)
                    """),
                    {
                        "job_id": str(job_id) if job_id else None,
                        "agent_id": agent_id,
                        "event_type": event_type,
                        "input_hash": input_hash,
                        "output_hash": output_hash,
                        "latency_ms": latency_ms,
                        "token_count": token_count,
                        "policy_violation": policy_violation,
                        "metadata": str(metadata or {}),
                        "message": message,
                    }
                )
                await session.commit()
        except Exception as e:
            logger.error("log_persist_failed", error=str(e))


class Timer:
    """Context manager for timing operations."""

    def __init__(self):
        self.start_time = None
        self.elapsed_ms = 0

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = int((time.perf_counter() - self.start_time) * 1000)
