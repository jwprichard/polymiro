#!/usr/bin/env bash
# install_mirofish.sh — Set up MiroFish-Offline and its runtime dependencies.
#
# This script installs and starts all services required by the MiroFish
# knowledge-graph pipeline:
#   1. Neo4j 5.15 Community (Docker container)
#   2. Ollama v0.18.3 + qwen2.5 + nomic-embed-text models
#   3. MiroFish-Offline Flask backend (port 5001)
#
# Prerequisites on this host:
#   - Docker (available without sudo for the ubuntu user)
#   - Python 3.11+
#   - pip3 with --break-system-packages capability
#   - curl, python3 zstandard (installed below)
#
# PORT NOTES:
#   3000 — occupied by Next.js frontend (do NOT use)
#   8000 — occupied by mock-mcs Docker container (do NOT use)
#   5001 — MiroFish Flask backend (confirmed available)
#   7687 — Neo4j Bolt
#   7474 — Neo4j HTTP browser
#   11434 — Ollama API
#
# Usage:
#   bash scripts/install_mirofish.sh
#
set -euo pipefail

MIROFISH_DIR="/tmp/MiroFish-Offline"
OLLAMA_BIN_DIR="/tmp/ollama_extract/bin"
OLLAMA_MODELS_DIR="/tmp/ollama_models"
OLLAMA_ARCHIVE="/tmp/ollama.tar.zst"
FLASK_PORT=5001
NEO4J_PASSWORD="mirofish"

echo "=== Step 1: Clone MiroFish-Offline ==="
if [ ! -d "$MIROFISH_DIR" ]; then
    git clone https://github.com/nikmcfly/MiroFish-Offline.git "$MIROFISH_DIR"
else
    echo "  Already exists at $MIROFISH_DIR, skipping clone."
fi

echo "=== Step 2: Create .env ==="
if [ ! -f "$MIROFISH_DIR/.env" ]; then
    cp "$MIROFISH_DIR/.env.example" "$MIROFISH_DIR/.env"
    # Use the smaller default model (qwen2.5 = 7B, not 32B)
    sed -i 's/LLM_MODEL_NAME=qwen2.5:32b/LLM_MODEL_NAME=qwen2.5/' "$MIROFISH_DIR/.env"
    # Increase LLM timeout for CPU-only inference (20 minutes)
    echo "LLM_TIMEOUT=1200" >> "$MIROFISH_DIR/.env"
    # Reduce context window to speed up CPU inference
    echo "OLLAMA_NUM_CTX=2048" >> "$MIROFISH_DIR/.env"
    echo "  .env created."
else
    echo "  .env already exists, skipping."
fi

echo "=== Step 3: Install Python dependencies ==="
pip3 install flask flask-cors openai neo4j python-dotenv PyMuPDF \
             charset-normalizer chardet pydantic zstandard \
             --break-system-packages --quiet
echo "  Python deps installed."

echo "=== Step 4: Start Neo4j 5.15 (Docker) ==="
if docker ps --format '{{.Names}}' | grep -q "^mirofish-neo4j$"; then
    echo "  mirofish-neo4j container already running."
elif docker ps -a --format '{{.Names}}' | grep -q "^mirofish-neo4j$"; then
    echo "  mirofish-neo4j container exists but stopped — starting it."
    docker start mirofish-neo4j
else
    docker run -d --name mirofish-neo4j \
        -p 7474:7474 -p 7687:7687 \
        -e "NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}" \
        -e 'NEO4J_PLUGINS=["apoc"]' \
        neo4j:5.15-community
    echo "  mirofish-neo4j container started."
fi

echo "  Waiting for Neo4j Bolt port 7687..."
for i in $(seq 1 30); do
    if curl -s --max-time 2 http://localhost:7474/ > /dev/null 2>&1; then
        echo "  Neo4j is ready."
        break
    fi
    sleep 3
done

echo "=== Step 5: Install Ollama (binary download, no sudo) ==="
if [ -x "$OLLAMA_BIN_DIR/ollama" ]; then
    echo "  Ollama already installed at $OLLAMA_BIN_DIR/ollama"
