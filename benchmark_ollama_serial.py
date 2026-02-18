import json
import time
import csv
import os
import re
from datetime import datetime
from statistics import mean, median, pstdev

import requests
import serial
import matplotlib.pyplot as plt

# =========================
# CONFIG (AJUSTA AQUÍ)
# =========================
PORT = "COM5"
BAUD = 115200

MODEL = "mistral-nemo:12b-instruct-2407-q4_0"  # usa EXACTO lo que sale en: ollama list
PROMPT_TEXT = "turn led on"       # la instrucción que vas a medir
N_RUNS = 100                                   # número de pruebas
WARMUP_RUNS = 3                                # pruebas de calentamiento (no se guardan)

SEND_SERIAL = True                             # si False, solo mide interpretación (sin write)
SERIAL_FLUSH = True                            # flush después de write (más “end-to-end”)
OUT_DIR = "bench_results"

OLLAMA_URL = "http://localhost:11434/api/generate"
TIMEOUT_S = 120

# =========================
# SCHEMA + SYSTEM PROMPT
# =========================
JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["on", "off", "blink", "hold", "pattern", "stop"]},
        "count": {"type": "integer", "minimum": 1, "maximum": 100},
        "on_ms": {"type": "integer", "minimum": 10, "maximum": 60000},
        "off_ms": {"type": "integer", "minimum": 10, "maximum": 60000},
        "duration_ms": {"type": "integer", "minimum": 10, "maximum": 600000},
        "repeat": {"type": "integer", "minimum": 1, "maximum": 50},
        "sequence_ms": {
            "type": "array",
            "items": {"type": "integer", "minimum": 10, "maximum": 60000},
            "minItems": 1,
            "maxItems": 50,
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Eres un parser de instrucciones para controlar un LED por puerto serial.
Devuelve ÚNICAMENTE un objeto JSON válido que cumpla el schema proporcionado.
No incluyas texto extra, ni markdown, ni explicaciones.

Interpretación:
- action=on/off: encender/apagar.
- action=blink: parpadear count veces, con on_ms/off_ms.
- action=hold: mantener encendido duration_ms y luego apagar.
- action=pattern: sequence_ms es lista de duraciones en ms alternando ON,OFF,ON,OFF..., iniciando en ON.
- action=stop: detener cualquier patrón y apagar.

Si el usuario pide algo ambiguo, elige valores seguros por defecto:
count=3, on_ms=200, off_ms=200, duration_ms=1000, repeat=1, sequence_ms=[200,200].
"""

# =========================
# OLLAMA (requests.Session para keep-alive)
# =========================
session = requests.Session()

def ollama_parse(model: str, user_text: str) -> dict:
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": user_text,
        "stream": False,
        "format": JSON_SCHEMA,              # structured outputs (schema)
        "options": {"temperature": 0},
    }
    r = session.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_S)
    r.raise_for_status()
    data = r.json()

    raw = data.get("response", "").strip()
    if not raw:
        raise ValueError("Ollama returned empty response")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {raw}") from e

def normalize_cmd(cmd: dict) -> dict:
    action = cmd.get("action")
    if action not in {"on", "off", "blink", "hold", "pattern", "stop"}:
        raise ValueError("Invalid action")

    out = {"action": action}

    if action == "blink":
        out["count"] = int(cmd.get("count", 3))
        out["on_ms"] = int(cmd.get("on_ms", 200))
        out["off_ms"] = int(cmd.get("off_ms", 200))

    elif action == "hold":
        out["duration_ms"] = int(cmd.get("duration_ms", 1000))

    elif action == "pattern":
        out["repeat"] = int(cmd.get("repeat", 1))
        seq = cmd.get("sequence_ms", [200, 200])
        out["sequence_ms"] = [int(x) for x in seq][:50]

    return out

def to_serial_line(cmd: dict) -> str:
    a = cmd["action"]
    if a == "on":
        return "SET 1\n"
    if a == "off":
        return "SET 0\n"
    if a == "stop":
        return "STOP\n"
    if a == "blink":
        return f"BLINK {cmd.get('count', 3)} {cmd.get('on_ms', 200)} {cmd.get('off_ms', 200)}\n"
    if a == "hold":
        return f"HOLD {cmd.get('duration_ms', 1000)}\n"
    if a == "pattern":
        seq = cmd.get("sequence_ms", [200, 200])
        n = len(seq)
        seq_part = " ".join(str(x) for x in seq)
        return f"PATTERN {cmd.get('repeat', 1)} {n} {seq_part}\n"
    raise ValueError("Unhandled action")

# =========================
# BENCHMARK
# =========================
def safe_name(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9_\-\.]+", "_", s)
    return s[:80] if len(s) > 80 else s

def benchmark():
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    model_tag = safe_name(MODEL)
    prompt_tag = safe_name(PROMPT_TEXT)

    csv_path = os.path.join(OUT_DIR, f"bench_{ts}_{model_tag}.csv")
    png_path = os.path.join(OUT_DIR, f"bench_{ts}_{model_tag}.png")

    ser = None
    if SEND_SERIAL:
        ser = serial.Serial(PORT, BAUD, timeout=0.1, write_timeout=1.0)
        time.sleep(0.2)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass

    # Warmup (no se guarda)
    print(f"Warmup: {WARMUP_RUNS} runs...")
    for _ in range(WARMUP_RUNS):
        cmd = ollama_parse(MODEL, PROMPT_TEXT)
        cmd = normalize_cmd(cmd)
        line = to_serial_line(cmd)
        if SEND_SERIAL and ser is not None:
            ser.write(line.encode("utf-8"))
            if SERIAL_FLUSH:
                ser.flush()

    times_s = []
    rows = []

    print(f"Benchmark: {N_RUNS} runs...")
    for i in range(1, N_RUNS + 1):
        t0 = time.perf_counter()

        cmd = ollama_parse(MODEL, PROMPT_TEXT)
        cmd = normalize_cmd(cmd)
        line = to_serial_line(cmd)

        if SEND_SERIAL and ser is not None:
            ser.write(line.encode("utf-8"))
            if SERIAL_FLUSH:
                ser.flush()

        t1 = time.perf_counter()
        dt = t1 - t0

        times_s.append(dt)
        rows.append({"trial": i, "seconds": dt, "ms": dt * 1000.0})

        if i % 10 == 0:
            print(f"  {i}/{N_RUNS} -> {dt*1000.0:.1f} ms")

    if ser is not None:
        ser.close()

    # Save CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["trial", "seconds", "ms"])
        w.writeheader()
        w.writerows(rows)

    # Plot (con mean y banda ±std) + prompt
    trials = [r["trial"] for r in rows]
    ms = [r["ms"] for r in rows]

    mu = mean(ms)
    sigma = pstdev(ms)  # usa stdev(ms) si quieres desviación estándar muestral
    lower = mu - sigma
    upper = mu + sigma

    plt.figure()
    plt.plot(trials, ms, marker="o", linewidth=1)

    # Mean line
    plt.axhline(mu, linestyle="--", linewidth=1, label=f"mean = {mu:.2f} ms")

    # Banda ±1σ
    plt.fill_between(
        trials,
        [lower] * len(trials),
        [upper] * len(trials),
        alpha=0.2,
        label=f"±1σ = {sigma:.2f} ms",
    )

    # Título con prompt (corto) y modelo
    prompt_short = PROMPT_TEXT if len(PROMPT_TEXT) <= 80 else (PROMPT_TEXT[:77] + "...")
    plt.title(f"Latency per trial (model={MODEL})\nPrompt: {prompt_short}")
    plt.xlabel("Trial #")
    plt.ylabel("Time (ms)")
    plt.grid(True)
    plt.legend(loc="best")
    plt.tight_layout()

    # # Caja con prompt completo + stats (por si el título se recorta)
    # stats_text = (
    #     f"Prompt:\n{PROMPT_TEXT}\n\n"
    #     f"mean = {mu:.2f} ms\n"
    #     f"std  = {sigma:.2f} ms\n"
    #     f"mean±std = [{lower:.2f}, {upper:.2f}] ms\n"
    #     f"N = {len(ms)}"
    # )
    # plt.gcf().text(
    #     0.02, 0.02, stats_text,
    #     fontsize=9,
    #     verticalalignment="bottom",
    #     bbox=dict(boxstyle="round", alpha=0.15)
    # )

    plt.savefig(png_path, dpi=150)
    plt.close()


    # Summary
    avg = mean(ms)
    med = median(ms)
    p95 = sorted(ms)[int(0.95 * (len(ms) - 1))]
    print("\n=== DONE ===")
    print(f"Model: {MODEL}")
    print(f"Prompt: {PROMPT_TEXT}")
    print(f"SEND_SERIAL={SEND_SERIAL}, SERIAL_FLUSH={SERIAL_FLUSH}")
    print(f"Mean:  {avg:.2f} ms")
    print(f"Median:{med:.2f} ms")
    print(f"P95:   {p95:.2f} ms")
    print(f"CSV: {csv_path}")
    print(f"PNG: {png_path}")

if __name__ == "__main__":
    benchmark()
