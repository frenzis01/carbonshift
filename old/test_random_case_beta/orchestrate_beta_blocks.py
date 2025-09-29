
# ------------------------
#    RUN with
# nohup python3.8 orchestrate_beta_blocks.py 5 5 ratio_/input_beta.csv > log.txt
# change the ratio_ directory to the one you want
# ------------------------ 


from argparse import ArgumentParser
import os
import math
import sys
import subprocess
import time


# Import beta' data
def import_input_beta(input_beta):
    beta = []

    with open(input_beta, "r") as file:
        for line in file:
            values = line.replace("\n","").split(",")
            t = int(values[0])
            beta.append(t)

    return beta

"""
# Find the maximum index value in a list.
def find_max_value_index_within_delta(array):    
    max_value, index = 0, 0

    for idx, value in enumerate(array):
        if value > max_value:
            max_value = value
            index = idx

    return index
"""

# Fill full blocks
def decrease_assignment(sum, assignment, ratio_slot):
    count_blocks = 0

    for v in range(0,len(assignment)):                                  
        while( assignment[v]>=ratio_slot ):
            sum.append(v)
            assignment[v] -= ratio_slot
            count_blocks += 1

    return count_blocks

directory = "ratio_1000_50/"

def main(DELTA, error, input_beta):
    beta = import_input_beta(input_beta)
    assert len(beta) >= DELTA, "The length of BETA must be at least DELTA."

    # Clear the output files
    for d in range(1, DELTA+1):     #{1,2,3,4,5}
        OUTPUT_TIME = directory+"times_0"+str(d)+".csv"

        with open(OUTPUT_TIME, "w") as file_out:                
            file_out.write(f"")                 
            file_out.write(f"all_requests,computing_time,solver_status,solve_time,objective_value,all_emissions,slot_emissions,all_errors,slot_errors,iteration\n")
            file_out.flush()

        with open(directory+"compression_time.csv", "w") as file_out:
            file_out.write(f"")                
            file_out.write(f"all_requests,delta,slot,compression_time\n")

    # Prepare the output files
    for d in range(1, DELTA+1):     #{1,2,3,4,5}
        N_REQUESTS = 1024
        while N_REQUESTS<131073 :    #limit: 2^17+1
            
            # File with randomly collected requests
            INPUT_FILE="input_requests/delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"                 
            OUTPUT_FILE=directory+"delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"

            assignment = []
            sum_all, linea_index = [],0
            ratio_all = [1 for _ in range(0,d)]                 # ratio for each slot
            compression_all = [0 for _ in range(0,d)]           # compression valuation for each slot
            
            with open(INPUT_FILE, "r", buffering=1) as file_in: # buffered reader with chunk size
                for line in file_in:
                    start_time = time.time() 
                                        
                    assignment = [0 for _ in range(0,d)]        # reset assignment for each slot                    
                    values = line.replace("\n","").split(",")                      
                    for v in values:            
                        assignment[int(v)] += 1                 # frequence of deadline values for the slot
                    
                    #print("ass:",assignment, len(assignment))
                    #sys.stdout.flush()

                    sum = []
                    ratio_slot_sup = math.ceil(N_REQUESTS / beta[linea_index])  # max requests per block
                    ratio_slot_inf = math.floor(N_REQUESTS / beta[linea_index]) # min requests per block

                    #print("req",N_REQUESTS,"ratio_slot_sup",ratio_slot_sup,"ratio_slot_inf",ratio_slot_inf)
                    #sys.stdout.flush()

                    ratio_all[linea_index] = ratio_slot_sup     # max approximation for the objective valuation 

                    """
                    # use this to give predominance to the max frequent deadline
                    count_blocks = decrease_assignment(sum, assignment, ratio_slot_sup) # fill full blocks

                    if( N_REQUESTS>beta[linea_index] ):
                        while( count_blocks < beta[linea_index]):                   # fill remaining blocks            
                            v = find_max_value_index_within_delta(assignment)       
                            assignment[v] = 0
                            sum.append(v)
                            count_blocks += 1
                    """

                    missing_with_inf = N_REQUESTS - (ratio_slot_inf * beta[linea_index])                    
                    missing_with_sup = (ratio_slot_sup * beta[linea_index]) - N_REQUESTS                    
                    missing_amount = missing_with_inf + missing_with_sup 
                    #print("missing_with_inf", missing_with_inf, "missing_with_sup", missing_with_sup)
                    #sys.stdout.flush()
                    
                    if missing_amount == 0:  # means, ratio_slot_sup == ratio_slot_inf 
                        if ratio_slot_sup != 0:
                            count_blocks = decrease_assignment(sum, assignment, ratio_slot_sup)
                        elif ratio_slot_inf != 0:                           
                            count_blocks = decrease_assignment(sum, assignment, ratio_slot_inf)                    
                        #print("count_blocks in equal ratio", N_REQUESTS, count_blocks)
                        #sys.stdout.flush()

                    else: # if missing_amount == beta[linea_index], means the ratio is not the same 

                        # N_REQUESTS = ratio_slot_sup * missing_with_inf + ratio_slot_inf * missing_with_sup
                        count_blocks_all = 0
                        if(ratio_slot_sup != 0):
                            count_blocks = 0
                            for v in range(0,len(assignment)):                          
                                while( count_blocks < missing_with_inf and assignment[v]>=ratio_slot_sup ):
                                    sum.append(v)
                                    assignment[v] -= ratio_slot_sup
                                    count_blocks += 1
                            count_blocks_all += count_blocks                                 
                        #print("count_blocks in sup", count_blocks_all, missing_with_inf)
                        #sys.stdout.flush()
                        
                        if (ratio_slot_inf != 0):   
                            count_blocks = 0
                            for v in range(0,len(assignment)):                          
                                while( count_blocks < missing_with_sup and assignment[v]>=ratio_slot_inf ):
                                    sum.append(v)
                                    assignment[v] -= ratio_slot_inf
                                    count_blocks += 1
                            count_blocks_all += count_blocks
                        #print("count_blocks in inf", count_blocks, missing_with_sup)#, assignment)
                        #sys.stdout.flush()                        

                        # when remaining requsts are sparsed, give the block the earlier deadline 
                        while(count_blocks_all < beta[linea_index] and N_REQUESTS>beta[linea_index] ): 

                            count_missing = 0 
                            for v in range(0,len(assignment)):            
                                if assignment[v] > 0:   
                                    count_missing += assignment[v]

                            #print("before", count_blocks_all, count_missing, assignment, missing_with_inf, missing_with_sup, ratio_slot_sup, ratio_slot_inf)
                            #sys.stdout.flush()

                            missing_blocks = beta[linea_index] - count_blocks_all
                            ratio_slot_sup = math.ceil(count_missing / missing_blocks)  # max remaining requests per block
                            ratio_slot_inf = math.floor(count_missing / missing_blocks) # min remaining requests per block
                            missing_with_inf = count_missing - (ratio_slot_inf * missing_blocks)                    
                            missing_with_sup = (ratio_slot_sup * missing_blocks) - count_missing                   
                            missing_amount = missing_with_inf + missing_with_sup 

                            #print("after", missing_blocks, missing_with_inf, missing_with_sup, missing_amount, ratio_slot_sup, ratio_slot_inf)
                            #sys.stdout.flush()
                            
                            if(ratio_slot_sup == ratio_slot_inf):                       # always the same ratio 
                                count_blocks = 0
                                for v in range(0,len(assignment)):   
                                    while (count_blocks < missing_blocks):                       
                                        c = 0
                                        sum.append(v)
                                        while( c < ratio_slot_sup and assignment[v]>=1 ):
                                            assignment[v] -= 1
                                            c += 1
                                        count_blocks += 1
                                count_blocks_all += count_blocks
                            
                            #print("N_REQUESTS", N_REQUESTS, assignment)
                            #sys.stdout.flush()     

                            """
                            # wroks if the count_missing is a multiple of the ratio
                            if(count_missing>=ratio_slot_sup and ratio_slot_sup!=0 and count_missing % ratio_slot_sup == 0):
                                for v in range(0,len(assignment)):    
                                    if assignment[v] > 0:
                                        sum.append(v) 
                                        ratio_slot_sup_support = ratio_slot_sup
                                        for w in range(v,len(assignment)):                                            
                                            if(ratio_slot_sup_support > 0 and assignment[w] > 0):
                                                ratio_slot_sup_support -= assignment[w]
                                                count_missing -= assignment[w]
                                                assignment[w] = 0
                                        count_blocks += 1

                            elif( count_missing >= ratio_slot_inf and ratio_slot_inf!=0 and count_missing % ratio_slot_inf == 0):
                                for v in range(0,len(assignment)):   
                                    if assignment[v] > 0:                       
                                        sum.append(v) 
                                        ratio_slot_inf_support = ratio_slot_inf
                                        for w in range(v,len(assignment)):
                                            if (ratio_slot_inf_support > 0 and assignment[w] > 0):
                                                ratio_slot_inf_support -= assignment[w]
                                                count_missing -= assignment[w]
                                                assignment[w] = 0
                                        count_blocks += 1
                            
                            count_blocks_all += count_blocks
                            """                       

                    #print((len(sum)))  
                    #sys.stdout.flush()

                    t = {}
                    t['slot'] = linea_index           # the slot within the window
                    t['sum'] = sum
                    sum_all.append(t)

                    diff = time.time() - start_time
                    if diff > 300:  # 300 seconds = 5 minutes
                        print(f"Skipping slot {linea_index} due to elapsed timeout for requests {N_REQUESTS}")
                        sys.stdout.flush()
                    compression_all[linea_index] += diff

                    linea_index += 1
            
            with open(OUTPUT_FILE, "w") as file_out:
                for i in sum_all: 
                    len_sum_i = len(i['sum'])
                    for j in range(0,len_sum_i-1):
                        file_out.write(f"{i['sum'][j]},")
                    file_out.write(f"{i['sum'][len_sum_i-1]}\n")

            with open("input_ratio.csv", "w") as file_out:
                for i in ratio_all:
                    file_out.write(f"{i}\n")

            with open(directory+"compression_time.csv", "a") as file_out:
                for index,i in enumerate(compression_all, start=0):
                    file_out.write(f"{N_REQUESTS},{d},{index},{i}\n")


            INPUT_FILE  = OUTPUT_FILE            
            OUTPUT_FILE = directory+"output_assignment.csv"
            OUTPUT_TIME = directory+"times_0"+str(d)+".csv"

            with open(OUTPUT_TIME, "a") as file_out:                
 
                for i in range(1, 11):     # repeat sequentially at each iteration 
                    START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())

                    os.system("python3.8 carbon_aware_patterns.py "+
                              INPUT_FILE+
                              " input_strategies_fixed.csv input_co2_fixed.csv "+
                              str(d)+" "+
                              input_beta+
                              " input_ratio.csv " +
                              str(error)+" "+
                              OUTPUT_FILE)
                    
                    END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())

                    os.system("echo "+str(N_REQUESTS)+','+
                              "$(echo " +str(END-START)+"| bc),"+
                              "$(grep 'solver_status:' "+str(OUTPUT_FILE)+"),"+
                              "$(grep 'solve_time:' "+str(OUTPUT_FILE)+"),"+
                              "$(grep 'objective_value:' "+str(OUTPUT_FILE)+"),"+
                              "$(grep 'all_emissions:' "+str(OUTPUT_FILE)+"),"+
                              "$(grep 'slot_emissions:' "+str(OUTPUT_FILE)+"),"+
                              "$(grep 'all_errors:' "+str(OUTPUT_FILE)+"),"+
                              "$(grep 'slot_errors:' "+str(OUTPUT_FILE)+"),"+
                              str(i)+">>"+OUTPUT_TIME)

            N_REQUESTS *= 2
                    
                    
# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Optimizer of assignments of blocks requests to strategies and time slots.")
parser.add_argument('delta', type=int, help='Window size')
parser.add_argument('error', type=int, help='Tolerated error (%).')
parser.add_argument('input_beta', type=str, help='File with the number of blocks per each slot')
args = parser.parse_args()
main(args.delta,args.error,args.input_beta)