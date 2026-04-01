#!/bin/bash
# Start the full stack: vLLM + Proxy
# Usage: ./start.sh [model_name]

set -e

MODEL="${1:-Qwen/Qwen3-235B-A22B}"
VLLM_PORT=8000
PROXY_PORT=9000
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONDA_ENV="${VLLM_CONDA_ENV:-vllm2}"

# Activate conda and set library path for libstdc++ compatibility
source /opt/conda/bin/activate "$CONDA_ENV"
export LD_LIBRARY_PATH="/home/jovyan/.conda/envs/$CONDA_ENV/lib:${LD_LIBRARY_PATH:-}"
export PYTHONNOUSERSITE=1

echo "================================================"
echo "  Claw Code Local LLM Stack"
echo "  Model: $MODEL"
echo "  vLLM:  http://localhost:$VLLM_PORT"
echo "  Proxy: http://localhost:$PROXY_PORT"
echo "================================================"

# Step 1: Start vLLM
echo ""
echo "[1/2] Starting vLLM server..."
vllm serve "$MODEL" \
  --tensor-parallel-size 8 \
  --gpu-memory-utilization 0.9 \
  --max-model-len 32768 \
  --tool-call-parser hermes \
  --enable-auto-tool-choice \
  --host 0.0.0.0 \
  --port $VLLM_PORT \
  --api-key "local-key" &
VLLM_PID=$!

# Wait for vLLM to be ready
echo "Waiting for vLLM to load model..."
for i in $(seq 1 300); do
  if curl -s http://localhost:$VLLM_PORT/health > /dev/null 2>&1; then
    echo "vLLM is ready!"
    break
  fi
  if ! kill -0 $VLLM_PID 2>/dev/null; then
    echo "ERROR: vLLM process died"
    exit 1
  fi
  sleep 2
done

# Step 2: Start proxy
echo ""
echo "[2/2] Starting Anthropic proxy..."
cd "$SCRIPT_DIR"
VLLM_BASE_URL="http://localhost:$VLLM_PORT" \
VLLM_MODEL="$MODEL" \
PROXY_PORT=$PROXY_PORT \
  python server.py &
PROXY_PID=$!

sleep 2

echo ""
echo "================================================"
echo "  Stack is running!"
echo "  vLLM PID:  $VLLM_PID"
echo "  Proxy PID: $PROXY_PID"
echo ""
echo "  To use with Claw Code:"
echo "    export ANTHROPIC_BASE_URL=http://localhost:$PROXY_PORT"
echo "    export ANTHROPIC_API_KEY=local-key"
echo "    claw \"hello\""
echo ""
echo "  Press Ctrl+C to stop all services"
echo "================================================"

# Trap cleanup
trap "kill $VLLM_PID $PROXY_PID 2>/dev/null; exit" INT TERM

wait
