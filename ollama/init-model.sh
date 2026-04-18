#!/usr/bin/env bash
set -euo pipefail

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:14b-instruct-q4_K_M}"
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

export OLLAMA_HOST

ollama pull "$OLLAMA_MODEL"
