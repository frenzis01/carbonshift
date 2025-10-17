# ------------------------
# 
#    RUN with
# python3.8 normalized_graphs.py 
# ------------------------ 



import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# === Load data ===

df = pd.read_csv("all_policies_results.csv")

# Aggregate: sum of emissions, mean of errors

policy_stats = df.groupby("policy").agg(
    all_emissions=("all_emissions", "sum"),
    avg_errors=("avg_errors", "mean")
    ).reset_index()

# Baseline emissions

baseline_value = policy_stats.loc[policy_stats["policy"] == "Baseline", "all_emissions"].values[0]

# Normalized emissions relative to Baseline, then scaled to [0,1]

#policy_stats["emissions_norm"] = 1 - (policy_stats["all_emissions"] / baseline_value)
#policy_stats["emissions_norm"] = (policy_stats["emissions_norm"] - policy_stats["emissions_norm"].min()) / (policy_stats["emissions_norm"].max() - policy_stats["emissions_norm"].min())
policy_stats["emissions_norm"] = policy_stats["all_emissions"] / policy_stats["all_emissions"].max()

# Normalized errors [0,1]

policy_stats["errors_norm"] = policy_stats["avg_errors"] / policy_stats["avg_errors"].max()

# Colors for each policy

palette = plt.cm.tab10.colors
policy_list = policy_stats["policy"].tolist()
colors = {p: palette[i % len(palette)] for i, p in enumerate(policy_list)}

# === Scatter plot ===

fig, ax = plt.subplots(figsize=(9, 6))

for p in policy_list:
    row = policy_stats[policy_stats["policy"] == p]
    x = row["emissions_norm"].values[0]
    y = row["errors_norm"].values[0]


    ax.scatter(x, y, s=200, marker="o", color=colors[p],edgecolor="black", linewidth=0.8)
    ax.text(x, y + 0.03, p, fontsize=9, ha="center")


# Axis labels in English

ax.set_xlabel("Normalized Emissions")
ax.set_ylabel("Normalized Average Error")

# Add margins to avoid cutting circles on the borders

ax.set_xlim(-0.05, 1.15)
ax.set_ylim(-0.05, 1.15)

# Grid

ax.grid(True, linestyle="--", alpha=0.6)

plt.tight_layout()

# === Save outputs ===

fig.savefig("plot/normalized_scatter_plot.pdf")
#fig.savefig("normalized_scatter_plot.png", dpi=300)

#plt.show()
