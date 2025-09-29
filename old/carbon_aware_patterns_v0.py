# carbon_aware_patterns.py file without Copilot suggestions

from argparse import ArgumentParser
from ortools.sat.python import cp_model
import uuid

# import requests' data
def import_input_requests(input_requests):
    id = 0 #int(uuid.uuid4())               # make a random platform independent uuid 
    requests,i = [],0

    with open(input_requests, "r") as file:
        for line in list(file)[0:]:
            values = line.replace("\n","").split(",")
            requests.insert(i,[]) 
            for v in values:
                t,t["id"],t["deadline"] = {},id,int(v)
                id += 1
                requests[i].append(t)
            i += 1

    return requests

# import strategies' data
def import_input_strategies(input_strategies):
    strategies = []

    with open(input_strategies, "r") as file:
        for line in list(file)[1:]:
            values = line.replace("\n","").split(",")
            t,t["error"],t["duration"] = {},int(values[0]),int(values[1])
            strategies.append(t)

    return strategies

# import carbon intensities' data
def import_input_carbon(input_co2):
    carbon = []

    with open(input_co2, "r") as file:
        for line in list(file)[:]:
            values = line.replace("\n","").split(",")
            carbon.append(int(values[0]))

    return carbon

# class to collect the results returned by the CPModel
class SolutionCollector(cp_model.CpSolverSolutionCallback):
    def __init__(self,assignment,all_slots,all_strategies,requests):
        cp_model.CpSolverSolutionCallback.__init__(self)
        self.__variables = []                   # add all assignment's variables        
        self.__assignment = assignment
        self.__all_slots = all_slots
        self.__all_strategies = all_strategies
        self.__requests = requests

        for i in all_slots:
            for j in all_strategies:
                for k in requests[i]:
                    self.__variables.append(assignment[(i,j,k["id"])])
        self.__solution_list = []
    
    def on_solution_callback(self):
        new_solution,new_solution["chosen"] = {},[]

        for i in self.__all_slots:
            for j in self.__all_strategies:
                for k in self.__requests[i]:
                    if self.Value(self.__assignment[(i,j,k["id"])]):
                        new_s,new_s["time_slot"],new_s["strategy"],new_s["request_id"] = {},i,j,k["id"]
                        new_solution["chosen"].append(new_s)
        self.__solution_list.append(new_solution)
    
    def get_solutions(self):
        return self.__solution_list

# compute emissions due to choosing a strategy for a specific time_slot
def emissions(carbon_intensity, strategy_duration):
    return carbon_intensity * strategy_duration

# compute the emissions of a given assignment
def assignment_emissions(assignment, carbon, strategy):
    co2 = 0
    for a in assignment["chosen"]:
        carb = carbon[a["time_slot"]]
        strat=strategy[a["strategy"]]["duration"]
        e = emissions(carb,strat)
        co2 += e
    return co2

