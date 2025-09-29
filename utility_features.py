import math

def decrease_assignment(sum_list, assignment, ratio_slot):      # Fill full blocks
    count_blocks = 0

    for v in range(0,len(assignment)):                                  
        while( assignment[v]>=ratio_slot ):
            sum_list.append(v)
            assignment[v] -= ratio_slot
            count_blocks += 1

    return count_blocks


def clear_file(OUTPUT_TIME):                                    # Clear the content of the files
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")                 
        file_out.write(f"all_requests,solver_status,computing_time,solve_time,all_emissions,slot_emissions,avg_errors,iteration\n")
        file_out.flush()


def clear_file_greedy(OUTPUT_TIME):
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")                 
        file_out.write(f"all_requests,computing_time,strategy,all_emissions,slot_emissions,avg_errors\n")
        file_out.flush()


def prepare_file(d, beta, N_REQUESTS, INPUT_FILE, OUTPUT_FILE):
            
    assignment, sum_all, ratio = [],[],1                # store the assignment and the sum of deadlines
    
    with open(INPUT_FILE, "r", buffering=1) as file_in: # buffered reader with chunk size               
        line = file_in.readline()                       # read the first line only 
        if line: 
                                
            assignment = [0 for _ in range(0,d)]        # init assignment for each admitted deadline                    
            values = line.replace("\n","").split(",")                      
            for v in values:            
                assignment[int(v)] += 1                 # frequence of deadline values for the slot

            sum_list = []
            ratio_slot_sup = math.ceil(N_REQUESTS / beta)  # max requests per block
            ratio_slot_inf = math.floor(N_REQUESTS / beta) # min requests per block

            ratio = ratio_slot_sup                      # max approximation for the objective valuation 

            missing_with_inf = N_REQUESTS - (ratio_slot_inf * beta)                    
            missing_with_sup = (ratio_slot_sup * beta) - N_REQUESTS                    
            missing_amount = missing_with_inf + missing_with_sup 
            
            if missing_amount == 0:  # means, ratio_slot_sup == ratio_slot_inf 
                if ratio_slot_sup != 0:
                    count_blocks = decrease_assignment(sum_list, assignment, ratio_slot_sup)
                elif ratio_slot_inf != 0:                           
                    count_blocks = decrease_assignment(sum_list, assignment, ratio_slot_inf)                    

            else: # if missing_amount == beta, means the ratio is not the same 

                # N_REQUESTS = ratio_slot_sup * missing_with_inf + ratio_slot_inf * missing_with_sup
                count_blocks_all = 0
                if(ratio_slot_sup != 0):
                    count_blocks = 0
                    for v in range(0,len(assignment)):                          
                        while( count_blocks < missing_with_inf and assignment[v]>=ratio_slot_sup ):
                            sum_list.append(v)
                            assignment[v] -= ratio_slot_sup
                            count_blocks += 1
                    count_blocks_all += count_blocks                                 
                
                if (ratio_slot_inf != 0):   
                    count_blocks = 0
                    for v in range(0,len(assignment)):                          
                        while( count_blocks < missing_with_sup and assignment[v]>=ratio_slot_inf ):
                            sum_list.append(v)
                            assignment[v] -= ratio_slot_inf
                            count_blocks += 1
                    count_blocks_all += count_blocks                       

                # when remaining requsts are sparsed, give the block the earlier deadline 
                while(count_blocks_all < beta and N_REQUESTS>beta ): 

                    count_missing = 0 
                    for v in range(0,len(assignment)):            
                        if assignment[v] > 0:   
                            count_missing += assignment[v]

                    missing_blocks = beta - count_blocks_all
                    ratio_slot_sup = math.ceil(count_missing / missing_blocks)  # max remaining requests per block
                    ratio_slot_inf = math.floor(count_missing / missing_blocks) # min remaining requests per block
                    missing_with_inf = count_missing - (ratio_slot_inf * missing_blocks)                    
                    missing_with_sup = (ratio_slot_sup * missing_blocks) - count_missing                   
                    missing_amount = missing_with_inf + missing_with_sup 
                    
                    if(ratio_slot_sup == ratio_slot_inf):                       # always the same ratio 
                        count_blocks = 0
                        for v in range(0,len(assignment)):   
                            while (count_blocks < missing_blocks):                       
                                c = 0
                                sum_list.append(v)
                                while( c < ratio_slot_sup and assignment[v]>=1 ):
                                    assignment[v] -= 1
                                    c += 1
                                count_blocks += 1
                        count_blocks_all += count_blocks
                    else:
                        for v in range(0,len(assignment)): 
                            count_blocks_s, count_blocks_i = 0, 0

                            while (count_blocks_s < missing_with_sup):                       
                                c_s = 0
                                sum_list.append(v)
                                while( c_s < ratio_slot_sup and assignment[v]>=1 ):
                                    assignment[v] -= 1
                                    c_s += 1
                                count_blocks_s += 1

                            while (count_blocks_i < missing_with_inf):                       
                                c_i = 0
                                while( c_i < ratio_slot_inf and assignment[v]>=1 ):
                                    assignment[v] -= 1
                                    c_i += 1
                                count_blocks_i += 1

                        count_blocks = count_blocks_s + count_blocks_i
                        count_blocks_all += count_blocks                       

            t = {}
            t['slot'] = 0           # the slot within the window
            t['sum'] = sum_list
            sum_all.append(t)


    with open(OUTPUT_FILE, "w") as file_out:
        for i in sum_all:
            len_sum_i = len(i['sum'])
            for j in range(0, len_sum_i - 1):
                file_out.write(f"{i['sum'][j]},")
            file_out.write(f"{i['sum'][len_sum_i - 1]}\n")

    return ratio
