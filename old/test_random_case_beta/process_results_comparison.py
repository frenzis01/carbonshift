# ------------------------
# move to test_random_case_beta folder
#    RUN with
"""
python3.8 process_results_comparison.py 
ratio_1000_1000/times_01.csv 
ratio_1000_1000/times_02.csv 
ratio_1000_1000/times_03.csv 
ratio_1000_1000/times_04.csv 
ratio_1000_1000/times_05.csv 
ratio_1000_200/times_01.csv 
ratio_1000_200/times_02.csv 
ratio_1000_200/times_03.csv 
ratio_1000_200/times_04.csv 
ratio_1000_200/times_05.csv 
ratio_1000_100/times_01.csv 
ratio_1000_100/times_02.csv 
ratio_1000_100/times_03.csv 
ratio_1000_100/times_04.csv 
ratio_1000_100/times_05.csv 
ratio_1000_50/times_01.csv 
ratio_1000_50/times_02.csv 
ratio_1000_50/times_03.csv 
ratio_1000_50/times_04.csv 
ratio_1000_50/times_05.csv 
"""
# ------------------------ 

import matplotlib.pyplot as plt
#import seaborn as sns  NOTE: per grafici diversi?
from argparse import ArgumentParser
import sys


# Import times' data
def import_input_results(times_delta):
    with open(times_delta, "r") as file:            # NOTE: file without empty lines

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

