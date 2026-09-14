# Task Orchestrator via Local LLM (Ollama / LM Studio)

Splits a task into subtasks and executes them step-by-step via a local LLM through Ollama or LM Studio.

## Requirements

- Python 3.14+
- `requests` library
- A running Ollama (`http://localhost:11434`) or LM Studio (`http://localhost:1234`) instance with at least one model pulled/loaded

## Installation

```bat
pip install -r requirements.txt
```

Or minimal:

```bat
pip install requests
```

## Usage

1. Write your overall task description into `task.md` (plain Markdown, any language).
2. Run the script:

```bat
python script.py
```

On the first run you will be asked to choose a backend (Ollama / LM Studio) and a model. The choice is saved to `config.json`.

CLI flags:

- `--reconfigure` — ask for backend/model again and overwrite `config.json`
  ```bat
  python script.py --reconfigure
  ```
- `--resume` — continue from the last unfinished step (based on `progress.json`), without re-planning
  ```bat
  python script.py --resume
  ```
- `--dry-run` — only generate the subtask plan into `tasks/`, do not execute them
  ```bat
  python script.py --dry-run
  ```

On Windows you can also use `start.bat` (it checks Python, installs `requests` if missing, and forwards all arguments):

```bat
start.bat --dry-run
```

## Project Structure

```
project/
├── script.py        # main orchestrator script
├── task.md          # task description (written by user)
├── tasks/           # created by script — one .md file per subtask (01_slug.md, ...)
├── output/          # created by script — model answers per subtask
├── progress.json    # created by script — per-step status (done / pending / failed)
├── config.json      # created on first run — saved backend/base_url/model choice
├── requirements.txt # Python dependencies (requests)
├── start.bat        # Windows launcher
└── README.md        # this file
```

`progress.json` example:

```json
{
  "steps": [
    {"id": 1, "slug": "hero_section", "status": "done", "duration_sec": 45.2},
    {"id": 2, "slug": "styles", "status": "pending"}
  ]
}
```

## How It Works

1. **Task splitting** — `task.md` is sent to the selected model with a planner prompt that requests a strict JSON plan (`{"steps": [...]}`). Each step becomes `tasks/NN_slug.md`. Up to 2 retries are attempted on invalid JSON; on failure the raw answer is saved to `tasks/00_raw_plan_failed.md`.
2. **Per-step execution** — each subtask file is sent to the model together with a short summary of previous steps (first meaningful line of each prior answer, not the full history). The full answer is saved to `output/NN_slug.md`.
3. **Progress tracking** — after each step `progress.json` is updated and a progress bar (`3/8 (37%)`) is printed. Failed steps (connection error, timeout) are marked `failed` without stopping the whole run; a final summary lists them.

## Notes / Limitations

- Small local models may struggle with complex planning or strict JSON output; if the plan is bad, edit `tasks/*.md` manually and re-run with `--resume`.
- Generation timeout is 300 seconds per request since local inference can be slow.
- Only one model is used for the whole run; multi-model comparison is out of scope.
- Recommend testing with `--dry-run` first to inspect the generated plan before the (slower) execution phase.
