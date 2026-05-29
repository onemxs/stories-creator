#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Load .env if it exists
ENV_FILE="$ROOT/backend/.env"
if [ -f "$ENV_FILE" ]; then
  export $(grep -v '^#' "$ENV_FILE" | grep -v '^$' | xargs)
  echo "✓ Variables de entorno cargadas desde .env"
fi

# Validate at least one AI key is set
if [ -z "$GEMINI_API_KEY" ] && [ -z "$OPENAI_API_KEY" ]; then
  echo ""
  echo "⚠️  No se encontró GEMINI_API_KEY ni OPENAI_API_KEY en backend/.env"
  echo "   Edita el archivo: $ENV_FILE"
  echo "   y agrega tu clave de Gemini (gratuita): https://aistudio.google.com/app/apikey"
  echo ""
fi

if [ -n "$GEMINI_API_KEY" ] && [ "$GEMINI_API_KEY" != "PEGA_TU_NUEVA_CLAVE_AQUI" ]; then
  echo "✓ Usando Gemini (gratuito)"
elif [ -n "$OPENAI_API_KEY" ]; then
  echo "✓ Usando OpenAI (GPT-4o-mini)"
fi

echo ""
echo "🚀 Iniciando Stories Creator..."

# Backend
echo "▸ Iniciando backend (FastAPI)..."
cd "$ROOT/backend"
"$ROOT/backend/venv/bin/uvicorn" main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

# Frontend
echo "▸ Iniciando frontend (Next.js)..."
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "✅ App corriendo:"
echo "   Frontend → http://localhost:3000"
echo "   Backend  → http://localhost:8000"
echo "   API docs → http://localhost:8000/docs"
echo ""
echo "Presiona Ctrl+C para detener."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Detenido.'" SIGINT SIGTERM
wait
