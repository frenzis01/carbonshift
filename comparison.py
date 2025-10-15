# ------------------------
# 
# 	RUN with
# python3.8 comparison.py 
# ------------------------ 

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages

err = 4.0

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
plt.figure(figsize=(10, 6))

# Creare il grafico a dispersione
plt.scatter(df['Reduction of Carbon Emissions'], df['Average Error'], s=80, c='tab:grey', zorder=2) # Ho aumentato s per punti più visibili

# Aggiungere il nome delle strategie sui rispettivi punti
for i, row in df.iterrows():
    # Estrarre le coordinate e l'etichetta
    x = row['Reduction of Carbon Emissions']
    y = row['Average Error']
    label = row['Strategy']
    
    # Aggiungere l'annotazione
    # xytext definisce la posizione del testo (spostato leggermente sopra e a destra)
    # textcoords='offset points' usa xytext come offset dal punto (x, y)
    plt.annotate(
        label, 
        (x, y), 
        textcoords="offset points", 
        xytext=(5, 5),  # Sposta l'etichetta di 0 punti a destra e 5 in alto
        ha='center',      # Allinea il testo a sinistra
        fontsize=12,
        color='black'
    )

# Aggiungere la linea orizzontale rossa tratteggiata a y=err
plt.axhline(y=err, color='red', linestyle='--', linewidth=1.5, zorder=1) # zorder per metterla sotto i punti ma sopra la griglia


# Configurare gli assi
plt.xlabel('Reduction of Carbon Emissions (%)', fontsize=12)
plt.ylabel('Average Error',fontsize=12)

# Impostare i limiti degli assi per farli assomigliare di più all'esempio
plt.xlim(0, 100) # Adattato il limite per includere tutti i dati
plt.ylim(-0.2, 6) # Adattato il limite per includere tutti i dati e le annotazioni

# Aggiungere una griglia (come nell'esempio)
plt.grid(True, linestyle='-', alpha=0.7, zorder=0) # zorder per metterla sotto i punti e la linea

# Mostrare il grafico (opzionale se si salva solo in PDF)
# plt.tight_layout()
# plt.show()


fig = plt.gcf()
with PdfPages("grafico_confronto"+str(int(err))+".pdf") as pdf:
    pdf.savefig(fig)
    plt.close(fig)