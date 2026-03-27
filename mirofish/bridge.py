"""
MiroFish API Bridge — graph construction only (no social simulation).

=============================================================================
INSTALLATION SOURCE
=============================================================================
GitHub repository:  https://github.com/nikmcfly/MiroFish-Offline
Local clone path:   /tmp/MiroFish-Offline
Type:               Python/Flask application (NOT a PyPI package, NOT npm)

=============================================================================
PREREQUISITES
=============================================================================
Neo4j 5.15 (Docker):
    docker run -d --name mirofish-neo4j \
      -p 7474:7474 -p 7687:7687 \
      -e NEO4J_AUTH=neo4j/mirofish \
      -e NEO4J_PLUGINS='["apoc"]' \
      neo4j:5.15-community

Ollama + models (binary at /tmp/ollama_extract/bin/ollama):
    OLLAMA_MODELS=/tmp/ollama_models /tmp/ollama_extract/bin/ollama serve &
    OLLAMA_MODELS=/tmp/ollama_models /tmp/ollama_extract/bin/ollama pull qwen2.5
    OLLAMA_MODELS=/tmp/ollama_models /tmp/ollama_extract/bin/ollama pull nomic-embed-text

Python deps (installed system-wide):
    pip3 install flask flask-cors openai neo4j python-dotenv PyMuPDF \
                 charset-normalizer chardet pydantic --break-system-packages

=============================================================================
START COMMAND
=============================================================================
    cd /tmp/MiroFish-Offline
    FLASK_PORT=5001 python3 backend/run.py

The server binds on 0.0.0.0:5001 by default (FLASK_PORT env var overrides).

PORT 3000 is occupied by Next.js — must not use port 3000.
PORT 8000 is occupied by mock-mcs Docker container — must not use port 8000.

=============================================================================
CONFIRMED API CONTRACT  (verified from source: backend/app/api/graph.py)
=============================================================================
Health check:
    GET /health
    Response: {"status": "ok", "service": "MiroFish-Offline Backend"}

STEP 1 — Upload documents and generate ontology:
    POST /api/graph/ontology/generate
    Content-Type: multipart/form-data
    Fields:
        files               — one or more uploaded files (txt/pdf/md)
        simulation_requirement — string description (REQUIRED)
        project_name        — optional label
        additional_context  — optional extra notes
    Success response:
        {
          "success": true,
          "data": {
            "project_id": "proj_<hex>",   <-- KEY: project_id (NOT graph_id)
            "project_name": "...",
            "ontology": { "entity_types": [...], "edge_types": [...] },
            "analysis_summary": "...",
            "files": [...],
            "total_text_length": 12345
          }
        }

STEP 2 — Trigger asynchronous graph build:
    POST /api/graph/build
    Content-Type: application/json
    Body: { "project_id": "proj_<hex>" }
    Success response:
        {
          "success": true,
          "data": {
            "project_id": "proj_<hex>",
            "task_id": "<uuid>",           <-- KEY: task_id (UUID string)
            "message": "Graph build task started. ..."
          }
        }

STEP 3 — Poll task until complete:
    GET /api/graph/task/{task_id}
    Success response:
        {
          "success": true,
          "data": {
            "task_id": "<uuid>",
            "status": "<status_value>",    <-- STATUS FIELD: "status"
            "progress": 0-100,
            "message": "...",
            "result": {                    <-- populated when status == "completed"
              "project_id": "proj_<hex>",
              "graph_id": "<uuid>",        <-- KEY: graph_id (from result dict)
              "node_count": N,
              "edge_count": N,
              "chunk_count": N
            },
            "error": null
          }
        }
    Status values (TaskStatus enum):
        "pending"    — task queued, not started
        "processing" — graph build in progress
        "completed"  — graph build finished successfully  <-- READY VALUE
        "failed"     — build failed; check "error" field

STEP 4 — Fetch graph data (optional verification):
    GET /api/graph/data/{graph_id}
    Success response: { "success": true, "data": { nodes: [...], edges: [...] } }

PROJECT STATUS (ProjectStatus enum, on GET /api/graph/project/{project_id}):
    "created"            — project exists, no ontology yet
    "ontology_generated" — Step 1 complete, ready for Step 2
    "graph_building"     — Step 2 in progress
    "graph_completed"    — both steps done
    "failed"             — error state

=============================================================================
IMPORTANT NOTES
=============================================================================
* DO NOT call POST /api/simulation/start — the simulation layer is unused in
  this pipeline. MiroFish is used ONLY for knowledge graph construction.
* The ontology generate step calls Ollama LLM (qwen2.5) — on CPU-only hardware
  this takes 5-20 minutes per call. Plan accordingly.
* graph_id is NOT returned by Step 1. It is only available after Step 2
  completes, inside task result: data.result.graph_id
* All routes are prefixed /api/graph/ (blueprinted at that prefix).
* The /health endpoint has NO prefix.
=============================================================================
"""