def main(input_requests,input_strategies,input_co2,delta,error,output_assignment):
                                                                        
    num_slots               = delta #4                                  # Data          
    all_slots               = range(num_slots)                
    error_threshold         = error #5                                  
    strategies              = import_input_strategies(input_strategies) 
    num_strategies          = len(strategies)
    all_strategies          = range(num_strategies)                     
    carbon                  = import_input_carbon(input_co2)            
    requests                = import_input_requests(input_requests)
    
    assert (delta <= len(carbon) and delta <= len(requests)), "Provide a carbon intensity per slot and at least one request" 

    req_actual,req_forecast = len(requests[0]),0
    for k in requests:                                      # NOTE: 0 <= deadline < delta
        req_forecast += len(k)
    req_forecast -= req_actual

    model = cp_model.CpModel()                                          # Model

    # (1) boolean assignment[(i,j,k)] variables: request 'k' is set with strategy 'j' at slot 'i'
    assignment = {}
    for z in all_slots: 
        for k in requests[z]:
            for i in all_slots:
                for j in all_strategies:
                    k_id = k["id"]
                    assignment[(i,j,k_id)] = model.new_bool_var(f"assignment_d{i}_s{j}_n{k_id}")

    # (1) request 'k' has exactly one slot 'i' and one strategy 'j' per window
    for z in all_slots:
        for k in requests[z]:
            model.add_exactly_one(assignment[(i,j,k["id"])] for i in all_slots for j in all_strategies)
    
    # (3) request 'k' has exactly one slot 'i' and one strategy 'j' within deadline < window
    for z in all_slots:
        for k in requests[z]:   
            d_min = min(z+k["deadline"], num_slots-1) 
            slots = range(z, d_min+1) 
            model.add_exactly_one(assignment[(i,j,k["id"])] for i in slots for j in all_strategies)

    # (2A) average precision for actual requests R[0] is at least error threshold
    max_weighted_error_threshold_actual = error_threshold * req_actual 
    model.Add(max_weighted_error_threshold_actual >= sum(
        assignment[(i,j,k["id"])] * strategies[j]["error"] 
        for i in all_slots
        for j in all_strategies
        for k in requests[0]
    ))

    # (2B) average precision for forecasted requests R[1, Delta-1] is at least error threshold
    max_weighted_error_threshold_forecast = error_threshold * req_forecast         
    model.Add(max_weighted_error_threshold_forecast >= sum(
        assignment[(i,j,k["id"])] * strategies[j]["error"] 
        for i in range(1, num_slots)
        for j in all_strategies
        for z in range(1,num_slots)
        for k in requests[z]
    ))

    model.minimize(                                         # OBJECTIVE: minimize emission 
        sum(
            assignment[(i,j,k["id"])] * emissions(carbon[i], strategies[j]["duration"])
            for i in all_slots          # 1: to check for each slot 
            for j in all_strategies
            for z in all_slots          # 2: to check for requests which are a list of requests
            for k in requests[z]
        )      
    )

    solver = cp_model.CpSolver()                            # Solver
    status = solver.solve(model)                   # Without collecting intermediate results
    
    """                                                # By collecting intermediate results
    solution_collector = SolutionCollector(assignment,all_slots,all_strategies,requests)
    #solver.parameters.enumerate_all_solutions = True    
    status = solver.solve(model,solution_collector)
    """
    solver.parameters.max_time_in_seconds = 300.0    # Sets a time limit of 5 minutes.
    elapsed_time = solver.UserTime()
                  
    if status != cp_model.OPTIMAL:                      # No optimal solution found
        print("No optimal solution found! (status=",status,")")
        return 

    err_per_slot = []
    for z in all_slots:
        err_per_slot.insert(z,0)

    with open(output_assignment, "w") as output_csv: # consider if append vs. write to the exit file
        output_csv.write("Max weighted error threshold for actual requests:"+str(max_weighted_error_threshold_actual)+"\t")
        output_csv.write("and for forecasted requests:"+str(max_weighted_error_threshold_forecast)+"\n")

        """
        output_csv.write("Solution for requests of R[0]:\nrequest_id,strategy,time_slot,emission,error\n")
        #print("Solution for actual requests R[0] only:")
        for i in all_slots:
            for j in all_strategies:
                for k in requests[0]:
                    if solver.value(assignment[(i,j,k["id"])]):
                        e = emissions(carbon[i],strategies[j]["duration"])
                        err_per_slot[i] += strategies[j]["error"]
                        #print(
                        #    "ID",k["id"],"with strategy",j,"at slot",i,"emission",e,
                        #    "and error",strategies[j]["error"]
                        #)
                        output_csv.write(str(k["id"])+","+str(j)+","+str(i)+","+str(e)+","+str(strategies[j]["error"])+"\n")
        """

        output_csv.write("Solution for all requests: request_id,strategy,time_slot,emission,error\n")    
        for z in all_slots:
            for k in requests[z]:
                for i in all_slots:
                    for j in all_strategies:
                            if solver.value(assignment[(i,j,k["id"])]):
                                e = emissions(carbon[i],strategies[j]["duration"])
                                err_per_slot[i] += strategies[j]["error"]
                                """ print(
                                    "ID",k["id"],"with strategy",j,"at slot",i,"emission",e,
                                    "and error",strategies[j]["error"]
                                ) """
                                output_csv.write(str(k["id"])+","+str(j)+","+str(i)+","+str(e)+","+str(strategies[j]["error"])+"\n")

        #print(f"Sum of emissions per requests met = {solver.objective_value}")
        output_csv.write("emissions:"+str(solver.objective_value)+"\n")

        #print(f"Sum of errors per slots met = {err_per_slot}")
        output_csv.write("Sum of errors per slots met ="+str(err_per_slot)+"\n")

        #print("\nStatistics"+f"\tconflicts: {solver.num_conflicts}"+f"\tbranches: {solver.num_branches}"+f"\twall time: {solver.wall_time}s\n")
        #print(f"  - solve time: {round(elapsed_time,4)}ms")

        output_csv.write("Statistics: conflicts,branches,wall_time\t")
        output_csv.write(str(solver.num_conflicts)+","+str(solver.num_branches)+","+str(solver.wall_time)+"\n")
        output_csv.write("solve_time:"+str(round(elapsed_time,4))+"\n\n") 


# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Optimizer of assignments of requests to strategies and time slots")
parser.add_argument('input_requests', type=str, help='File with requests'' deadline data')
parser.add_argument('input_strategies', type=str, help='File with statistics on strategies'' errors and duration')
parser.add_argument('input_co2', type=str, help='File with carbon intensities'' data, per each slot')
parser.add_argument('delta', type=int, help='Number of slots per window')
parser.add_argument('error', type=int, help='Tolerated error (%)')
parser.add_argument('output_assignment', type=str, help='File where to write the output assignment')
args = parser.parse_args()

main(args.input_requests,args.input_strategies,args.input_co2,args.delta,args.error,args.output_assignment)

"""
with timeout_results from bash: 
./run_experiment.sh test/input_strategies_fixed.csv test/input_co2_fixed.csv 
./run_experiment.sh: line 36: 361094 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 361258 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 361424 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 361610 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 361766 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 361932 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 362413 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 362604 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 362768 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 362935 Killed                  timeout 5m python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE

with timeout as a parameter of the solver:
...
./run_experiment.sh: line 36: 749158 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 749842 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 750510 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 751202 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 751920 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 752610 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 753294 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
No optimal solution found!
./run_experiment.sh: line 36: 802361 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 803101 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 803806 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 804500 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 805210 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 805913 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 806614 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 807312 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 808021 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
./run_experiment.sh: line 36: 808748 Killed                  python3 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
"""
