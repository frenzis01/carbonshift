# ------------------------
# move to test/ and
#    RUN with
# python3.8 process_results_comparison.py 
# ------------------------ 

import matplotlib.pyplot as plt
from argparse import ArgumentParser
import os


num_reqs_values = ["1K","2K","4K","8K","16K","32K","64K","128K"] #[2**i for i in range(10, 18)]  # Define the x-axis values
BETA = [100, 250, 500, 1000]    # 4 different values
len_beta = len(BETA)
id_gr_b = 3                     # fixed last β index 
DELTA = [1,2,3,4,5]
len_delta = len(DELTA)
#set_align = "horizontal"
set_align = "vertical"
num_shown_values=-4          # show only the last 4 values of the requests
#num_shown_values=2          # show only the first 2 values of the requests
show_timing = 0
show_error = 1
amp = 0.10

def import_input_results(times_delta):                          # Import times' data
    with open(times_delta, "r") as file:           
        results = {"reqs": []}
        t = {"tot_reqs": 0}                                 # Store the results of each request

        next(file)                                          # skip the first line with the headers
        for line in file:
            
            slot_emissions = line[line.find('['):line.find(']') + 1]#.replace(" ","")   Extract the substring between the first '[' and the first ']'
            #slot_errors = line[line.rfind('['):line.rfind(']') + 1]#.replace(" ","")  Extract the substring between the last '[' and the last ']'
            #v = line.replace("\n", "").replace(slot_emissions, "").replace(slot_errors, "").split(",") # Replace and split the line by comma            
            v = line.replace("\n", "").replace(slot_emissions, "").split(",") # Replace and split the line by comma            

            slot_emissions  = [int(x) for x in slot_emissions.strip('[]').split(',')]  # Convert elements to integers
            #slot_errors     = [int(x) for x in slot_errors.strip('[]').split(',')]

            #solv_status     = int(v[1])                    # never shown
            comp_time       = float(v[2]).__round__(2) 
            solv_time       = float(v[3]).__round__(2)   
            emissions       = float(v[4]).__round__(0)     
            errors          = float(v[6])
        
            if t["tot_reqs"] != int(v[0]):                  
                t = {                                   # first insertion of the request
                    "tot_reqs"      : int(v[0]),                    
                    "comp_time"     : [comp_time],                    
                    "solve_time"    : [solv_time],
                    "emission"      : [emissions],                    
                    "slot_emissions": slot_emissions,                    
                    "error"         : [errors],         # the same for all iterations
                    #"slot_errors"   : slot_errors       # the same for all the iterations
                }         
                results["reqs"].append(t)
            else:                                   # update the request
                t["comp_time"].append(comp_time)
                t["solve_time"].append(solv_time)
                t["emission"].append(emissions)
                t["error"].append(errors)

        for n in results["reqs"]:                   # unify the results at iteation level
            if len(n["comp_time"]) > 0:                    
                n["comp_time_avg"] = round(sum(n["comp_time"]) / len(n["comp_time"]), 4)
            else:
                n["comp_time_avg"] = 0

            if len(n["solve_time"]) > 0:                    
                n["solve_time_avg"] = round(sum(n["solve_time"]) / len(n["solve_time"]), 4)
            else:
                n["solve_time_avg"] = 0

            if len(n["emission"]) > 0:
                n["emission_avg"] = round(sum(n["emission"]) / len(n["emission"]), 2)
            else:
                n["emission_avg"] = 0

            if len(n["error"]) > 0:
                n["error_avg"] = round(sum(n["error"]) / len(n["error"]), 2)
            else:
                n["error_avg"] = 0

        return results

priorities = ["BASE","MIXED","HIGH","MEDIUM","LOW","RANDOM"]    # Import greedy times' data
def import_input_greedy_results(times_delta):

    with open(times_delta, "r") as file:           
        results = {
            "BASE"     : [],
            "MIXED"    : [],
            "HIGH"     : [],
            "MEDIUM"   : [],
            "LOW"      : [],
            "RANDOM"   : []
        }                         
        
        next(file)                                              # skip the first line with the headers
        while True:
            lines = [file.readline() for _ in range(6)]         # read 6 lines (strategies) at a time
            if not lines[0]:                                    # Check if the first line is empty (end of file)
                break            

            for index_l,line in enumerate(lines):  
            
                slot_emissions  = line[line.find('['):line.find(']') + 1]#.replace(" ","")   Extract the substring between the first '[' and the first ']'
                #slot_errors     = line[line.rfind('['):line.rfind(']') + 1]#.replace(" ","")  Extract the substring between the last '[' and the last ']'
                #v = line.replace("\n", "").replace(slot_emissions, "").replace(slot_errors, "").split(",") # Replace and split the line by comma            
                v = line.replace("\n", "").replace(slot_emissions, "").split(",") # Replace and split the line by comma            

                slot_emissions  = [int(x) for x in slot_emissions.strip('[]').split(',')]  # Convert elements to integers
                #slot_errors     = [int(x) for x in slot_errors.strip('[]').split(',')]

                comp_time       = float(v[1]).__round__(2) 
                #strategy        = str(v[2])
                emissions       = int(v[3])               
                errors          = float(v[5])                            

                priority = priorities[index_l]

                found = -1
                for l in results[priority]:
                    if l["tot_reqs"] == int(v[0]):              # Update  
                        found = 0
                        l["comp_time"].append(comp_time)
                        l["emission"].append(emissions)
                        break
                if found == -1:                                 # First insertion
                    tt = {
                        'tot_reqs'      : int(v[0]),
                        "comp_time"     : [comp_time],
                        "emission"      : [emissions],                    
                        #"slot_emissions": slot_emissions,                        
                        "error"         : [errors]              # the same for all iterations            
                        #"slot_errors"   : slot_errors          # the same for all the iterations
                    }
                    results[priority].append(tt)
       
        for priority in priorities:
            for n in results[priority]:                            # in [2**i for i in range(10, 18)]:
                if len(n["comp_time"]) > 0:                    
                    n["comp_time_avg"] = round(sum(n["comp_time"]) / len(n["comp_time"]), 4)
                else:
                    n["comp_time_avg"] = 0

                if len(n["emission"]) > 0:
                    n["emission_avg"] = round(sum(n["emission"]) / len(n["emission"]), 2)
                else:
                    n["emission_avg"] = 0

                if len(n["error"]) > 0:
                    n["error_avg"] = round(sum(n["error"]) / len(n["error"]), 2)
                else:
                    n["error_avg"] = 0
                        
        return results
    
