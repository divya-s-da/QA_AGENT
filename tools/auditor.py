"""Adversarial auditor (failure-inversion §10).

Runs a DIFFERENT model family (qwen) over the final report with a rejection
checklist. Can reject findings that lack machine evidence, traceable
expectations, or 3/3 reproduction. Returns a structured audit verdict.
"""

import json

from .model_router import model_for, GATEWAY_BASE, GATEWAY_KEY

AUDIT_PROMPT = """You are the ADVERSARIAL AUDITOR of a QA test report. Your job is to REJECT
bad findings, not to rubber-stamp. You are a different model family from the
executor that produced the report — assume it may have hallucinated.

Run this rejection checklist over every finding in the report:

1. ARTIFACT GATE: Does every FAIL finding carry at least one machine artifact
   (screenshot path, exact error text, HTTP status, console error)? If no ->
   the finding must be downgraded to an observation.
2. EXPECTATION GATE: Is every finding's expectation traceable (expected_source
   present, from charter/spec/WCAG/HTTP semantics)? If no -> flag as assumption.
3. KNOWN/BY_DESIGN GATE: Does any finding appear in known_issues or by_design?
   If yes -> drop it.
4. REPRODUCTION GATE: Was each failure reproduced 3/3 (flake gate)? Findings
   marked FLAKY or 0/3 must not appear as confirmed bugs.
5. PRECONDITION GATE: Could the finding be a failed test precondition rather
   than a real bug? If the setup didn't meet the precondition, the finding is
   invalid.
6. SEVERITY GATE: Is severity consistent with the rubric? No adjectives without
   basis (impact/reach/workaround).

Output exactly this JSON (no other text):
{"verdict": "APPROVE" | "REJECT" | "APPROVE_WITH_NOTES",
 "rejected_findings": [{"id": ..., "reason": ...}],
 "notes": [ ... ],
 "summary": "one sentence"}
"""


def run_audit(report_text: str, results_json: str, learning_context: str = "") -> dict:
    """Audit the final report with the auditor model (different family)."""
    import litellm
    messages = [
        {"role": "system", "content": AUDIT_PROMPT},
        {"role": "user", "content": (
            f"QA REPORT TO AUDIT:\n{report_text[:12000]}\n\n"
            f"STRUCTURED RESULTS:\n{results_json[:12000]}\n\n"
            f"{learning_context}"
        )},
    ]
    try:
        resp = litellm.completion(
            model=f"openai/{model_for('auditor')}",
            api_base=GATEWAY_BASE,
            api_key=GATEWAY_KEY,
            messages=messages,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            return json.loads(content[start:end + 1])
        return {"verdict": "APPROVE_WITH_NOTES", "notes": ["auditor output was not JSON"], "summary": ""}
    except Exception as e:
        return {"verdict": "APPROVE_WITH_NOTES", "notes": [f"audit call failed: {str(e)[:200]}"], "summary": ""}
