from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="MEGA AI — Multi-Agent Orchestration System",
    description="Production-grade multi-agent system with self-improving eval loop, dynamic tool orchestration, and SSE streaming.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/")
async def root():
    return {
        "name": "MEGA AI",
        "description": "Multi-Agent Orchestration System",
        "endpoints": {
            "POST /api/v1/query": "Submit a query (returns job_id)",
            "GET /api/v1/query/{job_id}/stream": "SSE stream of execution events",
            "GET /api/v1/executions/{job_id}": "Full execution trace",
            "GET /api/v1/evals/summary": "Latest eval run summary",
            "POST /api/v1/prompts/review": "Approve/reject prompt rewrites",
            "POST /api/v1/evals/run": "Trigger eval/re-eval run",
        },
        "docs": "/docs",
    }