def main(
        times_delta_01_b1000,times_delta_02_b1000,times_delta_03_b1000,times_delta_04_b1000,times_delta_05_b1000,        
        times_delta_01_b200,times_delta_02_b200,times_delta_03_b200,times_delta_04_b200,times_delta_05_b200,
        times_delta_01_b100,times_delta_02_b100,times_delta_03_b100,times_delta_04_b100,times_delta_05_b100,        
        times_delta_01_b50,times_delta_02_b50,times_delta_03_b50,times_delta_04_b50,times_delta_05_b50        
        ):

    res_1_10 = import_input_results(times_delta_01_b1000)
    res_2_10 = import_input_results(times_delta_02_b1000)
    res_3_10 = import_input_results(times_delta_03_b1000)
    res_4_10 = import_input_results(times_delta_04_b1000)
    res_5_10 = import_input_results(times_delta_05_b1000)

    res_1_2 = import_input_results(times_delta_01_b200)
    res_2_2 = import_input_results(times_delta_02_b200)
    res_3_2 = import_input_results(times_delta_03_b200)
    res_4_2 = import_input_results(times_delta_04_b200)
    res_5_2 = import_input_results(times_delta_05_b200)

    res_1_1 = import_input_results(times_delta_01_b100)
    res_2_1 = import_input_results(times_delta_02_b100)
    res_3_1 = import_input_results(times_delta_03_b100)
    res_4_1 = import_input_results(times_delta_04_b100)
    res_5_1 = import_input_results(times_delta_05_b100)

    res_1_05 = import_input_results(times_delta_01_b50)
    res_2_05 = import_input_results(times_delta_02_b50)
    res_3_05 = import_input_results(times_delta_03_b50)
    res_4_05 = import_input_results(times_delta_04_b50)
    res_5_05 = import_input_results(times_delta_05_b50)


    num_reqs_values = [2**i for i in range(10, 18)]  # Define the x-axis values
    fig, ax = plt.subplots(5, 1, figsize=(12.69, 11.3))  # Dynamically adjust figure size
    #beta = [200,100,50]
    beta = [1000,200,100,50]
    len_beta = len(beta)

    # List of lists to store computational (or solve) time averages for each beta of each delta
    lbls_d1 = [[] for _ in range(len_beta)]
    lbls_d2 = [[] for _ in range(len_beta)]
    lbls_d3 = [[] for _ in range(len_beta)]
    lbls_d4 = [[] for _ in range(len_beta)]
    lbls_d5 = [[] for _ in range(len_beta)]

    # COMPUTE AVERAGE [COMPUTATIONAL, SOLVE]TIME and EMISSIONS PER blocks of REQUESTS PER SLOT
    """
    time = "comp_time_avg"
    ax[0].set_title('Comparison of Computational Seconds')  
    png = 'compared_computational_time.png'
    """
    """
    time = "solve_time_avg"
    ax[0].set_title('Comparison of Solve Seconds')  
    png = 'compared_solve_time.png'
    """

    time = "emission_avg"
    ax[0].set_title('Comparison of CO2 emissions')  
    png = 'compared_CO2_emissions.png'

    #for index_beta, elem in enumerate([res_1_10["reqs"], res_1_05["reqs"]], start=0):
    for index_beta, elem in enumerate([res_1_10["reqs"], res_1_2["reqs"], res_1_1["reqs"], res_1_05["reqs"]], start=0):
    #for index_beta, elem in enumerate([res_1_2["reqs"], res_1_1["reqs"], res_1_05["reqs"]], start=0):
        for i in range(0, len_beta):  # Loop through the beta values        
            for num_reqs in [2**i for i in range(10, 18)]:                
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        lbls_d1[index_beta].append(r[time])

    for index in range(len(lbls_d1)):
        l = beta[index%len_beta]
        ax[0].plot(num_reqs_values, lbls_d1[index], marker='o', label='Beta '+str(l))  
        ax[0].set_ylabel('Delta = 1')  # Label the y-axis
        ax[0].legend()  # Add a legend to distinguish the plots

    #for index_beta, elem in enumerate([res_1_10["reqs"], res_1_05["reqs"]], start=0):
    for index_beta, elem in enumerate([res_2_10["reqs"], res_2_2["reqs"], res_2_1["reqs"], res_2_05["reqs"]], start=0):
    #for index_beta, elem in enumerate([res_2_2["reqs"], res_2_1["reqs"], res_2_05["reqs"]], start=0):
        for i in range(0, len_beta):  # Loop through the beta values        
            for num_reqs in [2**i for i in range(10, 18)]:                
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        lbls_d2[index_beta].append(r[time])

    for index in range(len(lbls_d2)):
        l = beta[index%len_beta]
        ax[1].plot(num_reqs_values, lbls_d2[index], marker='o', label='Beta '+str(l))  
        ax[1].set_ylabel('Delta = 2')  # Label the y-axis
        ax[1].legend()  # Add a legend to distinguish the plots

    #for index_beta, elem in enumerate([res_1_10["reqs"], res_1_05["reqs"]], start=0):
    for index_beta, elem in enumerate([res_3_10["reqs"], res_3_2["reqs"], res_3_1["reqs"], res_3_05["reqs"]], start=0):
    #for index_beta, elem in enumerate([res_3_2["reqs"], res_3_1["reqs"], res_3_05["reqs"]], start=0):
        for i in range(0, len_beta):  # Loop through the beta values        
            for num_reqs in [2**i for i in range(10, 18)]:                
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        lbls_d3[index_beta].append(r[time])

    for index in range(len(lbls_d3)):
        l = beta[index%len_beta]
        ax[2].plot(num_reqs_values, lbls_d3[index], marker='o', label='Beta '+str(l))  
        ax[2].set_ylabel('Delta = 3')  # Label the y-axis
        ax[2].legend()  # Add a legend to distinguish the plots

    #for index_beta, elem in enumerate([res_1_10["reqs"], res_1_05["reqs"]], start=0):
    for index_beta, elem in enumerate([res_4_10["reqs"], res_4_2["reqs"], res_4_1["reqs"], res_4_05["reqs"]], start=0):
    #for index_beta, elem in enumerate([ res_4_2["reqs"], res_4_1["reqs"], res_4_05["reqs"]], start=0):
        for i in range(0, len_beta):  # Loop through the beta values        
            for num_reqs in [2**i for i in range(10, 18)]:                
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        lbls_d4[index_beta].append(r[time])

    for index in range(len(lbls_d4)):
        l = beta[index%len_beta]
        ax[3].plot(num_reqs_values, lbls_d4[index], marker='o', label='Beta '+str(l))  
        ax[3].set_ylabel('Delta = 4')  # Label the y-axis
        ax[3].legend()  # Add a legend to distinguish the plots

    #for index_beta, elem in enumerate([res_1_10["reqs"], res_1_05["reqs"]], start=0):
    for index_beta, elem in enumerate([res_5_10["reqs"], res_5_2["reqs"], res_5_1["reqs"], res_5_05["reqs"]], start=0):
    #for index_beta, elem in enumerate([res_5_2["reqs"], res_5_1["reqs"], res_5_05["reqs"]], start=0):
        for i in range(0, len_beta):  # Loop through the beta values        
            for num_reqs in [2**i for i in range(10, 18)]:                
                for r in elem:
                    if r["tot_reqs"] == num_reqs and index_beta == i:
                        lbls_d5[index_beta].append(r[time])

    for index in range(len(lbls_d5)):
        l = beta[index%len_beta]
        ax[4].plot(num_reqs_values, lbls_d5[index], marker='o', label='Beta '+str(l))  
        ax[4].set_ylabel('Delta = 5')  # Label the y-axis
        ax[4].legend()  # Add a legend to distinguish the plots
    
    ax[4].set_xlabel('Requests per Slot')  # Label the x-axis

    plt.savefig(png)  
    


# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Process of assignments of requests to strategies and time slots")
parser.add_argument('times_delta_01_10', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_02_10', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_03_10', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_04_10', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_05_10', type=str, help='File with times results'' data and delta')

parser.add_argument('times_delta_01_2', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_02_2', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_03_2', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_04_2', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_05_2', type=str, help='File with times results'' data and delta')

parser.add_argument('times_delta_01_1', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_02_1', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_03_1', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_04_1', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_05_1', type=str, help='File with times results'' data and delta')

parser.add_argument('times_delta_01_05', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_02_05', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_03_05', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_04_05', type=str, help='File with times results'' data and delta')
parser.add_argument('times_delta_05_05', type=str, help='File with times results'' data and delta')

args = parser.parse_args()

main(
    args.times_delta_01_10,args.times_delta_02_10,args.times_delta_03_10,args.times_delta_04_10,args.times_delta_05_10,
    args.times_delta_01_2,args.times_delta_02_2,args.times_delta_03_2,args.times_delta_04_2,args.times_delta_05_2,
    args.times_delta_01_1,args.times_delta_02_1,args.times_delta_03_1,args.times_delta_04_1,args.times_delta_05_1,
    args.times_delta_01_05,args.times_delta_02_05,args.times_delta_03_05,args.times_delta_04_05,args.times_delta_05_05
    )