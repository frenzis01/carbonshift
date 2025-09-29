from argparse import ArgumentParser
import subprocess
import random

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

def import_input_carbon(input_co2):                 # Import carbon intensities data
    carbon = []

    with open(input_co2, "r") as file:
        for line in file: 
            co2 = int(line.replace("\n",""))
            carbon.append(co2)

    return carbon


def baseline_policy(requests, strategies, carbon):                  # Co2 unaware: no slot shifting, no strategy shifting
    """
    Each request is served in the slot it arrives,
    with the strategy with lowest error (i.e. High). 
    """

    assignment = []
    for k in requests:                  
        best_slot = 0                                                           # Select first slot carbon intensity                                                                                             
        valid_strategies = [s for s in strategies if s['strategy'] == 'High']   # Select the lowest error strategy
        best_strategy = min(valid_strategies, key=lambda s: s['error'])                        
        emission = carbon[best_slot] * best_strategy['duration']

        # converting to gCO2 based on server consumption
        server_kwh_per_hour = 0.05  # Typical server consumption in kiloWatt-hour
        server_wh_per_second = server_kwh_per_hour / 3600  # Convert to Watt-second
        emission = round(emission*server_wh_per_second, 6)  # Emission in grams of CO2

        assignment.append({                                 
            'request_id': k['id'],
            'strategy': best_strategy['strategy'], 
            'time_slot': best_slot,
            'emission': emission,                  
            'error': best_strategy['error']
        })
    return assignment

def random_policy(requests, fixed_strategy, strategies, carbon):    # Co2 unaware: no slot shifting, ok strategy shifting
    """
    Each request is served in the slot it arrives,
    with a random selected strategy.
    """

    assignment = []
    best_slot = 0                

    for k in requests:                                                                           
        valid_strategies = [s for s in strategies if s['strategy'] == fixed_strategy]   # Filter strategies 
        valid_strategies.sort(key=lambda s: (s['error'], s['duration']))                # Sort strategies by error and then by duration                 
        best_strategy = min(valid_strategies, key=lambda s: s['error'])                        
        emission = carbon[best_slot] * best_strategy['duration']

        # converting to gCO2 based on server consumption
        server_kwh_per_hour = 0.05  # Typical server consumption in kiloWatt-hour
        server_wh_per_second = server_kwh_per_hour / 3600  # Convert to Watt-second
        emission = round(emission*server_wh_per_second, 6)  # Emission in grams of CO2


        assignment.append({                                   
            'request_id': k['id'],
            'strategy': best_strategy['strategy'],
            'time_slot': best_slot,
            'emission': emission,          
            'error': best_strategy['error']
        })

    return assignment

def naive_carbon(requests, strategies, carbon):                     # Co2 unaware: no slot shifting, ok strategy shifting
    """
    Each request is served in the slot it arrives, 
    with a strategy chosen based on https://carbonintensity.org.uk/ 
    values of 27/05/25.
    """

    assignment = []
    for k in requests:                                                                           
        best_slot = 0                                       

        if carbon[best_slot] >= 181:# and carbon[best_slot] <= 227:         
            fixed_strategy = "Low"
        elif carbon[best_slot] >= 119 and carbon[best_slot] <= 180: 
            fixed_strategy = "Medium"
        else: 
            fixed_strategy = "High"

        valid_strategies = [s for s in strategies if s['strategy'] == fixed_strategy] 
        best_strategy = min(valid_strategies, key=lambda s: s['error'])                        
        emission = carbon[best_slot] * best_strategy['duration']

        # converting to gCO2 based on server consumption
        server_kwh_per_hour = 0.05  # Typical server consumption in kiloWatt-hour
        server_wh_per_second = server_kwh_per_hour / 3600  # Convert to Watt-second
        emission = round(emission*server_wh_per_second, 6)  # Emission in grams of CO2


        assignment.append({                                  
            'request_id': k['id'],
            'strategy': best_strategy['strategy'], 
            'time_slot': best_slot,
            'emission': emission,                  
            'error': best_strategy['error']
        })
    return assignment


