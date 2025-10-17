
# ------------------------
# 
#    RUN with
# python3.8 policies_results.py 
# ------------------------ 

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# Carica il file CSV
df = pd.read_csv("all_policies_results.csv")

# Aggrega emissioni totali e errore medio per policy
agg_df = df.groupby('policy').agg(
    total_emissions=('all_emissions', 'sum'),
    avg_error=('avg_errors', 'mean')
).reset_index()

# Mantieni l'ordine originale delle policy
policy_order = df['policy'].drop_duplicates().tolist()
agg_df['policy'] = pd.Categorical(agg_df['policy'], categories=policy_order, ordered=True)
agg_df = agg_df.sort_values('policy')
agg_df['policy_label'] = agg_df['policy']

# Imposta colori
color_emissions = '#709957'  # verde
color_errors = '#F2B705'     # giallo

# Parametri per grafico
x = np.arange(len(agg_df))
bar_height = 0.4

# Crea figura
fig, ax1 = plt.subplots(figsize=(16, 6))

# Barre per emissioni totali (Carbon Intensity)
bars1 = ax1.barh(x - bar_height/2, agg_df['total_emissions'],
                 height=bar_height, color=color_emissions, label='Carbon Intensity')

# Asse Y
ax1.set_yticks(x)
ax1.set_yticklabels(agg_df['policy_label'], fontsize=21)
ax1.tick_params(axis='x', labelcolor='black', labelsize=20)
ax1.grid(axis='x', linestyle='--', linewidth=0.5)
ax1.invert_yaxis()

# Barre per errori medi su asse gemello (Average Error)
ax2 = ax1.twiny()
bars2 = ax2.barh(x + bar_height/2, agg_df['avg_error'],
                 height=bar_height, color=color_errors, label='Average Error')

# Etichette numeriche in % alla fine delle barre gialle
for i, (val, y_pos) in enumerate(zip(agg_df['avg_error'], x + bar_height/2)):
    ax2.text(val + 0.1, y_pos, f"{val:.2f}%", va='center', fontsize=12, color='black')

# Rimuovi tick numerici in alto
ax2.tick_params(axis='x', labelbottom=False, labeltop=False, bottom=False, top=False)
ax2.set_xlim(0, max(agg_df['avg_error']) * 1.6)

# Legenda
lines = [bars1, bars2]
labels = ['Carbon Intensity', 'Average Error']
ax1.legend(lines, labels, loc='lower right', fontsize=14)

# Salva grafico in PDF
fig.tight_layout()
plt.savefig("plot/all_results_together.pdf", format='pdf')
