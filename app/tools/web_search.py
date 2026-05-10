import asyncio
import random
from typing import Optional
from app.tools import BaseTool, ToolResult, ToolStatus
from app.core import Timer


# Simulated search results database
SEARCH_DATABASE = {
    "capital france": [
        {"title": "Paris - Capital of France", "url": "https://en.wikipedia.org/wiki/Paris",
         "snippet": "Paris is the capital and most populous city of France, with a population of 2,102,650 in the city proper.",
         "relevance": 0.98},
        {"title": "France Overview", "url": "https://en.wikipedia.org/wiki/France",
         "snippet": "France is a country in Western Europe. Its capital Paris is among the world's largest cities.",
         "relevance": 0.85},
    ],
    "great wall china visible space": [
        {"title": "Great Wall Visibility Myth", "url": "https://www.nasa.gov/greatwall",
         "snippet": "The Great Wall of China is NOT visible from space with the naked eye. This is a common misconception debunked by multiple astronauts.",
         "relevance": 0.95},
        {"title": "Myths About the Great Wall", "url": "https://www.history.com/greatwall-myths",
         "snippet": "Despite popular belief, the Great Wall cannot be seen from low Earth orbit without aid. Its width is too narrow.",
         "relevance": 0.88},
    ],
    "homeopathy water memory": [
        {"title": "Scientific Consensus on Homeopathy", "url": "https://www.ncbi.nlm.nih.gov/homeopathy",
         "snippet": "Systematic reviews consistently find that homeopathy performs no better than placebo. The concept of water memory has no scientific support.",
         "relevance": 0.96},
    ],
    "thermodynamics laws": [
        {"title": "Laws of Thermodynamics", "url": "https://en.wikipedia.org/wiki/Laws_of_thermodynamics",
         "snippet": "The laws of thermodynamics define fundamental physical quantities and describe how they behave under various circumstances.",
         "relevance": 0.95},
        {"title": "First Law of Thermodynamics", "url": "https://www.physics.org/thermo1",
         "snippet": "Energy cannot be created or destroyed in an isolated system. The total energy remains constant.",
         "relevance": 0.90},
    ],
    "quicksort mergesort comparison": [
        {"title": "Sorting Algorithm Comparison", "url": "https://www.geeksforgeeks.org/sorting",
         "snippet": "QuickSort average O(n log n), worst O(n²). MergeSort always O(n log n). QuickSort preferred for arrays, MergeSort for linked lists and stability.",
         "relevance": 0.93},
        {"title": "When to Use Which Sort", "url": "https://stackoverflow.com/sorting-comparison",
         "snippet": "QuickSort has better cache locality and is in-place. MergeSort is stable and has guaranteed O(n log n) worst case.",
         "relevance": 0.89},
    ],
    "earth shape": [
        {"title": "Shape of the Earth", "url": "https://en.wikipedia.org/wiki/Figure_of_the_Earth",
         "snippet": "The Earth is an oblate spheroid, slightly flattened at the poles. This has been confirmed by satellite measurements.",
         "relevance": 0.99},
    ],
}


def _find_results(query: str) -> list:
    """Fuzzy match against search database."""
    query_lower = query.lower()
    results = []
    for key, vals in SEARCH_DATABASE.items():
        if any(word in query_lower for word in key.split()):
            results.extend(vals)

    if not results:
        # Generic fallback
        results = [
            {"title": f"Search results for: {query}", "url": "https://search.example.com/results",
             "snippet": f"No specific results found for '{query}'. Try refining your search terms.",
             "relevance": 0.3}
        ]
    # Sort by relevance and deduplicate
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x["relevance"], reverse=True):
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)
    return unique


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web for information. Returns structured results with source URLs and relevance scores."

    FAILURE_CONTRACTS = {
        ToolStatus.TIMEOUT: {
            "action": "return_cached_or_empty",
            "retry": True,
            "message": "Search timed out. Retry with simpler query.",
        },
        ToolStatus.EMPTY_RESULT: {
            "action": "suggest_query_refinement",
            "retry": True,
            "message": "No results found. Try different keywords.",
        },
        ToolStatus.MALFORMED_INPUT: {
            "action": "return_schema",
            "retry": False,
            "message": "Input must include 'query' field as a non-empty string.",
        },
        ToolStatus.ERROR: {
            "action": "return_error",
            "retry": True,
            "message": "Search service error.",
        },
    }

    def validate_input(self, input_data: dict) -> tuple[bool, Optional[str]]:
        if "query" not in input_data:
            return False, "Missing required field: 'query'"
        if not isinstance(input_data["query"], str) or not input_data["query"].strip():
            return False, "'query' must be a non-empty string"
        if len(input_data["query"]) > 500:
            return False, "'query' must be under 500 characters"
        return True, None

    async def execute(self, input_data: dict) -> ToolResult:
        # Validate
        valid, error = self.validate_input(input_data)
        if not valid:
            return ToolResult(
                tool_name=self.name, status=ToolStatus.MALFORMED_INPUT,
                error_message=error, retry_suggested=False,
            )

        with Timer() as t:
            try:
                # Simulate search latency
                await asyncio.sleep(random.uniform(0.1, 0.3))
                results = _find_results(input_data["query"])

                if not results or (len(results) == 1 and results[0]["relevance"] < 0.5):
                    return ToolResult(
                        tool_name=self.name, status=ToolStatus.EMPTY_RESULT,
                        data={"results": results, "query": input_data["query"]},
                        latency_ms=t.elapsed_ms,
                        retry_suggested=True,
                        retry_hint="Try broader or different keywords",
                    )

                return ToolResult(
                    tool_name=self.name, status=ToolStatus.SUCCESS,
                    data={"results": results, "total": len(results), "query": input_data["query"]},
                    latency_ms=t.elapsed_ms,
                )

            except asyncio.TimeoutError:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.TIMEOUT,
                    error_message="Search timed out after 30s",
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                    retry_hint="Simplify search query",
                )
            except Exception as e:
                return ToolResult(
                    tool_name=self.name, status=ToolStatus.ERROR,
                    error_message=str(e),
                    latency_ms=t.elapsed_ms,
                    retry_suggested=True,
                )
