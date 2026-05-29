#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

# Load .env
if [ -f "$ROOT/backend/.env" ]; then
  export $(grep -v '^#' "$ROOT/backend/.env" | grep -v '^$' | xargs 2>/dev/null)
fi

# Check Electron is installed
if [ ! -f "$ROOT/node_modules/.bin/electron" ]; then
  echo "⚙ Instalando Electron…"
  cd "$ROOT" && npm install
fi

echo "🚀 Abriendo Stories Creator…"
cd "$ROOT"
npm run electron:dev
