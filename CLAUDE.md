# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-notebook workshop (`agentic_design_patterns.ipynb`, DSPT, 2026-10-10) that demonstrates agentic
design patterns with **Google ADK** agents backed by **local Ollama models via LiteLLM**. There is no package,
test suite, or build. See `README.md` for onboarding.

**Do not execute the notebook unless the user asks.** It needs a running Ollama server with pulled models,
makes live HTTP calls to Open-Meteo, and appends to `memory.txt`.

## Environment

- Python 3.13 venv at `.venv`, created with uv. Dependencies are in `requirements.txt`:
  `google-adk==2.11.0`, `litellm` (pinned to 1.103.2 rather than the notebook's 1.104.0, because of the user's
  uv `exclude-newer` cooldown; don't bypass that cooldown without asking), plus `jupyterlab` and `ipykernel`.
- Jupyter kernel name: `dspt-agenticai`.
- Ollama runs at `http://localhost:11434`. Models: `gemma4:e4b` (default, multimodal) and `granite4:tiny-h`.
- The notebook's section-2 cells (`curl | sh` installer, `ollama pull`, `%pip install`) are for Colab/Linux
  attendees. On this Mac, setup comes from the README instead.

## Notebook architecture

Two helpers defined early on are used by every later cell:

- `llm(model="gemma4:e4b")` returns `LiteLlm(model="openai/<model>", api_base="http://localhost:11434/v1", ...)`.
  It treats Ollama as an OpenAI-compatible endpoint, with `temperature=0` and `reasoning_effort="none"`.
- `ask(agent, query, image=None)` builds a fresh `Runner` + `InMemorySessionService` on each call, so no
  conversation state carries over between `ask` calls. It prints `[TOOL]` and `[ANSWER]` lines.

Patterns build on each other, and later cells reuse earlier agents:

- **Tools**: `current_temp(city)` calls the Open-Meteo geocoding and forecast APIs.
- **Memory**: an `after_tool_callback` (`save_to_memory`) appends to `memory.txt`, and `with_memory()` is an
  instruction *provider* function that injects the file's contents into the prompt. This persistence is the
  only kind that survives `ask` calls.
- **Routing**: a `router` agent with `sub_agents=[weather_agent, general_agent]`.
- **Parallel + multi-agent**: `ParallelAgent` of `.clone({...})`-ed agents that write to state through
  `output_key`. A `writer` reads that state through `{weather}` / `{places}` placeholders in its instruction,
  all inside a `SequentialAgent`.
- **Planning / reflection**: planner → executor (`output_key="plan"` / `"draft"`), then a `LoopAgent` of
  critic (`exit_loop` tool) + reviser that rewrites `draft`, with `max_iterations=3`.

State flows between agents only via `output_key` and `{key}` templating in instructions.

## Cinema listings cache (`cartaz_porto.py`)

A standalone scraper, separate from the notebook. It merges Porto-district sessions from filmspot.pt (room numbers,
address, GPS), cinecartaz.publico.pt (director, cast, age rating, weekly Thu→Wed grid with no dates) and
magazine-hd.com. Output goes to `cache/cartaz_porto.json` (sessions per day plus film records) and `cache/cinemas_porto.json`.
None of the sources publish seat counts, so those come from the hand-maintained `salas_porto_manual.json`; only add
numbers that have a source. The script obeys each site's robots.txt and Crawl-delay, and fetches once a day unless run with `--forcar`.

`ratings_filmes.py` reads the films from that cache and writes `cache/ratings_filmes.json`. It only processes films that
aren't already in that file (`--refazer` redoes all of them). The IMDb ID comes from the filmspot film page, and the rating
and vote count come from the official IMDb dataset (`title.ratings.tsv.gz`). imdb.com's robots.txt forbids scraping, so
don't fetch its pages. Ípsilon critic stars (0–5) come from Cinecartaz.

## Known gaps

- `cat.jpg`, which section 4 references, is not in the repo.
- Images in the notebook are embedded base64, so the `.ipynb` is large. Read it via a JSON dump of
  `cells[*].source` rather than in full.