# ARCHITECTURAL CONSTRAINT: /api/simulation/start must never be called from this module

import time
import requests
from pathlib import Path
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MIROFISH_BASE_URL, MIROFISH_POLL_INTERVAL_S, MIROFISH_POLL_TIMEOUT_S


class MiroFishError(RuntimeError):
    """Raised on any MiroFish API failure: HTTP error, missing fields, or timeout."""


def build_graph(question: str, doc_paths: list[Path]) -> str:
    """
    Upload documents and build a knowledge graph via MiroFish.

    Implements the two-step flow:
        1. POST /api/graph/ontology/generate  (multipart) -> project_id
        2. POST /api/graph/build              (JSON)       -> task_id
        3. GET  /api/graph/task/{task_id}     (poll)       -> completed
        4. Return graph_id from task result

    Args:
        question:  The market question passed as simulation_requirement.
        doc_paths: Paths to text/pdf/md documents to upload.

    Returns:
        graph_id (UUID string) once the graph build is complete.

    Raises:
        MiroFishError: on any HTTP error, API-level error, missing field,
                       or poll timeout. Never leaks requests.RequestException.
    """
    bridge = MiroFishBridge()
    project_id = bridge._generate_ontology(list(doc_paths), question, None)
    task_id = bridge._start_build(project_id)
    return bridge._poll_until_complete(task_id)