def compute_comparison(res_1,res_2,res_5,res_10,time,lbls_d1,lbls_b1):
    for index_beta, elem in enumerate([                             
        res_1["reqs"], 
        res_2["reqs"], 
        res_5["reqs"], 
        res_10["reqs"]
        ], start=0):

        for i in range(0, len_beta):                                # Loop through the β values        
            for num_reqs in [2**i for i in range(10, 18)]:          # Loop through the requests values                   
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        if time == "build_solve_time_avg":
                            lbls_d1[index_beta].append(r["solve_time_avg"])
                            lbls_b1[index_beta].append(r["comp_time_avg"] - r["solve_time_avg"]) 
                        else:
                            lbls_d1[index_beta].append(r[time])
                        
    return lbls_d1,lbls_b1

def compute_single_delta(res_1,res_2,res_5,res_10,time,opt_computation):
    for index_beta, elem in enumerate([                             
        res_1["reqs"], 
        res_2["reqs"], 
        res_5["reqs"], 
        res_10["reqs"]
        ], start=0):

        for i in range(0, len_beta):                                # Loop through the β values        
            for num_reqs in [2**i for i in range(10, 18)]:          # Loop through the requests values                   
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        opt_computation[index_beta].append(r[time])  # Store the computational time

    return opt_computation

def compute_single_delta_greedy(gr_res_1,gr_res_2,gr_res_5,gr_res_10,averages,g_opt_comp_base,g_opt_comp_mixed,g_opt_comp_high,g_opt_comp_medium,g_opt_comp_low,g_opt_comp_random):
    for index_beta, elem in enumerate([
        gr_res_1, 
        gr_res_2, 
        gr_res_5, 
        gr_res_10
        ], start=0):

        for priority in priorities:                                # Loop through the priorities
            for r in elem[priority]:
                if priority == 'BASE':  
                    g_opt_comp_base[index_beta].append(r[averages])                                                                    

                elif priority == 'MIXED':  
                    g_opt_comp_mixed[index_beta].append(r[averages])                                                                    

                elif priority == 'HIGH':  
                    g_opt_comp_high[index_beta].append(r[averages])                                                                    
                    
                elif priority == "MEDIUM":
                    g_opt_comp_medium[index_beta].append(r[averages])                                                                    

                elif priority == "LOW":
                    g_opt_comp_low[index_beta].append(r[averages])                                                                    

                elif priority == "RANDOM":
                    g_opt_comp_random[index_beta].append(r[averages])                                                                    

    return g_opt_comp_base,g_opt_comp_mixed,g_opt_comp_high,g_opt_comp_medium,g_opt_comp_low,g_opt_comp_random


