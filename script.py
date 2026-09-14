#!/usr/bin/env python3
"""Task orchestrator: splits task.md into subtasks and executes them via local LLM.

Supports backends:
  - Ollama (http://localhost:11434)
  - LM Studio (http://localhost:1234, OpenAI-compatible)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency 'requests'. Install it: pip install requests")
    sys.exit(1)

# Windows console (e.g. cp1250) cannot encode Cyrillic; avoid UnicodeEncodeError
# crashes on print()/argparse help by falling back to replacement chars.
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

PLANNER_PROMPT_TEMPLATE = """Ты — технический планировщик задач для разработки.

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

TASK_FILE_TEMPLATE = """# Шаг {id}: {title}

## Описание
{description}

## Ожидаемый результат
Файл: {output_file}
"""

RETRY_CLARIFICATION = "Верни только JSON, без markdown-разметки и пояснений."


# ---------------------------------------------------------------- config ---

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if all(k in cfg for k in ("backend", "base_url", "model")):
                return cfg
        except (json.JSONDecodeError, OSError) as e:
            print(f"Не удалось прочитать config.json ({e}), нужна повторная настройка.")
    return None


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"Конфигурация сохранена в {CONFIG_FILE.name}")


# --------------------------------------------------------------- backend ---

def fetch_ollama_models(base_url):
    url = base_url.rstrip("/") + "/api/tags"
    try:
        r = requests.get(url, timeout=LIST_MODELS_TIMEOUT)
        r.raise_for_status()
    except requests.ConnectionError:
        print(f"Не удалось подключиться к ollama по адресу {url}. "
              f"Убедитесь, что сервер запущен.")
        return []
    except requests.RequestException as e:
        print(f"Ошибка при получении списка моделей Ollama: {e}")
        return []
    try:
        data = r.json()
    except json.JSONDecodeError:
        print("Ollama вернул невалидный JSON при запросе списка моделей.")
        return []
    models = data.get("models", [])
    return [m.get("name") for m in models if m.get("name")]


def fetch_lmstudio_models(base_url):
    url = base_url.rstrip("/") + "/v1/models"
    try:
        r = requests.get(url, timeout=LIST_MODELS_TIMEOUT)
        r.raise_for_status()
    except requests.ConnectionError:
        print(f"Не удалось подключиться к lmstudio по адресу {url}. "
              f"Убедитесь, что сервер запущен.")
        return []
    except requests.RequestException as e:
        print(f"Ошибка при получении списка моделей LM Studio: {e}")
        return []
    try:
        data = r.json()
    except json.JSONDecodeError:
        print("LM Studio вернул невалидный JSON при запросе списка моделей.")
        return []
    items = data.get("data", [])
    return [m.get("id") for m in items if m.get("id")]


