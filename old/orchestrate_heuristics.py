
# ------------------------
#    RUN with <delta, error>
# nohup python3.8 orchestrate_heuristics.py 5 1 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 2 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 3 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 4 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 5 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 6 > log.txt
# nohup python3.8 orchestrate_heuristics.py 5 7 > log.txt
# ------------------------ 

from argparse import ArgumentParser
import subprocess
import compress_beta_blocks
import os

INPUT_STRATEGIES                       = "test/input_strategies.csv"

#heuristic                              = "greedy"
heuristic                               = "carbonshift"
#NOTE: TBD heuristic = "carbonstat"

def main(DELTA, error):    
    for w in range(2,5):       #(0,5):                  # {0,1,2,3,4} -> 5 sliding windows
        INPUT_CO2                       = f"test/window_{w}/input_co2.csv"
        BETA                            = [100,250,500,1000]   # 4 different β values
        for beta in BETA:                     
            directory                   = f"test/window_{w}/beta_"+str(beta)+"/"

            OUTPUT_TIME                 = ""
            #OUTPUT_COMPRESSION          = directory+"compression_time.csv"
            OUTPUT_ASSIGNMENT           = directory+"output_assignment.csv"

            OUTPUT_ASSIGNMENT_baseline  = directory+"output_assignment_baseline.csv"
            OUTPUT_ASSIGNMENT_mixed     = directory+"output_assignment_mixed.csv"
            OUTPUT_ASSIGNMENT_high      = directory+"output_assignment_high.csv"
            OUTPUT_ASSIGNMENT_medium    = directory+"output_assignment_medium.csv"
            OUTPUT_ASSIGNMENT_low       = directory+"output_assignment_low.csv"
            OUTPUT_ASSIGNMENT_random    = directory+"output_assignment_random.csv"

            for d in range(1, DELTA+1):             #{1,2,3,4,5} -> 5 slots per window
                if heuristic == "carbonshift":
                    OUTPUT_TIME = directory+"times_0"+str(d)+".csv"
                    compress_beta_blocks.clear_file(OUTPUT_TIME)#, OUTPUT_COMPRESSION)
                elif heuristic == "greedy":
                    OUTPUT_TIME = directory+"greedy_times_0"+str(d)+".csv"
                    compress_beta_blocks.clear_file_greedy(OUTPUT_TIME)


            for d in range(1, DELTA+1):     #{1,2,3,4,5}
                N_REQUESTS = 1024
                while N_REQUESTS<131073 :    #limit: 2^17+1
                    
                    INPUT_FILE          =f"test/window_{w}/input_requests/delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"                 

                    if heuristic == "carbonshift":
                        OUTPUT_FILE=directory+"delta_"+str(d)+"/input_"+str(N_REQUESTS)+".csv"

                        # d=slot, beta=blocks, ratio=1
                        # ratio=1 -> requests are not compressed
                        # ratio=2 -> requests are compressed in N_REQUESTS/blocks=2 each .. 
                        ratio = compress_beta_blocks.prepare_file(d, beta, 1, N_REQUESTS, INPUT_FILE, OUTPUT_FILE) #OUTPUT_COMPRESSION, INPUT_FILE, OUTPUT_FILE)
                        #print(f"Ratio: {ratio} for {N_REQUESTS} requests, delta={d}, beta={beta}, window={w}", flush=True)
                        

                        INPUT_FILE  = OUTPUT_FILE                           # File with the compressed requests into blocks      
                        #open(OUTPUT_ASSIGNMENT, 'w').close()

                        for i in range(1,11):     # iterate sequentially  
                            # Ensure OUTPUT_ASSIGNMENT is cleared before each run
                            if os.path.exists(OUTPUT_ASSIGNMENT):
                                os.remove(OUTPUT_ASSIGNMENT)

                            START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip()) # = time.time() gives negative values            
                            process = subprocess.Popen([
                                "python3.8","carbonshift.py",
                                INPUT_FILE,INPUT_STRATEGIES,INPUT_CO2, 
                                str(d),str(beta),str(ratio),str(error),
                                OUTPUT_ASSIGNMENT
                            #])
                            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                                                   
                                      
                            #
                            stdout, stderr = process.communicate()  # Waits for process to finish and captures output                    
                            #stdout, stderr = process.communicate(timeout=310)                            

                            """
                            try:
                                stdout, stderr = process.communicate(timeout=310)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                stdout, stderr = process.communicate()
                                print(f"Process exceeded 310 seconds and was terminated for {OUTPUT_ASSIGNMENT}", flush=True)
                            """


                            END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
                            #process.terminate() # terminate the process to avoid memory leaks
                            #print(f"Finished solver for {OUTPUT_ASSIGNMENT}. Return code: {process.returncode}", flush=True)
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

                                # Check if output file was written
                                if not os.path.exists(OUTPUT_ASSIGNMENT):
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

                    elif heuristic == "greedy":    
                        for i in range(1, 11):                                                  
                            process = subprocess.Popen([
                                "python3.8","greedy.py",
                                INPUT_FILE,INPUT_STRATEGIES,INPUT_CO2,                   
                                str(d),
                                OUTPUT_ASSIGNMENT_baseline,OUTPUT_ASSIGNMENT_mixed,
                                OUTPUT_ASSIGNMENT_high,OUTPUT_ASSIGNMENT_medium,
                                OUTPUT_ASSIGNMENT_low,OUTPUT_ASSIGNMENT_random
                            ])
                            process.wait()

                            OUTPUT_TIME = directory+"/greedy_times_0"+str(d)+".csv"
                            with open(OUTPUT_TIME, "a") as file_out:                
                                
                                comp_time       = ""
                                strategy        = ""
                                all_emissions   = ""
                                slot_emissions  = []
                                avg_errors      = ""
                                #slot_errors = []

                                output_files = [
                                    OUTPUT_ASSIGNMENT_baseline,OUTPUT_ASSIGNMENT_mixed,
                                    OUTPUT_ASSIGNMENT_high,OUTPUT_ASSIGNMENT_medium,
                                    OUTPUT_ASSIGNMENT_low,OUTPUT_ASSIGNMENT_random ]
                                
                                for f in output_files:
                                    with open(f, "r") as output_file:
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

                                        file_out.write(
                                            f"{N_REQUESTS},"
                                            f"{comp_time},"
                                            f"{strategy},"
                                            f"{all_emissions},"
                                            f"[{','.join(slot_emissions)}],"
                                            f"{avg_errors},"
                                            #f"[{','.join(slot_errors)}],"
                                            f"{i}\n"
                                        )
                    elif heuristic == "carbonstat": 
                        print("Heuristic not completely implemented yet")
                        #return 
                    
                        for i in range(1, 11):  
                            START = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())
                                                
                            process = subprocess.Popen([
                                "python3.8", 
                                "carbonstat.py",
                                #INPUT_FILE,                 # TBD: convert in the format the carbonstat.py needs
                                "input_requests_STAT.csv",
                                "input_strategies_STAT.csv", # make similar to input_strategies.csv 
                                str(error),
                                #OUTPUT_ASSIGNMENT
                                "output_assignment_STAT.csv"
                            ])
                            process.wait()

                            END = float(subprocess.check_output("date +%s.%N", shell=True).decode().strip())

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