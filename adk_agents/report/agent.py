"""ADK web exposure for the QA report analyst agent.

Loaded by `python -m google.adk.cli web adk_agents` — reuses the pipeline's
report agent object (narrative-only, same as run_pipeline uses).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from react_qa_agent.prompts.report import REPORT_PROMPT
from react_qa_agent.config import LLM_MODEL
from google.adk.agents import Agent

agent = Agent(
    name="report_generator",
    model=LLM_MODEL,
    description="Analyzes QA results: TOP_BREAKAGES + FIX_THIS_FIRST narrative.",
    instruction=REPORT_PROMPT,
    tools=[],
    output_key="final_report",
    mode="chat",
)

root_agent = agent
