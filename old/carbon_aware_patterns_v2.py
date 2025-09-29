# versione con sigma per le forecasted requests

from argparse import ArgumentParser
from ortools.sat.python import cp_model
#import uuid

# max dimension for a block of forecasted requests 
# having the same expiring deadline
SIGMA = 1000

# Import requests' data
def import_input_requests(input_requests, delta):
    request_id = 0 #int(uuid.uuid4())               # make a random platform independent uuid 
    requests,i = [],0

    with open(input_requests, "r") as file:
        for line in file: 
            values = line.replace("\n","").split(",")
            requests.append([]) 
            for v in values:
                t = {}
                t['id'] = request_id
                t['deadline'] = int(v)

                if t['deadline'] >= delta: 
                    t['interval'] = list(range(i, delta))  #[arrival's slot, .., window's slot)
                else:                      
                    t['interval'] = list(range(i, i + int(v) + 1))  #[arrival's slot, .., deadline's slot]

                #print(t['id'], i, t['deadline'], len(t['interval']), t['interval'])
                request_id += 1
                requests[i].append(t)
            i += 1

    return requests

# Import strategies' data
def import_input_strategies(input_strategies):
    strategies = []

    with open(input_strategies, "r") as file:
        next(file) # skip the first line with the headers
        for line in file:
            values = line.replace("\n","").split(",")
            t = {}
            t['error'] = int(values[0])
            t['duration'] = int(values[1])
            strategies.append(t)

    return strategies

# Import carbon intensities' data
def import_input_carbon(input_co2):
    carbon = []

    with open(input_co2, "r") as file:
        for line in file:
            values = line.replace("\n","").split(",")
            carbon.append(int(values[0]))

    return carbon

# class to collect the results returned by the CPModel
class SolutionCollector(cp_model.CpSolverSolutionCallback):
    def __init__(self, config: dict):
        """
        Initializes the SolutionCollector with the given configuration.

        Args:
            config (dict): A dictionary containing the following keys:
                - 'assignment': The assignment variables.
                - 'all_slots': The range of all time slots.
                - 'all_strategies': The range of all strategies.
                - 'requests': The requests of each time slot.
        """
        super().__init__()
        self.__assignment = config['assignment']
        self.__all_slots = config['all_slots']
        self.__all_strategies = config['all_strategies']
        self.__requests = config['requests']

        self.__variables = [
            self.__assignment[(i, j, k['id'])]
            for j in self.__all_strategies
            for z in self.__all_slots
            for k in self.__requests[z]
            for i in k['interval']
        ]
        self.__solution_list = []
    
    def on_solution_callback(self):
        """
        This method is called by the solver when a new solution is found.
        It collects the current solution and appends it to the solution list.
        """
        new_solution = { "chosen": [] }

        for z in self.__all_slots:
            for j in self.__all_strategies:
                for k in self.__requests[z]:
                    for i in k['interval']:
                        new_s = {"time_slot":i,"strategy":j,"request_id":k['id']}
                        new_solution["chosen"].append(new_s)
        self.__solution_list.append(new_solution)
    
    def get_solutions(self):
        """ Returns the list of solutions found by the solver. """
        return self.__solution_list

# Compute emission due to choosing a strategy for a specific time slot
def emissions(carbon_intensity, strategy_duration):
    return carbon_intensity * strategy_duration

# Compute emissions of a given assignment
def assignment_emissions(assignment, carbon, strategy):
    co2 = 0
    co2 = sum(
        emissions(carbon[a["time_slot"]], strategy[a["strategy"]]["duration"])
        for a in assignment["chosen"]
    )
    return co2

