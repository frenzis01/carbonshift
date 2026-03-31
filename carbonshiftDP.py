from argparse import ArgumentParser
import sys, os, csv
from collections import defaultdict
import math, time

def import_input_requests(input_requests):          # Import requests' data

    r_id, requests = 0, []

    with open(input_requests, "r") as file:
        line = file.readline()                      # read only the first line 
        if line:                                    # check if the file is not empty
            values = line.replace("\n","").split(",")
            for v in values:
                r = {}
                r['id'] = r_id
                r['deadline'] = int(v)
                r_id += 1
                requests.append(r)

    return requests

def import_input_strategies(input_strategies):      # Import strategies' data
    strategies = []

    with open(input_strategies, "r") as file:
        next(file)                                  # skip the line with the headers
        for line in file:
            values = line.replace("\n","").split(",")
            s = {}
            s['strategy'] = values[2]  
            s['error'] = int(values[0])
            s['duration'] = int(values[1])
            strategies.append(s)

    return strategies

def import_input_carbon(input_co2):                 # Import carbon intensities' data
    carbon = []

    with open(input_co2, "r") as file:
        for line in file: 
            co2 = int(line.replace("\n",""))
            carbon.append(co2)

    return carbon


# ------------------------
# FATTORI DI SCALA PER LA DP
# ------------------------
# L'errore deve essere discreto per la DP (si assume che l'errore sia un intero 0-100 (percentuale)).
# Lo impostiamo a 1 assumendo che strategies[s]['error'] sia già un intero (es. 10, 20)
# Altrimenti, lo usiamo per evitare float.
SCALE_FACTOR = 1  

def solve_with_dynamic_programming(blocks, strategies, carbon, delta, error):
    """
    Contiene la logica per risolvere il problema di assegnazione utilizzando la programmazione dinamica.
    Stato: D[b][e] = minimo costo carbonio per assegnare i primi 'b' blocchi con errore cumulativo 'e'.
    """
    
    B = len(blocks)
    S = range(len(strategies))
    T = range(delta)
    
    # 1. PREPARAZIONE E VINCOLI
    
    # Vincolo: ogni blocco ha deadline = min delle deadline interne
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    # Calcolo del massimo errore cumulativo tollerato (E_max), definisce la dimensione della DP
    E_max = error * B * SCALE_FACTOR 

    # Inizializzazione della DP (D[errore] = costo)
    # D_prev memorizza lo stato DP per il blocco precedente (b-1)
    # D_prev[e] = Minimo costo carbonio accumulato per errore 'e'
    D_prev = {e: float('inf') for e in range(E_max + 1)}
    D_prev[0] = 0  # Stato base: 0 blocchi assegnati, 0 errore, 0 costo

    # Tracciamento della ricostruzione della soluzione (per tracciare la strategia scelta)
    # Trace[b][e] = (errore_precedente, strategia, slot) che ha portato a questo stato
    trace = [[None] * (E_max + 1) for _ in range(B + 1)] 
    
    # 2. ALGORITMO DI PROGRAMMAZIONE DINAMICA
    for b in range(1, B + 1):  # Iterazione sui blocchi da 1 a B
        D_curr = {e: float('inf') for e in range(E_max + 1)}
        
        # Considera il blocco corrente da assegnare: blocks[b-1]
        block_index = b - 1 
        current_block_size = len(blocks[block_index])
        
        # Iterazione su tutti gli stati di errore raggiunti nel blocco precedente
        for e_prev, cost_prev in D_prev.items():
            if cost_prev == float('inf'):
                continue
            
            # Per ogni possibile assegnazione del blocco corrente (Strategia s in Slot t)
            for s in S:
                for t in T:
                    
                    # VINCOLO: Verifica la deadline
                    if t > block_deadlines[block_index]:
                        continue
                    
                    # Calcola i costi e gli errori della transizione calcolato per unità di richiesta
                    error_s = strategies[s]["error"] * SCALE_FACTOR
                    carbon_cost = carbon[t] * strategies[s]["duration"] * current_block_size                    
                    e_current = e_prev + error_s
                    
                    # Verifica il vincolo di errore globale
                    if e_current <= E_max:
                        new_cost = cost_prev + carbon_cost
                        
                        # Aggiornamento dello stato DP: D[b][e_current]
                        if new_cost < D_curr[e_current]:
                            D_curr[e_current] = new_cost
                            
                            # Memorizza il percorso: (errore_precedente, strategia_scelta, slot_scelto)
                            trace[b][e_current] = (e_prev, s, t)

        D_prev = D_curr # Aggiorna lo stato precedente per il ciclo successivo

    # 3. RISULTATO FINALE E RICOSTRUZIONE DEL PERCORSO
    
    # Trova il minimo costo tra tutti gli stati finali (l'ultima riga della DP)

    min_cost = min(D_prev.values())
    final_error = -1
    for e in range(E_max + 1):
        if D_prev[e] == min_cost:
            final_error = e
            break

    if min_cost == float('inf'):
        return None, float('inf'), -1, "INFEASIBLE"


    # Ricostruzione della soluzione ottimale
    optimal_assignments = [] # Lista di (blocco_index, strategia, slot)
    
    current_error = final_error
    for b in range(B, 0, -1): # Ricostruisci all'indietro
        
        # Ottieni la mossa che ha portato a questo stato
        e_prev, s_chosen, t_chosen = trace[b][current_error]
        
        optimal_assignments.append({
            "block_index": b - 1, # Indice blocco 0-based
            "strategy": s_chosen,
            "time_slot": t_chosen,
        })
        
        # Aggiorna l'errore per il blocco precedente
        current_error = e_prev
    
    optimal_assignments.reverse() # Ordina in avanti
    
    # 4. Calcolo metriche finali
    avg_error = (final_error / SCALE_FACTOR) / B
    
    return optimal_assignments, min_cost, avg_error, "OPTIMAL"



