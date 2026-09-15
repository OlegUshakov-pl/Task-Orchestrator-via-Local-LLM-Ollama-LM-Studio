#!/usr/bin/env python3
"""Task orchestrator: splits task.md into subtasks and executes them via local LLM.

Supports backends:
  - Ollama (http://localhost:11434)
  - LM Studio (http://localhost:1234, OpenAI-compatible)
"""

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Non-UTF-8 Windows consoles (e.g. cp1250) may fail on some Unicode characters;
# fall back to replacement chars instead of crashing on print()/argparse help.
try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
TASK_MD = BASE_DIR / "task.md"
TASKS_DIR = BASE_DIR / "tasks"
OUTPUT_DIR = BASE_DIR / "output"
PROGRESS_FILE = BASE_DIR / "progress.json"
CONFIG_FILE = BASE_DIR / "config.json"

GENERATION_TIMEOUT = 300  # seconds, local models can be slow
LIST_MODELS_TIMEOUT = 15

BACKENDS = {
    "1": ("ollama", "http://localhost:11434"),
    "2": ("lmstudio", "http://localhost:1234"),
}

PLANNER_PROMPT_TEMPLATE = """You are a technical task planner for software development.

Read the task below and split it into 4-8 small, sequential,
self-contained implementation subtasks.

Rules:
- Each subtask must be as narrow as possible (one section, one file,
  one feature at a time)
- Subtasks must be ordered by execution (skeleton/structure first,
  then content, then styles, then interactivity, then final review)
- Do not write code, only the plan
- Do not add any explanations outside the JSON

Return ONLY valid JSON strictly in this format, with no markdown formatting,
no ```json wrappers, no comments of any kind:
{{
  "steps": [
    {{
      "id": 1,
      "title": "short step title",
      "slug": "short_slug_latin",
      "description": "what exactly needs to be done, 2-4 sentences",
      "output_file": "name of the file that should be created or changed"
    }}
  ]
}}

Task:
---
{TASK_MD_CONTENT}
---
"""

TASK_FILE_TEMPLATE = """# Step {id}: {title}

## Description
{description}

## Expected result
File: {output_file}
"""

RETRY_CLARIFICATION = "Return only JSON, without markdown formatting or explanations."

PLANNER_PROMPT_TEMPLATE_RU = """Ты — технический планировщик задач для разработки.

Прочитай задачу ниже и разбей её на 4-8 небольших, последовательных
и самодостаточных подзадач для реализации.

Правила:
- Каждая подзадача должна быть максимально узкой (одна секция, один файл,
  одна функциональность за раз)
- Подзадачи должны идти в порядке выполнения (сначала скелет/структура,
  потом контент, потом стили, потом интерактив, потом финальная проверка)
- Не пиши код, только план
- Не добавляй пояснений вне JSON

Верни ТОЛЬКО валидный JSON строго в этом формате, без markdown-разметки,
без ```json оберток, без каких-либо комментариев:
{{
  "steps": [
    {{
      "id": 1,
      "title": "короткое название шага",
      "slug": "short_slug_latin",
      "description": "что именно нужно сделать, 2-4 предложения",
      "output_file": "имя файла, который должен получиться или измениться"
    }}
  ]
}}

Задача:
---
{TASK_MD_CONTENT}
---
"""

TASK_FILE_TEMPLATE_RU = """# Шаг {id}: {title}

## Описание
{description}

## Ожидаемый результат
Файл: {output_file}
"""

RETRY_CLARIFICATION_RU = "Верни только JSON, без markdown-разметки и пояснений."


def lang_texts(lang):
    """LLM-facing strings in the plan language ('en' or 'ru').

    Console/CLI messages always stay in English; only the prompts sent
    to the model and the generated subtask files follow --lang.
    """
    if lang == "ru":
        return {
            "planner": PLANNER_PROMPT_TEMPLATE_RU,
            "task_file": TASK_FILE_TEMPLATE_RU,
            "retry": RETRY_CLARIFICATION_RU,
            "empty_context": "пока ничего не сделано (это первый шаг)",
            "context_label": "Контекст (что уже сделано)",
            "current_label": "Текущая подзадача",
            "instruction": "Выполни только эту подзадачу. Не повторяй предыдущие шаги.",
        }
    return {
        "planner": PLANNER_PROMPT_TEMPLATE,
        "task_file": TASK_FILE_TEMPLATE,
        "retry": RETRY_CLARIFICATION,
        "empty_context": "nothing done yet (this is the first step)",
        "context_label": "Context (what has been done so far)",
        "current_label": "Current subtask",
        "instruction": "Execute only this subtask. Do not repeat previous steps.",
    }


