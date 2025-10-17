
# ------------------------
# change the path to the input files accordingly
# ------------------------
#    RUN with <delta, error>
# nohup python3.8 orchestrate_heuristics.py 5 2 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 4 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 5 > log.txt
# ------------------------ 

from argparse import ArgumentParser
import subprocess
import os

INPUT_STRATEGIES                       = "test/input_strategies.csv"

#heuristic                              = "greedy"
heuristic                               = "carbonshift"

# ------------------------ UTILITY FUNCTIONS ------------------------
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


# ------------------------ MAIN FUNCTION ------------------------

def main(DELTA, error):    
    for w in range(0,5):       #(0,5):                          # {0,1,2,3,4} -> 5 sliding windows
        INPUT_CO2                       = f"test/window_{w}/input_co2.csv"

        if heuristic == "carbonshift":
            BETA                        = [100,250,500,1000]    # 4 different β values
            for beta in BETA:                     
                directory               = f"test/window_{w}/beta_"+str(beta)+"/"
                OUTPUT_TIME             = ""
                OUTPUT_ASSIGNMENT       = directory+"output_assignment.csv"
                
                for d in range(1, DELTA+1):                         #{1,2,3,4,5} -> 5 slots per window
                    OUTPUT_TIME = directory+"times_0"+str(d)+".csv"
                    clear_file(OUTPUT_TIME)

                for d in range(1, DELTA+1):                         #{1,2,3,4,5} -> 5 slots per window
                    N_REQUESTS = 1024
                    while N_REQUESTS<131073 :                       #limit: 2^17+1
                        
                        INPUT_FILE          =f"test/window_{w}/input_requests/delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"                 
                        
                        for i in range(1,11):                       # Iterate sequentially                              
                            if os.path.exists(OUTPUT_ASSIGNMENT):   # Ensure OUTPUT_ASSIGNMENT is cleared before each run
                                os.remove(OUTPUT_ASSIGNMENT)

                            START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())            
                            process = subprocess.Popen([
                                "python3.8","carbonshift.py",
                                INPUT_FILE,INPUT_STRATEGIES,INPUT_CO2, 
                                str(d),str(beta),
                                str(error),
                                OUTPUT_ASSIGNMENT                            
                            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)  #])
                                                                                    
                            stdout, stderr = process.communicate()  # Waits for process to finish and captures output                    
                            END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
                            if stdout:
                                print("Solver STDOUT:", stdout.decode(), flush=True)
                            if stderr:
                                print("Solver STDERR:", stderr.decode(), flush=True)

                            OUTPUT_TIME = directory+"times_0"+str(d)+".csv"
                            with open(OUTPUT_TIME, "a") as file_out:
                                file_out.write(f"{N_REQUESTS},")
                                diff = round(END-START,2)
                                
                                solver_status   = ""
                                solve_time      = ""
                                all_emissions   = ""
                                slot_emissions  = []
                                avg_errors      = ""
                                #slot_errors    = []
                                
                                if not os.path.exists(OUTPUT_ASSIGNMENT):   # Check if output file was written
                                    print(f"ERROR: Output file {OUTPUT_ASSIGNMENT} was not created!", flush=True)
                                    break

                                with open(OUTPUT_ASSIGNMENT, "r", buffering=1) as output_file:
                                    lines = output_file.readlines()
                                    if len(lines) == 1:
                                        print(f"NO SOLUTIONS FOUND for {N_REQUESTS} requests, delta={d}, beta={beta}, window={w}", flush=True)
                                        break
                                    else:
                                        for line in lines:                            
                                            if "solver_status:" in line:
                                                solver_status = line.split(':')[1].split(',')[0].strip()
                                            elif "solve_time:" in line:
                                                solve_time = line.split(':')[1].split(',')[0].strip()
                                            elif "all_emissions:" in line:
                                                all_emissions = line.split(':')[1].split(',')[0].strip()
                                            elif "slot_emissions:" in line:
                                                slot_emissions = line.split(':')[1].strip().strip('[]').split(',')
                                            elif "all_errors:" in line:
                                                avg_errors = line.split(':')[1].split(',')[0].strip()                                        
                                            #elif "slot_errors:" in line:
                                            #    slot_errors = line.split(':')[1].strip().strip('[]').split(',')
                                
                                file_out.write(
                                    f"{solver_status},"
                                    f"{diff},"
                                    f"{solve_time},"
                                    f"{all_emissions},"
                                    f"[{','.join(slot_emissions)}],"
                                    f"{avg_errors}," 
                                    #f"[{','.join(slot_errors)}],"
                                    f"{i}\n"
                                )

                        N_REQUESTS *= 2

        elif heuristic == "greedy":
            directory                   = f"test_greedy/window_{w}/"

            policies                    = ["baseline","random","n_carbon","n_err2","n_err4","n_err5","n_shift"]            
            OUT_ASSIGN_baseline         = directory+"output_assignment_"+policies[0]+".csv"
            OUT_ASSIGN_random           = directory+"output_assignment_"+policies[1]+".csv"
            OUT_ASSIGN_n_carbon         = directory+"output_assignment_"+policies[2]+".csv"
            OUT_ASSIGN_n_err2           = directory+"output_assignment_"+policies[3]+".csv"
            OUT_ASSIGN_n_err4           = directory+"output_assignment_"+policies[4]+".csv"
            OUT_ASSIGN_n_err5           = directory+"output_assignment_"+policies[5]+".csv"
            OUT_ASSIGN_n_shift          = directory+"output_assignment_"+policies[6]+".csv"

            OUT_TIME_baseline         = directory+"times_"+policies[0]+".csv"
            OUT_TIME_random           = directory+"times_"+policies[1]+".csv"
            OUT_TIME_n_carbon         = directory+"times_"+policies[2]+".csv"
            OUT_TIME_n_err2           = directory+"times_"+policies[3]+".csv"
            OUT_TIME_n_err4           = directory+"times_"+policies[4]+".csv"
            OUT_TIME_n_err5           = directory+"times_"+policies[5]+".csv"
            OUT_TIME_n_shift          = directory+"times_"+policies[6]+".csv"

            clear_file_greedy(OUT_TIME_baseline)
            clear_file_greedy(OUT_TIME_random)
            clear_file_greedy(OUT_TIME_n_carbon)
            clear_file_greedy(OUT_TIME_n_err2)
            clear_file_greedy(OUT_TIME_n_err4)
            clear_file_greedy(OUT_TIME_n_err5)
            clear_file_greedy(OUT_TIME_n_shift)

            OUT_ASSIGN = [ OUT_ASSIGN_baseline,OUT_ASSIGN_random,OUT_ASSIGN_n_carbon,OUT_ASSIGN_n_err2,OUT_ASSIGN_n_err4,OUT_ASSIGN_n_err5, OUT_ASSIGN_n_shift]
            OUT_TIMES = [ OUT_TIME_baseline,OUT_TIME_random,OUT_TIME_n_carbon, OUT_TIME_n_err2,OUT_TIME_n_err4, OUT_TIME_n_err5, OUT_TIME_n_shift]


            N_REQUESTS = 1024
            while N_REQUESTS<131073 :                       #limit: 2^17+1
                
                INPUT_FILE          =f"test_greedy/window_{w}/input_"+str(N_REQUESTS)+".csv"                 

                process = subprocess.Popen([
                    "python3.8","greedy.py",
                    INPUT_FILE,INPUT_STRATEGIES,INPUT_CO2,                   
                    str(5), # Window size
                    OUT_ASSIGN_baseline, OUT_ASSIGN_random, OUT_ASSIGN_n_carbon,
                    OUT_ASSIGN_n_err2, OUT_ASSIGN_n_err4, OUT_ASSIGN_n_err5, OUT_ASSIGN_n_shift
                ])
                process.wait()

                for a_elem, t_elem in zip(OUT_ASSIGN, OUT_TIMES):
                    comp_time       = ""
                    strategy        = ""
                    all_emissions   = ""
                    slot_emissions  = []
                    avg_errors      = ""
                    #slot_errors = []

                    with open(a_elem, "r") as output_file:
                        lines = output_file.readlines()
                        for line in lines:                            
                            if "computing_time:" in line:
                                comp_time = line.split(':')[1].split(',')[0].strip()
                            elif "strategy:" in line:
                                strategy = line.split(':')[1].split(',')[0].strip()
                            elif "all_emissions:" in line:
                                all_emissions = line.split(':')[1].split(',')[0].strip()
                            elif "slot_emissions:" in line:
                                slot_emissions = line.split(':')[1].strip().strip('[]').split(',')
                            elif "avg_errors:" in line:
                                avg_errors = line.split(':')[1].split(',')[0].strip()
                            #elif "slot_errors:" in line:
                            #    slot_errors = line.split(':')[1].strip().strip('[]').split(',')

                    with open(t_elem, "a") as file_out:                                                                                                           
                        file_out.write(
                            f"{N_REQUESTS},"
                            f"{comp_time},"
                            f"{strategy},"
                            f"{all_emissions},"
                            f"[{','.join(slot_emissions)}],"
                            f"{avg_errors}\n"
                        )

                N_REQUESTS *= 2
                    
                    
# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Orchestrater of blocks of requests to compressor and optimzer for strategies and time slot.")

parser.add_argument('delta', type=int, help='Window size')
parser.add_argument('error', type=int, help='Tolerated error (%).')

args = parser.parse_args()
main(args.delta,args.error)