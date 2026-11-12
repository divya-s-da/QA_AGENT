"""Model router (failure-inversion §4) — capability manifest per agent role.

Routes each role to a model on aiRouter.ch (flat-rate, no token counting).
Auditor uses a DIFFERENT model family from executor for adversarial review.

Roles:
  planner    -> strong reasoning (charter/CUJ decomposition)
  executor   -> fast + cheap (navigator)
  verifier   -> strong judgement (Qwen3.8)
  auditor    -> deliberately different family (Qwen3.8 for adversarial review)
  reporter   -> narrative generation
"""

import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

GATEWAY_BASE = "https://api.airouter.ch/v1"
# This .env names it AI_ROUTER_API; accept the documented name too (as config.py does).
GATEWAY_KEY = os.getenv("AIROUTER_API_KEY", "") or os.getenv("AI_ROUTER_API", "")

# Default role -> model. Auditor uses Qwen3.8 (different family from DeepSeek).
ROLE_MODELS = {
    "planner": os.getenv("QA_MODEL_PLANNER", "Qwen3.8"),
    "executor": os.getenv("QA_MODEL_EXECUTOR", "DeepSeek-V4-Flash"),
    "verifier": os.getenv("QA_MODEL_VERIFIER", "Qwen3.8"),
    "auditor": os.getenv("QA_MODEL_AUDITOR", "Qwen3.8"),
    "reporter": os.getenv("QA_MODEL_REPORTER", "DeepSeek-V4-Flash"),
}


def model_for(role: str) -> str:
    return ROLE_MODELS.get(role, "DeepSeek-V4-Flash")


def build_lite_llm(role: str):
    """Build a LiteLlm bound to aiRouter for the given role."""
    from google.adk.models.lite_llm import LiteLlm
    return LiteLlm(
        model=f"openai/{model_for(role)}",
        api_base=GATEWAY_BASE,
        api_key=GATEWAY_KEY,
    )


def gateway_kwargs(role: str) -> dict:
    return {
        "api_base": GATEWAY_BASE,
        "api_key": GATEWAY_KEY,
    }
