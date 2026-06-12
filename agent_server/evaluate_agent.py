import asyncio
import logging

import mlflow
from dotenv import load_dotenv
from mlflow.genai.agent_server import get_invoke_function
from mlflow.genai.scorers import (
    Completeness,
    ConversationalSafety,
    ConversationCompleteness,
    Fluency,
    KnowledgeRetention,
    RelevanceToQuery,
    Safety,
    ToolCallCorrectness,
    UserFrustration,
)
from mlflow.genai.simulators import ConversationSimulator
from mlflow.types.responses import ResponsesAgentRequest

# Load environment variables from .env if it exists
load_dotenv(dotenv_path=".env", override=True)
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

# need to import agent for our @invoke-registered function to be found
from agent_server import agent  # noqa: F401

# Create your evaluation dataset
# Refer to documentation for evaluations:
# Scorers: https://docs.databricks.com/aws/en/mlflow3/genai/eval-monitor/concepts/scorers
# Predefined LLM scorers: https://mlflow.org/docs/latest/genai/eval-monitor/scorers/llm-judge/predefined
# Defining custom scorers: https://docs.databricks.com/aws/en/mlflow3/genai/eval-monitor/custom-scorers
test_cases = [
    {
        "goal": "Generate a 30/60/90-day HSE risk plan for concrete deck pours scheduled next month.",
        "persona": "A site HSE manager on a bridge construction project who needs a risk plan for upcoming concrete works.",
        "simulation_guidelines": [
            "Start by asking what activities are coming up in the schedule before requesting the full plan.",
            "Provide the project name 'Pacific Highway Upgrade', start date '01/07/2026', work pack 'Bridge Deck Concrete Works', and person responsible 'Site HSE Manager' when prompted.",
            "Ask a follow-up about what the Top 5 risks are for the 30-day horizon.",
        ],
    },
    {
        "goal": "Find out what processes a Planning & Project Controls manager should own during the Construction phase.",
        "persona": "A newly appointed PTL (Project Team Lead) responsible for Planning & Project Controls who wants to understand their role in the Construction phase.",
        "simulation_guidelines": [
            "Initially ask broadly about what to focus on, before providing the specific role and phase.",
            "Provide your role as 'Planning & Project Controls' and phase as 'Construction' when asked.",
            "Ask a follow-up about which checkpoints are needed to unlock the Completion phase.",
        ],
    },
    {
        "goal": "Understand what HSE controls apply to crane lifts adjacent to a waterway.",
        "persona": "A rigger supervisor who is planning a crane lift to install bridge girders over a river and wants to know the applicable HSE controls.",
        "simulation_guidelines": [
            "Ask about applicable FSR controls first, then ask about environmental (SER) controls.",
            "Ask specifically about what makes a lift a Top 5 risk.",
        ],
    },
    {
        "goal": "Check what scheduled activities are coming up in the next 30 days that involve scaffolding.",
        "persona": "A construction supervisor who wants to see the upcoming scaffolding programme.",
        "simulation_guidelines": [
            "Ask to preview the schedule with a scaffold filter before requesting any full plan.",
            "Follow up by asking which activities carry the highest risk classification.",
        ],
    },
]

simulator = ConversationSimulator(
    test_cases=test_cases,
    max_turns=5,
    user_model="databricks:/databricks-claude-sonnet-4-5",
)

# Get the invoke function that was registered via @invoke decorator in your agent
invoke_fn = get_invoke_function()
assert invoke_fn is not None, (
    "No function registered with the `@invoke` decorator found."
    "Ensure you have a function decorated with `@invoke()`."
)

# if invoke function is async, wrap it in a sync function.
# The simulator may already be running an event loop, so we use nest_asyncio
# to allow nested run_until_complete() calls without deadlocking.
if asyncio.iscoroutinefunction(invoke_fn):
    import nest_asyncio

    nest_asyncio.apply()

    def predict_fn(input: list[dict], **kwargs) -> dict:
        req = ResponsesAgentRequest(input=input)
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(invoke_fn(req))
        return response.model_dump()
else:

    def predict_fn(input: list[dict], **kwargs) -> dict:
        req = ResponsesAgentRequest(input=input)
        response = invoke_fn(req)
        return response.model_dump()


def evaluate():
    mlflow.genai.evaluate(
        data=simulator,
        predict_fn=predict_fn,
        scorers=[
            Completeness(),
            ConversationCompleteness(),
            ConversationalSafety(),
            KnowledgeRetention(),
            UserFrustration(),
            Fluency(),
            RelevanceToQuery(),
            Safety(),
            ToolCallCorrectness(),
        ],
    )
