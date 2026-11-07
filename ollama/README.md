# Локальная модель через Ollama

```
brew install ollama && ollama serve &          # или brew services start ollama
ollama pull qwen2.5:14b                         # ~9 ГБ
ollama create cqa-qwen14b -f ollama/Modelfile.qwen14b   # контекст 16k, temperature 0
```
`.env`: `LLM_PROVIDER=openai  LLM_BASE_URL=http://localhost:11434/v1  LLM_MODEL=cqa-qwen14b  LLM_API_KEY=ollama
LLM_CONCURRENCY=1  LLM_TIMEOUT_S=900  EVAL_MODE=per_dialog`. Модель на M1 Pro 32 ГБ отвечает ~10–15 токенов/с;
один диалог в режиме per_dialog — минуты, поэтому `LLM_CONCURRENCY=1` и пакет с `concurrency: 1`.
