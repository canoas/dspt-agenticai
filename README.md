# Building Agentic AI: Essential Design Patterns

Hands-on workshop notebook from **Data Science Portugal (DSPT), October 10, 2026**, by
[Ricardo Cardoso Pereira](https://ricardodcpereira.com) (University of Coimbra / CISUC).
Original notebook: [tinyurl.com/dspt-agenticai](https://tinyurl.com/dspt-agenticai).

Based on parts of *Agentic Design Patterns: A Hands-On Guide to Building Intelligent Systems*
by Antonio Gullí (Springer, 2025).

The single notebook, `agentic_design_patterns.ipynb`, walks through:

1. LLMs vs Agentic AI
2. Setup (Ollama + Google ADK + LiteLLM)
3. A first agent
4. A vision agent (multimodal input)
5. Tool use & memory management
6. Routing, parallelization & multi-agent systems
7. Planning & reflection

Every model runs **locally through Ollama**, so you don't need API keys.

## Onboarding (macOS)

### Prerequisites

| Requirement | Why | Install |
|---|---|---|
| Python **3.13** | Notebook was authored on 3.13 | `brew install python@3.13` |
| [uv](https://docs.astral.sh/uv/) | Creates the venv and installs dependencies | `brew install uv` |
| [Ollama](https://ollama.com) | Local model server at `http://localhost:11434` | `brew install ollama` (or the macOS app) |
| ~10 GB free disk | Model weights | — |
| Internet access | Model pulls, and the Open-Meteo weather API used by the tools | — |

### 1. Python environment

```bash
uv venv --python 3.13 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
python -m ipykernel install --user --name dspt-agenticai --display-name "Python (dspt-agenticai)"
```

> **Note on `litellm`:** the notebook's install cell pins `litellm==1.104.0`. `requirements.txt` pins
> `1.103.2` because a uv `exclude-newer` cooldown blocked 1.104.0 on the maintainer's machine. If you
> don't have that cooldown, you can bump it to 1.104.0 to match the notebook exactly.

### 2. Ollama models

```bash
ollama serve            # skip if the Ollama app is already running
ollama pull gemma4:e4b       # default model: multimodal, used by most agents
ollama pull granite4:tiny-h  # used by general_agent (routing section)
```

Check that the server is up with `curl -s localhost:11434/api/tags`.

### 3. Open the notebook

```bash
jupyter lab agentic_design_patterns.ipynb
```

Select the **Python (dspt-agenticai)** kernel. Then **skip the Setup cells in section 2**
(the `curl … | sh` Ollama installer, `ollama pull`, and `%pip install`), because the steps above
already cover them. Start running from the `llm()` / `ask()` definitions.

### Gotchas

- **`cat.jpg` is not in this repo.** The vision-agent cell (section 4) expects it next to the notebook.
  Add any image with that name, or change the path.
- **`memory.txt`** is created by the memory section and grows on every tool call. Delete it to reset
  the agent's long-term memory. It is git-ignored.
- Outputs depend on live weather data and local model behaviour, so results vary between runs.

## Cartaz Porto no GitHub Pages

O workflow `Refresh Cartaz Porto` atualiza diariamente `cache/cartaz_porto.json` e
`cache/cinemas_porto.json` e publica-os num site GitHub Pages. Também pode ser iniciado
manualmente em **Actions > Refresh Cartaz Porto > Run workflow**.

Na primeira utilização, ativa GitHub Pages nas definições do repositório com a origem
**GitHub Actions**. Opcionalmente, cria o secret `CARTAZ_UA` com um user-agent que inclua
um contacto. O URL público será `https://canoas.github.io/dspt-agenticai/`.
