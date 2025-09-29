"""
move to each test/ratio_ folder
    RUN with

python3.8 process_results.py 
times_01.csv times_02.csv times_03.csv times_04.csv times_05.csv 
greedy_times_01.csv greedy_times_02.csv greedy_times_03.csv greedy_times_04.csv greedy_times_05.csv 

compression_time.csv
"""

import matplotlib.pyplot as plt
from argparse import ArgumentParser
import sys

# Import times' data
def import_input_results(times_delta):
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

            comp_time       = float(v[1]).__round__(2) 
            #solv_status     = int(v[2])                    # never shown
            solv_time       = float(v[3]).__round__(2)   
            emissions       = float(v[4]).__round__(0)     
            errors          = float(v[6])

            #print(f"comp_time: {comp_time}, solv_time: {solv_time}, emissions: {emissions}, errors: {errors}")
            #return
        
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

# Import greedy times' data
priorities = ["BASE","MIXED","HIGH","MEDIUM","LOW","RANDOM"]
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

                #strategy        = str(v[2])
                comp_time       = float(v[1]).__round__(2) 
                emissions       = int(v[3])               
                errors          = float(v[5])                            

                #print(f"comp_time: {comp_time}, emissions: {emissions}, errors: {errors}")
                #return

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

# Import compression' data
def import_input_compression_time_(compression_beta):
    with open(compression_beta, "r") as file:           

        results = {"reqs": []}
        t = {"tot_reqs": 0}                         # Store the results of each request

        next(file)                                  # skip the first line with the headers
        for line in file:
            v = line.replace("\n","").split(",")

            delta = int(v[1])
            #slot = int(v[2])
            comp_time_avg   = float(v[2]).__round__(2)
            values = [{"delta":delta,"comp_time_avg":comp_time_avg}]

            if t["tot_reqs"] != int(v[0]):                  
                t = {                               # first insertion of the request
                    "tot_reqs"  : int(v[0]),                                        
                    "values"    : values
                }    
                results["reqs"].append(t)
            else:                                   # update the request
                t["values"].append({
                    "delta": delta,"comp_time_avg": comp_time_avg
                })

        return results

