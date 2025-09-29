
# ------------------------
# move to each test_random_case_beta/ratio_ folder
#    RUN with
# python3.8 process_results.py times_01.csv times_02.csv times_03.csv times_04.csv times_05.csv compression_time.csv
# ------------------------ 


import matplotlib.pyplot as plt
from argparse import ArgumentParser

# Import times' data
def import_input_results(times_delta):
    with open(times_delta, "r") as file:           

        results = {"reqs": []}
        t = {"tot_reqs": 0}                         # dictionary to store the results of each request

        next(file)                                  # skip the first line with the headers
        for line in file:
            v = line.replace("\n","").split(",")

            comp_time   = float(v[1]) 
            r_comp_time = round(comp_time,2)

            s_status      = str(v[2]).split(":")   
            solv_status = int(s_status[1])

            s_time      = str(v[3]).split(":")   
            if len(s_time) > 1:
                solv_time   = float(s_time[1])
                r_solv_time = round(solv_time,2)
            else:
                solv_time   = 0.0
                r_solv_time = 0.0

            emission    = str(v[4]).split(":")   
            if len(emission) > 1:
                emission_   = float(emission[1])
                r_emission  = round(emission_,2)
            else:
                emission_   = 0.0
                r_emission  = 0.0

            if t["tot_reqs"] != int(v[0]):                  
                t = {                               # first insertion of the request
                    "tot_reqs"      : int(v[0]),                    
                    "comp_time_avg" : r_comp_time,
                    "comp_time"     : [r_comp_time],
                    "solve_status"  : solv_status,
                    "solve_time_avg": r_solv_time,
                    "solve_time"    : [r_solv_time],
                    "emission_avg"  : r_emission,
                    "emission"      : [r_emission]
                }         
                results["reqs"].append(t)
            else:                                   # update the request
                t["comp_time_avg"] += r_comp_time
                t["comp_time"].append(r_comp_time)
                t["solve_time_avg"] += r_solv_time
                t["solve_time"].append(r_solv_time)
                t["emission_avg"] += r_emission
                t["emission"].append(r_emission)

        for r in results["reqs"]:   
            r["comp_time_avg"] = round(r["comp_time_avg"]/len(r["comp_time"]),2)  
            r["solve_time_avg"] = round(r["solve_time_avg"]/len(r["solve_time"]),2)          
            r["emission_avg"] = round(r["emission_avg"]/len(r["emission"]),2)          

        return results

# Import compression' data
def import_input_compression_time_(compression_beta):
    with open(compression_beta, "r") as file:           

        results = {"reqs": []}
        t = {"tot_reqs": 0}                         # dictionary to store the results of each request

        next(file)                                  # skip the first line with the headers
        for line in file:
            v = line.replace("\n","").split(",")

            delta = int(v[1])
            slot = int(v[2])
            comp_time_avg   = float(v[3])
            r_comp_time_avg = round(comp_time_avg,2)

            if t["tot_reqs"] != int(v[0]):                  
                t = {                               # first insertion of the request
                    "tot_reqs"      : int(v[0]),                                        
                    "delta": {
                        delta: [
                            {
                                "slot"          : slot,
                                "comp_time_avg" : r_comp_time_avg
                            }
                        ]
                    }
                }    
                results["reqs"].append(t)
            else:                                   # update the request
                if delta in t["delta"]: 
                    t["delta"][delta].append({
                        "slot": slot,
                        "comp_time_avg": r_comp_time_avg
                    })
                else:
                    t["delta"][delta] = [{
                        "slot": slot,
                        "comp_time_avg": r_comp_time_avg
                    }]



        return results



