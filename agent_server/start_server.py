import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException
from fastapi.responses import Response
from mlflow.genai.agent_server import AgentServer, setup_mlflow_git_based_version_tracking

logger = logging.getLogger(__name__)

# Load env vars from .env before importing the agent for proper auth
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

# Need to import the agent to register the functions with the server
import agent_server.agent  # noqa: E402
from agent_server.file_store import get_file  # noqa: E402

agent_server = AgentServer("ResponsesAgent", enable_chat_proxy=True)

# Define the app as a module level variable to enable multiple workers
app = agent_server.app  # noqa: F841


@app.get("/download/{token}")
async def download_file(token: str):
    result = get_file(token)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or expired.")
    content, filename = result
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
try:
    setup_mlflow_git_based_version_tracking()
except Exception as e:
    logger.warning(f"MLflow version tracking setup failed (non-fatal): {e}")


def main():
    agent_server.run(app_import_string="agent_server.start_server:app")