# ---------------------------------------------------------------- config ---

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if (all(k in cfg for k in ("backend", "base_url", "model"))
                    and cfg["backend"] in ("ollama", "lmstudio")):
                return cfg
            print(f"Ignoring {CONFIG_FILE.name}: unknown backend "
                  f"'{cfg.get('backend')}', reconfiguration is required.")
        except (json.JSONDecodeError, OSError) as e:
            print(f"Could not read config.json ({e}), reconfiguration is required.")
    return None


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"Configuration saved to {CONFIG_FILE.name}")


# --------------------------------------------------------------- backend ---

def http_json(method, url, payload=None, timeout=15):
    """Send an HTTP request with an optional JSON body, return parsed JSON.

    Uses only the standard library. Raises:
      - ConnectionError on connection failures (server not running, timeout)
      - RuntimeError on HTTP error statuses or invalid JSON responses
    """
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}")
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ConnectionError(f"Could not connect to {url}: {reason}")
    if status >= 400:
        raise RuntimeError(f"HTTP {status} from {url}: {body[:500]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}")


def fetch_ollama_models(base_url):
    url = base_url.rstrip("/") + "/api/tags"
    try:
        data = http_json("GET", url, timeout=LIST_MODELS_TIMEOUT)
    except ConnectionError:
        print(f"Could not connect to ollama at {url}. "
              f"Make sure the server is running.")
        return []
    except RuntimeError as e:
        print(f"Error while fetching the Ollama model list: {e}")
        return []
    if not isinstance(data, dict):
        print("Ollama returned an unexpected model list format.")
        return []
    models = data.get("models", [])
    return [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]


def fetch_lmstudio_models(base_url):
    url = base_url.rstrip("/") + "/v1/models"
    try:
        data = http_json("GET", url, timeout=LIST_MODELS_TIMEOUT)
    except ConnectionError:
        print(f"Could not connect to lmstudio at {url}. "
              f"Make sure the server is running.")
        return []
    except RuntimeError as e:
        print(f"Error while fetching the LM Studio model list: {e}")
        return []
    if not isinstance(data, dict):
        print("LM Studio returned an unexpected model list format.")
        return []
    items = data.get("data", [])
    return [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]


def choose_backend_and_model():
    print("Select a backend for task execution:")
    print("1) Ollama (http://localhost:11434)")
    print("2) LM Studio (http://localhost:1234)")
    while True:
        choice = input("Enter number: ").strip()
        if choice in BACKENDS:
            backend, base_url = BACKENDS[choice]
            break
        print("Invalid input. Enter 1 or 2.")

    list_fn = fetch_ollama_models if backend == "ollama" else fetch_lmstudio_models
    models = list_fn(base_url)
    if not models:
        print("The model list is empty or the backend is unavailable.")
        manual = input("Enter the model name manually (or press Enter to exit): ").strip()
        if not manual:
            sys.exit(1)
        model = manual
    else:
        print("\nAvailable models:")
        for i, m in enumerate(models, 1):
            print(f"  {i}) {m}")
        while True:
            sel = input("Enter model number: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(models):
                model = models[int(sel) - 1]
                break
            print(f"Enter a number from 1 to {len(models)}.")

    cfg = {"backend": backend, "base_url": base_url, "model": model}
    save_config(cfg)
    return cfg


# ------------------------------------------------------------ generation ---

def generate_ollama(base_url, model, prompt, need_json=False):
    url = base_url.rstrip("/") + "/api/generate"
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # Thinking/reasoning models (qwen3, deepseek-r1, etc.) return empty
        # `response` when `format=json` is forced. Disable thinking so we
        # actually get JSON back.
        "think": False,
        "options": {"temperature": 0.1},
    }
    if need_json:
        body["format"] = "json"
    try:
        data = http_json("POST", url, payload=body, timeout=GENERATION_TIMEOUT)
    except ConnectionError:
        raise ConnectionError(
            f"Could not connect to ollama at {url}. "
            f"Make sure the server is running."
        )
    if not isinstance(data, dict):
        raise RuntimeError(f"Ollama returned an unexpected response format: {data}")
    resp = (data.get("response") or "").strip()
    if not resp:
        # Help debugging: show what Ollama actually returned
        keys = list(data.keys())
        raise RuntimeError(
            f"Ollama returned an empty response (keys: {keys}). "
            f"Model '{model}' may not support format=json or is still loading. "
            f"Full reply: {json.dumps(data, ensure_ascii=False)[:1000]}"
        )
    return resp


