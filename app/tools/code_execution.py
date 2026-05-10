import asyncio
import sys
import io
import traceback
from typing import Optional
from app.tools import BaseTool, ToolResult, ToolStatus
from app.core import Timer


class CodeExecutionTool(BaseTool):
    name = "code_execution"
    description = "Execute Python code snippets in a sandboxed environment. Returns stdout, stderr, and exit code."

    FAILURE_CONTRACTS = {
        ToolStatus.TIMEOUT: {
            "action": "kill_and_return_partial",
            "retry": True,
            "message": "Code execution timed out after 10 seconds. Simplify the code.",
        },
        ToolStatus.EMPTY_RESULT: {
            "action": "return_with_note",
            "retry": False,
            "message": "Code executed successfully but produced no output.",
        },
        ToolStatus.MALFORMED_INPUT: {
            "action": "return_syntax_error",
            "retry": False,
            "message": "Invalid Python code provided.",
        },
        ToolStatus.ERROR: {
            "action": "return_traceback",
            "retry": True,
            "message": "Runtime error during execution.",
        },
    }

    BLOCKED_IMPORTS = {"os", "subprocess", "shutil", "socket", "http", "urllib", "requests", "sys"}
    MAX_EXECUTION_TIME = 10  # seconds
    MAX_OUTPUT_SIZE = 10000  # characters

    def validate_input(self, input_data: dict) -> tuple[bool, Optional[str]]:
        if "code" not in input_data:
            return False, "Missing required field: 'code'"
        if not isinstance(input_data["code"], str) or not input_data["code"].strip():
            return False, "'code' must be a non-empty string"

        code = input_data["code"]

        # Check for blocked imports
        for blocked in self.BLOCKED_IMPORTS:
            if f"import {blocked}" in code or f"from {blocked}" in code:
                return False, f"Blocked import: '{blocked}' is not allowed in sandbox"

        # Syntax check
        try:
            compile(code, "<sandbox>", "exec")
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        return True, None

    async def execute(self, input_data: dict) -> ToolResult:
        valid, error = self.validate_input(input_data)
        if not valid:
            return ToolResult(
                tool_name=self.name, status=ToolStatus.MALFORMED_INPUT,
                error_message=error, retry_suggested=False,
            )

        code = input_data["code"]

        with Timer() as t:
            try:
                result = await asyncio.wait_for(
                    self._run_code(code),
                    timeout=self.MAX_EXECUTION_TIME,
                )

                stdout = result["stdout"][:self.MAX_OUTPUT_SIZE]
                stderr = result["stderr"][:self.MAX_OUTPUT_SIZE]
                exit_code = result["exit_code"]

                if exit_code != 0:
                    return ToolResult(
                        tool_name=self.name, status=ToolStatus.ERROR,
                        data={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
                        error_message=stderr or "Non-zero exit code",
                        latency_ms=t.elapsed_ms,
                        retry_suggested=True,
                        retry_hint="Fix the error in stderr and retry",
                    )

                if not stdout.strip() and not stderr.strip():
                    return ToolResult(
                        tool_name=self.name, status=ToolStatus.EMPTY_RESULT,
                        data={"stdout": "", "stderr": "", "exit_code": 0},
                        latency_ms=t.elapsed_ms,
                        retry_suggested=False,
                    )

                return ToolResult(
                    tool_name=self.name, status=ToolStatus.SUCCESS,
                    data={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
                    latency_ms=t.elapsed_ms,
                )

            except asyncio.TimeoutError:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.TIMEOUT,
                    error_message=f"Execution timed out after {self.MAX_EXECUTION_TIME}s",
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                    retry_hint="Reduce computation complexity or add early termination",
                )
            except Exception as e:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.ERROR,
                    error_message=str(e),
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                )

    async def _run_code(self, code: str) -> dict:
        """Execute Python code in isolated namespace."""
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        namespace = {"__builtins__": __builtins__}
        old_stdout, old_stderr = sys.stdout, sys.stderr

        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            exec(code, namespace)

            return {
                "stdout": stdout_capture.getvalue(),
                "stderr": stderr_capture.getvalue(),
                "exit_code": 0,
            }
        except Exception as e:
            return {
                "stdout": stdout_capture.getvalue(),
                "stderr": traceback.format_exc(),
                "exit_code": 1,
            }
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