def main(times_delta_01,times_delta_02,times_delta_03,times_delta_04,times_delta_05,compression_beta):

    res_1 = import_input_results(times_delta_01)
    res_2 = import_input_results(times_delta_02)
    res_3 = import_input_results(times_delta_03)
    res_4 = import_input_results(times_delta_04)
    res_5 = import_input_results(times_delta_05)

    compression_beta = import_input_compression_time_(compression_beta)
    #print(compression_beta)
    fig, ax = plt.subplots(figsize=(11.69,9.27))
    

    for index_delta, elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):        
        optimal_lbl,optimal_comp = [],[]     
        optimal_lbl_d,optimal_comp_d = [],[]     

        for num_reqs in [2**i for i in range(10, 18)]:   
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                    optimal_lbl.append(r["tot_reqs"])
                    optimal_comp.append(r["comp_time_avg"])

        if optimal_lbl:            
            ax.plot(optimal_lbl, optimal_comp, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax.text(optimal_lbl[-1], optimal_comp[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        #ax.legend()
        #ax.set_title('Optimal Solutions per Window Size (Delta)')
        #ax.set_ylabel('Computational Seconds')
        #ax.set_xlabel('Requests per slot')
    
    colors = ['b', 'g', 'r', 'c', 'm', 'y', 'k']  # Define a list of colors for better visualization
    for num_reqs in [2**i for i in range(10, 18)]:   
        for r in compression_beta["reqs"]:
            if r["tot_reqs"] == num_reqs:
                optimal_lbl_d, optimal_comp_d = [], []
                
                for delta, slots in r["delta"].items():
                    for slot in slots:
                        optimal_lbl_d.append(num_reqs)
                        optimal_comp_d.append(slot["comp_time_avg"])
                
                if optimal_lbl_d:
                    color = colors[(num_reqs // 1024) % len(colors)]  # Cycle through colors
                    ax.plot(optimal_lbl_d, optimal_comp_d, label=f'Compression Delta {num_reqs}', 
                            marker='o', linestyle='--', color=color)
                    # Annotate the first and last points for better clarity
                    ax.text(optimal_lbl_d[0], optimal_comp_d[0], f'{optimal_lbl_d[0]}', 
                            horizontalalignment='right', verticalalignment='bottom', fontsize=8)
                    ax.text(optimal_lbl_d[-1], optimal_comp_d[-1], f'{optimal_lbl_d[-1]}', 
                            horizontalalignment='right', verticalalignment='bottom', fontsize=8)
    
    #ax.legend()
    ax.set_title('Compression Computational Time per Requests')
    ax.set_xlabel('Requests per Slot')
    ax.set_ylabel('Computational Time (Seconds)')
    plt.grid(True, linestyle='--', alpha=0.6)  # Add grid for better readability
    plt.savefig('unified_computational_compression_time.png')  




    """
    # decomment to COMPUTE EACH ITERATION [COMPUTATIONAL TIME, SOLVE TIME, EMISSION] PER blocks of REQUESTS PER SLOT
    
    for index_delta,elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):
        f, axarray = plt.subplots(8, 3, figsize=(12.69,8.27)) #(11.69,8.27))    
        for index_row,num_reqs in enumerate([2**i for i in range(10, 18)]): # from 2^10 to 2^17               
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                    axarray[index_row,0].plot(r["comp_time"]) #plt.plot(x, y2#,label=num_reqs)#,marker='*') 
                    lst = [ r["comp_time_avg"] for elem in range(0,10) ]
                    axarray[index_row,0].plot(range(0,10),lst)
                    axarray[index_row,0].set_xlabel("Computational Seconds")
                    axarray[index_row,0].set_ylabel(num_reqs)                   

                    axarray[index_row,1].boxplot(r["solve_time"])
                    axarray[index_row,1].set_xlabel("Solve Seconds")

                    axarray[index_row,2].scatter(range(0,10),r["emission"],marker='*')
                    axarray[index_row,2].set_xlabel("CO2 emissions")

        plt.savefig('plot_0'+str(index_delta)+'.png')                       # Save as PNG file           
        
    # decomment to COMPUTE EACH ITERATION [COMPUTATIONAL TIME, SOLVE TIME, EMISSION] UNIFIED PER blocks of REQUESTS PER SLOT
    
    for index_delta,elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):        
        f, ax = plt.subplots(3, 1, figsize=(11.69,9.27)) #(11.69,8.27))    
        lbl,data_comp,data_solv,data_em = [],[],[],[]          
        lst_comp_avg,lst_solv_avg,lst_em_avg = [],[],[]
        for index_row,num_reqs in enumerate([2**i for i in range(10, 18)]): # from 2^10 to 2^17           
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                    lbl.append(num_reqs)
                    data_comp.append(r["comp_time"])
                    data_solv.append(r["solve_time"])
                    data_em.append(r["emission"])
                    lst_comp_avg.append(r["comp_time_avg"])
                    #lst_solv_avg.append(r["solve_time_avg"])
                    #lst_em_avg.append(r["emission_avg"])

        # Create boxplot with multiple columns, parameter notch=True per notched box
        ax[0].boxplot(data_comp, labels=lbl) 
        for i, mean in enumerate(lst_comp_avg):
            ax[0].text(i + 1, mean, f'{mean:.2f}', 
                    horizontalalignment='center', verticalalignment='bottom')
        ax[0].set_ylabel('Computational Seconds')

        ax[1].boxplot(data_solv, labels=lbl)
        ax[1].set_ylabel('Solve Seconds')

        ax[2].boxplot(data_em, labels=lbl)
        ax[2].set_ylabel('gCO2-eq/kWh')
        ax[2].set_xlabel('Requests per slot')
        
        plt.savefig('unified_plot_0'+str(index_delta)+'.png')   
    
    # decomment to COMPUTE AVERAGE COMPUTATIONAL TIME PER blocks of REQUESTS PER SLOT
    
    fig, ax = plt.subplots(figsize=(11.69,9.27))
    for index_delta, elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):        
        optimal_lbl,optimal_comp,feasible_lbl,feasible_comp = [],[],[],[]        
        for num_reqs in [2**i for i in range(10, 18)]:   
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                        if r["solve_status"] == 4:
                            optimal_lbl.append(r["tot_reqs"])
                            optimal_comp.append(r["comp_time_avg"])
                        elif r["solve_status"] == 2:
                            feasible_lbl.append(r["tot_reqs"])
                            feasible_comp.append(r["comp_time_avg"])

        if optimal_lbl:
            ax.plot(optimal_lbl, optimal_comp, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax.text(optimal_lbl[-1], optimal_comp[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        if feasible_lbl:
            ax.plot(feasible_lbl, feasible_comp, label=f'Feasible Delta {index_delta}', marker='o', linestyle='--')
            # plot first and last feasible requests' points
            ax.text(feasible_lbl[0], feasible_comp[0], f'{feasible_lbl[0]}', 
                horizontalalignment='right', verticalalignment='bottom')
            ax.text(feasible_lbl[-1], feasible_comp[-1], f'{feasible_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        ax.legend()
        ax.set_title('Optimal Solutions per Window Size (Delta)')
        ax.set_ylabel('Computational Seconds')
        ax.set_xlabel('Requests per slot')

    plt.savefig('unified_computational_time.png')  
    
    # decomment to COMPUTE AVERAGE SOLVE TIME PER blocks of REQUESTS PER SLOT
    
    fig, ax = plt.subplots(figsize=(11.69,9.27))
    for index_delta, elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):        
        optimal_lbl,optimal_solv,feasible_lbl,feasible_solv = [],[],[],[]        
        for num_reqs in [2**i for i in range(10, 18)]:                            
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                        if r["solve_status"] == 4:
                            optimal_lbl.append(r["tot_reqs"])
                            optimal_solv.append(r["solve_time_avg"])
                        elif r["solve_status"] == 2:
                            feasible_lbl.append(r["tot_reqs"])
                            feasible_solv.append(r["solve_time_avg"])

        if optimal_lbl:
            ax.plot(optimal_lbl, optimal_solv, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax.text(optimal_lbl[-1], optimal_solv[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        if feasible_lbl:
            ax.plot(feasible_lbl, feasible_solv, label=f'Feasible Delta {index_delta}', marker='o', linestyle='--')
            # plot first and last feasible requests' points
            ax.text(feasible_lbl[0], feasible_solv[0], f'{feasible_lbl[0]}', 
                horizontalalignment='right', verticalalignment='bottom')
            ax.text(feasible_lbl[-1], feasible_solv[-1], f'{feasible_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        ax.legend()
        ax.set_title('Optimal Solutions per Window Size (Delta)')
        ax.set_ylabel('Solve Seconds')
        ax.set_xlabel('Requests per slot')

    plt.savefig('unified_solve_time.png')  

    # decomment to COMPUTE AVERAGE CARBON EMISSIONS PER  blocks of REQUESTS PER SLOT

    fig, ax = plt.subplots(figsize=(11.69,9.27))
    for index_delta, elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):        
        optimal_lbl,optimal_em,feasible_lbl,feasible_em = [],[],[],[]        
        for num_reqs in [2**i for i in range(10, 18)]:                            
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                        if r["solve_status"] == 4:
                            optimal_lbl.append(r["tot_reqs"])
                            optimal_em.append(r["emission_avg"])
                        elif r["solve_status"] == 2:
                            feasible_lbl.append(r["tot_reqs"])
                            feasible_em.append(r["emission_avg"])

        if optimal_lbl:
            ax.plot(optimal_lbl, optimal_em, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax.text(optimal_lbl[-1], optimal_em[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        if feasible_lbl:
            ax.plot(feasible_lbl, feasible_em, label=f'Feasible Delta {index_delta}', marker='o', linestyle='--')
            # plot first and last feasible requests' points
            ax.text(feasible_lbl[0], feasible_em[0], f'{feasible_lbl[0]}', 
                horizontalalignment='right', verticalalignment='bottom')
            ax.text(feasible_lbl[-1], feasible_em[-1], f'{feasible_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        ax.legend()
        ax.set_title('Optimal Solutions per Window Size (Delta)')
        ax.set_ylabel('gCO2-eq/kWh')
        ax.set_xlabel('Requests per slot')
        
    plt.savefig('unified_emissions.png')  

    # decomment to COMPUTE AVERAGE [COMPUTATIONAL TIME, SOLVE TIME, EMISSION] PER  blocks of REQUESTS PER SLOT
    fig, ax = plt.subplots(3,1, figsize=(11.69,9.27))
    for index_delta, elem in enumerate([res_1["reqs"],res_2["reqs"],res_3["reqs"],res_4["reqs"],res_5["reqs"]],start=1):        
        optimal_lbl,optimal_em,feasible_lbl,feasible_em = [],[],[],[]        
        optimal_lbl,optimal_solv,feasible_lbl,feasible_solv = [],[],[],[]        
        optimal_lbl,optimal_comp,feasible_lbl,feasible_comp = [],[],[],[]        
        for num_reqs in [2**i for i in range(10, 18)]:   
            for r in elem:
                if r["tot_reqs"] == num_reqs:
                    if r["solve_status"] == 4:
                        optimal_lbl.append(num_reqs)
                        optimal_comp.append(r["comp_time_avg"])
                        optimal_solv.append(r["solve_time_avg"])
                        optimal_em.append(r["emission_avg"])
                    elif r["solve_status"] == 2:
                        feasible_lbl.append(num_reqs)
                        feasible_comp.append(r["comp_time_avg"])
                        feasible_solv.append(r["solve_time_avg"])
                        feasible_em.append(r["emission_avg"])

        if optimal_lbl:
            ax[0].plot(optimal_lbl, optimal_comp, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax[0].text(optimal_lbl[-1], optimal_comp[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

            ax[1].plot(optimal_lbl, optimal_solv, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax[1].text(optimal_lbl[-1], optimal_solv[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')
            
            ax[2].plot(optimal_lbl, optimal_em, label=f'Optimal Delta {index_delta}', marker='*') 
            # plot last optimal requests' point
            ax[2].text(optimal_lbl[-1], optimal_em[-1], f'{optimal_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        if feasible_lbl:
            ax[0].plot(feasible_lbl, feasible_comp, label=f'Feasible Delta {index_delta}', marker='o', linestyle='--')
            # plot first and last feasible requests' points
            ax[0].text(feasible_lbl[0], feasible_comp[0], f'{feasible_lbl[0]}', 
                horizontalalignment='right', verticalalignment='bottom')
            ax[0].text(feasible_lbl[-1], feasible_comp[-1], f'{feasible_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

            ax[1].plot(feasible_lbl, feasible_solv, label=f'Feasible Delta {index_delta}', marker='o', linestyle='--')
            # plot first and last feasible requests' points
            ax[1].text(feasible_lbl[0], feasible_solv[0], f'{feasible_lbl[0]}', 
                horizontalalignment='right', verticalalignment='bottom')
            ax[1].text(feasible_lbl[-1], feasible_solv[-1], f'{feasible_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

            ax[2].plot(feasible_lbl, feasible_em, label=f'Feasible Delta {index_delta}', marker='o', linestyle='--')
            # plot first and last feasible requests' points
            ax[2].text(feasible_lbl[0], feasible_em[0], f'{feasible_lbl[0]}', 
                horizontalalignment='right', verticalalignment='bottom')
            ax[2].text(feasible_lbl[-1], feasible_em[-1], f'{feasible_lbl[-1]}', 
                horizontalalignment='right', verticalalignment='bottom')

        ax[0].set_ylabel("Computational Seconds")
        ax[1].set_ylabel("Solve Seconds")
        ax[2].set_ylabel('gCO2-eq/kWh')

        ax[2].legend()
        ax[0].set_title('Solutions per Window Size (Delta)')
        ax[2].set_xlabel('Requests per slot')
        
    plt.savefig('unified_all.png')  
    
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

parser.add_argument('compression_beta', type=str, help='File with compression times results'' data')

args = parser.parse_args()

main(args.times_delta_01,args.times_delta_02,args.times_delta_03,args.times_delta_04,args.times_delta_05,args.compression_beta)