def generate_lmstudio(base_url, model, prompt):
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        data = http_json("POST", url, payload=body, timeout=GENERATION_TIMEOUT)
    except ConnectionError:
        raise ConnectionError(
            f"Could not connect to lmstudio at {url}. "
            f"Make sure the server is running."
        )
    if not isinstance(data, dict):
        raise RuntimeError(f"LM Studio returned an unexpected response format: {data}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected LM Studio response format: {e}\nResponse: {data}")


def generate(cfg, prompt, need_json=False):
    if cfg["backend"] == "ollama":
        return generate_ollama(cfg["base_url"], cfg["model"], prompt, need_json)
    return generate_lmstudio(cfg["base_url"], cfg["model"], prompt)


# ----------------------------------------------------------------- plan ---

def sanitize_slug(slug, fallback):
    slug = slug.strip().lower().replace(" ", "_")
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or fallback


def extract_json(text):
    """Try to parse strict JSON, tolerating ```json fences and extra text."""
    text = text.strip()
    # Strip markdown fences if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: largest {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("No valid JSON found", text, 0)


def validate_plan(data):
    if not isinstance(data, dict) or "steps" not in data:
        raise ValueError("JSON must contain a 'steps' key")
    steps = data["steps"]
    if not isinstance(steps, list) or not (1 <= len(steps) <= 20):
        raise ValueError("'steps' must be a non-empty list")
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise ValueError(f"Step {i} must be an object")
        for key in ("title", "slug", "description", "output_file"):
            if key not in s or not str(s[key]).strip():
                raise ValueError(f"Step {i}: missing or empty field '{key}'")
    return steps


def build_plan(cfg, task_content, lang="en"):
    texts = lang_texts(lang)
    prompt = texts["planner"].replace("{TASK_MD_CONTENT}", task_content)
    raw = ""
    last_err = None
    for attempt in range(3):  # 1 initial + 2 retries
        try:
            p = prompt if attempt == 0 else (prompt + "\n\n" + texts["retry"])
            # Last attempt: drop format=json — some models return empty
            # response with it enforced, but produce parseable JSON anyway.
            use_json = attempt < 2
            raw = generate(cfg, p, need_json=use_json)
            data = extract_json(raw)
            steps = validate_plan(data)
            return steps, raw
        except (json.JSONDecodeError, ValueError, RuntimeError, ConnectionError) as e:
            last_err = e
            print(f"Planning attempt {attempt + 1}/3 failed: {e}")
            if attempt == 2:
                err = RuntimeError(f"Could not get a valid plan: {e}\nRaw response:\n{raw}")
                err.last_raw = raw
                raise err from e
    raise RuntimeError(f"Could not get a valid plan: {last_err}")  # unreachable


def write_task_files(steps, lang="en"):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    template = lang_texts(lang)["task_file"]
    created = []
    for i, s in enumerate(steps, 1):
        slug = sanitize_slug(str(s["slug"]), f"step_{i}")
        fname = f"{i:02d}_{slug}.md"
        content = template.format(
            id=i,
            title=str(s["title"]).strip(),
            description=str(s["description"]).strip(),
            output_file=str(s["output_file"]).strip(),
        )
        (TASKS_DIR / fname).write_text(content, encoding="utf-8")
        # keep normalized slug for progress tracking
        s["_fname"] = fname
        s["_slug"] = slug
        created.append(fname)
    return created


def init_progress(steps):
    progress = {"steps": []}
    for i, s in enumerate(steps, 1):
        slug = s.get("_slug", sanitize_slug(str(s.get("slug", "")), f"step_{i}"))
        progress["steps"].append({"id": i, "slug": slug, "status": "pending"})
    save_progress(progress)
    return progress


def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return None


# -------------------------------------------------------------- execution ---

def progress_bar(done, total, slug, width=20):
    pct = done / total if total else 1.0
    filled = int(width * pct)
    if filled >= width:
        bar = "=" * width
    else:
        bar = "=" * filled + ">" + " " * (width - filled - 1)
    print(f"[{bar}] {done}/{total} ({pct:.0%}) - running: {slug}")