class MiroFishBridge:
    """
    Thin HTTP client for the MiroFish graph-construction pipeline.

    Implements the two-step flow:
        1. POST /api/graph/ontology/generate  -> project_id
        2. POST /api/graph/build              -> task_id
        3. GET  /api/graph/task/{task_id}     -> poll until status == "completed"
        4. Extract graph_id from task result
    """

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or MIROFISH_BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def health(self) -> bool:
        """Return True if MiroFish is reachable and healthy."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False

    def build_graph(
        self,
        doc_paths: list[str],
        question: str,
        project_name: Optional[str] = None,
    ) -> str:
        """
        Upload documents and build a knowledge graph.

        Args:
            doc_paths:    Absolute paths to text/pdf/md files to upload.
            question:     The market question / simulation_requirement string.
            project_name: Optional human-readable label.

        Returns:
            graph_id (UUID string) once graph build is complete.

        Raises:
            MiroFishError on API error or timeout.
        """
        project_id = self._generate_ontology(doc_paths, question, project_name)
        task_id = self._start_build(project_id)
        graph_id = self._poll_until_complete(task_id)
        return graph_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_ontology(
        self,
        doc_paths: list,
        question: str,
        project_name: Optional[str],
    ) -> str:
        """Step 1: upload files + requirement, return project_id."""
        files = []
        handles = []
        try:
            for path in doc_paths:
                path_str = str(path)
                fh = open(path_str, "rb")
                handles.append(fh)
                files.append(("files", (path_str.split("/")[-1], fh, "text/plain")))

            data = {"simulation_requirement": question}
            if project_name:
                data["project_name"] = project_name

            try:
                r = self.session.post(
                    f"{self.base_url}/api/graph/ontology/generate",
                    files=files,
                    data=data,
                    timeout=1800,  # LLM inference on CPU can take 20+ min
                )
            except requests.RequestException as exc:
                raise MiroFishError(
                    f"ontology/generate request failed: {exc}"
                ) from exc
        finally:
            for fh in handles:
                fh.close()

        if not r.ok:
            raise MiroFishError(
                f"ontology/generate failed HTTP {r.status_code}: {r.text[:400]}"
            )
        try:
            body = r.json()
        except ValueError as exc:
            raise MiroFishError(
                f"ontology/generate returned non-JSON body: {r.text[:200]}"
            ) from exc
        if not body.get("success"):
            raise MiroFishError(f"ontology/generate error: {body.get('error')}")
        project_id = (body.get("data") or {}).get("project_id")
        if not project_id:
            raise MiroFishError(
                f"ontology/generate response missing data.project_id: {body}"
            )
        return project_id

    def _start_build(self, project_id: str) -> str:
        """Step 2: trigger graph build, return task_id."""
        try:
            r = self.session.post(
                f"{self.base_url}/api/graph/build",
                json={"project_id": project_id},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise MiroFishError(
                f"graph/build request failed: {exc}"
            ) from exc

        if not r.ok:
            raise MiroFishError(
                f"graph/build failed HTTP {r.status_code}: {r.text[:400]}"
            )
        try:
            body = r.json()
        except ValueError as exc:
            raise MiroFishError(
                f"graph/build returned non-JSON body: {r.text[:200]}"
            ) from exc
        if not body.get("success"):
            raise MiroFishError(f"graph/build error: {body.get('error')}")
        task_id = (body.get("data") or {}).get("task_id")
        if not task_id:
            raise MiroFishError(
                f"graph/build response missing data.task_id: {body}"
            )
        return task_id

    def _poll_until_complete(self, task_id: str) -> str:
        """
        Poll GET /api/graph/task/{task_id} until status == "completed".

        Returns graph_id from the completed task result.
        Raises MiroFishError on failure or timeout.
        """
        deadline = time.time() + MIROFISH_POLL_TIMEOUT_S
        while time.time() < deadline:
            try:
                r = self.session.get(
                    f"{self.base_url}/api/graph/task/{task_id}",
                    timeout=10,
                )
            except requests.RequestException as exc:
                raise MiroFishError(
                    f"task poll request failed: {exc}"
                ) from exc

            if not r.ok:
                raise MiroFishError(
                    f"task poll failed HTTP {r.status_code}: {r.text[:200]}"
                )
            try:
                body = r.json()
            except ValueError as exc:
                raise MiroFishError(
                    f"task poll returned non-JSON body: {r.text[:200]}"
                ) from exc

            task = body.get("data", {})
            status = task.get("status")  # "pending" | "processing" | "completed" | "failed"

            if status == "completed":
                result = task.get("result") or {}
                graph_id = result.get("graph_id")
                if not graph_id:
                    raise MiroFishError(
                        f"task completed but graph_id missing in result: {result}"
                    )
                return graph_id

            if status == "failed":
                raise MiroFishError(
                    f"graph build task failed: {task.get('error', 'no details')}"
                )

            # Still pending or processing — wait and retry
            time.sleep(MIROFISH_POLL_INTERVAL_S)

        raise MiroFishError(
            f"Graph build task {task_id} did not complete within "
            f"{MIROFISH_POLL_TIMEOUT_S}s"
        )


if __name__ == "__main__":
    import tempfile
    import pathlib
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("test document content")
        tmp = pathlib.Path(f.name)
    gid = build_graph("Will this test pass?", [tmp])
    print(f"graph_id: {gid}")
    tmp.unlink()
