#!/bin/bash

# run as: 
# chmod +x ./run_random_experiment.sh
# ./run_random_experiment.sh test/input_strategies_fixed.csv test/input_co2_fixed.csv 
# nohup ./run_random_experiment.sh test_random_case/input_strategies_fixed.csv test_random_case/input_co2_fixed.csv > log.txt &

#nohup ./run_random_experiment.sh test_random_case_beta/input_strategies_fixed.csv test_random_case_beta/input_co2_fixed.csv > log.txt &


#python3.8 carbon_aware_patterns.py test_random_case/sigma_500/delta_3/input_512.csv test_random_case/input_strategies_fixed.csv test_random_case/input_co2_fixed.csv 3 5 test_random_case/sigma_500/output_assignment.csv

# ---------
#SIGMA=1000                               # 1, 500, 1000
#OUTPUT_FILE=test_random_case/sigma_$SIGMA/output_assignments.csv # generic output file for carbonshift

OUTPUT_FILE=test_random_case_beta/ratio_/output_assignments.csv # generic output file for carbonshift
INPUT_STRATEGIES=$1                     # fixed 
INPUT_CO2=$2                            # fixed
ITERATIONS=10                           # nr. of iterations
ERROR=5                                 # fixed
DELTA=5                                 # max proven delta

for d in $(seq $DELTA)                  # {1,2,3,4,5}
do
    #OUTPUT_TIMES=test_random_case/sigma_$SIGMA/times_0$d.csv     # collected times of computing carbonshift
    OUTPUT_TIMES=test_random_case_beta/ratio_/times_0$d.csv     # collected times of computing carbonshift

    printf "all_requests,computing_time,solver_status,solve_time,objective_value,all_emissions,slot_emissions,all_errors,slot_errors,iteration\n" > $OUTPUT_TIMES

    # fixed input requests files for carbonshift 

    for (( N_REQUESTS=512; N_REQUESTS<65537; N_REQUESTS=N_REQUESTS*2)) #limit: 2^16+1
    do
        #INPUT_FILE=test_random_case/sigma_$SIGMA/delta_$d/input_$N_REQUESTS.csv      
        INPUT_FILE=test_random_case_beta/ratio_/delta_$d/input_$N_REQUESTS.csv      

        for i in $(seq $ITERATIONS)     # repeat sequentially at each iteration 
        do
            printf "" > $OUTPUT_FILE    # clear the output file
            START=$(date +%s.%N)

            # tested with Python 3.8.10
            python3.8 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE

            END=$(date +%s.%N)
            echo $N_REQUESTS','$(echo "$END - $START" | bc)','$(grep 'solver_status:' $OUTPUT_FILE)','$(grep 'solve_time:' $OUTPUT_FILE)','$(grep 'objective_value:' $OUTPUT_FILE)','$(grep 'all_emissions:' $OUTPUT_FILE)','$(grep 'slot_emissions:' $OUTPUT_FILE)','$(grep 'all_errors:' $OUTPUT_FILE)','$(grep 'slot_errors:' $OUTPUT_FILE)','$i >> $OUTPUT_TIMES
        done
    done
done


# TBD: post-process  file has to consider the new naming convention

# post-process into aggregate results TBD
#for d in $(seq $DELTA)                  # {1,2,3,4,5}
#do
#    # list the path to the CSV file where the collected times of computing carbonshift are stored.
#    OUTPUT_TIMES_LIST+=("test/times_0$d.csv")
#done
#python3.8 process_results.py $OUTPUT_TIMES_LIST 

# run as:
#python3.8 process_results.py test_random_case/times_01.csv test_random_case/times_02.csv test_random_case/times_03.csv test_random_case/times_04.csv test_random_case/times_05.csv
