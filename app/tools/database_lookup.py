import asyncio
from typing import Optional
from sqlalchemy import text

from app.tools import BaseTool, ToolResult, ToolStatus
from app.core import Timer
from app.core.llm import LLMClient, parse_llm_json
from app.database import AsyncSessionLocal


NL_TO_SQL_PROMPT = """You are a SQL query generator. Convert the natural language query to a PostgreSQL query.

Available table: sample_users
Columns:
  - id (SERIAL, primary key)
  - name (VARCHAR(100))
  - age (INTEGER)
  - department (VARCHAR(50)) — values: Engineering, Marketing, Sales, Management
  - salary (NUMERIC(10,2))
  - joined_at (DATE)

Rules:
- Only generate SELECT queries. No INSERT, UPDATE, DELETE, DROP, ALTER, etc.
- Return valid PostgreSQL syntax
- LIMIT results to 50 rows max

Respond ONLY with JSON:
{"sql": "your SQL query here", "explanation": "brief explanation of what the query does"}
"""


class DatabaseLookupTool(BaseTool):
    name = "database_lookup"
    description = "Query a local database using natural language. Converts NL to SQL and returns results."

    FAILURE_CONTRACTS = {
        ToolStatus.TIMEOUT: {
            "action": "return_timeout",
            "retry": True,
            "message": "Database query timed out. Try a simpler query.",
        },
        ToolStatus.EMPTY_RESULT: {
            "action": "return_empty_with_context",
            "retry": True,
            "message": "Query returned no results. Try different criteria.",
        },
        ToolStatus.MALFORMED_INPUT: {
            "action": "return_schema_hint",
            "retry": False,
            "message": "Input must include 'query' field with a natural language question about the database.",
        },
        ToolStatus.ERROR: {
            "action": "return_sql_error",
            "retry": True,
            "message": "SQL execution error. The generated query may have syntax issues.",
        },
    }

    BLOCKED_KEYWORDS = ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE"]

    def __init__(self):
        self.llm = LLMClient()

    def validate_input(self, input_data: dict) -> tuple[bool, Optional[str]]:
        if "query" not in input_data:
            return False, "Missing required field: 'query'"
        if not isinstance(input_data["query"], str) or not input_data["query"].strip():
            return False, "'query' must be a non-empty string"
        return True, None

    def _validate_sql(self, sql: str) -> tuple[bool, Optional[str]]:
        """Safety check on generated SQL."""
        sql_upper = sql.upper().strip()
        if not sql_upper.startswith("SELECT"):
            return False, "Only SELECT queries are allowed"
        for keyword in self.BLOCKED_KEYWORDS:
            if keyword in sql_upper:
                return False, f"Blocked SQL keyword: {keyword}"
        return True, None

    async def execute(self, input_data: dict) -> ToolResult:
        valid, error = self.validate_input(input_data)
        if not valid:
            return ToolResult(
                tool_name=self.name, status=ToolStatus.MALFORMED_INPUT,
                error_message=error, retry_suggested=False,
            )

        with Timer() as t:
            try:
                # Step 1: Convert NL to SQL via LLM
                llm_result = await self.llm.generate(
                    system_prompt=NL_TO_SQL_PROMPT,
                    user_prompt=input_data["query"],
                    max_tokens=500,
                )

                parsed = parse_llm_json(llm_result["content"])
                if "sql" not in parsed:
                    return ToolResult(
                        tool_name=self.name, status=ToolStatus.ERROR,
                        error_message="LLM failed to generate valid SQL",
                        data={"llm_output": parsed},
                        latency_ms=t.elapsed_ms,
                        retry_suggested=True,
                        retry_hint="Rephrase the query more clearly",
                    )

                sql = parsed["sql"]

                # Step 2: Validate SQL safety
                sql_valid, sql_error = self._validate_sql(sql)
                if not sql_valid:
                    return ToolResult(
                        tool_name=self.name, status=ToolStatus.ERROR,
                        error_message=f"SQL safety check failed: {sql_error}",
                        data={"generated_sql": sql},
                        latency_ms=t.elapsed_ms,
                        retry_suggested=True,
                    )

                # Step 3: Execute SQL
                async with AsyncSessionLocal() as session:
                    result = await session.execute(text(sql))
                    rows = result.fetchall()
                    columns = list(result.keys()) if result.keys() else []

                if not rows:
                    return ToolResult(
                        tool_name=self.name, status=ToolStatus.EMPTY_RESULT,
                        data={"sql": sql, "explanation": parsed.get("explanation", ""),
                              "columns": columns, "rows": [], "row_count": 0},
                        latency_ms=t.elapsed_ms,
                        retry_suggested=True,
                        retry_hint="Try different filter criteria",
                    )

                # Convert rows to dicts
                row_dicts = [dict(zip(columns, [str(v) for v in row])) for row in rows]

                return ToolResult(
                    tool_name=self.name, status=ToolStatus.SUCCESS,
                    data={
                        "sql": sql,
                        "explanation": parsed.get("explanation", ""),
                        "columns": columns,
                        "rows": row_dicts,
                        "row_count": len(row_dicts),
                    },
                    latency_ms=t.elapsed_ms,
                )

            except asyncio.TimeoutError:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.TIMEOUT,
                    error_message="Database query timed out",
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                )
            except Exception as e:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.ERROR,
                    error_message=str(e),
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                )
