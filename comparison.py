# ------------------------
# 
# 	RUN with
# python3.8 comparison.py 
# ------------------------ 

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages

err = 2.0

if err == 2.0:
    data = {
        "Strategy": ["Random", "Carbon-driven", "Error-driven", "Cleanest slot", "Carbonshift"],
        "Reduction of Carbon Emissions": [23.6, 58.6, 32.1, 45.8, 72.8],
        "Average Error": [2.4, 2.4, 1.99, 0, 2]
    }
elif err == 4.0:
    data = {
        "Strategy": ["Random", "Carbon-driven", "Error-driven", "Cleanest slot", "Carbonshift"],
        "Reduction of Carbon Emissions": [23.6, 58.6, 48.3, 45.8, 86.8],
        "Average Error": [2.4, 2.4, 3.01, 0, 4]
    }
else: #elif err == 5.0:
    data = {
        "Strategy": ["Random", "Carbon-driven", "Error-driven", "Cleanest slot", "Carbonshift"],
        "Reduction of Carbon Emissions": [23.6, 58.6, 48.3, 45.8, 92.1],
        "Average Error": [2.4, 2.4, 3, 0, 5]
    }

# Creare un DataFrame Pandas
df = pd.DataFrame(data)

# Creare la figura e gli assi
# **MODIFICA**: Usiamo plt.subplots per ottenere fig e ax
fig, ax = plt.subplots(figsize=(7, 5)) 

# Creare il grafico a dispersione
# **MODIFICA**: Usiamo ax.scatter invece di plt.scatter
ax.scatter(df['Reduction of Carbon Emissions'], df['Average Error'], s=80, c='tab:grey', zorder=2) 


# Aggiungere il nome delle strategie sui rispettivi punti
for i, row in df.iterrows():
    # Estrarre le coordinate e l'etichetta
    x = row['Reduction of Carbon Emissions']
    y = row['Average Error']
    label = row['Strategy']
    
    # Aggiungere l'annotazione
    # **MODIFICA**: Usiamo ax.annotate invece di plt.annotate
    ax.annotate(
        label, 
        (x, y), 
        textcoords="offset points", 
        xytext=(5, 5),  # Sposta l'etichetta di 5 punti a destra e 5 in alto
        ha='center', 
        fontsize=16,
        color='black'
    )

# Aggiungere la linea orizzontale rossa tratteggiata a y=err
# **MODIFICA**: Usiamo ax.axhline invece di plt.axhline
ax.axhline(y=err, color='red', linestyle='--', linewidth=1.5, zorder=1) 


# Configurare gli assi
# **MODIFICA**: Usiamo ax.set_xlabel/ylabel invece di plt.xlabel/ylabel
ax.set_xlabel('Reduction of Carbon Emissions (%)', fontsize=16)
ax.set_ylabel('Average Error (%)',fontsize=16)

# Impostare i limiti degli assi per farli assomigliare di più all'esempio
# **MODIFICA**: Usiamo ax.set_xlim/ylim invece di plt.xlim/ylim
ax.set_xlim(0, 105) # Ho aumentato leggermente il limite per dare spazio
ax.set_ylim(-0.2, 6.5) # Ho aumentato leggermente il limite per dare spazio

# **NUOVE ISTRUZIONI**: Rimuovere le spine superiori e destre
ax.spines['right'].set_visible(False)
ax.spines['top'].set_visible(False)

# **MODIFICA**: Usiamo ax.set_xticks/yticks invece di plt.xticks/yticks
ax.set_xticks(np.arange(0, 101, 20))
ax.set_yticks(np.arange(0, 7, 1))
ax.set_xticklabels(np.arange(0, 101, 20), fontsize=16)
ax.set_yticklabels(np.arange(0, 7, 1), fontsize=16)

# Aggiungere una griglia (come nell'esempio)
# **MODIFICA**: Usiamo ax.grid invece di plt.grid
ax.grid(True, linestyle='-', alpha=0.7, zorder=0) 

# Rimuove i margini bianchi interni attorno al grafico prima del salvataggio
plt.subplots_adjust(left=0.01, right=0.99, top=0.9, bottom=0.1)

# Salvataggio in PDF con rimozione dei margini bianchi esterni
# **MODIFICA**: fig è già definito da plt.subplots
with PdfPages("grafico_confronto"+str(int(err))+".pdf") as pdf:
    pdf.savefig(fig, bbox_inches='tight') 
    plt.close(fig)