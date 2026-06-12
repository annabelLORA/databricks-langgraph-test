import json
import logging
from datetime import datetime
from typing import AsyncGenerator, Optional

import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain_core.tools import tool
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    to_chat_completions_input,
)

from agent_server.file_store import store_file
from agent_server.hse_planner import build_risk_plan, write_excel
from agent_server.knowledge import get_activities_in_window
from agent_server.models import AVAILABLE_MODELS, DEFAULT_MODEL
from agent_server.playbook import search_playbook
from agent_server.utils import (
    get_session_id,
    process_agent_astream_events,
)

logger = logging.getLogger(__name__)
mlflow.langchain.autolog()

_VALID_ENDPOINTS = {m["endpoint"] for m in AVAILABLE_MODELS}
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
sp_workspace_client = WorkspaceClient()

PLAYBOOK_SYSTEM_PROMPT = """
## Project Lifecycle Playbook Workflow

You are ALSO the **Project Lifecycle Playbook Assistant**. Activate this workflow whenever the user
asks about process, next steps, what they should do, their responsibilities, or anything that implies
they want to know which playbook processes apply to their role and phase.

### Intake Gate (MANDATORY before calling the playbook tool)
You CANNOT give useful guidance without knowing BOTH the user's **role** AND their **project phase**.

If either is missing, reply ONLY with:

> To point you to the right actions I need two quick things:
> **1. Your role** — e.g. Planning & Project Controls, Project Leader, Bid Leader, Commercial,
> Delivery Leader, Design, Commissioning/Services, Quality, HSE, Procurement, Digital, etc.
> **2. Your current phase** — Tender · Planning · Procurement & Supply · Design · Construction ·
> Completion (and the sub-phase if you know it, e.g. Detailed Design, Mobilisation, Handover).

Only ask for the missing part. Do NOT list example tasks or pre-empt the answer.

Once you have BOTH, call the `query_playbook_processes` tool with the role, phase, and sub-phase.

### Response format after retrieval
1. One-line confirmation of how you read the question.
2. **Recommended actions by priority** — group under:
   - **🔴 P1 – Critical** (Functional Checkpoints / formal gateways)
   - **🟠 P2 – You own this** (Primary Responsible)
   - **🟢 P3 – Support/contribute** (Secondary Responsible)
   Each action: **Bold title** *(ID, Sub-Phase)*, one sentence on what to do, Owner/with, gateway flag if applicable, and supporting links as `[Title](URL)`.
3. A closing note: **"What unlocks the next phase"** — flag the checkpoints to clear.

Hyperlink rules: always use `[Readable title](URL)` — never bare URLs, never invented URLs.
If no link exists, write *"No linked reference."*
"""

SYSTEM_PROMPT = """You are a Civil Engineering HSE Risk Planning Specialist for Laing O'Rourke. \
Strict Australian English. No preamble, commentary, or meta-text. Generate everything from \
project knowledge only. If required input is missing, ask before generating.

You handle two types of requests:

**1. General Construction Q&A**
Answer questions about construction methods, HSE standards, risk management, engineering \
principles, site management, safety regulations, and industry best practices. Be clear, \
practical, and accurate. Cite AS/NZS standards and Australian legislation where applicable.

**2. HSE Risk Plan Generation (30/60/90 Day)**
Detect when the user is asking for a risk plan, risk register, SWMS, JSA, 30/60/90 plan, \
or HSE risk assessment. Extract the following from the message (ask if missing):
  - project_name: Project/Office/Depot name
  - start_date: Plan start date in DD/MM/YYYY format (default: today if not given)
  - work_pack: Work pack description (e.g. "Bridge Deck Concrete Works — June 2026")
  - person_responsible: Name or role (default: [Assignee] if not given)
  - work_pack_filter: Optional keyword to filter schedule activities (e.g. "concrete", "crane")

Once you have those, call `generate_hse_risk_plan`. The tool will:
- Pull real scheduled activities from the P6/Aphex project programme
- Classify each activity to HSE risk categories
- Select applicable FSR/SER and cross-cutting controls from the HSE Controls library
- Apply the 30/60/90 day horizon logic (5 Top 5 risks per horizon)
- Populate and return the base template workbook as a downloadable Excel file

After the tool responds, present:
1. An executive summary using nested bullets — one block per horizon, each risk cluster as a sub-bullet with its single key mitigation:
   - **30-Day Horizon**
     - *Risk Category*: Key mitigation
     - ...
   - **60-Day Horizon**
     - ...
   - **90-Day Horizon**
     - ...
2. A download link on its own line using the download_token from the tool response:
   [📥 Download Risk Plan](/download/{download_token})

Never fabricate risk details, categories, or controls — everything comes from the project data.
""" + PLAYBOOK_SYSTEM_PROMPT


