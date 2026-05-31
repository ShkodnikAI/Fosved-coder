# MindCoder

An AI-powered coding assistant with persistent memory. Combines the best ideas from **Aider** (Repo Map), **Claude Code** (autonomous agents), **agentmemory** (4-level memory system), and **Cursor** (IDE-like UI) into a single self-hosted application.

![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

### Smart Memory System

A memory system inspired by [agentmemory](https://github.com/rohitg00/agentmemory) and the Ebbinghaus forgetting curve:

- **Semantic Search** — finds observations by meaning, not just keywords. Query "where did we fix auth" finds "fixed JWT token validation in auth middleware"
  - Engine: `sentence-transformers` (all-MiniLM-L6-v2, 384-dim, runs locally, no API needed)
  - Storage: numpy BLOB in SQLite / PostgreSQL (no pgvector dependency)
  - Combines: FTS5 + vector search via **Reciprocal Rank Fusion** (RRF, k=60)

- **Smart Context Assembly** — injects semantically relevant facts from past sessions into the system prompt on each request, instead of just the last N observations

- **Memory Decay (Ebbinghaus Curve)** — smooth memory decay instead of hard thresholds
  - Score = recency_factor x access_boost
  - Frequently used facts persist longer, rare ones decay faster
  - Background eviction loop (hourly)
  - Hard floor: 180 days

- **Observations** — compressed records of agent actions (tool use, errors, decisions, insights). Background LLM compression, privacy tag stripping, 3-layer progressive search

- **Session Summaries** — AI-generated summaries created on WebSocket disconnect

### Agent Core

- **Smart Router** — single model selection point considering: explicit user choice, project priority models, fallback chain, intelligent router recommendation (task classification), probed model list. Background revalidation every 5 minutes
- **Cyclic Agent** — tool-calling loop up to 10 iterations. Anti-stuck: 3 identical tool_calls in a row triggers loop interruption
- **Dual Mode** — Tool Calling (Claude, GPT, Gemini) + Prompt Injection (fallback for Qwen, Llama, Ollama)
- **Autonomous Mode** — AI iteratively executes tasks without user interaction

### Tools & Context

- **Repo Map** — scans project structure, extracts function/class signatures. MD5 cache invalidation
- **Idea Injector** — analyzes GitHub repositories via API: downloads key files, creates AI-generated architecture summaries
- **Context Compressor** — LLM compression + regex fallback. Non-destructive archiving
- **69 Built-in Skills** — ready-to-use presets for ppt, pdf, docx, xlsx, market research, UI/UX, charts, and more

### Security & Infrastructure

- **Cyborg Mode** — blocks critical commands (rm -rf, DROP TABLE) with confirmation and git checkpoint
- **Persistent Database** — PostgreSQL (Supabase/Neon/Render) + SQLite fallback. 18 tables, async SQLAlchemy
- **REST API** — 120+ endpoints under `/api/v1` for external integrations
- **WebSocket** — RFC 6455 PING frames keepalive, token recovery from database

---

## Quick Start

### Prerequisites

- Python 3.10+
- At least one LLM API key: [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/), [Groq](https://console.groq.com/) (free), [Cerebras](https://cloud.cerebras.ai/) (free), [Google Gemini](https://aistudio.google.com/apikey) (free tier), or others
- Optional: PostgreSQL database (Supabase, Neon, Render) — falls back to SQLite if not configured

### Installation

```bash
# Clone the repository
git clone https://github.com/ShkodnikAI/MindCoder.git
cd MindCoder

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate        # Linux/macOS
# venv\Scripts\activate         # Windows (PowerShell)

# Install dependencies
pip install -r requirements.txt

# (Optional) Copy config template
cp config.example.yaml config.yaml

# Run the server
python run.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

### First Launch

1. Open the web UI at `http://localhost:8000`
2. Go to **Settings** (gear icon) and add your API keys
3. Click **Probe** to check which models are accessible
4. Select a model and start chatting

That's it. No database setup needed — SQLite is used by default.

### Using PostgreSQL (Production)

Set the `DATABASE_URL` environment variable:

```bash
export DATABASE_URL=postgresql://user:password@host:5432/dbname
python run.py
```

Or add it to your `.env` file (see `.env.example`).

### Docker

```bash
docker build -t mindcoder .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e DATABASE_URL=postgresql://... \
  mindcoder
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | HTTP server port | `8000` |
| `DATABASE_URL` | PostgreSQL or SQLite URL | SQLite (`data/mindcoder.db`) |
| `ANTROPIC_API_KEY` | Anthropic API key | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `GEMINI_API_KEY` | Google Gemini API key | — |
| `XAI_API_KEY` | xAI/Grok API key | — |
| `DEEPSEEK_API_KEY` | DeepSeek API key | — |
| `QWEN_API_KEY` | Qwen/Alibaba API key | — |
| `CEREBRAS_API_KEY` | Cerebras API key (free) | — |
| `GROQ_API_KEY` | Groq API key (free) | — |
| `GITHUB_TOKEN` | GitHub PAT for git operations | — |

All API keys are optional. You can also add them through the web UI after launch.

---

## Project Structure

```
MindCoder/
├── run.py                     # Entry point (Uvicorn) + WebSocket handler
├── config.example.yaml        # Configuration template
├── .env.example                # Environment variables template
├── requirements.txt           # Python dependencies
├── Dockerfile                  # Docker setup
│
├── core/                       # Business logic (17 modules)
│   ├── agent.py                # LLM loop, tool-calling, fallback
│   ├── intelligent_router.py   # Model selection advisor
│   ├── keys_manager.py         # API keys, validation, revalidation
│   ├── memory.py               # SQLAlchemy models + CRUD (18 tables)
│   ├── memory_embeddings.py    # Vector embeddings + RRF fusion
│   ├── memory_decay.py         # Memory decay (Ebbinghaus) + eviction
│   ├── observation_manager.py  # Observations, search, context assembly
│   ├── context_compressor.py   # LLM + regex history compression
│   ├── context_manager.py      # Repo Map (scanning, caching)
│   ├── executor.py             # Async shell commands, cyborg mode
│   ├── response_parser.py      # Model response parsing
│   ├── prompt_injector.py      # Skill and context injection
│   ├── ideas_injector.py       # GitHub API analysis
│   ├── auto_agent.py           # Autonomous mode
│   ├── action_logger.py        # Structured logging
│   ├── code_tester.py          # Test runner
│   └── apk_builder.py          # APK builder
│
├── api/                        # REST API (120+ endpoints)
│   └── endpoints.py
│
├── ui/                         # Web interface
│   ├── static/style.css        # VS Code dark theme
│   └── templates/index.html    # SPA + WebSocket + JS
│
├── skills/                     # 69 built-in skills
└── data/                       # SQLite DB, logs, embedding cache
```

---

## Supported LLM Providers

| Provider | Type | Example Models |
|----------|------|---------------|
| Anthropic | Paid | `claude-sonnet-4-6`, `claude-opus-4-7` |
| OpenAI | Paid | `gpt-4o`, `gpt-4.1` |
| xAI Grok | Paid | `grok-3`, `grok-3-mini` |
| Google Gemini | Paid | `gemini-2.5-pro`, `gemini-2.5-flash` |
| DeepSeek | Paid | `deepseek-chat`, `deepseek-reasoner` |
| Qwen (Alibaba) | Paid | `qwen3-235b-a22b` |
| Z.AI (GLM) | Paid | `glm-4.5` |
| Kimi (Moonshot) | Paid | `kimi-k2-0711` |
| **Groq** | **Free** | `llama-3.3-70b-versatile` |
| **Cerebras** | **Free** | `llama-4-scout-17b-16e-instruct` |
| Ollama | Local | `llama3`, `qwen3`, any GGUF model |

---

## Memory Architecture

```
User Query
    │
    ▼
┌──────────────────────────────────────────────┐
│         Smart Context Assembly                │
│                                               │
│  1. Session Summaries (last 3)                │
│  2. Recent Observations (24h)                 │
│  3. Semantic Search (query -> embedding ->    │
│     cosine similarity -> top-5 relevant facts)  │
│                                               │
│  Output -> injected into system prompt         │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│         Hybrid Search (search_observations)    │
│                                               │
│  FTS5 (keywords) --+                          │
│                     +-- RRF (k=60) -> ranked   │
│  Vector (semantic) -+                          │
│                                               │
│  decay_score = e^(-lambda*days) * (1+ln(acc+1))│
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│         Background Tasks                      │
│                                               │
│  - Embedding computation (per observation)     │
│  - LLM compression (per observation)           │
│  - Session summary (on WS disconnect)          │
│  - Decay eviction loop (hourly)               │
└──────────────────────────────────────────────┘
```

---

## REST API

Base URL: `http://localhost:8000/api/v1/`

Full Swagger docs: `http://localhost:8000/docs`

| Domain | Prefix | Examples |
|--------|--------|----------|
| Projects | `/projects` | CRUD, rename, key regeneration |
| Keys | `/keys` | add, remove, validate providers |
| Models | `/models` | list, probe, local, custom |
| Skills | `/skills` | list, activate |
| Ideas | `/ideas` | GitHub repo analysis |
| Memory | `/memory` | observations, search, stats |
| Drafts | `/drafts` | CRUD, prompt generation |
| Chat | `/chat` | history, clear |

---

## Slash Commands

| Command | Description |
|---------|-------------|
| `/terminal <cmd>` | Execute a shell command |
| `/git_pull` | git pull |
| `/git_push` | git push |
| `/quick_push` | git add -A + commit + push |
| `/git_clone <url>` | Clone a GitHub repository |
| `/repo_map` | Show project structure |
| `/ideas <url>` | Analyze a GitHub repository |
| `/clear` | Clear chat history |
| `/help` | Show help |

---

## Contributing

This is an open-source project and **contributions are welcome**! Whether you want to fix a bug, add a feature, improve documentation, or translate the UI — every contribution helps.

### How to Contribute

1. **Fork** the repository
2. **Create a branch**: `git checkout -b feature/your-feature-name`
3. **Make your changes** and test them
4. **Commit**: `git commit -m "feat: description of your change"`
5. **Push**: `git push origin feature/your-feature-name`
6. **Open a Pull Request** on GitHub

### Areas That Need Help

- **Documentation** — translate the UI and prompts to English (currently Russian)
- **Testing** — add unit tests (there are none yet!)
- **UI/UX** — the interface is functional but could use polish
- **Mobile** — improve the responsive design for tablets
- **Skills** — add new skills (the system supports plugins)
- **Integrations** — GitLab, Bitbucket support
- **Embeddings** — add support for OpenAI embeddings as an alternative to local sentence-transformers

### Development Setup

```bash
git clone https://github.com/ShkodnikAI/MindCoder.git
cd MindCoder
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python run.py
```

### Code Style

- Python 3.10+ with type hints
- Async-first (asyncio + FastAPI)
- Each core module is independent and importable
- UI: vanilla HTML/CSS/JS (no build step needed)

---

## License

[MIT](LICENSE) — use it, modify it, ship it.