def main(
    times_delta_01,times_delta_02,times_delta_03,times_delta_04,times_delta_05,
    greedy_times_delta_01,greedy_times_delta_02,greedy_times_delta_03,greedy_times_delta_04,greedy_times_delta_05
    #,compression_beta
    ):

    res_1 = import_input_results(times_delta_01)                            # Read Carbonshift results
    res_2 = import_input_results(times_delta_02)
    res_3 = import_input_results(times_delta_03)
    res_4 = import_input_results(times_delta_04)
    res_5 = import_input_results(times_delta_05)
    #compression_beta = import_input_compression_time_(compression_beta)

    g_res_1 = import_input_greedy_results(greedy_times_delta_01)            # Read Greedy results
    g_res_2 = import_input_greedy_results(greedy_times_delta_02)
    g_res_3 = import_input_greedy_results(greedy_times_delta_03)
    g_res_4 = import_input_greedy_results(greedy_times_delta_04)
    g_res_5 = import_input_greedy_results(greedy_times_delta_05)

    num_reqs_values = ["1K","2K","4K","8K","16K","32K","64K","128K"] #[2**i for i in range(10, 18)]  # Define the x-axis values
    delta = [1,2,3,4,5]
    len_delta = len(delta)

    #opt_compression    = [[] for _ in range(len_delta)]                    

    opt_computation     = [[] for _ in range(len_delta)]                    # Initialize Carbonshift results
    opt_build           = [[] for _ in range(len_delta)]
    opt_solve           = [[] for _ in range(len_delta)]
    g_opt_comp_base     = [[] for _ in range(len_delta)]                    # Initialize Greedy results
    g_opt_comp_mixed    = [[] for _ in range(len_delta)]                    
    g_opt_comp_high     = [[] for _ in range(len_delta)]                    
    g_opt_comp_medium   = [[] for _ in range(len_delta)]
    g_opt_comp_low      = [[] for _ in range(len_delta)]
    g_opt_comp_random   = [[] for _ in range(len_delta)]
    
    opt_emissions       = [[] for _ in range(len_delta)]
    g_emissions_base    = [[] for _ in range(len_delta)] 
    g_emissions_mixed   = [[] for _ in range(len_delta)] 
    g_emissions_high    = [[] for _ in range(len_delta)] 
    g_emissions_medium  = [[] for _ in range(len_delta)] 
    g_emissions_low     = [[] for _ in range(len_delta)] 
    g_emissions_random  = [[] for _ in range(len_delta)] 

    opt_slot_emissions  = [[] for _ in range(len_delta)] 
    #g_slot_em_base     = [[] for _ in range(len_delta)] 
    #g_slot_em_mixed    = [[] for _ in range(len_delta)] 
    #g_slot_em_high     = [[] for _ in range(len_delta)] 
    #g_slot_em_medium   = [[] for _ in range(len_delta)] 
    #g_slot_em_low      = [[] for _ in range(len_delta)] 
    #g_slot_em_random   = [[] for _ in range(len_delta)] 

    opt_errors          = [[] for _ in range(len_delta)]
    g_errors_base       = [[] for _ in range(len_delta)]
    g_errors_mixed      = [[] for _ in range(len_delta)]
    g_errors_high       = [[] for _ in range(len_delta)]
    g_errors_medium     = [[] for _ in range(len_delta)]
    g_errors_low        = [[] for _ in range(len_delta)]
    g_errors_random     = [[] for _ in range(len_delta)]

    for index_delta, elem in enumerate([                                    # Store Carbonshift results
        res_1["reqs"],
        res_2["reqs"],
        res_3["reqs"],
        res_4["reqs"],
        res_5["reqs"]
        ],start=0):        

        for i in range(0, len_delta):
            for num_reqs in [2**i for i in range(10, 18)]:   
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_delta == i:                        

                        opt_computation[index_delta].append(r["comp_time_avg"])
                        opt_solve[index_delta].append(r["solve_time_avg"])
                        diff = round(r["comp_time_avg"] - r["solve_time_avg"], 2)                        
                        opt_build[index_delta].append(diff)

                        opt_emissions[index_delta].append(r["emission_avg"])
                        opt_slot_emissions[index_delta].append(r["slot_emissions"])

                        opt_errors[index_delta].append(r["error_avg"])


    for index_delta, elem in enumerate([                                    # Store Greedy results
        g_res_1,
        g_res_2,
        g_res_3,
        g_res_4,
        g_res_5
        ],start=0):        
        
        for priority in priorities:        
            for r in elem[priority]:

                if priority == 'BASE':  
                    g_opt_comp_base[index_delta].append(r["comp_time_avg"])                                                                    
                    g_emissions_base[index_delta].append(r["emission_avg"])
                    g_errors_base[index_delta].append(r["error_avg"])                    

                elif priority == 'MIXED':  
                    g_opt_comp_mixed[index_delta].append(r["comp_time_avg"])                                                                    
                    g_emissions_mixed[index_delta].append(r["emission_avg"])
                    g_errors_mixed[index_delta].append(r["error_avg"])                    

                elif priority == 'HIGH':  
                    g_opt_comp_high[index_delta].append(r["comp_time_avg"])                                                                    
                    g_emissions_high[index_delta].append(r["emission_avg"])
                    g_errors_high[index_delta].append(r["error_avg"])                    

                elif priority == "MEDIUM":
                    g_opt_comp_medium[index_delta].append(r["comp_time_avg"])                                                                    
                    g_emissions_medium[index_delta].append(r["emission_avg"])
                    g_errors_medium[index_delta].append(r["error_avg"])

                elif priority == "LOW":
                    g_opt_comp_low[index_delta].append(r["comp_time_avg"])                                                                    
                    g_emissions_low[index_delta].append(r["emission_avg"])
                    g_errors_low[index_delta].append(r["error_avg"])

                elif priority == "RANDOM":
                    g_opt_comp_random[index_delta].append(r["comp_time_avg"])                                                                    
                    g_emissions_random[index_delta].append(r["emission_avg"])
                    g_errors_random[index_delta].append(r["error_avg"])

    show_unified_times = False
    if show_unified_times:

        fig, ax = plt.subplots(figsize=(11.69,8.27))
        handles = [
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C0', markeredgecolor='C0', markersize=10, label='Solve Δ1'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='none', markeredgecolor='C0', markersize=10, linestyle='--', label='Build Δ1'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C1', markeredgecolor='C1', markersize=10, label='Solve Δ2'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='none', markeredgecolor='C1', markersize=10, linestyle='--', label='Build Δ2'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C2', markeredgecolor='C2', markersize=10, label='Solve Δ3'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='none', markeredgecolor='C2', markersize=10, linestyle='--', label='Build Δ3'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C3', markeredgecolor='C3', markersize=10, label='Solve Δ4'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='none', markeredgecolor='C3', markersize=10, linestyle='--', label='Build Δ4'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C4', markeredgecolor='C4', markersize=10, label='Solve Δ5'),
            plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='none', markeredgecolor='C4', markersize=10, linestyle='--', label='Build Δ5')
            ]
        fig.legend(handles=handles, loc='upper center', ncol=5, fontsize='large', bbox_to_anchor=(0.5, 0.95))


        """
        for index_delta in range(0, len_delta):                             # Store Compressed results
            for r in compression_beta["reqs"]:
                for num_reqs in [2**i for i in range(10, 18)]:   
                    for d in r["values"]:
                        if r["tot_reqs"] == num_reqs and (index_delta+1) == d["delta"]:                        
                            opt_compression[index_delta].append(d["comp_time_avg"])                

        for index in range(0,len(opt_computation)):
            l = delta[index]

            ax.plot([2**i for i in range(10, 18)], 
                opt_computation[index], label=f'Full {l}', 
                marker='*', linestyle='-.', color=f'C{index}')  

            ax.plot([2**i for i in range(10, 18)], 
                opt_build[index], label=f'Build {l}', 
                marker='*', linestyle='-', color=f'C{index}')  

            ax.plot([2**i for i in range(10, 18)], 
                opt_solve[index], label=f'Solve {l}', 
                marker='o', linestyle='--', color=f'C{index}')  

            ax.plot([2**i for i in range(10, 18)], 
                opt_compression[index], label=f'Compress {l}', 
                marker='o', linestyle=':', color=f'C{index}')

        fig.suptitle('Optimal Execution Times [s] per Window Size (Delta)')
        ax.set_xticks([2**i for i in range(10, 18)])                    # Show the values of num_reqs_values on the x-axis
        ax.set_xticklabels(num_reqs_values, rotation=45, ha='right')    # Rotate labels for better readability 
        """ 

        for index in range(0,len(opt_computation)):
            #l = delta[index]

            # Plot the solve time as a bar with a unique color for each delta
            ax.bar([x + index * 0.15 for x in range(len(num_reqs_values))], 
                opt_solve[index], width=0.15, 
                #label=f'Solve Time (Δ {l})', 
                alpha=0.7, color=f'C{index}')

            # Plot the build time as a stacked bar on top of the solve time with a unique color for each delta
            ax.bar([x + index * 0.15 for x in range(len(num_reqs_values))], 
                opt_build[index], bottom=opt_solve[index], 
                width=0.15, alpha=0.7, 
                edgecolor=f'C{index}',  # Use the same color for the edge
                facecolor='none')       # Set the fill color to none for a different appearance
            
        #fig.suptitle('Optimal Execution Times [s] per Window Size (Delta)')    
        ax.set_xlabel('Requests')
        ax.set_ylabel('Seconds')
        ax.set_ylim(0, 2.2)
        ax.set_xticks([x + 0.15 * (len_delta - 1) / 2 for x in range(len(num_reqs_values))])    # Center the ticks
        ax.set_xticklabels(num_reqs_values, ha='center')        
        plt.grid(True, linestyle='--', alpha=0.6)                                               # Add grid for better readability
        plt.savefig('unified_times.pdf', format='pdf')


    show_times = False
    if show_times:
        fig, ax = plt.subplots(3, 1, figsize=(12.89, 10.8))    
    else:
        fig, ax = plt.subplots(2, 1, figsize=(12.89, 10.8))    

    fig.subplots_adjust(hspace=0.3)  # Adjust space between subplots 
    handles = [
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C0', markeredgecolor='C0', markersize=10, label='Carbonshift'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C1', markeredgecolor='C1', markersize=10, label='Baseline'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C2', markeredgecolor='C2', markersize=10, label='Mixed Baseline'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C3', markeredgecolor='C3', markersize=10, label='Greedy High'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C4', markeredgecolor='C4', markersize=10, label='Greedy Medium'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C5', markeredgecolor='C5', markersize=10, label='Greedy Low'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C6', markeredgecolor='C6', markersize=10, label='Greedy Random')
    ]
    fig.legend(handles=handles[:3], loc='upper center', ncol=3, fontsize='large', bbox_to_anchor=(0.5, 0.97))
    fig.legend(handles=handles[-4:], loc='upper center', ncol=4, fontsize='large', bbox_to_anchor=(0.5, 0.94))
    #fig.suptitle('Exhaustive vs Greedy Execution Times and Emissions per Window Size (Delta)', fontsize=14)   

    d = 4
    amp = 0.10
    use_scale_log = True

    ax[0].set_title('Δ = '+str(delta[d]))    

    storage = [opt_emissions[d],g_emissions_base[d],g_emissions_mixed[d],g_emissions_high[d],g_emissions_medium[d],g_emissions_low[d],g_emissions_random[d]]    
    for index_s, elem_s in enumerate(storage, start=0):
        ax[0].bar([x+amp*index_s for x in range(len(num_reqs_values))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')      
    ax[0].tick_params(axis='both', labelsize=12)  
    #ax[0].set_xlabel('Requests', fontsize=14)   
    ax[0].set_xticks([x + amp*(len_delta-1)/2 for x in range(len(num_reqs_values))])  # Center the ticks
    ax[0].set_xticklabels(num_reqs_values, ha='center', fontsize=12)      
    ax[0].set_ylabel('gCO2-eq/kWh', fontsize=14)   
    if use_scale_log:
        ax[0].set_yscale('log')
    ax[0].grid(True, linestyle='--', alpha=0.6)                      

    storage = [opt_errors[d],g_errors_base[d],g_errors_mixed[d],g_errors_high[d],g_errors_medium[d],g_errors_low[d],g_errors_random[d]]
    for index_s, elem_s in enumerate(storage, start=0):
        ax[1].bar([x+amp*index_s for x in range(len(num_reqs_values))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')

    ax[1].tick_params(axis='both', labelsize=12)                        # Set fontsize for both x and y ticks on ax[0]
    ax[1].set_xlabel('Requests', fontsize=14)
    ax[1].set_xticks([x + amp*(len_delta-1)/2 for x in range(len(num_reqs_values))])  # Center the ticks
    ax[1].set_xticklabels(num_reqs_values, ha='center', fontsize=12)    # Rotate labels for better readability and increase font size    
    ax[1].set_ylabel('Errors', fontsize=14)   
    #if use_scale_log:
    #    ax[1].set_yscale('log')    
    ax[1].grid(True, linestyle='--', alpha=0.6)    

    if show_times:
        storage = [opt_computation[d],g_opt_comp_base[d],g_opt_comp_mixed[d],g_opt_comp_high[d],g_opt_comp_medium[d],g_opt_comp_low[d],g_opt_comp_random[d]]
        for index_s, elem_s in enumerate(storage, start=0):
            ax[2].bar([x+amp*index_s for x in range(len(num_reqs_values))], elem_s, width=amp, alpha=0.7, color=f'C{index_s}')  # Plot each bar with a different color
        ax[2].tick_params(axis='both', labelsize=12)                        # Set fontsize for both x and y ticks on ax[0]
        #ax[2].set_xlabel('Requests', fontsize=14)
        ax[2].set_xticks([x + amp*(len_delta-1)/2 for x in range(len(num_reqs_values))])  # Center the ticks
        ax[2].set_xticklabels(num_reqs_values, ha='center', fontsize=12)    # Rotate labels for better readability and increase font size    
        ax[2].set_ylabel('Seconds', fontsize=14)   
        #ax[2].set_ylim(0, 2.2)
        #if use_scale_log:
        #    ax[2].set_yscale('log')
        ax[2].grid(True, linestyle='--', alpha=0.6)  

    plt.savefig('optimal_vs_greedy.pdf', format='pdf')  


    f, ax = plt.subplots(figsize=(11.69,9.27))   
    f.suptitle('Optimal CO2 emissions for Window Size (Δ)')    
    handles = [
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C0', markeredgecolor='C0', markersize=10, label='Δ1'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C1', markeredgecolor='C1', markersize=10, label='Δ2'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C2', markeredgecolor='C2', markersize=10, label='Δ3'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C3', markeredgecolor='C3', markersize=10, label='Δ4'),
        plt.Line2D([0], [0], color='none', marker='s', markerfacecolor='C4', markeredgecolor='C4', markersize=10, label='Δ5')
        ]
    f.legend(handles=handles, loc='upper center', ncol=5, fontsize='large', bbox_to_anchor=(0.5, 0.95))
    ax.set_xlabel('Requests')
    ax.set_ylabel('gCO2-eq/kWh')

    """
    for index in range(0,len(opt_emissions)):        
        ax.plot([x + index * 0.15 for x in range(len(num_reqs_values))], opt_emissions[index], marker='o', linestyle='-', color=f'C{index}')
        #ax.plot([x + index * 0.15 for x in range(len(num_reqs_values))], g_emissions_high[index], marker='*', linestyle='-', color=f'C{index}')
        #ax.plot([x + index * 0.15 for x in range(len(num_reqs_values))], g_emissions_medium[index], marker='*', linestyle='-', color=f'C{index}')
        #ax.plot([x + index * 0.15 for x in range(len(num_reqs_values))], g_emissions_low[index], marker='*', linestyle='-', color=f'C{index}')
        #ax.plot([x + index * 0.15 for x in range(len(num_reqs_values))], g_emissions_random[index], marker='*', linestyle='-', color=f'C{index}')
                
        for i, value in enumerate(opt_emissions[index]):    # Add data labels to each point
            ax.text(i + index * 0.15, value, f'{value:.2f}', horizontalalignment='center', verticalalignment='bottom', fontsize=8)

    ax.set_xticks([x + 0.15 * (len_delta - 1) / 2 for x in range(len(num_reqs_values))])  # Center the ticks
    ax.set_xticklabels(num_reqs_values, ha='center')    

    plt.savefig('unified_emissions_averages.pdf', format='pdf')  
    """

    """
    f, ax = plt.subplots(5,1,figsize=(11.69,9.27))   
    #f.suptitle('CO2 emissions for Window Size (Delta)')    
    f.legend(handles=handles, loc='upper center', ncol=5, fontsize='large', bbox_to_anchor=(0.5, 0.95))

    # Flatten the nested list of errors for each delta    
    for id in range(0,len(opt_slot_emissions)):        
        for i in range(0, len(num_reqs_values)):
            diff =  len_delta - len(opt_slot_emissions[id][i])            
            while diff > 0:
                opt_slot_emissions[id][i].append(0)
                diff -= 1        
        flattened_errors = [item for sublist in opt_slot_emissions[id] for item in sublist if isinstance(item, (int, float))]
        ax[id].bar( range(len(flattened_errors)), flattened_errors, alpha=0.7, color=f'C{id}' )

        for idx, value in enumerate(flattened_errors):
            ax[id].text(
                idx, max(value - (value * 0.1), 0),  # Adjust y-coordinate to place the text slightly inside the bar
                f'{(idx % 5) +1}',                  # Display the modulo 5 value denoting the slot
                horizontalalignment='center', verticalalignment='bottom', fontsize=8, color='black') 
        ax[id].set_xlabel('Emissions on Slots for Requests') # affected slots by the number of requests
        #ax[id].set_ylim(0, 60000000) #for 1000, 300000 for others  
        ax[id].set_ylabel('gCO2-eq/kWh')
        ax[id].grid(True, linestyle='--', alpha=0.1)  # Add grid for better readability
        ax[id].set_yscale('log')
        ticks = [x + 0.15 * (len_delta - 1) / 2 for x in range(0, len(flattened_errors), 5)]
        labels = num_reqs_values[:len(ticks)]  # Ensure the number of labels matches the number of ticks
        ax[id].set_xticks(ticks) 
        ax[id].set_xticklabels(labels, ha='center')  # Center the ticks

    plt.savefig('unified_emissions.pdf', format='pdf')  
    """

    """
    f, ax = plt.subplots(5,1,figsize=(11.69,9.27))   
    #f.suptitle('CO2 emissions for Window Size (Delta) [GREEDY]')    
    f.legend(handles=handles, loc='upper center', ncol=5, fontsize='large', bbox_to_anchor=(0.5, 0.95))

    # Flatten the nested list of errors for each delta    
    for id in range(0,len(g_slot_em_high)):        
        for i in range(0, len(num_reqs_values)):
            diff =  len_delta - len(g_slot_em_high[id][i])            
            while diff > 0:
                g_slot_em_high[id][i].append(0)
                diff -= 1
        
        flattened_errors = [item for sublist in g_slot_em_high[id] for item in sublist if isinstance(item, (int, float))]
        ax[id].bar( range(len(flattened_errors)), flattened_errors, alpha=0.7, color=f'C{id}')

        for idx, value in enumerate(flattened_errors):
            ax[id].text( 
                idx, value + 10,  
                f'{(idx % 5) +1}',  
                horizontalalignment='center', verticalalignment='bottom', fontsize=8, color='black') 
        ax[id].set_xlabel('Emissions on Slots for Requests') 
        #ax[id].set_ylim(0, 3000000) #for 1000, 300000 for others  
        ax[id].set_ylabel('gCO2-eq/kWh')
        ax[id].grid(True, linestyle='--', alpha=0.1)  # Add grid for better readability
        ax[id].set_yscale('log')
        ticks = [x + 0.15 * (len_delta - 1) / 2 for x in range(0, len(flattened_errors), 5)]
        labels = num_reqs_values[:len(ticks)]  
        ax[id].set_xticks(ticks) 
        ax[id].set_xticklabels(labels, ha='center')  # Center the ticks
    plt.savefig('unified_greedy_high_emissions.pdf', format='pdf')  
    """

    
# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Process of assignments of requests to strategies and time slots")

parser.add_argument('times_delta_01', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_02', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_03', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_04', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_05', type=str, help='File with times results'' data and delta')

parser.add_argument('greedy_times_delta_01', type=str, help='File with greedy times results'' data and delta')
parser.add_argument('greedy_times_delta_02', type=str, help='File with greedy times results'' data and delta')
parser.add_argument('greedy_times_delta_03', type=str, help='File with greedy times results'' data and delta')
parser.add_argument('greedy_times_delta_04', type=str, help='File with greedy times results'' data and delta')
parser.add_argument('greedy_times_delta_05', type=str, help='File with greedy times results'' data and delta')

#parser.add_argument('compression_beta', type=str, help='File with compression times results'' data')

args = parser.parse_args()

main(
    args.times_delta_01,args.times_delta_02,args.times_delta_03,args.times_delta_04,args.times_delta_05,
    args.greedy_times_delta_01,args.greedy_times_delta_02,args.greedy_times_delta_03,args.greedy_times_delta_04,args.greedy_times_delta_05
    
    #,args.compression_beta
)