def main(input_requests,input_strategies,input_co2,delta,beta,error,output_assignment):
    
    #all_slots               = range(delta)                                      # {0,1,...,delta-1}
    strategies              = import_input_strategies(input_strategies)                  
    carbon                  = import_input_carbon(input_co2)            
    requests                = import_input_requests(input_requests)
    #max_weighted_error      = error * beta
    
    # Divisione delle richieste in blocchi (β)
    if beta is None:
        beta = 1000
    if beta >= len(requests):                   # Versione base → ogni richiesta è un blocco separato
        blocks = [[req] for req in requests]
    else:                                       # Versione con blocchi → raggruppa le richieste in β blocchi        
        sorted_requests = sorted(requests, key=lambda r: r["deadline"])   
        group_size                   = math.ceil(len(requests) / beta)              
        blocks = [sorted_requests[i:i + group_size] for i in range(0, len(sorted_requests), group_size)]

    # Mappatura richiesta → blocco (essenziale per l'output)
    req_to_block = {}
    for b, group in enumerate(blocks):
        for req in group:
            req_to_block[req["id"]] = b
    
    # Vincolo: ogni blocco ha deadline = min delle deadline interne (necessario per DP)
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    start_time = time.time()                    # Inizio misurazione tempo
    optimal_assignments_dp, all_emissions_UNSCALED, avg_error_scaled, status_str = solve_with_dynamic_programming(
        blocks, strategies, carbon, delta, error
    )
    solve_time = time.time() - start_time

    # ----------------------------------------------------
    # GESTIONE DELL'OUTPUT
    # ----------------------------------------------------
    status                  = -1 # Solver status code

    if status_str == "INFEASIBLE":
        raise RuntimeError("No feasible assignment found")
        
        output_file = output_assignment 
        with open(output_file, "w", newline="") as csvfile:
            csvfile.write(f"solver_status: {status_str}\n")
            csvfile.write(f"solve_time:{round(solve_time, 4)}\n")
        return


    # Variabili fisiche di scala (NON APPLICATE ALLA DP)
    SERVER_KWH_PER_HOUR = 0.05
    SCALING_FACTOR_PHYSICAL = SERVER_KWH_PER_HOUR / 3600
    # *** NUOVA DEFINIZIONE: all_emissions = totale DP scalato ***
    all_emissions = all_emissions_UNSCALED * SCALING_FACTOR_PHYSICAL

    rows = []
    total_output_emissions_check = 0.0 # Contatore per la controprova

    for assign in optimal_assignments_dp:       # Utilizziamo l'assegnazione ottimale trovata 
        b = assign["block_index"]
        s = assign["strategy"]
        t = assign["time_slot"]
        
        strategy_data = strategies[s]           # Recupera i dati della strategia
        strat_name = strategy_data["strategy"]
        duration = int(strategy_data["duration"])
        error_val = int(strategy_data["error"])

        # Calcolo dell'emissione (la parte DP calcola l'obiettivo, qui calcoliamo il valore in output)
        
        # Emissione di un blocco: CO2[t] * durata[s] * dimensione_blocco
        emission_total_block_unscaled = carbon[t] * duration * len(blocks[b])

        # Emissione per Singola Richiesta (SCALED per CSV)
        emission_per_req_unscaled = emission_total_block_unscaled / len(blocks[b])
        emission_scaled = emission_per_req_unscaled * SCALING_FACTOR_PHYSICAL
        emission = round(emission_scaled, 6)

        # CONTROPROVA: accumula il totale delle emissioni calcolate per richiesta
        total_output_emissions_check += emission * len(blocks[b])

        for req in blocks[b]:    #Assegna il risultato a ogni richiesta nel blocco
            req_id = req["id"]
            rows.append([req_id, strat_name, t, emission, error_val])
        
           

    # Scrittura su CSV degli assegnamenti
    output_file = output_assignment 
    file_exists = os.path.isfile(output_file)
    with open(output_file, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        if not file_exists:
            writer.writerow(["request_id", "strategy", "time_slot", "emission", "error"])

        rows.sort(key=lambda r: r[0])           # Ordina le righe per request_id
        for row in rows:            
            writer.writerow(row)        
        
        # Calcolo metriche per l'output (basate sui valori della DP)
        num_requests = len(requests)
        avg_error_final = round((avg_error_scaled * SCALE_FACTOR), 4) if num_requests > 0 else 0.0

        # Calcolo delle emissioni per slot (DEVE usare l'assegnazione per blocco)
        slot_emissions_dict = defaultdict(float)        

        # Iteriamo sugli assegnamenti DP (che sono unici per blocco)
        for assign in optimal_assignments_dp:
            b = assign["block_index"]
            t = assign["time_slot"]
            s = assign["strategy"] 
            
            # Recuperiamo i dati necessari
            duration = strategies[s]["duration"]
            carbon_intensity = carbon[t]
            block_size = len(blocks[b])

            # *** LOGICA DI CALCOLO DEL COSTO DEL BLOCCO (Stessa logica della funzione obiettivo DP) ***
            # Emissione Totale del Blocco: carbon[t] * duration * block_size
            emission_total_block_unscaled = carbon_intensity * duration * block_size

            # Emissione Totale del Blocco: emissione_per_req * block_size (già calcolata sopra)
            #emission_per_req = emission_total_block / block_size # Re-calcolo solo per chiarezza, è emissione per richiesta
            
            # Sommiamo il contributo totale del blocco
            #slot_emissions_dict[t] += emission_per_req * len(blocks[b])        
            slot_emissions_dict[t] += emission_total_block_unscaled * SCALING_FACTOR_PHYSICAL        
        slot_emissions_list = [slot_emissions_dict.get(t, 0) for t in range(delta)]

        # *** CONTROLLO DI COERENZA ***
        # all_emissions (risultato DP) dovrebbe essere molto vicino a total_output_emissions_check
        # Un piccolo delta è accettabile a causa degli arrotondamenti (round(emission, 6))

        total_slot_emissions_final = sum(slot_emissions_list)        
        
        if status_str == "OPTIMAL":
            status = 4

        # Scrittura delle metriche nel file
        csvfile.write(f"\n"
            f"max_weighted_error_threshold: {error * num_requests}\n"   # Errore massimo totale consentito
            f"solver_status: {status}\n"
            f"all_emissions:{all_emissions}\n"                          # Questo è l'obiettivo minimizzato (totale)
            f"slot_emissions:{slot_emissions_list}\n"    # VALUTA COERENZA 
            f"all_errors:{avg_error_final}\n"
            f"solve_time:{round(solve_time, 4)}\n"
            # CONTROPROVA: La differenza dovrebbe essere molto vicina a zero
            #f"DP_vs_Output_Check: {all_emissions - total_slot_emissions_final}\n"        
        )



# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Optimizer of assignments of blocks of requests to strategies and time slots.")

parser.add_argument('input_requests', type=str, help="File with requests' deadline data.")
parser.add_argument('input_strategies', type=str, help="File with statistics on strategies' errors and duration, for each delta's slot.")
parser.add_argument('input_co2', type=str, help="File with carbon intensities' data, for each delta's slot.")
parser.add_argument('delta', type=int, help='Number of slots per window.')
parser.add_argument('beta', type=int, help='Number of blocks of requests per actual slot.')
parser.add_argument('error', type=int, help='Tolerated error (%).')
parser.add_argument('output_assignment', type=str, help='File where to write the output assignment.')

args = parser.parse_args()
main(
    args.input_requests,
    args.input_strategies,
    args.input_co2,
    args.delta,
    args.beta,
    args.error,
    args.output_assignment
    )