def choose_backend_and_model():
    print("Выберите backend для выполнения задач:")
    print("1) Ollama (http://localhost:11434)")
    print("2) LM Studio (http://localhost:1234)")
    while True:
        choice = input("Введите номер: ").strip()
        if choice in BACKENDS:
            backend, base_url = BACKENDS[choice]
            break
        print("Некорректный ввод. Введите 1 или 2.")

    list_fn = fetch_ollama_models if backend == "ollama" else fetch_lmstudio_models
    models = list_fn(base_url)
    if not models:
        print("Список моделей пуст или backend недоступен.")
        manual = input("Введите имя модели вручную (или Enter для выхода): ").strip()
        if not manual:
            sys.exit(1)
        model = manual
    else:
        print("\nДоступные модели:")
        for i, m in enumerate(models, 1):
            print(f"  {i}) {m}")
        while True:
            sel = input("Введите номер модели: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(models):
                model = models[int(sel) - 1]
                break
            print(f"Введите число от 1 до {len(models)}.")

    cfg = {"backend": backend, "base_url": base_url, "model": model}
    save_config(cfg)
    return cfg


# ------------------------------------------------------------ generation ---

def generate_ollama(base_url, model, prompt, need_json=False):
    url = base_url.rstrip("/") + "/api/generate"
    body = {"model": model, "prompt": prompt, "stream": False}
    if need_json:
        body["format"] = "json"
    try:
        r = requests.post(url, json=body, timeout=GENERATION_TIMEOUT)
        r.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(
            f"Не удалось подключиться к ollama по адресу {url}. "
            f"Убедитесь, что сервер запущен."
        )
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama вернул невалидный JSON: {e}")
    return data.get("response", "")


def generate_lmstudio(base_url, model, prompt):
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        r = requests.post(url, json=body, timeout=GENERATION_TIMEOUT)
        r.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(
            f"Не удалось подключиться к lmstudio по адресу {url}. "
            f"Убедитесь, что сервер запущен."
        )
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LM Studio вернул невалидный JSON: {e}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Неожиданный формат ответа LM Studio: {e}\nОтвет: {data}")


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
        raise ValueError("JSON должен содержать ключ 'steps'")
    steps = data["steps"]
    if not isinstance(steps, list) or not (1 <= len(steps) <= 20):
        raise ValueError("'steps' должен быть непустым списком")
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise ValueError(f"Шаг {i} должен быть объектом")
        for key in ("title", "slug", "description", "output_file"):
            if key not in s or not str(s[key]).strip():
                raise ValueError(f"Шаг {i}: отсутствует или пустое поле '{key}'")
    return steps


def build_plan(cfg, task_content):
    prompt = PLANNER_PROMPT_TEMPLATE.replace("{TASK_MD_CONTENT}", task_content)
    raw = ""
    last_err = None
    for attempt in range(3):  # 1 initial + 2 retries
        try:
            p = prompt if attempt == 0 else (prompt + "\n\n" + RETRY_CLARIFICATION)
            raw = generate(cfg, p, need_json=True)
            data = extract_json(raw)
            steps = validate_plan(data)
            return steps, raw
        except (json.JSONDecodeError, ValueError, RuntimeError, ConnectionError) as e:
            last_err = e
            print(f"Попытка планирования {attempt + 1}/3 не удалась: {e}")
            if attempt == 2:
                err = RuntimeError(f"Не удалось получить валидный план: {e}\nСырой ответ:\n{raw}")
                err.last_raw = raw
                raise err from e
    raise RuntimeError(f"Не удалось получить валидный план: {last_err}")  # unreachable
    raise RuntimeError("Не удалось получить валидный план")  # unreachable


def write_task_files(steps):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for i, s in enumerate(steps, 1):
        slug = sanitize_slug(str(s["slug"]), f"step_{i}")
        fname = f"{i:02d}_{slug}.md"
        content = TASK_FILE_TEMPLATE.format(
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
    print(f"[{bar}] {done}/{total} ({pct:.0%}) — выполняется: {slug}")


def short_summary_line(answer, max_len=200):
    for line in answer.splitlines():
        line = line.strip().lstrip("#-* ").strip()
        if line:
            return line[:max_len]
    return answer.strip().replace("\n", " ")[:max_len]


def execute_steps(cfg, task_files):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    total = len(task_files)
    summary_parts = []
    failed = []

    progress = load_progress()
    # Rebuild progress if it doesn't match current task files
    fnames = [p.name for p in task_files]
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
            progress_bar(idx, total, slug + " (уже выполнен, пропуск)")
            continue

        progress_bar(idx - 1, total, slug)
        subtask = tf.read_text(encoding="utf-8")
        context = "; ".join(summary_parts) if summary_parts else "пока ничего не сделано (это первый шаг)"
        prompt = (
            f"Контекст (что уже сделано): {context}\n\n"
            f"Текущая подзадача:\n{subtask}\n\n"
            f"Выполни только эту подзадачу. Не повторяй предыдущие шаги."
        )
        t0 = time.time()
        try:
            answer = generate(cfg, prompt, need_json=False)
        except (ConnectionError, RuntimeError, requests.RequestException) as e:
            duration = round(time.time() - t0, 1)
            print(f"Ошибка на шаге {slug}: {e}")
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
                   help="заново спросить backend/модель")
    p.add_argument("--resume", action="store_true",
                   help="продолжить с последнего невыполненного шага")
    p.add_argument("--dry-run", action="store_true",
                   help="только сгенерировать план подзадач, не выполнять их")
    return p.parse_args()


def main():
    args = parse_args()

    # Step 1: environment checks
    if not TASK_MD.exists():
        print(f"Ошибка: файл task.md не найден в {BASE_DIR}. "
              f"Создайте task.md с описанием задачи и запустите скрипт снова.")
        sys.exit(1)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Step 2: backend selection
    cfg = None if args.reconfigure else load_config()
    if cfg is None:
        cfg = choose_backend_and_model()
    else:
        print(f"Backend: {cfg['backend']} ({cfg['base_url']}), модель: {cfg['model']}")
        print("(флаг --reconfigure — выбрать заново)")

    task_content = TASK_MD.read_text(encoding="utf-8")
    if not task_content.strip():
        print("Ошибка: task.md пуст. Опишите задачу и запустите снова.")
        sys.exit(1)

    # Step 3: planning (skip if --resume and tasks already exist)
    existing = sorted(TASKS_DIR.glob("*.md"))
    # filter out failure dump from the list logic
    existing = [p for p in existing if p.name != "00_raw_plan_failed.md"]

    if args.resume and existing:
        print(f"Режим --resume: найдено {len(existing)} подзадач, планирование пропущено.")
        task_files = existing
    else:
        print("Разбиение задачи на подзадачи...")
        last_raw = ""
        try:
            steps, last_raw = build_plan(cfg, task_content)
        except Exception as e:
            last_raw = getattr(e, "last_raw", last_raw)
            # Save raw response for manual splitting
            raw_path = TASKS_DIR / "00_raw_plan_failed.md"
            raw_path.write_text(
                f"# Не удалось автоматически разбить задачу\n\nОшибка: {e}\n\n"
                f"## Сырой ответ модели\n\n{last_raw}\n",
                encoding="utf-8",
            )
            print(f"Не удалось получить валидный план после 3 попыток: {e}")
            print(f"Сырой ответ сохранён в {raw_path}. Разбейте задачу вручную.")
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

        created = write_task_files(steps)
        init_progress(steps)
        print("Созданные подзадачи:")
        for f in created:
            print(f"  - {f}")
        task_files = sorted(TASKS_DIR.glob("*.md"))

    if args.dry_run:
        print(f"Режим --dry-run: план из {len(task_files)} шагов создан, выполнение пропущено.")
        return

    # Step 4: execution
    if not task_files:
        print("Нет подзадач для выполнения.")
        sys.exit(1)
    failed, total_time = execute_steps(cfg, task_files)

    # Step 5: summary
    progress = load_progress() or {}
    done = sum(1 for s in progress.get("steps", []) if s.get("status") == "done")
    print("\n=== Итог ===")
    print(f"Выполнено: {done}/{len(task_files)} за {total_time} c.")
    print(f"Файлы подзадач: {TASKS_DIR}/")
    print(f"Файлы результатов: {OUTPUT_DIR}/")
    if failed:
        print("Проваленные шаги:")
        for s in failed:
            print(f"  - {s}")
    else:
        print("Все шаги выполнены успешно.")


if __name__ == "__main__":
    main()
