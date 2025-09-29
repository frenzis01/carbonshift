# ------------------------
# move to test_greedy/ and
#    RUN with
# python3.8 aggregate_greedy_times.py 5 
# ------------------------ 

from argparse import ArgumentParser
import os
import csv

def clear_file(OUTPUT_TIME):
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")                 
        file_out.write(f"all_requests,computing_time,all_emissions,slot_emissions,avg_errors\n")
        file_out.flush()

def clear_file_w(OUTPUT_TIME):
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")                 
        file_out.write(f"all_requests,computing_time,strategy,all_emissions,slot_emissions,avg_errors,window\n")
        file_out.flush()


def main(DELTA):    
    policies                  = ["baseline","random","n_carbon","n_err2","n_err4","n_err5","n_shift"]            
    AGG_TIME_baseline         = "agg_times_"+policies[0]+".csv"
    AGG_TIME_random           = "agg_times_"+policies[1]+".csv"
    AGG_TIME_n_carbon         = "agg_times_"+policies[2]+".csv"
    AGG_TIME_n_err2           = "agg_times_"+policies[3]+".csv"
    AGG_TIME_n_err4           = "agg_times_"+policies[4]+".csv"
    AGG_TIME_n_err5           = "agg_times_"+policies[5]+".csv"
    AGG_TIME_n_shift          = "agg_times_"+policies[6]+".csv"

    AGG_TIMES = [AGG_TIME_baseline,AGG_TIME_random,AGG_TIME_n_carbon,AGG_TIME_n_err2,AGG_TIME_n_err4,AGG_TIME_n_err5,AGG_TIME_n_shift]
    for a in AGG_TIMES:
        clear_file_w(a)

    for w in range(0,DELTA):       #(0,5):                          # {0,1,2,3,4} -> 5 sliding windows
        directory               = f"window_{w}/"

        OUT_TIME_baseline       = directory+"times_"+policies[0]+".csv"
        OUT_TIME_random         = directory+"times_"+policies[1]+".csv"
        OUT_TIME_n_carbon       = directory+"times_"+policies[2]+".csv"
        OUT_TIME_n_err2         = directory+"times_"+policies[3]+".csv"
        OUT_TIME_n_err4         = directory+"times_"+policies[4]+".csv"
        OUT_TIME_n_err5         = directory+"times_"+policies[5]+".csv"
        OUT_TIME_n_shift        = directory+"times_"+policies[6]+".csv"
    
        OUT_TIMES = [OUT_TIME_baseline,OUT_TIME_random,OUT_TIME_n_carbon,OUT_TIME_n_err2,OUT_TIME_n_err4,OUT_TIME_n_err5,OUT_TIME_n_shift]

        for a_elem, t_elem in zip(AGG_TIMES, OUT_TIMES):
            if os.path.exists(t_elem):
                with open(t_elem, "r", newline='') as infile, open(a_elem, "a", newline='') as outfile:
                    reader = csv.reader(infile)                    
                    next(reader, None)              # Skip the header 
                    writer = csv.writer(outfile)
                    for row in reader:
                        writer.writerow(row+[w])    # Append the window number to each row


    # ------------------------    
    # Aggregate the results from all windows
    # ------------------------    

    FINAL_AGG_TIME_baseline         = "all_agg_times_"+policies[0]+".csv"
    FINAL_AGG_TIME_random           = "all_agg_times_"+policies[1]+".csv"
    FINAL_AGG_TIME_n_carbon         = "all_agg_times_"+policies[2]+".csv"
    FINAL_AGG_TIME_n_err2           = "all_agg_times_"+policies[3]+".csv"
    FINAL_AGG_TIME_n_err4           = "all_agg_times_"+policies[4]+".csv"
    FINAL_AGG_TIME_n_err5           = "all_agg_times_"+policies[5]+".csv"
    FINAL_AGG_TIME_n_shift          = "all_agg_times_"+policies[6]+".csv"

    FINAL_AGG_TIMES = [
        FINAL_AGG_TIME_baseline,FINAL_AGG_TIME_random,FINAL_AGG_TIME_n_carbon,
        FINAL_AGG_TIME_n_err2,FINAL_AGG_TIME_n_err4,FINAL_AGG_TIME_n_err5,FINAL_AGG_TIME_n_shift]
    for a in FINAL_AGG_TIMES:
        clear_file(a)

    for a_elem, t_elem in zip(AGG_TIMES, FINAL_AGG_TIMES):
        if os.path.exists(t_elem):
            with open(a_elem, "r", newline='') as infile, open(t_elem, "a", newline='') as outfile:

                next(infile)  # Skip header                       
                agg_data = {} 

                for line in infile:
                    slot_emissions = line[line.find('['):line.find(']') + 1]
                    v = line.replace("\n", "").replace(slot_emissions, "").split(",")
                    slot_emissions  = [float(x) for x in slot_emissions.strip('[]').split(',')]  # Convert elements to floats

                    all_requests        = int(v[0])
                    computing_time      = float(v[1])
                    #strategy           = v[2]          # Not used for aggregation 
                    all_emissions       = float(v[3])                        
                    avg_errors          = float(v[5])
                    window              = int(v[6])          

                    if (
                        (all_requests==16384 and window==0) or 
                        (all_requests==32768 and window==1) or 
                        (all_requests==65536 and window==2) or
                        (all_requests==131072 and window==3) or
                        (all_requests==65536 and window==4)
                    ):


                        key = (all_requests,window)  # Aggregate by all_requests and window, matching the if assignment conditions
                        if key not in agg_data:
                            agg_data[key] = {
                                "computing_time": 0.0,
                                "all_emissions": 0.0,
                                "slot_emissions": None,
                                "avg_errors": []
                            }
                        agg_data[key]["computing_time"] += computing_time
                        agg_data[key]["all_emissions"] += all_emissions
                        if agg_data[key]["slot_emissions"] is None:
                            agg_data[key]["slot_emissions"] = slot_emissions
                        else:
                            agg_data[key]["slot_emissions"] = [
                                x + y for x, y in zip(agg_data[key]["slot_emissions"], slot_emissions)
                            ]
                        agg_data[key]["avg_errors"].append(avg_errors)

                for (all_requests,window), vals in agg_data.items():
                    sum_computing_time = vals["computing_time"]
                    sum_all_emissions = vals["all_emissions"]
                    sum_slot_emissions = vals["slot_emissions"]
                    avg_avg_errors = sum(vals["avg_errors"]) / len(vals["avg_errors"])
                
                    outfile.write(
                        f"{all_requests},"
                        f"{sum_computing_time:.4f},"
                        f"{sum_all_emissions:.0f},"
                        f"{sum_slot_emissions},"
                        f"{avg_avg_errors:.2f}\n"
                    )




# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Aggregate greedy results.")

parser.add_argument('delta', type=int, help='Window size')

args = parser.parse_args()
main(args.delta)