# ── Tool 1: HSE Risk Plan Generation ─────────────────────────────────────────

@tool
def generate_hse_risk_plan(
    project_name: str,
    start_date: str,
    work_pack: str,
    person_responsible: str,
    work_pack_filter: str = "",
) -> str:
    """Generate a 30/60/90 day HSE Risk Plan Excel workbook from the project schedule.

    Args:
        project_name: Project/Office/Depot name (written into B1 of the template).
        start_date: Plan start date in DD/MM/YYYY format (e.g. "12/06/2026").
        work_pack: Work pack description for the plan name (e.g. "Bridge Deck Concrete").
        person_responsible: Person responsible text for every row (e.g. "[Assignee]" or a name).
        work_pack_filter: Optional keyword to filter schedule activities (e.g. "concrete").
                          Leave blank to include all activities in the 90-day window.

    Returns:
        JSON with base64-encoded Excel file, filename, row count, and executive summary.
    """
    try:
        plan_start = datetime.strptime(start_date.strip(), "%d/%m/%Y")
    except ValueError:
        return json.dumps({"error": f"Invalid start_date format: {start_date!r}. Use DD/MM/YYYY."})

    # Pull activities from both P6 and Aphex; combine and de-duplicate by description
    p6_acts = get_activities_in_window(plan_start, days=90, keyword_filter=work_pack_filter, source="P6")
    aphex_acts = get_activities_in_window(plan_start, days=90, keyword_filter=work_pack_filter, source="Aphex")

    # Prefer P6 where descriptions overlap; otherwise merge
    p6_descs = {a["description"].lower() for a in p6_acts}
    combined = p6_acts + [a for a in aphex_acts if a["description"].lower() not in p6_descs]
    combined.sort(key=lambda a: a["start_date"])

    # Cap per horizon to keep Excel manageable (max 10 tasks × ~8 controls = ~80 rows per horizon)
    from datetime import timedelta
    d30 = plan_start + timedelta(days=30)
    d60 = plan_start + timedelta(days=60)
    h30 = [a for a in combined if a["start_date"] <= d30][:10]
    h60 = [a for a in combined if d30 < a["start_date"] <= d60][:10]
    h90 = [a for a in combined if d60 < a["start_date"]][:10]
    combined = h30 + h60 + h90

    if not combined:
        return json.dumps({
            "error": (
                f"No activities found in the 90-day window starting {start_date} "
                f"matching filter: {work_pack_filter!r}. "
                "Try a broader filter or leave work_pack_filter blank."
            )
        })

    rows = build_risk_plan(combined, plan_start, person_responsible)

    start_month_year = plan_start.strftime("%B %Y")
    excel_bytes = write_excel(project_name, work_pack, start_month_year, rows)

    date_str = plan_start.strftime("%d_%m_%Y")
    filename = f"{date_str} - 30-60-90 - {work_pack} - {start_month_year}.xlsx"
    download_token = store_file(excel_bytes, filename)

    # Build summary per horizon
    from collections import Counter
    def hz_summary(hz: str) -> dict:
        hz_rows = [r for r in rows if r["Timing"] == hz]
        tasks = list(dict.fromkeys(r["Job Task.Record No."] for r in hz_rows))
        top5 = [r["Job Task.Record No."] for r in hz_rows if r["Top 5 Risk"] == "True"]
        categories = Counter(r["Risk Category.Name"] for r in hz_rows)
        return {
            "task_count": len(tasks),
            "control_rows": len(hz_rows),
            "top5_tasks": list(dict.fromkeys(top5)),
            "top_categories": [cat for cat, _ in categories.most_common(5)],
        }

    summary = {hz: hz_summary(hz) for hz in ("30D", "60D", "90D")}

    return json.dumps({
        "filename": filename,
        "download_token": download_token,
        "total_rows": len(rows),
        "activity_count": len(combined),
        "summary": summary,
    })


# ── Tool 2: Schedule preview ──────────────────────────────────────────────────

