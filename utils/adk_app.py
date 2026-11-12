"""ADK App factory with automatic event compaction.

Marathon web sessions replay their full tool-result history on every call;
without compaction they eventually exceed the model's context window
(ContextWindowExceededError). Wrapping agents in an App with an
EventsCompactionConfig makes ADK summarize old event ranges once the prompt
passes a token threshold, keeping long sessions alive.
"""

from google.adk.apps import App
from google.adk.apps.compaction import EventsCompactionConfig, LlmEventSummarizer


def build_compacting_app(name: str, root_agent, llm) -> App:
    """Wrap root_agent in an App that compacts history at ~150K prompt tokens.

    token_threshold=150000 fires compaction far below both windows in use
    (262144 Qwen / 393216 DeepSeek). event_retention_size keeps the last 20
    raw events so the current task context is never summarized away.
    """
    return App(
        name=name,
        root_agent=root_agent,
        events_compaction_config=EventsCompactionConfig(
            summarizer=LlmEventSummarizer(llm=llm),
            compaction_interval=5,
            overlap_size=1,
            token_threshold=150_000,
            event_retention_size=20,
        ),
    )
