from typing import Optional
from app.tools import BaseTool, ToolResult, ToolStatus
from app.core import Timer
from app.core.llm import LLMClient, parse_llm_json


REFLECTION_PROMPT = """You are a self-reflection module. Analyze the agent's previous outputs within this session and identify:
1. Contradictions between different outputs
2. Unsupported claims
3. Logical inconsistencies
4. Gaps in reasoning

Respond with JSON:
{
  "contradictions": [
    {"output_1": "reference to first contradicting output", "output_2": "reference to second", "description": "how they contradict"}
  ],
  "unsupported_claims": [
    {"claim": "the claim text", "location": "which output", "reason": "why unsupported"}
  ],
  "inconsistencies": [
    {"description": "the inconsistency", "outputs_involved": ["output references"]}
  ],
  "overall_coherence_score": 0.0-1.0,
  "recommendations": ["list of recommended fixes"]
}
"""


class SelfReflectionTool(BaseTool):
    name = "self_reflection"
    description = "Re-read the agent's own previous outputs within the session and identify contradictions, unsupported claims, and logical inconsistencies."

    FAILURE_CONTRACTS = {
        ToolStatus.TIMEOUT: {
            "action": "return_partial_analysis",
            "retry": True,
            "message": "Reflection analysis timed out.",
        },
        ToolStatus.EMPTY_RESULT: {
            "action": "return_no_issues",
            "retry": False,
            "message": "No previous outputs to reflect on.",
        },
        ToolStatus.MALFORMED_INPUT: {
            "action": "return_schema",
            "retry": False,
            "message": "Input must include 'outputs' as a list of previous agent outputs.",
        },
        ToolStatus.ERROR: {
            "action": "return_error",
            "retry": True,
            "message": "Reflection analysis failed.",
        },
    }

    def __init__(self):
        self.llm = LLMClient()

    def validate_input(self, input_data: dict) -> tuple[bool, Optional[str]]:
        if "outputs" not in input_data:
            return False, "Missing required field: 'outputs'"
        if not isinstance(input_data["outputs"], list):
            return False, "'outputs' must be a list"
        if len(input_data["outputs"]) == 0:
            return False, "'outputs' list cannot be empty"
        return True, None

    async def execute(self, input_data: dict) -> ToolResult:
        valid, error = self.validate_input(input_data)
        if not valid:
            if "outputs" in input_data and isinstance(input_data["outputs"], list) and len(input_data["outputs"]) == 0:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.EMPTY_RESULT,
                    data={"message": "No outputs to reflect on"},
                    retry_suggested=False,
                )
            return ToolResult(
                tool_name=self.name, status=ToolStatus.MALFORMED_INPUT,
                error_message=error, retry_suggested=False,
            )

        with Timer() as t:
            try:
                outputs_text = "\n\n---\n\n".join(
                    f"Output {i+1} (Agent: {o.get('agent_id', 'unknown')}):\n{o.get('content', str(o))}"
                    for i, o in enumerate(input_data["outputs"])
                )

                llm_result = await self.llm.generate(
                    system_prompt=REFLECTION_PROMPT,
                    user_prompt=f"Analyze these {len(input_data['outputs'])} outputs for contradictions and issues:\n\n{outputs_text}",
                    max_tokens=2000,
                )

                parsed = parse_llm_json(llm_result["content"])

                return ToolResult(
                    tool_name=self.name, status=ToolStatus.SUCCESS,
                    data=parsed,
                    latency_ms=t.elapsed_ms,
                )

            except Exception as e:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.ERROR,
                    error_message=str(e),
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                )
