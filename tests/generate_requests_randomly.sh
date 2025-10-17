#!/bin/bash

# move to test/ and
#    RUN with
# chmod +x ./generate_requests_randomly.sh
# ./generate_requests_randomly.sh


DELTA=5                                 # max proven delta
SLICES=4

for w in $(seq 0 $SLICES)                 # {0,1,2,3,4} all sliding windows
do
    for d in $(seq 1 $DELTA)                # {0,1,2,3,4,5} all deltas slots
    do
        for (( N_REQUESTS=1024; N_REQUESTS<131073; N_REQUESTS=N_REQUESTS*2)) #limit: 2^17+1
        do
            # fixed input file with randomly collected requests for DELTA sliding windows
            INPUT_FILE=window_$w/input_requests/delta_$d/input_$N_REQUESTS.csv     
            printf ""> $INPUT_FILE    # the first time it is a cleaning 

            #for i in $(seq $d)
            #do
                for s in $(seq $(($N_REQUESTS - 1)))
                do                
                    #printf $(( RANDOM % ( $d - $i + 1) ))"," >> $INPUT_FILE 
                    printf $(( RANDOM % ( $d ) ))"," >> $INPUT_FILE 
                done
                #printf $(( RANDOM % ( $d - $i + 1) ))"\n" >> $INPUT_FILE # recover tot nr. of requests 
                printf $(( RANDOM % ( $d ) ))"\n" >> $INPUT_FILE # recover tot nr. of requests 
            #done
        done
    done
done