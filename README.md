# MEGA AI — Multi-Agent Orchestration System

A production-grade multi-agent system with self-improving evaluation loop, dynamic tool orchestration, adversarial robustness testing, SSE streaming, and full observability.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         API Layer (FastAPI)                       │
│  POST /query → GET /stream (SSE) → GET /executions → GET /evals │
└──────────────┬──────────────────────────────────────┬────────────┘
               │                                      │
               ▼                                      ▼
┌──────────────────────────┐          ┌──────────────────────────┐
│   Orchestrator Agent     │          │    Eval Pipeline          │
│   (Dynamic Routing)      │          │    (15 test cases)        │
│                          │          │    LLM-as-Judge scoring   │
│  ┌─────┐ ┌─────┐        │          │                          │
│  │Decomp│→│Retr.│        │          │  ┌────────────────┐      │
│  └─────┘ └──┬──┘        │          │  │  Meta Agent     │      │
│             ▼            │          │  │  (Self-Improve) │      │
│  ┌─────┐ ┌─────┐        │          │  └────────────────┘      │
│  │Crit.│→│Synth│        │          └──────────────────────────┘
│  └─────┘ └─────┘        │
└──────────┬───────────────┘
           │
    ┌──────┴──────────────────────────────┐
    │         Tool Registry                │
    │  ┌──────────┐  ┌──────────────────┐  │
    │  │web_search│  │code_execution    │  │
    │  └──────────┘  └──────────────────┘  │
    │  ┌──────────┐  ┌──────────────────┐  │
    │  │db_lookup │  │self_reflection   │  │
    │  └──────────┘  └──────────────────┘  │
    └──────────────────────────────────────┘
           │
    ┌──────┴──────────────────────────────┐
    │    Context Budget Manager            │
    │    Token tracking per agent          │
    │    Violation logging (never silent)  │
    └─────────────────────────────────────┘
```

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env and add your GROQ_API_KEY

# 2. Start everything (zero manual steps)
docker compose up --build

# 3. API is at http://localhost:8000
# Docs at http://localhost:8000/docs
# pgAdmin at http://localhost:5050
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/query` | POST | Submit a query → returns `job_id` |
| `/api/v1/query/{job_id}/stream` | GET | SSE stream of real-time execution events |
| `/api/v1/executions/{job_id}` | GET | Full execution trace with agent steps, tool calls, budgets |
| `/api/v1/evals/summary` | GET | Latest eval run scores by category and dimension |
| `/api/v1/prompts/review` | POST | Approve/reject meta-agent prompt rewrites |
| `/api/v1/evals/run` | POST | Trigger full or targeted re-evaluation |

## Agents

### Orchestrator
- Analyzes incoming queries via LLM
- Dynamically decides which sub-agents to invoke, in what order, with what budget
- No hardcoded chains — routing is decided at runtime
- Automatically inserts critique before synthesis if missing

### Decomposition Agent
- Breaks queries into typed sub-tasks (factual_lookup, computation, analysis, clarification_needed)
- Produces dependency graphs with validation
- Flags ambiguities in underspecified queries

### Retrieval Agent
- Multi-hop reasoning across retrieved chunks
- Citation mapping (which chunk → which claim)
- Plans and executes tool calls based on sub-task types

### Critique Agent
- Per-claim confidence scoring (0.0–1.0)
- Span-level flagging (factual_error, unsupported_claim, logical_fallacy, prompt_injection, false_premise)
- Provides suggested corrections for each flagged span

### Synthesis Agent
- Merges all agent outputs into coherent final answer
- Resolves contradictions flagged by critique
- Produces provenance map linking each sentence to source agent/chunk
- Prioritizes critique corrections over raw agent outputs

### Compression Agent
- Triggered when any agent exceeds 85% budget utilization
- Lossless for: tool outputs, scores, citations, structured data
- Lossy only for: conversational filler, redundant explanations

## Tools

All tools implement explicit **failure contracts** — documented behavior for each failure mode:

| Tool | Failure Mode | Behavior | Retry? |
|------|-------------|----------|--------|
| web_search | timeout | Return cached/empty | Yes |
| web_search | empty_result | Suggest query refinement | Yes |
| code_execution | timeout (10s) | Kill process | Yes |
| code_execution | malformed_input | Return syntax error | No |
| database_lookup | SQL error | Return generated SQL + error | Yes |
| self_reflection | empty outputs | Return no-issues | No |

Retry logic is **in code**, not in prompts. Each retry attempt is logged separately with `retry_of` linking.

## Context Budget Manager

- Per-agent token tracking with configurable limits
- Budget violations are **logged and surfaced**, never silently truncated
- Compression triggered automatically at 85% utilization
- Full budget audit available in execution traces

