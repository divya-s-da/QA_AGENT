"""Centralized configuration for the QA agent."""
import os
from dotenv import load_dotenv
from google.adk.models.lite_llm import LiteLlm

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# LLM backend. All three providers are OpenAI-compatible, so only base URL,
# key and model names move. Order: LLM_PROVIDER if set ("airouter",
# "commandcode", "opencode"); else aiRouter when its key is set (the Command
# Code key expired on 2026-11-01), then Command Code, then opencode-go.
COMMAND_CODE_BASE = os.getenv("COMMAND_CODE_BASE_URL", "https://api.commandcode.ai/provider/v1")
# This .env names it COMMAND_CODE_API; the sibling projects use
# COMMAND_CODE_API_KEY. Accept either rather than fail on a naming slip.
COMMAND_CODE_KEY = os.getenv("COMMAND_CODE_API_KEY", "") or os.getenv("COMMAND_CODE_API", "")
# Only the deepseek models are served on this endpoint - qwen/claude/gpt all
# return "not supported on this endpoint", so the visual auditor shares it.
COMMAND_CODE_MODEL = os.getenv("COMMAND_CODE_CHAT_MODEL", "deepseek/deepseek-v4.1-flash")

# This .env names it AI_ROUTER_API; accept the documented name too.
AIROUTER_KEY = os.getenv("AIROUTER_API_KEY", "") or os.getenv("AI_ROUTER_API", "")
AIROUTER_BASE = "https://api.airouter.ch/v1"

# Cloudflare in front of Command Code rejects unrecognised clients with
# "error code: 1010" (a browser-signature block, not an auth failure), so the
# request has to carry a normal client UA.
_COMMAND_CODE_HEADERS = {"User-Agent": "openai-python/1.0"}


def _make_llm(model_name: str, base: str, key: str, headers: dict | None = None) -> LiteLlm:
    # Keep the completion reserve small: input + completion must fit the model
    # window, and these sessions run long. context_guard caps input at 150k.
    kwargs = dict(model=f"openai/{model_name}", api_base=base, api_key=key, max_tokens=12288)
    if headers:
        kwargs["extra_headers"] = headers
    return LiteLlm(**kwargs)


LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "").strip().lower()
                or ("airouter" if AIROUTER_KEY else "commandcode" if COMMAND_CODE_KEY else "opencode"))

if LLM_PROVIDER == "commandcode" and COMMAND_CODE_KEY:
    LLM_MODEL_DEEPSEEK = _make_llm(COMMAND_CODE_MODEL, COMMAND_CODE_BASE,
                                   COMMAND_CODE_KEY, _COMMAND_CODE_HEADERS)
    # Same model: this endpoint serves no second model, and the visual work is
    # done by visual_probe's measurements rather than by the model's "eyes".
    LLM_MODEL_QWEN = LLM_MODEL_DEEPSEEK
    LLM_MODEL = LLM_MODEL_DEEPSEEK
elif LLM_PROVIDER == "airouter" and AIROUTER_KEY:
    # Split by model strength: DeepSeek = reasoning/exploration endurance,
    # Qwen3.8 = visual/theme judgement. visual_auditor uses the Qwen instance.
    LLM_MODEL_DEEPSEEK = _make_llm("DeepSeek-V4-Flash", AIROUTER_BASE, AIROUTER_KEY)
    LLM_MODEL_QWEN = _make_llm("Qwen3.8", AIROUTER_BASE, AIROUTER_KEY)
    LLM_MODEL = LLM_MODEL_DEEPSEEK
else:
    # Last resort. If this is reached with no key set, every call fails with
    # "Missing credentials" - which is what a wrongly-named key variable looks
    # like, so check the names above before chasing the provider.
    OPENCODE_GO_KEY = os.getenv("OPENCODE_GO_API_KEY", "")
    LLM_MODEL_DEEPSEEK = LLM_MODEL_QWEN = LLM_MODEL = _make_llm(
        "deepseek-v4-flash", "https://opencode.ai/zen/go/v1", OPENCODE_GO_KEY)

# Site login credentials. Set them in .env; there are no defaults, so a
# missing value can never send one client's login to another client's site.
# Only tools/site_login.py reads them - they never go into a prompt.
SITE_EMAIL = os.getenv("SITE_EMAIL", "")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")
SITE_PASSWORD_FALLBACK = os.getenv("SITE_PASSWORD_FALLBACK", "")

MAX_RETRIES_PER_CHECK = 3
MAX_LLM_CALLS_PER_NAVIGATOR = int(os.getenv("MAX_LLM_CALLS_PER_NAVIGATOR", "50"))
# Was a flat 8. A deep pass on a busy screen (inventory + interactions +
# data plausibility + visual + persistence) needs more than 8 slots, and
# run_pipeline hard-slices checks[:MAX_CHECKS_PER_SCREEN] - anything beyond
# the cap was silently dropped, not deferred.
MAX_CHECKS_PER_SCREEN = int(os.getenv("MAX_CHECKS_PER_SCREEN", "16"))
MAX_TIME_PER_SCREEN_SECONDS = int(os.getenv("MAX_TIME_PER_SCREEN_SECONDS", "420"))
MAX_TOOL_CALLS_PER_RUN = int(os.getenv("MAX_TOOL_CALLS_PER_RUN", "260"))
HEARTBEAT_INTERVAL_SECONDS = 30
MAX_TOTAL_COST_USD = 2.0
# No default: a hardcoded path is machine-specific and breaks off the author's
# box. Empty means "no upload file" — run_pipeline passes "" and the navigator
# prompt cancels the file chooser in that case. Override in .env or --test-file.
TEST_FILE_PATH = os.getenv("TEST_FILE_PATH", "")

# Install LiteLLM rate limiting + 429 retry (free tier: ~20 req/min)
from .utils.throttle import install_throttle

install_throttle()