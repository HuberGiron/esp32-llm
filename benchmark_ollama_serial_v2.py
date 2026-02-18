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
PROMPT_TEXT = "turn led on"  # la instrucción a medir
N_RUNS = 1000                                   # número de pruebas
WARMUP_RUNS = 3                               # warmup (no se guarda)

SEND_SERIAL = True                            # si False, solo mide interpretación (sin write)
SERIAL_FLUSH = True                           # flush después de write (más “end-to-end”)
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

def _ns_to_ms(v):
    # Ollama suele devolver duraciones en nanosegundos (int).
    if v is None:
        return None
    try:
        return float(v) / 1e6
    except Exception:
        return None

def ollama_generate(model: str, user_text: str):
    """
    Llama a Ollama y regresa:
      - cmd_obj: dict (JSON parseado desde `response`)
      - meta: dict con métricas (tokens / duraciones) si existen
      - raw: string crudo del campo `response`
    """
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

    raw = (data.get("response") or "").strip()

    # Métricas típicas de Ollama (pueden variar por versión)
    meta = {
        "prompt_eval_count": data.get("prompt_eval_count"),  # tokens de entrada
        "eval_count": data.get("eval_count"),                # tokens de salida
        "total_duration_ns": data.get("total_duration"),
        "load_duration_ns": data.get("load_duration"),
        "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
        "eval_duration_ns": data.get("eval_duration"),
    }
    # Versiones nuevas pueden incluir otros campos
    for k in ("created_at", "done", "done_reason", "model"):
        if k in data:
            meta[k] = data.get(k)

    if not raw:
        # No rompemos el benchmark; reportamos vacío.
        return None, meta, raw

    try:
        cmd_obj = json.loads(raw)
    except json.JSONDecodeError:
        cmd_obj = None

    return cmd_obj, meta, raw

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
        cmd_obj, meta, raw = ollama_generate(MODEL, PROMPT_TEXT)
        if cmd_obj is None:
            continue
        cmd = normalize_cmd(cmd_obj)
        line = to_serial_line(cmd)
        if SEND_SERIAL and ser is not None:
            ser.write(line.encode("utf-8"))
            if SERIAL_FLUSH:
                ser.flush()

    rows = []
    print(f"Benchmark: {N_RUNS} runs...")

    for i in range(1, N_RUNS + 1):
        t0 = time.perf_counter()

        cmd_obj, meta, raw = ollama_generate(MODEL, PROMPT_TEXT)

        serial_line = None
        parse_ok = False
        err = ""

        try:
            if cmd_obj is None:
                raise ValueError("No JSON parsed from model response")
            cmd = normalize_cmd(cmd_obj)
            serial_line = to_serial_line(cmd)
            parse_ok = True

            if SEND_SERIAL and ser is not None:
                ser.write(serial_line.encode("utf-8"))
                if SERIAL_FLUSH:
                    ser.flush()
        except Exception as e:
            err = str(e)

        t1 = time.perf_counter()
        dt = t1 - t0

        rows.append({
            "trial": i,
            "seconds": dt,
            "ms": dt * 1000.0,
            "parse_ok": int(parse_ok),
            "prompt_tokens": meta.get("prompt_eval_count"),
            "output_tokens": meta.get("eval_count"),
            "total_ms_ollama": _ns_to_ms(meta.get("total_duration_ns")),
            "load_ms_ollama": _ns_to_ms(meta.get("load_duration_ns")),
            "prompt_eval_ms_ollama": _ns_to_ms(meta.get("prompt_eval_duration_ns")),
            "eval_ms_ollama": _ns_to_ms(meta.get("eval_duration_ns")),
            "error": err,
        })

        if i % 10 == 0:
            print(f"  {i}/{N_RUNS} -> {dt*1000.0:.1f} ms | in={meta.get('prompt_eval_count')} out={meta.get('eval_count')}")

    if ser is not None:
        ser.close()

    # Save CSV (incluye tokens y duraciones internas si existen)
    fieldnames = [
        "trial", "seconds", "ms", "parse_ok",
        "prompt_tokens", "output_tokens",
        "total_ms_ollama", "load_ms_ollama", "prompt_eval_ms_ollama", "eval_ms_ollama",
        "error"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # ---- Plot + stats ----
    trials = [r["trial"] for r in rows]
    ms = [r["ms"] for r in rows]

    mu = mean(ms)
    sigma = pstdev(ms) if len(ms) > 1 else 0.0
    lower = mu - sigma
    upper = mu + sigma

    # token metrics (promedios/medianas para mostrar en la figura)
    in_tok = [r["prompt_tokens"] for r in rows if isinstance(r["prompt_tokens"], int)]
    out_tok = [r["output_tokens"] for r in rows if isinstance(r["output_tokens"], int)]

    in_tok_med = int(median(in_tok)) if in_tok else None
    out_tok_med = int(median(out_tok)) if out_tok else None

    plt.figure()
    plt.plot(trials, ms, marker=".", linewidth=0)

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

    # Título con prompt + tokens
    prompt_short = PROMPT_TEXT if len(PROMPT_TEXT) <= 80 else (PROMPT_TEXT[:77] + "...")
    tok_str = ""
    if in_tok_med is not None or out_tok_med is not None:
        tok_str = f" | in_tok={in_tok_med} out_tok={out_tok_med}"

    plt.title(f"Latency per trial (model={MODEL})\nPrompt: {prompt_short}{tok_str}")
    plt.xlabel("Trial #")
    plt.ylabel("Time (ms)")
    plt.grid(True)
    plt.legend(loc="best")
    plt.tight_layout()

    # # Caja con prompt completo + stats + tokens
    # stats_text = (
    #     f"Prompt:\n{PROMPT_TEXT}\n\n"
    #     f"in_tokens (median)  = {in_tok_med}\n"
    #     f"out_tokens (median) = {out_tok_med}\n\n"
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
    med = median(ms)
    p95 = sorted(ms)[int(0.95 * (len(ms) - 1))]
    ok_rate = sum(r["parse_ok"] for r in rows) / len(rows) * 100.0

    print("\n=== DONE ===")
    print(f"Model: {MODEL}")
    print(f"Prompt: {PROMPT_TEXT}")
    print(f"SEND_SERIAL={SEND_SERIAL}, SERIAL_FLUSH={SERIAL_FLUSH}")
    print(f"Warmup runs: {WARMUP_RUNS}")
    print(f"Parse OK rate: {ok_rate:.1f}%")
    print(f"Mean:   {mu:.2f} ms")
    print(f"Std:    {sigma:.2f} ms")
    print(f"Median: {med:.2f} ms")
    print(f"P95:    {p95:.2f} ms")
    if in_tok_med is not None and out_tok_med is not None:
        print(f"Tokens (median): in={in_tok_med} out={out_tok_med}")
    print(f"CSV: {csv_path}")
    print(f"PNG: {png_path}")

if __name__ == "__main__":
    benchmark()