def format_duration(seconds):
    """Format seconds as Xm Ys, e.g. 90.5 -> '1m 30.5s'."""
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m}m {s:.1f}s"


def short_summary_line(answer, max_len=200):
    for line in answer.splitlines():
        line = line.strip().lstrip("#-* ").strip()
        if line:
            return line[:max_len]
    return answer.strip().replace("\n", " ")[:max_len]


def extract_code_block(answer):
    """Extract the largest code payload from a model answer.

    Prefers ```html fences, then any ``` fence, then raw HTML
    starting at <!DOCTYPE or <html. Returns None if nothing found.
    """
    fences = re.findall(r"```(?:html)?\s*(.*?)```", answer,
                        re.DOTALL | re.IGNORECASE)
    if fences:
        # largest block is usually the full file, not a snippet
        return max((b.strip() for b in fences), key=len)
    lower = answer.lower()
    for marker in ("<!doctype", "<html"):
        idx = lower.find(marker)
        if idx != -1:
            return answer[idx:].strip()
    return None


def target_file_for_task(task_file):
    """Parse 'File: <name>' from a task file, safe basename only."""
    try:
        text = task_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*File:\s*(.+?)\s*$", text,
                  re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    name = m.group(1).strip().strip("`\"'")
    # keep only basename to avoid path traversal (../../etc)
    name = Path(name).name
    if not name or name in (".", ".."):
        return ""
    return name


def finalize_outputs(task_files):
    """Write clean code files (e.g. index.html) next to output/*.md.

    Each step's answer is scanned for a code block; the block is saved
    under the 'File:' name declared in its task file. Steps sharing
    the same target overwrite each other, so the last done step wins.
    Returns dict {target_name: source_md_name}.
    """
    written = {}
    for tf in task_files:
        out_md = OUTPUT_DIR / tf.name
        if not out_md.exists():
            continue
        target = target_file_for_task(tf)
        if not target:
            continue
        try:
            answer = out_md.read_text(encoding="utf-8")
        except OSError:
            continue
        code = extract_code_block(answer)
        if not code:
            continue
        try:
            (OUTPUT_DIR / target).write_text(code, encoding="utf-8")
            written[target] = tf.name
        except OSError as e:
            print(f"Could not write final file {target}: {e}")
    return written


def execute_steps(cfg, task_files, lang="en"):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    texts = lang_texts(lang)
    total = len(task_files)
    summary_parts = []
    failed = []

    progress = load_progress()
    # Rebuild progress if it doesn't match current task files
    if (progress is None or "steps" not in progress
            or len(progress["steps"]) != total):
        progress = {"steps": [
            {"id": i + 1,
             "slug": task_files[i].stem[3:] if len(task_files[i].stem) > 3 else task_files[i].stem,
             "status": "pending"}
            for i in range(total)
        ]}
        save_progress(progress)

    start_all = time.time()
    for idx, tf in enumerate(task_files, 1):
        entry = progress["steps"][idx - 1]
        slug = tf.stem[3:] if len(tf.stem) > 3 else tf.stem

        if entry.get("status") == "done":
            # Rebuild summary context from existing output (first line only)
            out_file = OUTPUT_DIR / tf.name
            if out_file.exists():
                try:
                    prev = out_file.read_text(encoding="utf-8")
                    summary_parts.append(f"{slug}: {short_summary_line(prev)}")
                except OSError:
                    pass
            progress_bar(idx, total, slug + " (already done, skipping)")
            continue

        progress_bar(idx - 1, total, slug)
        subtask = tf.read_text(encoding="utf-8")
        context = "; ".join(summary_parts) if summary_parts else texts["empty_context"]
        prompt = (
            f"{texts['context_label']}: {context}\n\n"
            f"{texts['current_label']}:\n{subtask}\n\n"
            f"{texts['instruction']}"
        )
        t0 = time.time()
        try:
            answer = generate(cfg, prompt, need_json=False)
        except (ConnectionError, RuntimeError, OSError) as e:
            duration = round(time.time() - t0, 1)
            print(f"Error on step {slug}: {e}")
            entry.update({"slug": slug, "status": "failed",
                          "duration_sec": duration, "error": str(e)})
            save_progress(progress)
            failed.append(slug)
            continue
        duration = round(time.time() - t0, 1)

        (OUTPUT_DIR / tf.name).write_text(answer, encoding="utf-8")
        entry.update({"slug": slug, "status": "done", "duration_sec": duration})
        entry.pop("error", None)
        save_progress(progress)
        summary_parts.append(f"{slug}: {short_summary_line(answer)}")
        progress_bar(idx, total, slug)

    total_time = round(time.time() - start_all, 1)
    return failed, total_time