def show_single_delta_times(fx,id_row,id_column,opt_computation,g_opt_comp_base,g_opt_comp_mixed,g_opt_comp_high,g_opt_comp_medium,g_opt_comp_low,g_opt_comp_random):    
    d = id_gr_b # show the greedy of the last β, β=1000   
    v = num_shown_values     
    
    if set_align == "vertical":
        """    
        for b in range(len_beta):
            fx[id_row,id_column].bar([x+amp*b for x in range(len(num_reqs_values[v:]))], opt_computation[b][v:], width=amp, alpha=0.7, color=f'C{0}')            
            for idx, val in enumerate(opt_computation[b][v:]): # Add β value at the top of each bar
                fx[id_row,id_column].text(idx+amp*b, val, f'β={BETA[b]}',ha='center', va='top', fontsize=9, color='black', rotation=90)
        """
        storage = [opt_computation[d][v:],g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
        for index_s, elem_s in enumerate(storage, start=0):
            fx[id_row,id_column].bar([x+amp*index_s for x in range(len(num_reqs_values[v:]))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')  
        
        fx[id_row,id_column].tick_params(axis='both', labelsize=12)                        # Set fontsize for both x and y ticks on ax[0]
        #ax[0].set_xlabel('Requests', fontsize=14)
        fx[id_row,id_column].set_xticks([x+amp*(len_delta-1)/2 for x in range(len(num_reqs_values[v:]))])  # Center the ticks
        fx[id_row,id_column].set_xticklabels(num_reqs_values[v:], ha='center', fontsize=12)    # Rotate labels for better readability and increase font size    
        #fx[0,2].set_ylabel('Seconds', fontsize=14)   
        #fx[0,2].set_ylim(0, 2.2)
        fx[id_row,id_column].set_yscale('log')            
        #fx[0,2].grid(True, linestyle='--', alpha=0.6)                      

    else:
        """
        for b in range(len_beta):
            y_vals = opt_computation[b][v:]
            y_pos = [y+amp*b for y in range(len(y_vals))]
            if len(y_vals) == len(y_pos) and len(y_vals) > 0:
                fx[id_row,id_column].barh(y_pos, y_vals, height=amp, alpha=0.7, color=f'C{0}')
                for idx, val in enumerate(y_vals): 
                    fx[id_row,id_column].text(val, y_pos[idx], f'β={BETA[b]}', va='center', ha='right', fontsize=9, color='black')#, fontweight='bold')
        """
        storage = [opt_computation[d][v:],g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
        for index_s, elem_s in enumerate(storage, start=0):        
            fx[id_row,id_column].barh([y+amp*index_s for y in range(len(num_reqs_values[v:]))], elem_s, height=amp, alpha=0.7, color=f'C{index_s}')

        fx[id_row,id_column].tick_params(axis='both', labelsize=12)
        fx[id_row,id_column].set_yticks([y+amp*(len_delta-1)/2 for y in range(len(num_reqs_values[v:]))])
        fx[id_row,id_column].set_yticklabels(num_reqs_values[v:], va='center', fontsize=12)
        fx[id_row,id_column].set_xscale('log')

def show_single_delta_emissions(fx,id_row,id_column,opt_computation,g_opt_comp_base,g_opt_comp_mixed,g_opt_comp_high,g_opt_comp_medium,g_opt_comp_low,g_opt_comp_random):
    d = id_gr_b    
    v = num_shown_values     

    if set_align == "vertical":
        """
        for b in range(len_beta):
            fx[id_row,id_column].bar([x+amp*b for x in range(len(num_reqs_values[v:]))], opt_computation[b][v:], width=amp, alpha=0.7, color=f'C{0}')                 
            for idx, val in enumerate(opt_computation[b][v:]): 
                fx[id_row,id_column].text(idx + amp*b, val, f'β={BETA[b]}',ha='center', va='bottom', fontsize=9, color='black', rotation=90)
        """
        if show_error == 1:
            storage=[ opt_computation[d][v:],g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
            #print(opt_computation[d][-1:])


            for index_s, elem_s in enumerate(storage, start=0):
                fx[id_row,id_column].bar([x+amp*index_s for x in range(len(num_reqs_values[v:]))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')

            fx[id_row,id_column].tick_params(axis='both', labelsize=12)                        # Set fontsize for both x and y ticks on ax[0]
            #ax[0].set_xlabel('Requests', fontsize=14)
            fx[id_row,id_column].set_xticks([x+amp*(len_delta-1)/2 for x in range(len(num_reqs_values[v:]))])  # Center the ticks
            fx[id_row,id_column].set_xticklabels(num_reqs_values[v:], ha='center', fontsize=12)    # Rotate labels for better readability and increase font size    
            #fx[0,2].set_ylabel('Seconds', fontsize=14)   
            #fx[0,2].set_ylim(0, 2.2)
            fx[id_row,id_column].set_yscale('log')    
            #fx[0,2].grid(True, linestyle='--', alpha=0.6)                      
        else:   
            storage=[ opt_computation[d][v:],g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
            for index_s, elem_s in enumerate(storage, start=0):
                fx[id_row].bar([x+amp*index_s for x in range(len(num_reqs_values[v:]))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')

            fx[id_row].tick_params(axis='both', labelsize=12)                        # Set fontsize for both x and y ticks on ax[0]
            #ax[0].set_xlabel('Requests', fontsize=14)
            fx[id_row].set_xticks([x+amp*(len_delta-1)/2 for x in range(len(num_reqs_values[v:]))])  # Center the ticks
            fx[id_row].set_xticklabels(num_reqs_values[v:], ha='center', fontsize=12)    # Rotate labels for better readability and increase font size    
            #fx[0,2].set_ylabel('Seconds', fontsize=14)   
            #fx[0,2].set_ylim(0, 2.2)
            fx[id_row].set_yscale('log')    
            #fx[0,2].grid(True, linestyle='--', alpha=0.6)                      



    else:
        """"
        for b in range(len_beta):
            y_vals = opt_computation[b][v:]
            y_pos = [y+amp*b for y in range(len(y_vals))]
            if len(y_vals) == len(y_pos) and len(y_vals) > 0:
                fx[id_row,id_column].barh(y_pos, y_vals, height=amp, alpha=0.7, color=f'C{0}')
                for idx, val in enumerate(y_vals): 
                    fx[id_row,id_column].text(val, y_pos[idx], f'β={BETA[b]}', va='center', ha='left', fontsize=9, color='black')#, fontweight='bold')
        """
        storage = [opt_computation[d][v:], g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
        for index_s, elem_s in enumerate(storage, start=0):
            fx[id_row,id_column].barh([y+amp*index_s for y in range(len(num_reqs_values[v:]))], elem_s, height=amp, alpha=0.7, color=f'C{index_s}')

        fx[id_row,id_column].tick_params(axis='both', labelsize=12)
        fx[id_row,id_column].set_yticks([y+amp*(len_delta-1)/2 for y in range(len(num_reqs_values[v:]))])
        fx[id_row,id_column].set_yticklabels(num_reqs_values[v:], va='center', fontsize=12)
        fx[id_row,id_column].set_xscale('log')
        #ax[1].set_ylabel('gCO2-eq/kWh', fontsize=14)   
        #ax[1].set_yscale('log')
        #ax[1].grid(True, linestyle='--', alpha=0.6)                      


def show_single_delta_errors(fx,id_row,id_column,opt_computation,g_opt_comp_base,g_opt_comp_mixed,g_opt_comp_high,g_opt_comp_medium,g_opt_comp_low,g_opt_comp_random):
    d = id_gr_b      
    v = num_shown_values     

    if set_align == "vertical":
        """"
        for b in range(len_beta):
            fx[id_row,id_column].bar([x+amp*b for x in range(len(num_reqs_values[v:]))], opt_computation[b][v:], width=amp, alpha=0.7, color=f'C{0}')                 
            for idx, val in enumerate(opt_computation[b][v:]):
                fx[id_row,id_column].text(idx+amp*b, val, f'β={BETA[b]}',ha='center', va='bottom', fontsize=9, color='black', rotation=90)
        """
        storage = [opt_computation[d][v:], g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
        for index_s, elem_s in enumerate(storage, start=0):        
            fx[id_row,id_column].bar([x+amp*index_s for x in range(len(num_reqs_values[v:]))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')  

        fx[id_row,id_column].tick_params(axis='both', labelsize=12)                        # Set fontsize for both x and y ticks on ax[0]
        #ax[0].set_xlabel('Requests', fontsize=14)
        fx[id_row,id_column].set_xticks([x+amp*(len_delta-1)/2 for x in range(len(num_reqs_values[v:]))])  # Center the ticks
        fx[id_row,id_column].set_xticklabels(num_reqs_values[v:], ha='center', fontsize=12)    # Rotate labels for better readability and increase font size    
        #fx[0,2].set_ylabel('Seconds', fontsize=14)   
        #fx[0,2].set_ylim(0, 2.2)
        
        #fx[id_row,id_column].set_yscale('log')    # NOTE: errors are not log-scaled

        #fx[0,2].grid(True, linestyle='--', alpha=0.6)                      

    else:
        """
        for b in range(len_beta):
            y_vals = opt_computation[b][v:]
            y_pos = [y + 0.10 * b for y in range(len(y_vals))]
            if len(y_vals) == len(y_pos) and len(y_vals) > 0:
                fx[id_row,id_column].barh(y_pos, y_vals, height=0.10, alpha=0.7, color=f'C{0}')
                for idx, val in enumerate(y_vals): # Add β value at the end of each bar
                    fx[id_row,id_column].text(val, y_pos[idx], f'β={BETA[b]}', va='center', ha='left', fontsize=9, color='black')#, fontweight='bold')
        """
        storage = [opt_computation[d][v:], g_opt_comp_base[d][v:], g_opt_comp_mixed[d][v:], g_opt_comp_high[d][v:], g_opt_comp_medium[d][v:], g_opt_comp_low[d][v:], g_opt_comp_random[d][v:]]
        for index_s, elem_s in enumerate(storage, start=0):                
            fx[id_row,id_column].barh([y+amp*index_s for y in range(len(num_reqs_values[v:]))], elem_s, height=amp, alpha=0.7, color=f'C{index_s}')

        fx[id_row,id_column].tick_params(axis='both', labelsize=12)
        fx[id_row,id_column].set_yticks([y+amp*(len_delta-1)/2 for y in range(len(num_reqs_values[v:]))])
        fx[id_row,id_column].set_yticklabels(num_reqs_values[v:], va='center', fontsize=12)
        #fx[id_row,id_column].set_xscale('log')                         # NOTE: errors are not log-scaled

        #ax[1].set_ylabel('gCO2-eq/kWh', fontsize=14)   
        #ax[1].set_yscale('log')
        #ax[1].grid(True, linestyle='--', alpha=0.6)                      



def main():

    EMISSION            = {
        'carbonshift'   : [[] for _ in range(len_delta)],
        'base'          : [[] for _ in range(len_delta)],
        'mixed'         : [[] for _ in range(len_delta)],
        'high'          : [[] for _ in range(len_delta)],
        'medium'        : [[] for _ in range(len_delta)],
        'low'           : [[] for _ in range(len_delta)],
        'random'        : [[] for _ in range(len_delta)]
    }

    ERROR               = {
        'carbonshift'   : [[] for _ in range(len_delta)],
        'base'          : [[] for _ in range(len_delta)],
        'mixed'         : [[] for _ in range(len_delta)],
        'high'          : [[] for _ in range(len_delta)],
        'medium'        : [[] for _ in range(len_delta)],
        'low'           : [[] for _ in range(len_delta)],
        'random'        : [[] for _ in range(len_delta)]
    }
    
    COMPUTATION         = {
        'carbonshift'   : [[] for _ in range(len_delta)],
        'base'          : [[] for _ in range(len_delta)],
        'mixed'         : [[] for _ in range(len_delta)],
        'high'          : [[] for _ in range(len_delta)],
        'medium'        : [[] for _ in range(len_delta)],
        'low'           : [[] for _ in range(len_delta)],
        'random'        : [[] for _ in range(len_delta)]
    }


    for w in range(0,5):                        # {0,1,2,3,4} -> 5 sliding windows
        base_dir = os.path.dirname(os.path.abspath(__file__))
        TIMES_CARBONSHIFT   = [os.path.join(base_dir, f"window_{w}/beta_{beta}/times_0{delta}.csv") for delta in DELTA for beta in BETA]  # Create a list of times for Carbonshift
        TIMES_GREEDY        = [os.path.join(base_dir, f"window_{w}/beta_{beta}/greedy_times_0{delta}.csv") for delta in DELTA for beta in BETA]  # Create a list of times for Greedy

        RESULTS             = [import_input_results(t) for t in TIMES_CARBONSHIFT]
        GREEDY              = [import_input_greedy_results(g) for g in TIMES_GREEDY]
        
        TEMP               = {
            'carbonshift'   : [[] for _ in range(len_delta)],
            'base'          : [[] for _ in range(len_delta)],
            'mixed'         : [[] for _ in range(len_delta)],
            'high'          : [[] for _ in range(len_delta)],
            'medium'        : [[] for _ in range(len_delta)],
            'low'           : [[] for _ in range(len_delta)],
            'random'        : [[] for _ in range(len_delta)]
        }

        # COMPUTE AVERAGE [COMPUTATIONAL, SOLVE]TIME and EMISSIONS PER blocks of REQUESTS PER SLOT

        #time = "emission_avg"                  # NOTE: check if the outputs shown here make sense

        time = "build_solve_time_avg"    
        #time = "comp_time_avg"                 # NOTE: useless, as included in build_solve_time_avg
        #time = "solve_time_avg"                # NOTE: useless, as included in build_solve_time_avg
        
        optimal_vs_greedy = 1

        pdf = ""

        if time == "build_solve_time_avg":
            fig, ax = plt.subplots(5, 4, figsize=(12.89, 10.8))  # <Δ rows, β columns>
            fig.subplots_adjust(hspace=0.3)  # Adjust space between subplots
            #fig.suptitle('Execution Times split between Build and Solve Times [s]')
            
            #ax[0,0].legend()  # Add a legend to distinguish the plots
            handles = [
                plt.Line2D([0], [0], color=plt.cm.Blues(0.6), lw=4, label='Solve Time'),
                plt.Line2D([0], [0], color=plt.cm.Oranges(0.6), lw=4, label='Build Time')
            ]
            fig.legend(handles=handles, loc='upper center', ncol=2, fontsize='large', bbox_to_anchor=(0.5, 0.95))  # Add a legend for all subplots after the suptitle
            
            for i in range(len_beta):
                ax[0,i].set_title(f'β = '+str(BETA[i]))   # Set the title for each subplot using Greek letter

            for j in range(len_delta): 
                ax[j,0].set_ylabel('Δ = '+str(DELTA[j]))       # Label the y-axis


            for j in range(len_delta): 
                for i in range(len_beta):
                    ax[j,i].set_ylim(0, 20.0)          
                    #ax[1,i].set_ylim(0, 25.0)          

                #for j in range(len_delta): 
                    

            #for i in range(len_beta):
            #    ax[0,i].set_ylim(0, 11.0)  # Set y-axis limits  


            pdf = 'build_solve_time.pdf'    

        else:
            fig, ax = plt.subplots(5, 1, figsize=(12.89, 10.8))  # Dynamically adjust figure size
            handles = [
                plt.Line2D([0], [0], color=plt.cm.Blues(0.6), lw=4, label=f'β = '+str(BETA[0])),
                plt.Line2D([0], [0], color=plt.cm.Oranges(0.6), lw=4, label=f'β = '+str(BETA[1])),
                plt.Line2D([0], [0], color=plt.cm.Greens(0.6), lw=4, label=f'β = '+str(BETA[2])),
                plt.Line2D([0], [0], color=plt.cm.Reds(0.6), lw=4, label=f'β = '+str(BETA[3]))
            ]
            fig.legend(handles=handles, loc='upper center', ncol=4, fontsize='large', bbox_to_anchor=(0.5, 0.95))  

            if time == "comp_time_avg":            
                fig.suptitle('Execution Times [s]')           

                for j in range(len_delta):
                    ax[j].set_ylim(0, 2.2)                  # same as build_solve_time_avg graph

                pdf = 'computational_time.pdf'

            elif time == "solve_time_avg":              
                fig.suptitle('Solve Times [s]')             

                for j in range(len_delta):
                    ax[j].set_ylim(0, 2.2)                  # same as build_solve_time_avg graph, BUT could be even smaller

                pdf = 'solve_time.pdf'

            elif time == "emission_avg":              
                fig.suptitle('CO2 emissions [gCO2-eq/kWh]')  

                #ax[0].set_ylim(0, 5)  
                #ax[1].set_ylim(0, 5)
                #ax[2].set_ylim(0, 5)
                #ax[3].set_ylim(0, 5)
                #ax[4].set_ylim(0, 5)      
                
                pdf = 'CO2_emissions.pdf'

        if optimal_vs_greedy == 1:
            if show_timing == 1:
                fig, fx = plt.subplots(5, 3, figsize=(12.89, 10.8))  # <Δ rows, [CO2, errors, time] columns>
                fx[4,2].set_xlabel('Times [s]')  

            else:    
                if show_error == 1:
                    fig, fx = plt.subplots(5, 2, figsize=(12.89, 10.8))  # <Δ rows, [CO2, errors] columns>
                    fx[4,0].set_xlabel('CO2e [gCO2-eq/kWh]')   
                    fx[4,1].set_xlabel('Error')  
                    for j in range(len_delta): 
                        fx[j,0].set_ylabel('Δ = '+str(DELTA[j]))       # Label the y-axis
                else:
                    fig, fx = plt.subplots(5, 1, figsize=(12.89, 10.8))
                    fx[4].set_xlabel('Error')  
                    for j in range(len_delta): 
                        fx[j].set_ylabel('Δ = '+str(DELTA[j]))       # Label the y-axis

            fig.subplots_adjust(hspace=0.3)  # Adjust space between subplots
            #fig.suptitle('Comparison of Optimal vs Greedy', fontsize=14)   

            handles = [
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C0', markeredgecolor='C0', markersize=10, label='Carbonshift'),
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C1', markeredgecolor='C1', markersize=10, label='Baseline'),
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C2', markeredgecolor='C2', markersize=10, label='Mixed Baseline'),
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C3', markeredgecolor='C3', markersize=10, label='Greedy High'),
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C4', markeredgecolor='C4', markersize=10, label='Greedy Medium'),
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C5', markeredgecolor='C5', markersize=10, label='Greedy Low'),
                plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C6', markeredgecolor='C6', markersize=10, label='Greedy Random')
                # NOTE: TBD carbonstat
                #plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C5', markeredgecolor='C5', markersize=10, label='Carbonstat')
            ]
            fig.legend(handles=handles[:3], loc='upper center', ncol=3, fontsize='large', bbox_to_anchor=(0.5, 0.97))
            fig.legend(handles=handles[-4:], loc='upper center', ncol=4, fontsize='large', bbox_to_anchor=(0.5, 0.94))        
            
            if set_align == "horizontal":
                pdf = 'optimal_vs_greedy_H.pdf'    
            else:
                pdf = 'optimal_vs_greedy_V.pdf'

        avg_single = [[] for _ in range(len_beta)]  # Store [computational-time, solve-time, CO2e] averages for each β of each Δ        
        avg_all = [[] for _ in range(len_beta)]     # Store [build-time] averages for each β of each Δ        

        # Δ = 1
        avg_single_temp = [[] for _ in range(len_beta)]  # Store [computational-time, solve-time, CO2e] averages for each β of each Δ        
        avg_all_temp = [[] for _ in range(len_beta)]     # Store [build-time] averages for each β of each Δ        
        avg_single_temp,avg_all_temp = compute_comparison(RESULTS[0],RESULTS[1],RESULTS[2],RESULTS[3],time,avg_single_temp,avg_all_temp)
        avg_single = [a + b for a, b in zip(avg_single, avg_single_temp)]
        avg_all = [a + b for a, b in zip(avg_all, avg_all_temp)]


        for index in range(len(avg_single)):
            l = BETA[index%len_beta]
            if time == "build_solve_time_avg":
                ax[0,index].bar(num_reqs_values, avg_single[index], label='Solve Time '+f'β = '+str(l), alpha=0.7)
                ax[0,index].bar(num_reqs_values, avg_all[index], bottom=avg_single[index], label='Build Time '+f'β = '+str(l), alpha=0.7)
                ax[0,index].set_xticks(num_reqs_values)  # Show the values of num_reqs_values on the x-axis
                ax[0,index].set_xticklabels(num_reqs_values, rotation=45, ha='right')  # Rotate labels for better readability
            
            else:
                ax[0].plot(num_reqs_values, avg_all[index], marker='o', label=f'β = '+str(l))  
                ax[0].set_ylabel('Δ = 1')  # Label the y-axis            

        if optimal_vs_greedy == 1:
            TEMP['carbonshift']    = compute_single_delta(RESULTS[0],RESULTS[1],RESULTS[2],RESULTS[3],"emission_avg",TEMP['carbonshift'])            
            TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                GREEDY[0],GREEDY[1],GREEDY[2],GREEDY[3],"emission_avg",
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

            if w == 0:
                EMISSION['carbonshift'] = [a + b for a, b in zip(EMISSION['carbonshift'],   TEMP['carbonshift'])]
                EMISSION['base']        = [a + b for a, b in zip(EMISSION['base'],          TEMP['base'])]
                EMISSION['mixed']       = [a + b for a, b in zip(EMISSION['mixed'],         TEMP['mixed'])]
                EMISSION['high']        = [a + b for a, b in zip(EMISSION['high'],          TEMP['high'])]
                EMISSION['medium']      = [a + b for a, b in zip(EMISSION['medium'],        TEMP['medium'])]
                EMISSION['low']         = [a + b for a, b in zip(EMISSION['low'],           TEMP['low'])]
                EMISSION['random']      = [a + b for a, b in zip(EMISSION['random'],        TEMP['random'])]
            else:
                EMISSION['carbonshift'] = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['carbonshift'],   TEMP['carbonshift'])]
                EMISSION['base']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['base'],          TEMP['base'])]
                EMISSION['mixed']       = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['mixed'],         TEMP['mixed'])]
                EMISSION['high']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['high'],          TEMP['high'])]
                EMISSION['medium']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['medium'],        TEMP['medium'])]
                EMISSION['low']         = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['low'],           TEMP['low'])]
                EMISSION['random']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['random'],        TEMP['random'])]
                
            show_single_delta_emissions(fx,0,0,
                EMISSION['carbonshift'],EMISSION['base'],EMISSION['mixed'],EMISSION['high'],EMISSION['medium'],EMISSION['low'],EMISSION['random'])
            #print(EMISSION['carbonshift'][0][0])

            if show_error == 1:
                TEMP['carbonshift']    = compute_single_delta(RESULTS[0],RESULTS[1],RESULTS[2],RESULTS[3],"error_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[0],GREEDY[1],GREEDY[2],GREEDY[3],"error_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                if w == 0:
                    ERROR['carbonshift']    = [a + b for a, b in zip(ERROR['carbonshift'],  TEMP['carbonshift'])]
                    ERROR['base']           = [a + b for a, b in zip(ERROR['base'],         TEMP['base'])]
                    ERROR['mixed']          = [a + b for a, b in zip(ERROR['mixed'],        TEMP['mixed'])]
                    ERROR['high']           = [a + b for a, b in zip(ERROR['high'],         TEMP['high'])]
                    ERROR['medium']         = [a + b for a, b in zip(ERROR['medium'],       TEMP['medium'])]
                    ERROR['low']            = [a + b for a, b in zip(ERROR['low'],          TEMP['low'])]
                    ERROR['random']         = [a + b for a, b in zip(ERROR['random'],       TEMP['random'])]
                else:
                    ERROR['carbonshift']    = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['carbonshift'],  TEMP['carbonshift'])]
                    ERROR['base']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['base'],         TEMP['base'])]
                    ERROR['mixed']          = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['mixed'],        TEMP['mixed'])]
                    ERROR['high']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['high'],         TEMP['high'])]
                    ERROR['medium']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['medium'],       TEMP['medium'])]
                    ERROR['low']            = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['low'],          TEMP['low'])]
                    ERROR['random']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['random'],       TEMP['random'])]

                show_single_delta_errors(fx,0,1,
                    ERROR['carbonshift'],ERROR['base'],ERROR['mixed'],ERROR['high'],ERROR['medium'],ERROR['low'],ERROR['random'])

            if show_timing == 1:
                TEMP['carbonshift']        = compute_single_delta(RESULTS[0],RESULTS[1],RESULTS[2],RESULTS[3],"comp_time_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[0],GREEDY[1],GREEDY[2],GREEDY[3],"comp_time_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                if w == 0:            
                    COMPUTATION['carbonshift']  = [a + b for a, b in zip(COMPUTATION['carbonshift'],    TEMP['carbonshift'])]           
                    COMPUTATION['base']         = [a + b for a, b in zip(COMPUTATION['base'],           TEMP['base'])]
                    COMPUTATION['mixed']        = [a + b for a, b in zip(COMPUTATION['mixed'],          TEMP['mixed'])]
                    COMPUTATION['high']         = [a + b for a, b in zip(COMPUTATION['high'],           TEMP['high'])]
                    COMPUTATION['medium']       = [a + b for a, b in zip(COMPUTATION['medium'],         TEMP['medium'])]
                    COMPUTATION['low']          = [a + b for a, b in zip(COMPUTATION['low'],            TEMP['low'])]
                    COMPUTATION['random']       = [a + b for a, b in zip(COMPUTATION['random'],         TEMP['random'])]
                else:
                    COMPUTATION['carbonshift']  = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['carbonshift'],TEMP['carbonshift'])]           
                    COMPUTATION['base']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['base'],       TEMP['base'])]
                    COMPUTATION['mixed']        = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['mixed'],      TEMP['mixed'])]
                    COMPUTATION['high']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['high'],       TEMP['high'])]
                    COMPUTATION['medium']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['medium'],     TEMP['medium'])]
                    COMPUTATION['low']          = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['low'],        TEMP['low'])]
                    COMPUTATION['random']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['random'],     TEMP['random'])]

                show_single_delta_times(fx,0,2,
                    COMPUTATION['carbonshift'],COMPUTATION['base'],COMPUTATION['mixed'],COMPUTATION['high'],COMPUTATION['medium'],COMPUTATION['low'],COMPUTATION['random'])

        # Δ = 2
        avg_single_temp = [[] for _ in range(len_beta)]
        avg_all_temp = [[] for _ in range(len_beta)]
        avg_single_temp,avg_all_temp = compute_comparison(RESULTS[4],RESULTS[5],RESULTS[6],RESULTS[7],time,avg_single_temp,avg_all_temp)
        avg_single = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_single, avg_single_temp) ]
        avg_all = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_all, avg_all_temp) ]

        for index in range(len(avg_single)):
            l = BETA[index%len_beta]
            if time == "build_solve_time_avg":
                ax[1,index].bar(num_reqs_values, avg_single[index], label='Solve Time '+f'β = '+str(l), alpha=0.7)
                ax[1,index].bar(num_reqs_values, avg_all[index], bottom=avg_single[index], label='Build Time '+f'β = '+str(l), alpha=0.7) # building time
                ax[1,index].set_xticks(num_reqs_values)  # Show the values of num_reqs_values on the x-axis
                ax[1,index].set_xticklabels(num_reqs_values, rotation=45, ha='right')  # Rotate labels for better readability

            else:
                ax[1].plot(num_reqs_values, avg_single[index], marker='o', label=f'β = '+str(l))  
                ax[1].set_ylabel('Δ = 2')  

        if optimal_vs_greedy == 1:
            TEMP['carbonshift']    = compute_single_delta(RESULTS[4],RESULTS[5],RESULTS[6],RESULTS[7],"emission_avg",TEMP['carbonshift'])
            TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                GREEDY[4],GREEDY[5],GREEDY[6],GREEDY[7],"emission_avg",
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

            EMISSION['carbonshift'] = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['carbonshift'],   TEMP['carbonshift'])]
            EMISSION['base']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['base'],          TEMP['base'])]
            EMISSION['mixed']       = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['mixed'],         TEMP['mixed'])]
            EMISSION['high']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['high'],          TEMP['high'])]
            EMISSION['medium']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['medium'],        TEMP['medium'])]
            EMISSION['low']         = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['low'],           TEMP['low'])]
            EMISSION['random']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['random'],        TEMP['random'])]
            
            show_single_delta_emissions(fx,1,0,
                EMISSION['carbonshift'],EMISSION['base'],EMISSION['mixed'],EMISSION['high'],EMISSION['medium'],EMISSION['low'],EMISSION['random'])

            if show_error == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[4],RESULTS[5],RESULTS[6],RESULTS[7],"error_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[4],GREEDY[5],GREEDY[6],GREEDY[7],"error_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])
                
                ERROR['carbonshift']    = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['carbonshift'],  TEMP['carbonshift'])]
                ERROR['base']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['base'],         TEMP['base'])]
                ERROR['mixed']          = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['mixed'],        TEMP['mixed'])]
                ERROR['high']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['high'],         TEMP['high'])]
                ERROR['medium']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['medium'],       TEMP['medium'])]
                ERROR['low']            = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['low'],          TEMP['low'])]
                ERROR['random']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['random'],       TEMP['random'])]

                show_single_delta_errors(fx,1,1,
                    ERROR['carbonshift'],ERROR['base'],ERROR['mixed'],ERROR['high'],ERROR['medium'],ERROR['low'],ERROR['random'])

            if show_timing == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[4],RESULTS[5],RESULTS[6],RESULTS[7],"comp_time_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[4],GREEDY[5],GREEDY[6],GREEDY[7],"comp_time_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                COMPUTATION['carbonshift']  = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['carbonshift'],TEMP['carbonshift'])]           
                COMPUTATION['base']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['base'],       TEMP['base'])]
                COMPUTATION['mixed']        = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['mixed'],      TEMP['mixed'])]
                COMPUTATION['high']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['high'],       TEMP['high'])]
                COMPUTATION['medium']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['medium'],     TEMP['medium'])]
                COMPUTATION['low']          = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['low'],        TEMP['low'])]
                COMPUTATION['random']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['random'],     TEMP['random'])]

                show_single_delta_times(fx,1,2,
                    COMPUTATION['carbonshift'],COMPUTATION['base'],COMPUTATION['mixed'],COMPUTATION['high'],COMPUTATION['medium'],COMPUTATION['low'],COMPUTATION['random'])

        # Δ = 3
        avg_single_temp = [[] for _ in range(len_beta)]
        avg_all_temp = [[] for _ in range(len_beta)]
        avg_single_temp,avg_all_temp = compute_comparison(RESULTS[8],RESULTS[9],RESULTS[10],RESULTS[11],time,avg_single_temp,avg_all_temp)
        avg_single = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_single, avg_single_temp) ]
        avg_all = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_all, avg_all_temp) ]

        for index in range(len(avg_single)):
            l = BETA[index%len_beta]
            if time == "build_solve_time_avg":
                ax[2,index].bar(num_reqs_values, avg_single[index], label='Solve Time '+f'β = '+str(l), alpha=0.7)
                ax[2,index].bar(num_reqs_values, avg_all[index], bottom=avg_single[index], label='Build Time '+f'β = '+str(l), alpha=0.7) # building time
                ax[2,index].set_xticks(num_reqs_values)  # Show the values of num_reqs_values on the x-axis
                ax[2,index].set_xticklabels(num_reqs_values, rotation=45, ha='right')  # Rotate labels for better readability

            else:
                ax[2].plot(num_reqs_values, avg_single[index], marker='o', label=f'β = '+str(l))  
                ax[2].set_ylabel('Δ = 3')  

        if optimal_vs_greedy == 1:
            TEMP['carbonshift'] = compute_single_delta(RESULTS[8],RESULTS[9],RESULTS[10],RESULTS[11],"emission_avg",TEMP['carbonshift'])            
            TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                GREEDY[8],GREEDY[9],GREEDY[10],GREEDY[11],"emission_avg",
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

            EMISSION['carbonshift'] = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['carbonshift'],   TEMP['carbonshift'])]
            EMISSION['base']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['base'],          TEMP['base'])]
            EMISSION['mixed']       = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['mixed'],         TEMP['mixed'])]
            EMISSION['high']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['high'],          TEMP['high'])]
            EMISSION['medium']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['medium'],        TEMP['medium'])]
            EMISSION['low']         = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['low'],           TEMP['low'])]
            EMISSION['random']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['random'],        TEMP['random'])]
            
            show_single_delta_emissions(fx,2,0,
                EMISSION['carbonshift'],EMISSION['base'],EMISSION['mixed'],EMISSION['high'],EMISSION['medium'],EMISSION['low'],EMISSION['random'])

            if show_error == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[8],RESULTS[9],RESULTS[10],RESULTS[11],"error_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[8],GREEDY[9],GREEDY[10],GREEDY[11],"error_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                ERROR['carbonshift']    = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['carbonshift'],  TEMP['carbonshift'])]            
                ERROR['base']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['base'],         TEMP['base'])]
                ERROR['mixed']          = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['mixed'],        TEMP['mixed'])]
                ERROR['high']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['high'],         TEMP['high'])]
                ERROR['medium']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['medium'],       TEMP['medium'])]
                ERROR['low']            = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['low'],          TEMP['low'])]
                ERROR['random']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['random'],       TEMP['random'])]
                
                show_single_delta_errors(fx,2,1,
                    ERROR['carbonshift'],ERROR['base'],ERROR['mixed'],ERROR['high'],ERROR['medium'],ERROR['low'],ERROR['random'])

            if show_timing == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[8],RESULTS[9],RESULTS[10],RESULTS[11],"comp_time_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[8],GREEDY[9],GREEDY[10],GREEDY[11],"comp_time_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                COMPUTATION['carbonshift']  = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['carbonshift'],TEMP['carbonshift'])]                           
                COMPUTATION['base']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['base'],       TEMP['base'])]
                COMPUTATION['mixed']        = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['mixed'],      TEMP['mixed'])]
                COMPUTATION['high']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['high'],       TEMP['high'])]
                COMPUTATION['medium']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['medium'],     TEMP['medium'])]
                COMPUTATION['low']          = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['low'],        TEMP['low'])]
                COMPUTATION['random']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['random'],     TEMP['random'])]

                show_single_delta_times(fx,2,2,
                    COMPUTATION['carbonshift'],COMPUTATION['base'],COMPUTATION['mixed'],COMPUTATION['high'],COMPUTATION['medium'],COMPUTATION['low'],COMPUTATION['random'])

        # Δ = 4
        avg_single_temp = [[] for _ in range(len_beta)]
        avg_all_temp = [[] for _ in range(len_beta)]
        avg_single_temp,avg_all_temp = compute_comparison(RESULTS[12],RESULTS[13],RESULTS[14],RESULTS[15],time,avg_single_temp,avg_all_temp)
        avg_single = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_single, avg_single_temp) ]
        avg_all = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_all, avg_all_temp) ]

        for index in range(len(avg_single)):
            l = BETA[index%len_beta]
            if time == "build_solve_time_avg":
                ax[3,index].bar(num_reqs_values, avg_single[index], label='Solve Time '+f'β = '+str(l), alpha=0.7)
                ax[3,index].bar(num_reqs_values, avg_all[index], bottom=avg_single[index], label='Build Time '+f'β = '+str(l), alpha=0.7) # building time
                ax[3,index].set_xticks(num_reqs_values)  # Show the values of num_reqs_values on the x-axis
                ax[3,index].set_xticklabels(num_reqs_values, rotation=45, ha='right')  # Rotate labels for better readability

            else:
                ax[3].plot(num_reqs_values, avg_single[index], marker='o', label=f'β = '+str(l))  
                ax[3].set_ylabel('Δ = 4')  

        if optimal_vs_greedy == 1:            
            TEMP['carbonshift'] = compute_single_delta(RESULTS[12],RESULTS[13],RESULTS[14],RESULTS[15],"emission_avg",TEMP['carbonshift'])
            TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                GREEDY[12],GREEDY[13],GREEDY[14],GREEDY[15],"emission_avg",
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

            EMISSION['carbonshift'] = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['carbonshift'],   TEMP['carbonshift'])]
            EMISSION['base']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['base'],          TEMP['base'])]
            EMISSION['mixed']       = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['mixed'],         TEMP['mixed'])]
            EMISSION['high']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['high'],          TEMP['high'])]
            EMISSION['medium']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['medium'],        TEMP['medium'])]
            EMISSION['low']         = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['low'],           TEMP['low'])]
            EMISSION['random']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['random'],        TEMP['random'])]
        
            show_single_delta_emissions(fx,3,0,
                EMISSION['carbonshift'],EMISSION['base'],EMISSION['mixed'],EMISSION['high'],EMISSION['medium'],EMISSION['low'],EMISSION['random'])

            if show_error == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[12],RESULTS[13],RESULTS[14],RESULTS[15],"error_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[12],GREEDY[13],GREEDY[14],GREEDY[15],"error_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                ERROR['carbonshift']    = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['carbonshift'],  TEMP['carbonshift'])]
                ERROR['base']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['base'],         TEMP['base'])]
                ERROR['mixed']          = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['mixed'],        TEMP['mixed'])]
                ERROR['high']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['high'],         TEMP['high'])]
                ERROR['medium']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['medium'],       TEMP['medium'])]
                ERROR['low']            = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['low'],          TEMP['low'])]
                ERROR['random']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['random'],       TEMP['random'])]

                show_single_delta_errors(fx,3,1,
                    ERROR['carbonshift'],ERROR['base'],ERROR['mixed'],ERROR['high'],ERROR['medium'],ERROR['low'],ERROR['random'])

            if show_timing == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[12],RESULTS[13],RESULTS[14],RESULTS[15],"comp_time_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[12],GREEDY[13],GREEDY[14],GREEDY[15],"comp_time_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                COMPUTATION['carbonshift']  = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['carbonshift'],TEMP['carbonshift'])]           
                COMPUTATION['base']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['base'],       TEMP['base'])]
                COMPUTATION['mixed']        = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['mixed'],      TEMP['mixed'])]
                COMPUTATION['high']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['high'],       TEMP['high'])]
                COMPUTATION['medium']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['medium'],     TEMP['medium'])]
                COMPUTATION['low']          = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['low'],        TEMP['low'])]
                COMPUTATION['random']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['random'],     TEMP['random'])]

                show_single_delta_times(fx,3,2,
                    COMPUTATION['carbonshift'],COMPUTATION['base'],COMPUTATION['mixed'],COMPUTATION['high'],COMPUTATION['medium'],COMPUTATION['low'],COMPUTATION['random'])

        # Δ = 5
        avg_single_temp = [[] for _ in range(len_beta)]
        avg_all_temp = [[] for _ in range(len_beta)]
        avg_single_temp,avg_all_temp = compute_comparison(RESULTS[16],RESULTS[17],RESULTS[18],RESULTS[19],time,avg_single_temp,avg_all_temp)
        avg_single = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_single, avg_single_temp) ]
        avg_all = [ [x + y for x, y in zip(a, b)] for a, b in zip(avg_all, avg_all_temp) ]

        for index in range(len(avg_single)):
            l = BETA[index%len_beta]
            if time == "build_solve_time_avg":
                ax[4,index].bar(num_reqs_values, avg_single[index], label='Solve Time '+f'β = '+str(l), alpha=0.7)
                ax[4,index].bar(num_reqs_values, avg_all[index], bottom=avg_single[index], label='Build Time '+f'β = '+str(l), alpha=0.7) # building time
                ax[4,index].set_xticks(num_reqs_values)  # Show the values of num_reqs_values on the x-axis
                ax[4,index].set_xticklabels(num_reqs_values, rotation=45, ha='right')  # Rotate labels for better readability
                ax[4,index].set_xlabel('Requests per Slot')  # Label the x-axis

            else:
                ax[4].plot(num_reqs_values, avg_single[index], marker='o', label=f'β = '+str(l))  
                ax[4].set_ylabel('Δ = 5') 
                ax[4].set_xlabel('Requests per Slot')  # Label the x-axis

        if optimal_vs_greedy == 1:
            TEMP['carbonshift'] = compute_single_delta(RESULTS[16],RESULTS[17],RESULTS[18],RESULTS[19],"emission_avg",TEMP['carbonshift'])
            TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                GREEDY[16],GREEDY[17],GREEDY[18],GREEDY[19],"emission_avg",
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

            EMISSION['carbonshift'] = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['carbonshift'],   TEMP['carbonshift'])]
            EMISSION['base']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['base'],          TEMP['base'])]
            EMISSION['mixed']       = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['mixed'],         TEMP['mixed'])]
            EMISSION['high']        = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['high'],          TEMP['high'])]
            EMISSION['medium']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['medium'],        TEMP['medium'])]
            EMISSION['low']         = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['low'],           TEMP['low'])]
            EMISSION['random']      = [[x + y for x, y in zip(a, b)] for a, b in zip(EMISSION['random'],        TEMP['random'])]
            
            show_single_delta_emissions(fx,4,0,
                EMISSION['carbonshift'],EMISSION['base'],EMISSION['mixed'],EMISSION['high'],EMISSION['medium'],EMISSION['low'],EMISSION['random'])

            if show_error == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[16],RESULTS[17],RESULTS[18],RESULTS[19],"error_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[16],GREEDY[17],GREEDY[18],GREEDY[19],"error_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                ERROR['carbonshift']    = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['carbonshift'],  TEMP['carbonshift'])]
                ERROR['base']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['base'],         TEMP['base'])]
                ERROR['mixed']          = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['mixed'],        TEMP['mixed'])]
                ERROR['high']           = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['high'],         TEMP['high'])]
                ERROR['medium']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['medium'],       TEMP['medium'])]
                ERROR['low']            = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['low'],          TEMP['low'])]
                ERROR['random']         = [[(x + y)/2 for x, y in zip(a, b)] for a, b in zip(ERROR['random'],       TEMP['random'])]

                show_single_delta_errors(fx,4,1,
                    ERROR['carbonshift'],ERROR['base'],ERROR['mixed'],ERROR['high'],ERROR['medium'],ERROR['low'],ERROR['random'])

            if show_timing == 1:
                TEMP['carbonshift'] = compute_single_delta(RESULTS[16],RESULTS[17],RESULTS[18],RESULTS[19],"comp_time_avg",TEMP['carbonshift'])
                TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'] = compute_single_delta_greedy(
                    GREEDY[16],GREEDY[17],GREEDY[18],GREEDY[19],"comp_time_avg",
                    TEMP['base'],TEMP['mixed'],TEMP['high'],TEMP['medium'],TEMP['low'],TEMP['random'])

                COMPUTATION['carbonshift']  = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['carbonshift'],    TEMP['carbonshift'])]           
                COMPUTATION['base']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['base'],           TEMP['base'])]
                COMPUTATION['mixed']        = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['mixed'],          TEMP['mixed'])]
                COMPUTATION['high']         = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['high'],           TEMP['high'])]
                COMPUTATION['medium']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['medium'],         TEMP['medium'])]
                COMPUTATION['low']          = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['low'],            TEMP['low'])]
                COMPUTATION['random']       = [[x + y for x, y in zip(a, b)] for a, b in zip(COMPUTATION['random'],         TEMP['random'])]

                show_single_delta_times(fx,4,2,
                    COMPUTATION['carbonshift'],COMPUTATION['base'],COMPUTATION['mixed'],COMPUTATION['high'],COMPUTATION['medium'],COMPUTATION['low'],COMPUTATION['random'])

        plt.savefig(pdf, format='pdf')


# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Process of assignments of requests to strategies and time slots")
args = parser.parse_args()

main()
