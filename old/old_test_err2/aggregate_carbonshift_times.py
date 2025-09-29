# ------------------------
# move to test_err/ and
#    RUN with
# python3.8 aggregate_carbonshift_times.py 5 
# ------------------------ 

from argparse import ArgumentParser
import os
import csv


def clear_file(OUTPUT_TIME):                                    
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")                 
        file_out.write(f"all_requests,solver_status,computing_time,solve_time,all_emissions,slot_emissions,avg_errors\n")
        file_out.flush()

def clear_file_w(OUTPUT_TIME):                                    
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")   
        file_out.write(f"all_requests,solver_status,computing_time,solve_time,all_emissions,slot_emissions,avg_errors,window\n") 
        file_out.flush()

def main(DELTA):

    BETA = [100, 250, 500, 1000]     
    for w in range(0,DELTA):       #(0,5):                          # {0,1,2,3,4} -> 5 sliding windows
        for beta in BETA:
            directory                 = f"window_{w}/beta_{beta}/"

            AVG_TIME_01         = directory+"avg_times_01.csv"
            AVG_TIME_02         = directory+"avg_times_02.csv"
            AVG_TIME_03         = directory+"avg_times_03.csv"
            AVG_TIME_04         = directory+"avg_times_04.csv"
            AVG_TIME_05         = directory+"avg_times_05.csv"
            
            AVG_TIMES = [AVG_TIME_01,AVG_TIME_02,AVG_TIME_03,AVG_TIME_04,AVG_TIME_05]
            for a in AVG_TIMES:
                clear_file(a)

            OUT_TIME_01         = directory+"times_01.csv"
            OUT_TIME_02         = directory+"times_02.csv"
            OUT_TIME_03         = directory+"times_03.csv"
            OUT_TIME_04         = directory+"times_04.csv"
            OUT_TIME_05         = directory+"times_05.csv"
    
            OUT_TIMES = [OUT_TIME_01,OUT_TIME_02,OUT_TIME_03,OUT_TIME_04,OUT_TIME_05]

            for a_elem, t_elem in zip(AVG_TIMES, OUT_TIMES):
                if os.path.exists(t_elem):
                    
                    data_by_requests = {}                           # Read all rows and group by all_requests
                    with open(t_elem, "r", newline='') as infile:
                        next(infile)  # Skip header                       
                        for line in infile:
                            slot_emissions = line[line.find('['):line.find(']') + 1]
                            v = line.replace("\n", "").replace(slot_emissions, "").split(",")
                            slot_emissions  = [int(x) for x in slot_emissions.strip('[]').split(',')]  # Convert elements to integers

                            all_requests        = v[0]
                            solver_status       = v[1]
                            computing_time      = float(v[2])
                            solve_time          = float(v[3])
                            all_emissions       = float(v[4])                        
                            avg_errors          = float(v[6])
                            # iteration         = row[7]  # Not used for aggregation

                            if all_requests not in data_by_requests:
                                data_by_requests[all_requests] = {
                                    "solver_status": solver_status,
                                    "computing_time": [],
                                    "solve_time": [],
                                    "all_emissions": [],
                                    "slot_emissions": [],
                                    "avg_errors": []
                                }
                            data_by_requests[all_requests]["computing_time"].append(computing_time)
                            data_by_requests[all_requests]["solve_time"].append(solve_time)
                            data_by_requests[all_requests]["all_emissions"].append(all_emissions)
                            data_by_requests[all_requests]["slot_emissions"].append(slot_emissions)
                            data_by_requests[all_requests]["avg_errors"].append(avg_errors)

                    # Write aggregated results
                    with open(a_elem, "a", newline='') as outfile:
                        
                        for all_requests, vals in data_by_requests.items():
                            solver_status = vals["solver_status"]
                            avg_computing_time = sum(vals["computing_time"]) / len(vals["computing_time"])
                            avg_solve_time = sum(vals["solve_time"]) / len(vals["solve_time"])
                            avg_all_emissions = sum(vals["all_emissions"]) / len(vals["all_emissions"])       
                             

                            slot_lists = vals["slot_emissions"]     # Average each slot across all rows                                                        
                            if slot_lists:
                                slot_len = len(slot_lists[0])
                                avg_slot_emissions = []
                                for i in range(slot_len):
                                    avg_slot_emissions.append( sum(sl[i] for sl in slot_lists) / len(slot_lists))

                            avg_avg_errors = sum(vals["avg_errors"]) / len(vals["avg_errors"])
                            outfile.write(
                                f"{all_requests},"
                                f"{solver_status},"
                                f"{avg_computing_time:.4f},"
                                f"{avg_solve_time:.4f},"
                                f"{avg_all_emissions:.0f},"
                                f"{avg_slot_emissions},"
                                f"{avg_avg_errors:.2f}\n"
                            )

                # ------------------------
                # if w == 0 and beta == BETA[3]:  # Only for the first window and first beta value                    
                #    if os.path.exists(AVG_TIME_05):


    # ------------------------ 
    # Aggregate the results for a fixed β and Δ value
    # ------------------------   
    AGG_TIME    = f"agg_times_carbonshift.csv"
    clear_file_w(AGG_TIME)

    for w in range(0,DELTA):       #(0,5):                          # {0,1,2,3,4} -> 5 sliding windows
        directory               = f"window_{w}/"

        
        OUT_TIME                = directory+"/beta_"+str(BETA[3])+"/avg_times_05.csv"  # The last β value with the max window size Δ

        if os.path.exists(OUT_TIME):
            with open(OUT_TIME, "r", newline='') as infile, open(AGG_TIME, "a", newline='') as outfile:
                reader = csv.reader(infile)                    
                next(reader, None)              # Skip the header 
                writer = csv.writer(outfile)
                for row in reader:
                    writer.writerow(row+[w])    # Append the window number to each row

    # ------------------------    
    # Aggregate the results from all windows for a fixed number of requests and sliding window
    # ------------------------    
    FINAL_AGG_TIME    = f"all_agg_times_carbonshift.csv"
    clear_file(FINAL_AGG_TIME)

    if os.path.exists(AGG_TIME):
        with open(AGG_TIME, "r", newline='') as infile, open(FINAL_AGG_TIME, "a", newline='') as outfile:

            next(infile)  # Skip header                       
            agg_data = {} 

            for line in infile:

                slot_emissions = line[line.find('['):line.find(']') + 1]
                v = line.replace("\n", "").replace(slot_emissions, "").split(",")
                slot_emissions  = [float(x) for x in slot_emissions.strip('[]').split(',')]  # Convert elements to floats


                all_requests        = int(v[0])
                solver_status       = int(v[1])
                computing_time      = float(v[2])
                solve_time          = float(v[3])
                all_emissions       = float(v[4])                        
                avg_errors          = float(v[6])                
                window              = int(v[7])  

                if (
                    (all_requests==16384 and window==0) or 
                    (all_requests==32768 and window==1) or 
                    (all_requests==65536 and window==2) or
                    (all_requests==131072 and window==3) or
                    (all_requests==65536 and window==4)
                ):
                    #print(f"Processing: {all_requests}, {solver_status}, {computing_time}, {solve_time}, {all_emissions}, {slot_emissions}, {avg_errors}, {window}")

                    key = (all_requests, window)  # Aggregate by all_requests and window, matching the if assignment conditions
                    if key not in agg_data:
                        agg_data[key] = {
                            "all_requests": all_requests,
                            "computing_time": 0.0,
                            "solve_time": 0.0,
                            "all_emissions": 0.0,
                            "slot_emissions": None,
                            "avg_errors": []#,
                            #"window": window
                        }
                    agg_data[key]["computing_time"] += computing_time
                    agg_data[key]["solve_time"] += solve_time
                    agg_data[key]["all_emissions"] += all_emissions
                    if agg_data[key]["slot_emissions"] is None:
                        agg_data[key]["slot_emissions"] = slot_emissions
                    else:
                        agg_data[key]["slot_emissions"] = [
                            x + y for x, y in zip(agg_data[key]["slot_emissions"], slot_emissions)
                        ]
                    agg_data[key]["avg_errors"].append(avg_errors)

            for (all_requests, window), vals in agg_data.items():
                sum_computing_time = vals["computing_time"]
                sum_solve_time = vals["solve_time"]
                sum_all_emissions = vals["all_emissions"]
                sum_slot_emissions = vals["slot_emissions"]
                avg_avg_errors = sum(vals["avg_errors"]) / len(vals["avg_errors"])
                #window = vals["window"]
            
                outfile.write(
                    f"{all_requests},"
                    f"{solver_status},"
                    f"{sum_computing_time:.4f},"
                    f"{sum_solve_time:.4f},"
                    f"{sum_all_emissions:.0f},"
                    f"{sum_slot_emissions},"
                    f"{avg_avg_errors:.2f}\n"
                )


# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Aggregate carbonshift results.")

parser.add_argument('delta', type=int, help='Window size')

args = parser.parse_args()
main(args.delta)