def main(input_requests,input_strategies,input_co2,delta,error,output_assignment):
                                                                        
    num_slots               = delta #4                                  # Data          
    all_slots               = range(num_slots)                
    error_threshold         = error #5                                  
    strategies              = import_input_strategies(input_strategies) 
    num_strategies          = len(strategies)
    all_strategies          = range(num_strategies)                     
    carbon                  = import_input_carbon(input_co2)            
    requests                = import_input_requests(input_requests, delta)
    
    assert (delta <= len(carbon) and delta <= len(requests)), "Provide a carbon intensity per slot and at least one request"

    req_actual,req_forecast = len(requests[0]),0
    for k in requests:                                      # NOTE: 0 <= deadline < delta
        req_forecast += len(k)
    req_forecast -= req_actual

    model = cp_model.CpModel()                                          # Model

    # (1) boolean assignment[(i,j,k)] variables: request 'k' is set with strategy 'j' at slot 'i'
    """
    An assignment dictionary populated with variables for all slots, strategies, and requests, 
    can lead to a large number of variables being created, 
    which might be causing the solver to run out of memory or time. 
    Optimize by only creating variables for relevant combinations of slots, strategies, and requests.
    This is a common issue when working with large datasets, 
    that has been addressed by implementing the interval variable in the model.
    """
    # NOTE: HA SENSO interval per le forecasted se sono a blocchi?????
    assignment = {}
    for z in all_slots: 
        for k in requests[z]:
            for i in k['interval']:
                for j in all_strategies:
                    assignment[(i,j,k['id'])] = model.new_bool_var(f"assignment_d{i}_s{j}_n{k['id']}")
    
    # (3) request 'k' has exactly one slot 'i' and one strategy 'j' within deadline < window
    for z in all_slots:
        for k in requests[z]:   
            model.add_exactly_one(assignment[(i,j,k['id'])] for i in k['interval'] for j in all_strategies)

    # (2A) average precision for actual requests R[0] is at least error threshold
    max_weighted_error_threshold_actual = error_threshold * req_actual 
    model.Add(max_weighted_error_threshold_actual >= sum(
        assignment[(i,j,k['id'])] * strategies[j]['error']         
        for j in all_strategies
        for k in requests[0]
        for i in k['interval']
    ))

    # (2B) average precision for forecasted requests R[1, Delta-1] is at least error threshold
    max_weighted_error_threshold_forecast = error_threshold * req_forecast        
    model.Add(max_weighted_error_threshold_forecast >= sum(
        assignment[(i,j,k['id'])] * strategies[j]['error'] 
        for j in all_strategies
        for z in range(1,num_slots) 
        for k in requests[z]
        for i in k['interval'] 
    ))

    model.minimize(                                         # OBJECTIVE: minimize emission 
        sum(
            assignment[(i,j,k['id'])] * emissions(carbon[i], strategies[j]['duration'])
            for j in all_strategies                     
            for k in requests[0]            # 2: for each actual request
            for i in k['interval']          # 1: minimize within its living slot 
        )
        + SIGMA * sum(
            assignment[(i,j,k['id'])] * emissions(carbon[i], strategies[j]['duration'])
            for j in all_strategies
            for z in range(1,delta)         # 2: for each sigma block of forecasted requests 
            for k in requests[z]
            for i in k['interval']          # 1: minimize within its living slot
        )      
    )

    solver = cp_model.CpSolver()                            # Solver

    solver.parameters.max_time_in_seconds = 300.0    
    # Sets a time limit of 5 minutes before calling solver.solve to ensure the time limit is respected.

    config = {
        'assignment': assignment,
        'all_slots': all_slots,
        'all_strategies': all_strategies,
        'requests': requests
    }
    solution_collector = SolutionCollector(config)
    solver.parameters.enumerate_all_solutions = True
    status = solver.solve(model, solution_collector)
         
    elapsed_time = solver.UserTime()

    if status in [cp_model.UNKNOWN, cp_model.MODEL_INVALID, cp_model.FEASIBLE, 
                  cp_model.INFEASIBLE, cp_model.OPTIMAL]:
        status_messages = {
            cp_model.UNKNOWN: "Solver encountered an unknown problem!", # status 0 no conclusion, most likely due to numerical issues
            cp_model.MODEL_INVALID: "Model is invalid!",                # status 1
            cp_model.FEASIBLE: "Feasible solution found!",              # status 2        
            cp_model.INFEASIBLE: "No feasible solution found!",         # status 3
            cp_model.OPTIMAL: "Optimal solution found!"                 # status 4
        }
        print(f"{status_messages[status]} (status={status})") 
        with open(output_assignment, "w") as output_csv:
            output_csv.write(f"solver_status:{status}\n") 

            if status == cp_model.FEASIBLE:
                print(f"Number of solutions found: {len(solution_collector.get_solutions())}")                
                output_csv.write(
                    f"solve_time:{round(elapsed_time,4)}\n"
                    f"emissions:{solver.objective_value}\n")
                
            elif status == cp_model.INFEASIBLE:
                print("Debugg INFEASIBLE status: check for constraints, variables and input data!")

                output_csv.write(
                    f"solve_time:{round(elapsed_time,4)}\n"
                    f"emissions:{solver.objective_value}\n")

            elif status == cp_model.UNKNOWN:
                output_csv.write(
                    f"solve_time:{0.0}\n"
                    f"emissions:{0.0}\n")

            elif status == cp_model.OPTIMAL:
                all_errors = 0
                err_per_slot = [0 for _ in all_slots]
                all_emissions = 0
                co2_per_slot = [0 for _ in all_slots]

                output_csv.write(
                    f"Assignment solution for R[0]:\n"
                    f"request_id,strategy,time_slot,emission,error\n"
                )    
                for z in all_slots:
                    for k in requests[z]:
                        for j in all_strategies:
                            if solver.value(assignment[(z,j,k['id'])]):
                                err = strategies[j]['error']
                                err_per_slot[z] += err
                                all_errors += err
                                co2 = emissions(carbon[z],strategies[j]['duration'])
                                co2_per_slot[z] += co2
                                all_emissions += co2
                                # if z!=0 moltiplica le emissioni per sigma per 
                                # contare bene le emissioni delle forecasted
                                if z == 0:
                                    output_csv.write(f"{k['id']},{j},{z},{co2},{err}\n")
                                #else:
                                #    output_csv.write(f"{k['id']},{j},{z},{co2*SIGMA},{err}\n")
                                # per ora i test sono sulla versione 
                                # senza aver moltiplicato per SIGMA
                                # NOTE: aggiornare i test per SIGMA

                output_csv.write(
                    f"Statistics:\n"
                    f"R[0] max_weighted_error_threshold: {max_weighted_error_threshold_actual}\n"
                    f"R[1,Delta-1] max_weighted_error_threshold: {max_weighted_error_threshold_forecast}\n"
                    f"objective_value:{solver.objective_value}\n"
                    f"all_emissions:{all_emissions}\n"
                    f"slot_emissions:{co2_per_slot}\n"
                    f"all_errors:{all_errors}\n"
                    f"slot_errors:{err_per_slot}\n"
                    f"conflicts:{solver.num_conflicts},branches:{solver.num_branches},wall_time:{solver.wall_time}\n"
                    f"solve_time:{round(elapsed_time,4)}\n\n"
                )
    else:
        print("Bad status condition!")

# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Optimizer of assignments of requests to strategies and time slots.")
parser.add_argument('input_requests', type=str, help="File with requests' deadline data.")
parser.add_argument('input_strategies', type=str, help="File with statistics on strategies' errors and duration.")
parser.add_argument('input_co2', type=str, help="File with carbon intensities' data, per each slot.")
parser.add_argument('delta', type=int, help='Number of slots per window.')
parser.add_argument('error', type=int, help='Tolerated error (%).')
parser.add_argument('output_assignment', type=str, help='File where to write the output assignment.')
args = parser.parse_args()

main(args.input_requests,args.input_strategies,args.input_co2,args.delta,args.error,args.output_assignment)
