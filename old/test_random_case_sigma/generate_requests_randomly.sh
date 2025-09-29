#!/bin/bash

# run as: 
# chmod +x ./generate_requests_randomly.sh
# ./generate_requests_randomly.sh

# NOTE: IN BASH GLI ARRAY SONO SPARSI


DELTA=4                                 # max proven delta
SIGMA=500                               # max proven sigma {1,500,1000}

for d in $(seq 2 $DELTA); do            # {2,3,4,5} skip 1 that would be the first INPUT_FILE line
    for (( N_REQUESTS=512; N_REQUESTS<65537; N_REQUESTS=N_REQUESTS*2)); do #limit: 2^16+1
        # fixed input file with randomly collected requests
        INPUT_FILE=sigma_1/delta_$d/input_$N_REQUESTS.csv     
        OUTPUT_FILE=sigma_$SIGMA/delta_$d/input_$N_REQUESTS.csv

        #value=$(less $FILE | tr -s ',' '\n' | grep $i | wc -l)

        printf "" > $OUTPUT_FILE
        head -n 1 $INPUT_FILE > $OUTPUT_FILE    # retrieve the first INPUT_FILE line

        index=0
        declare -a sum                          # declare sum as an array
        declare -a assignment                   # declare assignment as an array
        for i in $(seq 0 $((d-2))); do                
            assignment[$i]=0                    # initialize assignment array with 0
        done
        
        tail -n +2 $INPUT_FILE | while IFS=',' read -ra line_array; do
            for line in "${line_array[@]}"; do
                (( assignment[line]+=1 ))
            done
            
            for i in $(seq 0 $((d-2))); do      # interval of the forecasted starting from slot 1                
                while (( assignment[i]>0 )) ; do 
                    sum[$index]=$i          
                    ((index++))
                    ((assignment[i] -= SIGMA))
                done

                ((index-=1))
                for ((i=0; i<index; i++)); do
                    printf $((sum[i]))"," >> $OUTPUT_FILE
                done
                printf $((sum[index])) >> $OUTPUT_FILE 
            done
        done
        unset sum
        unset assignment
    done
done
