from argparse import ArgumentParser
import os

DELTA = 5
SIGMA = 500

def main():
    for d in range(1, DELTA+1): #{1,2,3,4,5}
        N_REQUESTS = 512
        while N_REQUESTS<65537 : #limit: 2^16+1
            # fixed input file with randomly collected requests
            INPUT_FILE="sigma_1/delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"     
            OUTPUT_FILE="sigma_"+str(SIGMA)+"/delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"

            first = ""
            assignment = []
            sum_all, linea_index = [],0

            with open(INPUT_FILE, "r") as file_in:
                first = file_in.readline().strip('\n')
                #print(INPUT_FILE)

                #print(first)
                #next(file_in) # skip the first line

                for line in file_in: 
                    assignment = [0 for _ in range(0,d-1)]
                    values = line.replace("\n","").split(",")                      
                    for v in values:
                        assignment[int(v)] += 1
                    
                    sum = []
                    for i in range(0,d-1):
                        while( assignment[i]>0 ):
                            sum.append(i)
                            assignment[i] -= SIGMA
                    #print(assignment, sum)  
                    #sum_all.append([])

                    t = {}
                    t['id'] = linea_index
                    t['sum'] = sum
                    sum_all.append(t)
                     
                    linea_index += 1
            #print(sum_all)
            
            with open(OUTPUT_FILE, "w") as file_out:
                file_out.write(f"{first}\n")
                #print(sum_all)
                #len_sum = len(sum_all)                
                for i in sum_all:#range(0,len_sum):
                    len_sum_i = len(i['sum'])
                    #print(len_sum_i)
                    for j in range(0,len_sum_i-1):
                        file_out.write(f"{i['sum'][j]},")
                    file_out.write(f"{i['sum'][len_sum_i-1]}\n")
            
            N_REQUESTS *= 2
                    
                    
                    
    
# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Optimizer of assignments of requests to strategies and time slots.")
args = parser.parse_args()

main()