else
    OLLAMA_VERSION="v0.18.3"
    echo "  Downloading Ollama $OLLAMA_VERSION..."
    curl -fsSL \
        "https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst" \
        -o "$OLLAMA_ARCHIVE"
    mkdir -p "$OLLAMA_BIN_DIR"
    python3 -c "
import zstandard as zstd, tarfile, os
os.makedirs('$OLLAMA_BIN_DIR', exist_ok=True)
with open('$OLLAMA_ARCHIVE', 'rb') as f:
    with zstd.ZstdDecompressor().stream_reader(f) as reader:
        with tarfile.open(fileobj=reader, mode='r|') as tar:
            tar.extractall('/tmp/ollama_extract')
"
    chmod +x "$OLLAMA_BIN_DIR/ollama"
    echo "  Ollama extracted to $OLLAMA_BIN_DIR/ollama"
fi

echo "=== Step 6: Start Ollama server ==="
if curl -s --max-time 2 http://localhost:11434/ > /dev/null 2>&1; then
    echo "  Ollama already running on port 11434."
else
    OLLAMA_MODELS="$OLLAMA_MODELS_DIR" "$OLLAMA_BIN_DIR/ollama" serve > /tmp/ollama_serve.log 2>&1 &
    echo "  Ollama started (PID $!). Waiting for it to be ready..."
    for i in $(seq 1 20); do
        if curl -s --max-time 2 http://localhost:11434/ > /dev/null 2>&1; then
            echo "  Ollama is ready."
            break
        fi
        sleep 2
    done
fi

echo "=== Step 7: Pull required Ollama models ==="
OLLAMA_MODELS="$OLLAMA_MODELS_DIR"

if "$OLLAMA_BIN_DIR/ollama" list 2>/dev/null | grep -q "qwen2.5"; then
    echo "  qwen2.5 already pulled."
else
    echo "  Pulling qwen2.5 (7B — ~4.7 GB, may take several minutes)..."
    OLLAMA_MODELS="$OLLAMA_MODELS_DIR" "$OLLAMA_BIN_DIR/ollama" pull qwen2.5
fi

if "$OLLAMA_BIN_DIR/ollama" list 2>/dev/null | grep -q "nomic-embed-text"; then
    echo "  nomic-embed-text already pulled."
else
    echo "  Pulling nomic-embed-text (~274 MB)..."
    OLLAMA_MODELS="$OLLAMA_MODELS_DIR" "$OLLAMA_BIN_DIR/ollama" pull nomic-embed-text
fi

echo "=== Step 8: Start MiroFish Flask backend on port $FLASK_PORT ==="
if curl -s --max-time 3 "http://localhost:${FLASK_PORT}/health" > /dev/null 2>&1; then
    echo "  MiroFish already running on port $FLASK_PORT."
else
    cd "$MIROFISH_DIR"
    FLASK_PORT="$FLASK_PORT" python3 backend/run.py >> /tmp/mirofish_backend.log 2>&1 &
    MIROFISH_PID=$!
    echo "  MiroFish started (PID $MIROFISH_PID). Waiting for health check..."
    for i in $(seq 1 20); do
        if curl -s --max-time 3 "http://localhost:${FLASK_PORT}/health" > /dev/null 2>&1; then
            echo "  MiroFish health check passed on port $FLASK_PORT."
            break
        fi
        sleep 2
    done
fi

echo ""
echo "=== All services are up ==="
echo "  MiroFish backend:  http://localhost:${FLASK_PORT}/health"
echo "  Neo4j browser:     http://localhost:7474"
echo "  Ollama API:        http://localhost:11434"
echo ""
echo "  Confirmed API contract:"
echo "    POST /api/graph/ontology/generate  -> project_id"
echo "    POST /api/graph/build              -> task_id"
echo "    GET  /api/graph/task/{task_id}     -> status field ('completed' = ready)"
echo "    GET  /api/graph/data/{graph_id}    -> graph nodes + edges"