@tool
def get_schedule_activities(start_date: str = "", keyword_filter: str = "") -> str:
    """Preview project schedule activities in the 30/60/90 day window.

    Use this to check what activities are available before generating a full risk plan,
    or to answer general questions about the upcoming programme.

    Args:
        start_date: Window start in DD/MM/YYYY format. Defaults to today.
        keyword_filter: Optional keyword to filter activities (e.g. 'crane', 'concrete').

    Returns:
        JSON summary of activities by horizon.
    """
    if start_date:
        try:
            plan_start = datetime.strptime(start_date.strip(), "%d/%m/%Y")
        except ValueError:
            plan_start = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        plan_start = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)

    p6 = get_activities_in_window(plan_start, 90, keyword_filter, "P6")
    aphex = get_activities_in_window(plan_start, 90, keyword_filter, "Aphex")
    p6_descs = {a["description"].lower() for a in p6}
    combined = p6 + [a for a in aphex if a["description"].lower() not in p6_descs]
    combined.sort(key=lambda a: a["start_date"])

    d30 = plan_start
    d30_end = plan_start.__class__(plan_start.year, plan_start.month, plan_start.day)
    from datetime import timedelta
    h30 = [a for a in combined if (a["start_date"] - plan_start).days <= 30]
    h60 = [a for a in combined if 30 < (a["start_date"] - plan_start).days <= 60]
    h90 = [a for a in combined if 60 < (a["start_date"] - plan_start).days <= 90]

    def fmt(acts):
        return [
            {
                "date": a["start_date"].strftime("%d/%m/%Y"),
                "code": a["activity_code"],
                "description": a["description"],
                "source": a["source"],
            }
            for a in acts[:20]
        ]

    return json.dumps({
        "window_start": plan_start.strftime("%d/%m/%Y"),
        "filter": keyword_filter or "(none)",
        "30D": {"count": len(h30), "activities": fmt(h30)},
        "60D": {"count": len(h60), "activities": fmt(h60)},
        "90D": {"count": len(h90), "activities": fmt(h90)},
        "total": len(combined),
    })


# ── Tool 3: Playbook process search ──────────────────────────────────────────

@tool
def query_playbook_processes(role: str, phase: str, sub_phase: str = "") -> str:
    """Search the Project Lifecycle Playbook for processes matching a role and phase.

    Use this when a user asks about their responsibilities, what they should do next,
    or which processes apply to them at a given project phase.

    Args:
        role: The user's role (e.g. "Planning & Project Controls", "Bid Leader", "Commercial").
        phase: The project phase (e.g. "Tender", "Design", "Construction", "Completion").
        sub_phase: Optional sub-phase (e.g. "Detailed Design", "Mobilisation", "Handover").

    Returns:
        JSON with p1 (critical checkpoints), p2 (owned processes), p3 (contributions),
        plus canonical role/phase labels used for matching.
    """
    result = search_playbook(role, phase, sub_phase)
    return json.dumps(result)


# ── Tool 4: Current time ──────────────────────────────────────────────────────

@tool
def get_current_time() -> str:
    """Get the current date and time."""
    return datetime.now().isoformat()


# ── Agent initialisation ──────────────────────────────────────────────────────

_agent_cache: dict[str, object] = {}


async def init_agent(model_endpoint: str = DEFAULT_MODEL, workspace_client=None):
    endpoint = model_endpoint if model_endpoint in _VALID_ENDPOINTS else DEFAULT_MODEL
    if endpoint in _agent_cache:
        return _agent_cache[endpoint]
    tools = [get_current_time, get_schedule_activities, generate_hse_risk_plan, query_playbook_processes]
    agent = create_agent(
        tools=tools,
        model=ChatDatabricks(
            endpoint=endpoint,
            temperature=0.2,
            top_p=0.5,
            model_kwargs={"extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"}},
        ),
        system_prompt=SYSTEM_PROMPT,
    )
    _agent_cache[endpoint] = agent
    return agent


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    outputs = [
        event.item
        async for event in stream_handler(request)
        if event.type == "response.output_item.done"
    ]
    return ResponsesAgentResponse(output=outputs)


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    if session_id := get_session_id(request):
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})

    model_endpoint = DEFAULT_MODEL
    if request.custom_inputs and isinstance(request.custom_inputs, dict):
        model_endpoint = request.custom_inputs.get("model_endpoint", DEFAULT_MODEL)

    agent = await init_agent(model_endpoint=model_endpoint)
    messages = {"messages": to_chat_completions_input([i.model_dump() for i in request.input])}

    async for event in process_agent_astream_events(
        agent.astream(input=messages, stream_mode=["updates", "messages"])
    ):
        yield event
