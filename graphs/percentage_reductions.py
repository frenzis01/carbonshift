# ------------------------
# 
#    RUN with
# python3.8 percentage_reductions.py 
# ------------------------ 


import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

# === PARAMETRI COLORI ===

color_emissions = '#709957'  # verde
color_errors = '#F2B705'     # giallo

# === LETTURA DATI ===

df = pd.read_csv("all_policies_results.csv")

# Aggregazione: somma emissioni, media errori

policy_stats = df.groupby("policy").agg(
    all_emissions=("all_emissions", "sum"),
    avg_errors=("avg_errors", "mean")
    ).reset_index()

# Ordine politiche desiderato (Random in alto, poi Naive Carbon, ecc.)

policy_order = [
    "Random",
    "Naive Carbon",
    "Naive Error (ε=2)",
    "Naive Error (ε=4)",
    "Naive Error (ε=5)",
    "Naive Shift",
    "Carbonshift (ε=2)",
    "Carbonshift (ε=4)",
    "Carbonshift (ε=5)"
    ]

# Valore baseline

baseline_value = policy_stats.loc[policy_stats["policy"] == "Baseline", "all_emissions"].values[0]

# Normalizzazione emissioni in % rispetto a Baseline

policy_stats["emissions_%"] = (1 - policy_stats["all_emissions"] / baseline_value) * 100

# Mantengo solo le politiche nell'ordine richiesto

plot_stats = policy_stats.set_index("policy").reindex(policy_order).dropna().reset_index()

policies = plot_stats["policy"].tolist()
emissions_pct = plot_stats["emissions_%"].values
errors = plot_stats["avg_errors"].values

# === GRAFICO ===

bar_height = 0.4
y = np.arange(len(policies))

#fig, ax = plt.subplots(figsize=(11, 7))
fig, ax = plt.subplots(figsize=(16, 6))

bars_em = ax.barh(y - bar_height/2, emissions_pct, bar_height,
color=color_emissions, label="Reduction of Carbon Emissions")
bars_err = ax.barh(y + bar_height/2, errors, bar_height,
color=color_errors, label="Average Error")

# Etichette valori emissioni (%)

for b in bars_em:
    w = b.get_width()
    ax.text(w + 1, b.get_y() + b.get_height()/2, f"{w:.1f}%", va="center", fontsize=12)

# Etichette valori errori (%)

for b in bars_err:
    w = b.get_width()
    ax.text(w + 0.15, b.get_y() + b.get_height()/2, f"{w:.2f}%", va="center", color="black", fontsize=12)

    # Assi

    ax.set_yticks(y)
    ax.set_yticklabels(policies, fontsize=21)
    ax.tick_params(axis='x', labelcolor='black', labelsize=20)
    ax.invert_yaxis()  # Random in alto
    ax.set_xlim(0, 100)  # scala fino a 100

    # Nessun titolo o etichetta asse X

    ax.set_xlabel("",fontsize=16)

    # Legenda in alto a destra

    ax.legend(loc="upper right", fontsize=14)

    plt.tight_layout()

# === SALVATAGGIO PDF ===

with PdfPages("plot/grafico_emissioni_errori.pdf") as pdf:
    pdf.savefig(fig)
    plt.close(fig)