# ------------------------------------------------------------------ main ---

def parse_args():
    p = argparse.ArgumentParser(description="Task orchestrator via local LLM (Ollama / LM Studio)")
    p.add_argument("--reconfigure", action="store_true",
                   help="ask for backend/model again")
    p.add_argument("--resume", action="store_true",
                   help="resume from the last unfinished step")
    p.add_argument("--dry-run", action="store_true",
                   help="only generate the subtask plan, do not execute it")
    p.add_argument("--lang", choices=["en", "ru"], default="en",
                   help="language of the generated plan/subtasks (default: en)")
    return p.parse_args()


def main():
    args = parse_args()

    # Step 1: environment checks
    if not TASK_MD.exists():
        print(f"Error: task.md not found in {BASE_DIR}. "
              f"Create task.md with a task description and run the script again.")
        sys.exit(1)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Step 2: backend selection
    cfg = None if args.reconfigure else load_config()
    if cfg is None:
        cfg = choose_backend_and_model()
    else:
        print(f"Backend: {cfg['backend']} ({cfg['base_url']}), model: {cfg['model']}")
        print("(use --reconfigure to choose again)")

    task_content = TASK_MD.read_text(encoding="utf-8")
    if not task_content.strip():
        print("Error: task.md is empty. Describe the task and run again.")
        sys.exit(1)

    # Step 3: planning (skip if --resume and tasks already exist)
    existing = sorted(TASKS_DIR.glob("*.md"))
    # filter out failure dump from the list logic
    existing = [p for p in existing if p.name != "00_raw_plan_failed.md"]

    if args.resume and existing:
        print(f"--resume mode: found {len(existing)} subtasks, skipping planning.")
        task_files = existing
    else:
        print("Splitting the task into subtasks...")
        last_raw = ""
        try:
            steps, last_raw = build_plan(cfg, task_content, args.lang)
        except Exception as e:
            last_raw = getattr(e, "last_raw", last_raw)
            # Save raw response for manual splitting
            raw_path = TASKS_DIR / "00_raw_plan_failed.md"
            raw_path.write_text(
                f"# Failed to split the task automatically\n\nError: {e}\n\n"
                f"## Raw model response\n\n{last_raw}\n",
                encoding="utf-8",
            )
            print(f"Could not get a valid plan after 3 attempts: {e}")
            print(f"Raw response saved to {raw_path}. Split the task manually.")
            sys.exit(1)

        # Clear old plan files (keep failure dump out of the way)
        for p in existing:
            try:
                p.unlink()
            except OSError:
                pass
        old_dump = TASKS_DIR / "00_raw_plan_failed.md"
        if old_dump.exists():
            try:
                old_dump.unlink()
            except OSError:
                pass

        created = write_task_files(steps, args.lang)
        init_progress(steps)
        print("Created subtasks:")
        for f in created:
            print(f"  - {f}")
        task_files = sorted(TASKS_DIR.glob("*.md"))

    if args.dry_run:
        print(f"--dry-run mode: plan of {len(task_files)} steps created, execution skipped.")
        return

    # Step 4: execution
    if not task_files:
        print("No subtasks to execute.")
        sys.exit(1)
    failed, total_time = execute_steps(cfg, task_files, args.lang)

    # Step 5: assemble clean final files (e.g. output/index.html)
    finalized = finalize_outputs(task_files)
    if finalized:
        print("Final files:")
        for target, src in finalized.items():
            print(f"  - {target} (from {src})")

    # Step 6: summary
    progress = load_progress() or {}
    done = sum(1 for s in progress.get("steps", []) if s.get("status") == "done")
    print("\n=== Summary ===")
    print(f"Done: {done}/{len(task_files)} in {format_duration(total_time)} ({total_time} s).")
    print(f"Subtask files: {TASKS_DIR}/")
    print(f"Result files: {OUTPUT_DIR}/")
    if failed:
        print("Failed steps:")
        for s in failed:
            print(f"  - {s}")
    else:
        print("All steps completed successfully.")


if __name__ == "__main__":
    main()
