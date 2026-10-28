#!/usr/bin/env bash
# Adds the prices Phoenix does not know, so the cost of every call is shown (run once per Phoenix, e.g. after
# starting it on a new server). Phoenix prices calls only from its own model list; OpenAI's chat models
# (gpt-4o-mini...) are in it, embedding models are not. Safe to run again: an existing name is reported, not doubled.
#   ./add_model_prices.sh                         (Phoenix on this server: http://localhost:6006)
#   PHOENIX_URL=http://phoenix:6006 ./add_model_prices.sh
set -euo pipefail
PHOENIX_URL="${PHOENIX_URL:-http://localhost:6006}"

add_model() {  # name, provider, input USD per 1M tokens, output USD per 1M tokens
  local query
  query=$(printf 'mutation { createModel(input: {name: "%s", provider: "%s", namePattern: "^%s$", costs: [{tokenType: "input", costPerMillionTokens: %s, kind: PROMPT}, {tokenType: "output", costPerMillionTokens: %s, kind: COMPLETION}]}) { model { name } } }' "$1" "$2" "$1" "$3" "$4")
  echo "$1: $(curl -s -X POST "$PHOENIX_URL/graphql" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$query")")"
}

# NewsAgent (Similar Articles, approved-article embeddings) and social_scrapper (Grok duplicate checks)
add_model "text-embedding-3-large" "openai" 0.13 0
