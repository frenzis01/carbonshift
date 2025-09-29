#!/bin/bash

# the test directory has to be substituted with the directory where the test files are stored
# [RANDOM, WORST CASE]

# run as: 
# chmod +x ./run_experiment.sh
# ./run_experiment.sh test/input_strategies_fixed.csv test/input_co2_fixed.csv 
# nohup ./run_experiment.sh test/input_strategies_fixed.csv test/input_co2_fixed.csv > log.txt &

INPUT_FILE=test_worst_case_greedy/input_requests.csv      # generic input file for carbonshift
OUTPUT_FILE=test_worst_case_greedy/output_assignments.csv # generic output file for carbonshift
INPUT_STRATEGIES=$1                     # fixed 
INPUT_CO2=$2                            # fixed
ITERATIONS=1                           # nr. of iterations
ERROR=5                                 # fixed
DELTA=1                                 # max proven delta

for d in $(seq $DELTA)                  # {1,2,3,4,5}
do
    OUTPUT_TIMES=test_worst_case_greedy/times_0$d.csv     # collected times of computing carbonshift
    # no greedy solution
    #printf "input_total_reqs,computing_time,solver_status,solve_time,emissions,iteration\n" > $OUTPUT_TIMES
    
    # greedy solution
    printf "input_total_reqs,computing_time,errors,emissions,iteration\n" > $OUTPUT_TIMES

    for (( N_REQUESTS=8; N_REQUESTS<65537; N_REQUESTS=N_REQUESTS*2)) #limit: 2^16+1
    do
        printf ""> $INPUT_FILE    # the first time it is a cleaning 

        for i in $(seq $d)
        do
            for s in $(seq $(($N_REQUESTS - 1)))
            do
                printf $(( $d - $i ))"," >> $INPUT_FILE
            done
            printf $(( $d - $i ))"\n" >> $INPUT_FILE # recover tot nr. of requests 
        done

        for i in $(seq $ITERATIONS) # repeat sequentially at each iteration 
        do
            printf ""> $OUTPUT_FILE
            START=$(date +%s.%N)
            # no greedy solution
            #python3.8 carbon_aware_patterns.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE            
            
            # greedy solution
            timeout 5m python3.8 carbon_aware_patterns_greedy.py $INPUT_FILE $INPUT_STRATEGIES $INPUT_CO2 $d $ERROR $OUTPUT_FILE
            END=$(date +%s.%N)
            
            # no greedy solution
            #echo $N_REQUESTS','$(echo "$END - $START" | bc)','$(grep 'solver_status:' $OUTPUT_FILE)','$(grep 'solve_time:' $OUTPUT_FILE)','$(grep 'emissions:' $OUTPUT_FILE)','$i >> $OUTPUT_TIMES
            
            # greedy solution
            echo $N_REQUESTS','$(echo "$END - $START" | bc)','$(grep 'errors:' $OUTPUT_FILE)','$(grep 'emissions:' $OUTPUT_FILE)','$i >> $OUTPUT_TIMES
        done
    done
done

# post-process into aggregate results TBD
#for d in $(seq $DELTA)                  # {1,2,3,4,5}
#do
#    # list the path to the CSV file where the collected times of computing carbonshift are stored.
#    OUTPUT_TIMES_LIST+=("test/times_0$d.csv")
#done
#python3.8 process_results.py $OUTPUT_TIMES_LIST 

# run as:
#python3.8 process_results.py test_worst_case/times_01.csv test_worst_case/times_02.csv test_worst_case/times_03.csv test_worst_case/times_04.csv test_worst_case/times_05.csv
