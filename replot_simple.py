# python replot_simple.py "bench_results/tu_archivo.csv"

import os
import sys
from statistics import mean, median, pstdev
import json

import pandas as pd
import matplotlib.pyplot as plt

def replot(csv_path: str) -> str:
    df = pd.read_csv(csv_path)

    if "trial" not in df.columns or "ms" not in df.columns:
        raise ValueError("El CSV debe tener columnas 'trial' y 'ms'.")

    trials = df["trial"].astype(int).tolist()
    ms = df["ms"].astype(float).tolist()

    mu = mean(ms)
    sigma = pstdev(ms) if len(ms) > 1 else 0.0
    lower = mu - sigma
    upper = mu + sigma

    # Tokens (median) si existen
    in_tok_med = None
    out_tok_med = None
    if "prompt_tokens" in df.columns:
        vals = [int(x) for x in df["prompt_tokens"].dropna().tolist() if str(x).strip() != ""]
        if vals:
            in_tok_med = int(median(vals))
    if "output_tokens" in df.columns:
        vals = [int(x) for x in df["output_tokens"].dropna().tolist() if str(x).strip() != ""]
        if vals:
            out_tok_med = int(median(vals))

    # Model/Prompt: si vienen en el CSV, úsalo; si no, usa placeholders
    MODEL = "unknown"
    PROMPT_TEXT = "(no incluido)"

    # 1) Si vienen en CSV, úsalos
    if "model" in df.columns and pd.notna(df["model"].iloc[0]):
        MODEL = str(df["model"].iloc[0])
    if "prompt" in df.columns and pd.notna(df["prompt"].iloc[0]):
        PROMPT_TEXT = str(df["prompt"].iloc[0])

    # 2) Si no vienen, busca meta.json en la misma carpeta del CSV
    if (MODEL == "unknown" or PROMPT_TEXT == "(no incluido)"):
        meta_path = os.path.join(os.path.dirname(csv_path), "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            MODEL = meta.get("model", MODEL)
            PROMPT_TEXT = meta.get("prompt", PROMPT_TEXT)

    base = os.path.splitext(csv_path)[0]
    png_path = base + "_replot.png"

    # === TU BLOQUE (casi igual, solo definimos png_path arriba) ===
    plt.figure()
    plt.plot(trials, ms, marker=".", linewidth=0)

    plt.axhline(mu, linestyle="--", linewidth=1, label=f"mean = {mu:.2f} ms")
    plt.fill_between(trials, [lower]*len(trials), [upper]*len(trials), alpha=0.2,
                     label=f"±1σ = {sigma:.2f} ms")

    prompt_short = PROMPT_TEXT if len(PROMPT_TEXT) <= 80 else (PROMPT_TEXT[:77] + "...")
    tok_str = ""
    if in_tok_med is not None and out_tok_med is not None:
        tok_str = f" | in_tok={in_tok_med} out_tok={out_tok_med}"

    plt.title(f"Latency per trial (model={MODEL})\nPrompt: {prompt_short}{tok_str}")
    plt.xlabel("Trial #")
    plt.ylabel("Time (ms)")
    plt.grid(True)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.savefig(png_path, dpi=150)
    plt.close()
    # === FIN BLOQUE ===

    return png_path

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python replot_simple.py <ruta_al_csv>")
        sys.exit(1)

    csv_path = sys.argv[1]
    out = replot(csv_path)
    print("OK ->", out)
