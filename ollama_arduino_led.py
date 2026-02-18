import json
import time
import requests
import serial

OLLAMA_URL = "http://localhost:11434/api/generate"

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

def ollama_parse(model: str, user_text: str) -> dict:
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": user_text,
        "stream": False,
        "format": JSON_SCHEMA,  # structured outputs with JSON schema
        "options": {"temperature": 0},
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    # /api/generate returns generated text in "response" when stream=false
    raw = data.get("response", "").strip()
    if not raw:
        raise ValueError("Ollama returned empty response")

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {raw}") from e
    return obj

def normalize_cmd(cmd: dict) -> dict:
    action = cmd.get("action")
    if action not in {"on", "off", "blink", "hold", "pattern", "stop"}:
        raise ValueError("Invalid action")

    # Defaults
    out = {"action": action}
    if action == "blink":
        out["count"] = int(cmd.get("count", 3))
        out["on_ms"] = int(cmd.get("on_ms", 200))
        out["off_ms"] = int(cmd.get("off_ms", 200))
        if not (1 <= out["count"] <= 100): raise ValueError("count out of range")
        for k in ("on_ms", "off_ms"):
            if not (10 <= out[k] <= 60000): raise ValueError(f"{k} out of range")

    elif action == "hold":
        out["duration_ms"] = int(cmd.get("duration_ms", 1000))
        if not (10 <= out["duration_ms"] <= 600000): raise ValueError("duration_ms out of range")

    elif action == "pattern":
        out["repeat"] = int(cmd.get("repeat", 1))
        seq = cmd.get("sequence_ms", [200, 200])
        if not isinstance(seq, list) or len(seq) < 1: raise ValueError("sequence_ms invalid")
        seq = [int(x) for x in seq][:50]
        if not (1 <= out["repeat"] <= 50): raise ValueError("repeat out of range")
        for x in seq:
            if not (10 <= x <= 60000): raise ValueError("sequence_ms value out of range")
        out["sequence_ms"] = seq

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
        return f"BLINK {cmd['count']} {cmd['on_ms']} {cmd['off_ms']}\n"
    if a == "hold":
        return f"HOLD {cmd['duration_ms']}\n"
    if a == "pattern":
        seq = cmd["sequence_ms"]
        n = len(seq)
        seq_part = " ".join(str(x) for x in seq)
        return f"PATTERN {cmd['repeat']} {n} {seq_part}\n"
    raise ValueError("Unhandled action")

def wait_for_ready(ser: serial.Serial, timeout_s: float = 3.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        line = ser.readline().decode(errors="ignore").strip()
        if line:
            print(f"[arduino] {line}")
        if line == "READY":
            return
    print("[warn] No READY seen (continuing).")

def main():
    # # Ajusta estos dos:
    # port = input("Serial port (e.g., COM5 or /dev/ttyACM0): ").strip()
    # model = input("Ollama model (e.g., llama3.2, qwen2.5): ").strip() or "llama3.2"

    # Fijos:
    port = "COM5"
    model = "mistral-nemo:12b-instruct-2407-q4_0"   # o el tag exacto que veas en `ollama list`

    ser = serial.Serial(port, 115200, timeout=0.5)
    time.sleep(1.5)  # allow Arduino reset on serial open
    wait_for_ready(ser)

    print("\nEscribe comandos en español. Ejemplos:")
    print("  - enciende el led")
    print("  - apaga el led")
    print("  - parpadea 3 veces cada 300 ms")
    print("  - mantenlo encendido por 2 segundos")
    print("  - patrón: 100ms encendido, 100ms apagado, 500ms encendido; repite 2 veces\n")
    print("Escribe 'exit' para salir.\n")

    while True:
        user_text = input("Tú> ").strip()
        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit"}:
            break

        try:
            cmd = ollama_parse(model, user_text)
            cmd = normalize_cmd(cmd)
            line = to_serial_line(cmd)

            print(f"[json] {cmd}")
            print(f"[tx ] {line.strip()}")

            ser.write(line.encode())

            # leer respuesta(s) breve(s)
            t0 = time.time()
            while time.time() - t0 < 1.0:
                resp = ser.readline().decode(errors="ignore").strip()
                if resp:
                    print(f"[rx ] {resp}")
                    # si llega OK/ERR, podemos salir
                    if resp.startswith("OK") or resp.startswith("ERR"):
                        break

        except Exception as e:
            print(f"[error] {e}")

    ser.close()

if __name__ == "__main__":
    main()
