# The ADK agent loads only when something asks for it (`adk web` reads `root_agent`).
# QA Agent v2 (react_qa_agent.v2) does not need ADK, so importing the package stays light.


def __getattr__(name):
    if name == "root_agent":
        from .agent import root_agent
        return root_agent
    raise AttributeError(name)