def naive_error(requests, strategies, carbon, threshold): # Co2 unaware: no slot shifting, ok strategy shifting
    """
    Each request is served with the strategy High if the average error is above the average accumulated error,
    otherwise, it randomly selects a strategy.
    The time slot is the first one.
    """


    assignment = []
    best_slot = 0
    avg_error = 0
    processed_requests = 0

    for k in requests:                                                                                   
        strategy = strategies[0]  # Default strategy, in case no condition is met, oppure None
        temp_avg_errors = round((avg_error/processed_requests),2) if processed_requests > 0 else 0

        if temp_avg_errors < threshold:  # If the average error is below the threshold, select a random strategy
            s_random = random.choice(['High','Medium','Low']) 
            for s in strategies:
                if s['strategy'] == s_random: 
                    strategy = s
                    break
        else:  # If the average error is above the threshold, select the strategy with the lowest error
            for s in strategies:
                if s['strategy'] == 'High': 
                    strategy = s
                    break
        processed_requests += 1           
        avg_error += strategy['error']      
        emission = carbon[best_slot] * strategy['duration']

        # converting to gCO2 based on server consumption
        server_kwh_per_hour = 0.05  # Typical server consumption in kiloWatt-hour
        server_wh_per_second = server_kwh_per_hour / 3600  # Convert to Watt-second
        emission = round(emission*server_wh_per_second, 6)  # Emission in grams of CO2

        assignment.append({                                   
            'request_id': k['id'],
            'strategy': strategy['strategy'], 
            'time_slot': best_slot,
            'emission': emission,          
            'error': strategy['error']
        })    

    return assignment


def naive_shift(requests, strategies, carbon): # Co2 aware: ok slot shifting, no strategy shifting
    """
    Each request is served with the strategy High,
    in the time slot with the lowest carbon intensity within the request's deadline.
    """

    assignment = []

    for k in requests:                                                                                   
        best_slot = min(range(k['deadline'] + 1), key=lambda i: carbon[i])  # Greenest slot within the request's deadline
        
        valid_strategies = [s for s in strategies if s['strategy'] == 'High']   
        valid_strategies.sort(key=lambda s: (s['error'], s['duration']))                       
        best_strategy = min(valid_strategies, key=lambda s: s['error'])
        emission = carbon[best_slot] * best_strategy['duration']

        # converting to gCO2 based on server consumption
        server_kwh_per_hour = 0.05  # Typical server consumption in kiloWatt-hour
        server_wh_per_second = server_kwh_per_hour / 3600  # Convert to Watt-second
        emission = round(emission*server_wh_per_second, 6)  # Emission in grams of CO2                        
        
        assignment.append({                                   
            'request_id': k['id'],
            'strategy': best_strategy['strategy'], 
            'time_slot': best_slot,
            'emission': emission,          
            'error': best_strategy['error']
        })

    return assignment





