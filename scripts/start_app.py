#!/usr/bin/env python3
"""
Start script for running frontend and backend processes concurrently.

Requirements:
1. Not reporting ready until BOTH frontend and backend processes are ready
2. Exiting as soon as EITHER process fails
3. Printing error logs if either process fails

Usage:
    start-app [OPTIONS]

All options are passed through to the backend server (start-server).
See 'uv run start-server --help' for available options.
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

# Readiness patterns
BACKEND_READY = [r"Uvicorn running on", r"Application startup complete", r"Started server process"]
FRONTEND_READY = [r"Server is running on http://localhost"]


def check_port_available(port: int) -> bool:
    """Check if a port is available (nothing is actively listening on it)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("localhost", port))
        return False  # Something is listening
    except (ConnectionRefusedError, OSError):
        return True  # Nothing listening = available


class ProcessManager:
    def __init__(self, port=8000, no_ui=False):
        self.backend_process = None
        self.frontend_process = None
        self.backend_ready = False
        self.frontend_ready = False
        self.failed = threading.Event()
        self.backend_log = None
        self.frontend_log = None
        self.port = port
        self.no_ui = no_ui

    def check_ports(self):
        """Check that required ports are available before starting processes."""
        backend_port = self.port

        errors = []
        if not check_port_available(backend_port):
            errors.append(
                f"Port {backend_port} (backend) is already in use.\n"
                f"  To free it: lsof -ti :{backend_port} | xargs kill -9"
            )

        if not self.no_ui:
            frontend_port = int(os.environ.get("CHAT_APP_PORT", os.environ.get("PORT", "3000")))

            if backend_port == frontend_port:
                print(
                    f"ERROR: Backend and frontend are both configured to use port {backend_port}."
                )
                print("  Set CHAT_APP_PORT in .env to a different port (e.g., CHAT_APP_PORT=3000).")
                sys.exit(1)

            if not check_port_available(frontend_port):
                port_source = (
                    "CHAT_APP_PORT"
                    if os.environ.get("CHAT_APP_PORT")
                    else "PORT"
                    if os.environ.get("PORT")
                    else "default"
                )
                errors.append(
                    f"Port {frontend_port} (frontend, source: {port_source}) is already in use.\n"
                    f"  To free it: lsof -ti :{frontend_port} | xargs kill -9\n"
                    f"  Or set a different port: CHAT_APP_PORT=<port> in .env"
                )

        if errors:
            print("ERROR: Port(s) already in use:\n")
            for error in errors:
                print(f"  {error}\n")
            sys.exit(1)

    def monitor_process(self, process, name, log_file, patterns):
        is_ready = False
        try:
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break

                line = line.rstrip()
                log_file.write(line + "\n")
                print(f"[{name}] {line}")

                # Check readiness
                if not is_ready and any(re.search(p, line, re.IGNORECASE) for p in patterns):
                    is_ready = True
                    if name == "backend":
                        self.backend_ready = True
                    else:
                        self.frontend_ready = True
                    print(f"✓ {name.capitalize()} is ready!")

                    if self.no_ui and self.backend_ready:
                        print("\n" + "=" * 50)
                        print("✓ Backend is ready! (running without UI)")
                        print(f"✓ API available at http://localhost:{self.port}")
                        print("=" * 50 + "\n")
                    elif self.backend_ready and self.frontend_ready:
                        print("\n" + "=" * 50)
                        print("✓ Both frontend and backend are ready!")
                        print(f"✓ Open the frontend at http://localhost:{self.port}")
                        print("=" * 50 + "\n")

            process.wait()
            if process.returncode != 0:
                self.failed.set()

        except Exception as e:
            print(f"Error monitoring {name}: {e}")
            self.failed.set()

    # Imported from the single source of truth in agent_server/models.py
    from agent_server.models import AVAILABLE_MODELS, DEFAULT_MODEL

    def patch_frontend(self, frontend_dir: Path):
        """Inject model selector component and wire it into the chat API calls."""
        import json as _json

        # ------------------------------------------------------------------
        # 1. Write the ModelSelector component
        # ------------------------------------------------------------------
        components_dir = frontend_dir / "src" / "components"
        components_dir.mkdir(parents=True, exist_ok=True)

        options_js = _json.dumps(self.AVAILABLE_MODELS)
        default_js = _json.dumps(self.DEFAULT_MODEL)

        selector_tsx = f"""\
"use client";
import React from "react";

export const AVAILABLE_MODELS = {options_js};
export const DEFAULT_MODEL = {default_js};

interface Props {{
  value: string;
  onChange: (endpoint: string) => void;
}}

export default function ModelSelector({{ value, onChange }}: Props) {{
  return (
    <div style={{{{ display: "flex", alignItems: "center", gap: "8px", padding: "8px 12px",
                   background: "var(--model-selector-bg, #f5f5f5)",
                   borderBottom: "1px solid var(--model-selector-border, #e0e0e0)" }}}}>
      <label htmlFor="model-select" style={{{{ fontSize: "13px", fontWeight: 500, whiteSpace: "nowrap" }}}}>
        Model:
      </label>
      <select
        id="model-select"
        value={{value}}
        onChange={{(e) => onChange(e.target.value)}}
        style={{{{ fontSize: "13px", padding: "4px 8px", borderRadius: "6px",
                  border: "1px solid #ccc", background: "white", cursor: "pointer" }}}}
      >
        {{AVAILABLE_MODELS.map((m) => (
          <option key={{m.endpoint}} value={{m.endpoint}}>
            {{m.label}}
          </option>
        ))}}
      </select>
    </div>
  );
}}
"""
        (components_dir / "ModelSelector.tsx").write_text(selector_tsx)

        # ------------------------------------------------------------------
        # 2. Patch every file that posts to the /api/chat or /invocations
        #    endpoint by inserting custom_inputs.model_endpoint.
        #    Strategy: find files that contain "custom_inputs" or the fetch/
        #    post pattern and inject model state there. If the template
        #    already has custom_inputs support we can just add our key;
        #    otherwise we add it to the body construction.
        # ------------------------------------------------------------------
        src_dir = frontend_dir / "src"
        patched_any = False

        # Look for the file that builds the request body sent to the backend
        for candidate in list(src_dir.rglob("*.ts")) + list(src_dir.rglob("*.tsx")):
            text = candidate.read_text(errors="replace")

            # Pattern 1: body already has custom_inputs object
            if "custom_inputs" in text and "model_endpoint" not in text:
                new_text = text.replace(
                    "custom_inputs: {",
                    "custom_inputs: { model_endpoint: selectedModel,",
                )
                if new_text != text:
                    candidate.write_text(new_text)
                    patched_any = True
                    print(f"  Patched custom_inputs in {candidate.relative_to(frontend_dir)}")

        if not patched_any:
            # Pattern 2: find where the POST body is assembled (JSON.stringify / body: {)
            # and inject custom_inputs wholesale.
            for candidate in list(src_dir.rglob("*.ts")) + list(src_dir.rglob("*.tsx")):
                text = candidate.read_text(errors="replace")
                if "JSON.stringify" in text and ("input" in text or "messages" in text):
                    # Insert custom_inputs before the closing of the stringified object
                    # by replacing the stringify call pattern
                    import re as _re
                    new_text = _re.sub(
                        r'(JSON\.stringify\s*\(\s*\{)',
                        r'\1 custom_inputs: { model_endpoint: selectedModel },',
                        text,
                        count=1,
                    )
                    if new_text != text:
                        candidate.write_text(new_text)
                        patched_any = True
                        print(f"  Patched JSON.stringify in {candidate.relative_to(frontend_dir)}")
                        break

        # ------------------------------------------------------------------
        # 3. Patch the main page (page.tsx / page.ts) to add model state
        #    and render <ModelSelector />.
        # ------------------------------------------------------------------
        page_candidates = list(src_dir.rglob("page.tsx")) + list(src_dir.rglob("page.ts"))
        for page_file in page_candidates:
            text = page_file.read_text(errors="replace")
            if "selectedModel" in text:
                # Already patched
                break

            # Add import
            if "ModelSelector" not in text:
                import_line = (
                    'import ModelSelector, { DEFAULT_MODEL } from "@/components/ModelSelector";\n'
                )
                # Insert after the last import line
                lines = text.splitlines(keepends=True)
                last_import = max(
                    (i for i, l in enumerate(lines) if l.startswith("import ")),
                    default=0,
                )
                lines.insert(last_import + 1, import_line)
                text = "".join(lines)

            # Add useState for selectedModel (after "use client" or first line)
            if "useState" in text and "selectedModel" not in text:
                text = text.replace(
                    "useState(",
                    "useState(",
                    1,  # no-op, just finding it
                )
                # Add state declaration inside the component function body
                # by inserting after the first const [ ... ] = useState( pattern
                import re as _re
                text = _re.sub(
                    r'(const\s+\[\w+,\s*\w+\]\s*=\s*useState\([^)]*\);)',
                    r'\1\n  const [selectedModel, setSelectedModel] = React.useState(DEFAULT_MODEL);',
                    text,
                    count=1,
                )

            # Render ModelSelector before the first <div or <main
            import re as _re
            text = _re.sub(
                r'(return\s*\(\s*\n?\s*)(<(?:div|main))',
                r'\1<>\n      <ModelSelector value={selectedModel} onChange={setSelectedModel} />\n      \2',
                text,
                count=1,
            )
            # Close the fragment
            text = _re.sub(
                r'(\s*</(?:div|main)>\s*\)\s*;?\s*$)',
                r'\n    </>\n  );\n',
                text,
                count=1,
            )

            page_file.write_text(text)
            print(f"  Patched page component: {page_file.relative_to(frontend_dir)}")
            break

        print("Frontend model selector patch applied.")

        # ------------------------------------------------------------------
        # 4. Patch next.config to proxy /download/* to the backend so the
        #    browser's native download works via a relative link.
        # ------------------------------------------------------------------
        self._patch_next_config_rewrites(frontend_dir)

    def _patch_next_config_rewrites(self, frontend_dir: Path):
        backend_url = f"http://localhost:{self.port}"
        rewrite_snippet = f"""
  async rewrites() {{
    return [
      {{
        source: '/download/:token',
        destination: '{backend_url}/download/:token',
      }},
    ];
  }},"""

        for config_name in ("next.config.mjs", "next.config.js", "next.config.ts"):
            config_path = frontend_dir / config_name
            if not config_path.exists():
                continue
            text = config_path.read_text(errors="replace")
            if "/download/" in text:
                # Already patched
                break
            # Insert rewrites before the closing of the config object/export
            import re as _re
            # Match the last closing brace of the config export
            new_text = _re.sub(
                r'(const\s+nextConfig\s*=\s*\{)',
                r'\1' + rewrite_snippet,
                text,
                count=1,
            )
            if new_text == text:
                # Fallback: try module.exports pattern
                new_text = _re.sub(
                    r'(module\.exports\s*=\s*\{)',
                    r'\1' + rewrite_snippet,
                    text,
                    count=1,
                )
            if new_text != text:
                config_path.write_text(new_text)
                print(f"  Patched Next.js rewrites in {config_name}")
            break
        else:
            # No config found — create a minimal one
            config_path = frontend_dir / "next.config.js"
            config_path.write_text(
                f"/** @type {{import('next').NextConfig}} */\n"
                f"const nextConfig = {{\n{rewrite_snippet}\n}};\n\n"
                f"module.exports = nextConfig;\n"
            )
            print("  Created next.config.js with download rewrite")

    def clone_frontend_if_needed(self):
        if Path("e2e-chatbot-app-next").exists():
            return True

        print("Cloning e2e-chatbot-app-next...")
        for url in [
            "https://github.com/databricks/app-templates.git",
            "git@github.com:databricks/app-templates.git",
        ]:
            try:
                subprocess.run(
                    ["git", "clone", "--filter=blob:none", "--sparse", url, "temp-app-templates"],
                    check=True,
                    capture_output=True,
                )
                break
            except subprocess.CalledProcessError:
                continue
        else:
            print("ERROR: Failed to clone repository.")
            print(
                "Manually download from: https://download-directory.github.io/?url=https://github.com/databricks/app-templates/tree/main/e2e-chatbot-app-next"
            )
            return False

        subprocess.run(
            ["git", "sparse-checkout", "set", "e2e-chatbot-app-next"],
            cwd="temp-app-templates",
            check=True,
        )
        Path("temp-app-templates/e2e-chatbot-app-next").rename("e2e-chatbot-app-next")
        shutil.rmtree("temp-app-templates", ignore_errors=True)
        return True

    def start_process(self, cmd, name, log_file, patterns, cwd=None):
        print(f"Starting {name}...")
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=cwd
        )

        thread = threading.Thread(
            target=self.monitor_process, args=(process, name, log_file, patterns), daemon=True
        )
        thread.start()
        return process

    def print_logs(self, log_path):
        print(f"\nLast 50 lines of {log_path}:")
        print("-" * 40)
        try:
            lines = Path(log_path).read_text().splitlines()
            print("\n".join(lines[-50:]))
        except FileNotFoundError:
            print(f"(no {log_path} found)")
        print("-" * 40)

    def cleanup(self):
        print("\n" + "=" * 42)
        print("Shutting down..." if self.no_ui else "Shutting down both processes...")
        print("=" * 42)

        for proc in [self.backend_process, self.frontend_process]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, Exception):
                    proc.kill()

        if self.backend_log:
            self.backend_log.close()
        if self.frontend_log:
            self.frontend_log.close()

    def run(self, backend_args=None):
        load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)
        if not os.environ.get("DATABRICKS_APP_NAME"):
            self.check_ports()

        if not self.no_ui:
            if not self.clone_frontend_if_needed():
                print("WARNING: Failed to clone frontend. Continuing with backend only.")
                self.no_ui = True
            else:
                # Set API_PROXY environment variable for frontend to connect to backend
                os.environ["API_PROXY"] = f"http://localhost:{self.port}/invocations"

        # Open log files
        self.backend_log = open("backend.log", "w", buffering=1)
        if not self.no_ui:
            self.frontend_log = open("frontend.log", "w", buffering=1)

        try:
            # Build backend command, passing through all arguments
            backend_cmd = ["uv", "run", "start-server"]
            if backend_args:
                backend_cmd.extend(backend_args)

            # Start backend
            self.backend_process = self.start_process(
                backend_cmd, "backend", self.backend_log, BACKEND_READY
            )

            if not self.no_ui:
                # Setup and start frontend
                frontend_dir = Path("e2e-chatbot-app-next")
                print("Patching frontend with model selector...")
                self.patch_frontend(frontend_dir)
                for cmd, desc in [("npm install", "install"), ("npm run build", "build")]:
                    print(f"Running npm {desc}...")
                    result = subprocess.run(
                        cmd.split(), cwd=frontend_dir, capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        print(f"npm {desc} failed: {result.stderr}")
                        return 1

                self.frontend_process = self.start_process(
                    ["npm", "run", "start"],
                    "frontend",
                    self.frontend_log,
                    FRONTEND_READY,
                    cwd=frontend_dir,
                )

                print(
                    f"\nMonitoring processes (Backend PID: {self.backend_process.pid}, Frontend PID: {self.frontend_process.pid})\n"
                )
            else:
                print(f"\nMonitoring backend process (PID: {self.backend_process.pid})\n")

            # Wait for failure
            while not self.failed.is_set():
                time.sleep(0.1)
                if self.backend_process.poll() is not None:
                    self.failed.set()
                    break
                if (
                    not self.no_ui
                    and self.frontend_process
                    and self.frontend_process.poll() is not None
                ):
                    self.failed.set()
                    break

            # Determine which failed
            if self.no_ui or self.backend_process.poll() is not None:
                failed_name = "backend"
                failed_proc = self.backend_process
            else:
                failed_name = "frontend"
                failed_proc = self.frontend_process
            exit_code = failed_proc.returncode if failed_proc else 1

            print(
                f"\n{'=' * 42}\nERROR: {failed_name} process exited with code {exit_code}\n{'=' * 42}"
            )
            self.print_logs("backend.log")
            if not self.no_ui:
                self.print_logs("frontend.log")
            return exit_code

        except KeyboardInterrupt:
            print("\nInterrupted")
            return 0

        finally:
            self.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="Start agent frontend and backend",
        usage="%(prog)s [OPTIONS]\n\nAll options are passed through to start-server. "
        "Use 'uv run start-server --help' for available options.",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Run backend only, skip frontend UI",
    )
    args, backend_args = parser.parse_known_args()

    # Extract port from backend_args if specified
    port = 8000
    for i, arg in enumerate(backend_args):
        if arg == "--port" and i + 1 < len(backend_args):
            try:
                port = int(backend_args[i + 1])
            except ValueError:
                pass
            break

    sys.exit(ProcessManager(port=port, no_ui=args.no_ui).run(backend_args))


if __name__ == "__main__":
    main()