## Evaluation Pipeline

### 15 Test Cases (seeded in DB)
- **5 Baseline**: Straightforward factual/computational (capital of France, Fibonacci, thermodynamics, SQL query, sorting comparison)
- **5 Ambiguous**: Underspecified queries (missing referent, missing context, vague entity, fully ambiguous)
- **5 Adversarial**: Prompt injection, false premises, fabricated sources, agent state injection, logic override

### Scoring Dimensions
Each test case scored on 6 dimensions (0.0–1.0):
1. **Correctness** — Factual accuracy
2. **Citation accuracy** — Proper source attribution
3. **Contradiction resolution** — Handling conflicting information
4. **Tool efficiency** — Right tools, no unnecessary calls
5. **Budget compliance** — Staying within token limits
6. **Critique agreement** — Incorporating critique feedback

Pass threshold: average ≥ 0.6 AND no dimension below 0.3.

## Self-Improving Loop

1. **Eval run completes** → worker triggers meta-agent
2. **Meta-agent** analyzes failing cases, identifies worst dimension
3. **Proposes prompt rewrite** with structured diff and justification
4. **Human reviews** via `POST /prompts/review` (approve/reject)
5. On approval → new prompt version activated, old deactivated
6. **Targeted re-eval** can run on just the failing cases to validate improvement

No automatic prompt changes — human-in-the-loop is required.

## Observability

- **Structured logging** via `structlog` to stdout + PostgreSQL `execution_logs` table
- Every LLM call logged with: input_hash, output_hash, latency_ms, token_count
- Every tool call logged with: status, retry_count, retry_of linkage
- Budget violations logged as policy violations
- Full audit trail queryable via pgAdmin at port 5050

## Design Decisions & Trade-offs

### Why Groq?
Fast inference (Llama 3.3 70B), generous free tier, simple API. Primary model: `llama-3.3-70b-versatile`, fallback: `llama-3.1-8b-instant`.

### Why not LangGraph/CrewAI?
Custom orchestration gives full control over routing logic, budget management, and logging. LangChain used lightly for structured LLM interaction, not for agent orchestration.

### Web search is stubbed
The web_search tool uses a simulated search database covering the eval test case topics. This is intentional — it ensures reproducible eval results and avoids external API dependencies. The stub covers all 15 test case topics with realistic results.

### Budget violations: log, don't truncate
Silent truncation hides bugs. We log violations and surface them in execution traces so the evaluator can see exactly where budgets were exceeded and why.

## Known Limitations

1. **Web search is a stub** — real implementation would need a search API integration
2. **Code execution sandbox is basic** — uses `exec()` with import blocking, not a proper container sandbox
3. **No persistent conversation memory** — each query is independent
4. **Single-model scoring** — eval uses the same LLM family as the agents being evaluated (potential bias)
5. **No rate limiting** on API endpoints
6. **No authentication** — all endpoints are open
7. **Compression agent is reactive** — only triggered at 85%, could be more proactive

## Project Structure

```
mega-ai/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── migrations/
│   └── init.sql            # Full schema + 15 eval test cases + prompt seeds
├── app/
│   ├── main.py             # FastAPI entry point
│   ├── config.py            # Pydantic Settings
│   ├── database.py          # Async + sync SQLAlchemy
│   ├── worker.py            # Background eval/meta-agent processor
│   ├── api/
│   │   └── routes.py        # 5 API endpoints + SSE streaming
│   ├── agents/
│   │   ├── __init__.py      # BaseAgent with _call_llm()
│   │   ├── orchestrator.py  # Dynamic routing
│   │   ├── decomposition.py # Sub-task breakdown
│   │   ├── retrieval.py     # Multi-hop + tools
│   │   ├── critique.py      # Per-claim scoring
│   │   ├── synthesis.py     # Merge + provenance
│   │   └── compression.py   # Budget management
│   ├── core/
│   │   ├── __init__.py      # Structured logging, Timer, hashing
│   │   ├── llm.py           # Groq client + streaming
│   │   ├── budget.py        # Context budget manager
│   │   └── context.py       # Shared context schema
│   ├── evaluation/
│   │   ├── harness.py       # Eval runner + LLM-as-judge scoring
│   │   └── meta_agent.py    # Self-improving prompt proposals
│   ├── models/
│   │   └── schemas.py       # Pydantic request/response models
│   └── tools/
│       ├── __init__.py      # BaseTool + failure contracts
│       ├── web_search.py    # Stub with simulated results
│       ├── code_execution.py # Python sandbox
│       ├── database_lookup.py # NL → SQL
│       ├── self_reflection.py # Contradiction detection
│       └── registry.py      # Tool registry + retry logic
└── README.md
```
