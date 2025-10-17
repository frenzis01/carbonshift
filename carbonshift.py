from argparse import ArgumentParser
from ortools.sat.python import cp_model
import sys, os, csv
from collections import defaultdict
import math

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


def main(input_requests,input_strategies,input_co2,delta,beta,error,output_assignment):
    
    all_slots               = range(delta)                                      # {0,1,...,delta-1}
    strategies              = import_input_strategies(input_strategies)                  
    carbon                  = import_input_carbon(input_co2)            
    requests                = import_input_requests(input_requests)
    max_weighted_error      = error * beta
   

    # Divisione delle richieste in blocchi (β)
    if beta is None:
        beta = 1000
    if beta >= len(requests):                   # Versione base → ogni richiesta è un blocco separato
        blocks = [[req] for req in requests]
    else:                                       # Versione con blocchi → raggruppa le richieste in β blocchi        
        sorted_requests = sorted(requests, key=lambda r: r["deadline"])   
        group_size                   = math.ceil(len(requests) / beta)              
        blocks = [sorted_requests[i:i + group_size] for i in range(0, len(sorted_requests), group_size)]

    model = cp_model.CpModel()

    B = list(range(len(blocks)))                # Indici blocchi
    S = list(range(len(strategies)))            # Indici strategie
    T = list(range(delta))                      # Indici time slot

    # Mappatura richiesta → blocco
    req_to_block = {}
    for b, group in enumerate(blocks):
        for req in group:
            req_to_block[req["id"]] = b

    # Vincolo: ogni blocco ha deadline = min delle deadline interne
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]

    # Variabili decisionali binarie: x[b,s,t] = 1 se blocco b è assegnato alla strategia s nello slot t
    x = {}
    for b in B:
        for s in S:
            for t in T:
                # Vincolo: slot t deve rispettare la deadline del blocco
                if t <= block_deadlines[b]:
                    x[(b, s, t)] = model.NewBoolVar(f"x_{b}_{s}_{t}")

    # Vincolo 1: ogni blocco deve essere assegnato ad una sola combinazione (slot, strategia)
    for b in B:
        model.AddExactlyOne(x[(b, s, t)] for s in S for t in T if (b, s, t) in x)

    # Vincolo 2: errore medio totale ≤ epsilon * numero_blocchi
    # Regola: somma degli errori pesati per le strategie usate deve essere entro soglia
    total_error_expr = []
    for b in B:
        for s in S:
            for t in T:
                if (b, s, t) in x:
                    total_error_expr.append(x[(b, s, t)] * strategies[s]["error"])
    model.Add(sum(total_error_expr) <= error * len(blocks))   # incide su max_weighted_error_threshold

    # Obiettivo: minimizzare somma(CO₂[t] * durata strategia s) su tutti i blocchi assegnati
    objective_terms = []
    for b in B:
        for s in S:
            for t in T:
                if (b, s, t) in x:
                    objective_terms.append(
                        x[(b, s, t)] * carbon[t] * strategies[s]["duration"]
                    )
    model.Minimize(sum(objective_terms))

    # Risoluzione
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 300.0 
    
    status = solver.Solve(model) # 0=UNKNOWN, 1=MODEL_INVALID, 2=FEASIBLE, 3=INFEASIBLE, 4=OPTIMAL
    solve_time = solver.UserTime()
    

    # Se non esiste soluzione ammissibile, segnala errore
    if status not in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        raise RuntimeError("No feasible assignment found")

    # Output finale: ogni richiesta eredita lo slot e la strategia assegnata al suo blocco
    assignment = {}

    # Scrittura su CSV degli assegnamenti
    output_file = output_assignment 
    file_exists = os.path.isfile(output_file)
    with open(output_file, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        if not file_exists:
            writer.writerow(["request_id", "strategy", "time_slot", "emission", "error"])
        
        
        rows = []
        for b in B:
            for s in S:
                for t in T:
                    if (b, s, t) in x and solver.BooleanValue(x[(b, s, t)]):
                        for req in blocks[b]:
                            req_id = req["id"]
                            strat_name = strategies[s]["strategy"]
                            duration = int(strategies[s]["duration"])
                            error = int(strategies[s]["error"])                           
                            emission = solver.Value(x[(b, s, t)]) * carbon[t] * duration / 3600 # in kWh
                            
                            # converting to gCO2 based on server consumption
                            server_kwh_per_hour = 0.05  # Typical server consumption in kiloWatt-hour    
                            emission = round(emission*server_kwh_per_hour, 6)  # Emission in grams of CO2

                            assignment[req_id] = (t, strat_name)
                            rows.append([req_id, strat_name, t, emission, error])

        
        rows.sort(key=lambda r: r[0])           # Ordina le righe per request_id
        for row in rows:            
            writer.writerow(row)

        # Calcolo delle metriche 
        max_weighted_error_threshold = sum(row[4] for row in rows)
        all_emissions = sum(row[3] for row in rows)

        slot_emissions_dict = defaultdict(int)
        for row in rows:
            slot = row[2]
            slot_emissions_dict[slot] += row[3]

        slot_emissions_list = [slot_emissions_dict.get(t, 0) for t in T]
        num_requests = len(rows)
        avg_error = round(max_weighted_error_threshold / num_requests, 4) if num_requests > 0 else 0.0
                

        # Scrittura delle metriche nel file
        csvfile.write(f"\n"
            f"max_weighted_error_threshold: {max_weighted_error_threshold}\n"
            f"solver_status: {status}\n"
            f"all_emissions:{all_emissions}\n"
            f"slot_emissions:{slot_emissions_list}\n"
            f"all_errors:{avg_error}\n"
            f"solve_time:{round(solve_time, 4)}\n"
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