# Use each assignment method and write the results to the output file.
def main(
        input_requests,
        input_strategies,
        input_co2,
        delta,        
        out_baseline,
        out_random,
        out_naive_carbon, 
        out_naive_err_2, 
        out_naive_err_4,
        out_naive_err_5,
        out_naive_shift 
    ): 

    all_slots           = range(delta)
    strategies          = import_input_strategies(input_strategies)
    carbon              = import_input_carbon(input_co2)
    requests            = import_input_requests(input_requests)
    
    #assert (delta <= len(carbon) and delta <= len(requests)), "Provide a carbon intensity per slot and at least one block of requests per slot."
    
    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_baseline = baseline_policy(requests, strategies, carbon)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_base = round(END-START,4)

    s_random = random.choice(['High','Medium','Low'])
    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_random   = random_policy(requests, s_random, strategies, carbon)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_random = round(END-START,4)

    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_naive_carbon = naive_carbon(requests, strategies, carbon)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_n_carbon = round(END-START,4)

    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_naive_err_2 = naive_error(requests, strategies, carbon,2)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_n_err2 = round(END-START,4)

    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_naive_err_4 = naive_error(requests, strategies, carbon,4)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_n_err4 = round(END-START,4)

    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_naive_err_5 = naive_error(requests, strategies, carbon,5)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_n_err5 = round(END-START,4)

    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    assign_naive_shift = naive_shift(requests, strategies, carbon)
    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
    time_n_shift = round(END-START,4)

    
    output_files = [out_baseline, out_random, out_naive_carbon, out_naive_err_2, out_naive_err_4, out_naive_err_5, out_naive_shift]
    output_assignments = [assign_baseline, assign_random, assign_naive_carbon, assign_naive_err_2, assign_naive_err_4, assign_naive_err_5, assign_naive_shift]
    print_each = True #False # True if you want to print each assignment, False otherwise

    for a_elem, f_elem in zip(output_assignments, output_files):

        #print(a_elem)
        

        all_emissions, avg_errors, fixed_strategy = 0, 0, a_elem[0]['strategy']
        co2_per_slot = [0 for _ in all_slots]
        #err_per_slot = [0 for _ in all_slots]

        if print_each:
            with open(f_elem, "w") as output_csv:
                output_csv.write(                                        
                    f"request_id,strategy,time_slot,emission,error\n"
                )    


        for a in a_elem:        
            co2_per_slot[a['time_slot']] += a['emission']       # emissions = the sum of all emissions in the time slot
            all_emissions += a['emission']

            #err_per_slot[a['time_slot']] += a['error']          # errors = the average sum of all errors in the time slot
            avg_errors += a['error'] 

            if print_each:
                with open(f_elem, "a") as output_csv:
                    output_csv.write(f"{a['request_id']},{a['strategy']},{a['time_slot']},{a['emission']},{a['error']}\n")



        avg_errors = round((avg_errors/len(requests)),2) if len(requests) > 0 else 0
        time = 0

        if a_elem == assign_baseline:                       
            time = time_base
        elif a_elem == assign_random:                       
            time = time_random
        elif a_elem == assign_naive_carbon:   
            time = time_n_carbon
        elif a_elem == assign_naive_err_2:
            time = time_n_err2
        elif a_elem == assign_naive_err_4:
            time = time_n_err4
        elif a_elem == assign_naive_err_5:
            time = time_n_err5
        elif a_elem == assign_naive_shift:
            time = time_n_shift
        
        methodology = "w"
        if print_each:
            methodology = "a"            
        with open(f_elem, methodology) as output_csv:
            output_csv.write(
                f"Statistics:\n"
                f"computing_time:{time}\n"
                f"strategy:{fixed_strategy}\n"
                f"all_emissions:{all_emissions}\n"      # NB: total emissions
                f"slot_emissions:{co2_per_slot}\n"
                f"avg_errors:{avg_errors}\n"            # NB: average error per request
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

parser.add_argument('out_baseline', type=str, help='File where to write the output assignment.')
parser.add_argument('out_random', type=str, help='File where to write the output assignment.')
parser.add_argument('out_naive_carbon', type=str, help='File where to write the output assignment.')
parser.add_argument('out_naive_err_2', type=str, help='File where to write the output assignment.')
parser.add_argument('out_naive_err_4', type=str, help='File where to write the output assignment.')
parser.add_argument('out_naive_err_5', type=str, help='File where to write the output assignment.')
parser.add_argument('out_naive_shift', type=str, help='File where to write the output assignment.')

args = parser.parse_args()
main(
    args.input_requests, 
    args.input_strategies, 
    args.input_co2, 
    args.delta, 
    
    args.out_baseline,
    args.out_random,
    args.out_naive_carbon,
    args.out_naive_err_2,
    args.out_naive_err_4,
    args.out_naive_err_5,
    args.out_naive_shift
)
