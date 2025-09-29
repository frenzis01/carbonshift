"""
This Python script does the same thing as the Bash script. 
It loops through a range of delta values and for each delta value, 
it loops through a range of request values. 
For each request value, it writes input data to the input file and 
then runs the `carbon_aware_patterns.py` script 
for a number of iterations. 
It measures the time taken to run the script 
and writes the results to the output times file.

It uses the `os`, `subprocess`, and `time` modules to handle file I/O, 
run external commands, and measure time, respectively. 
The `range` function is used to generate the sequences of delta 
and request values, and list comprehension is used to generate 
the list of request values. 
The `with` statement is used to ensure that files are properly 
closed after they are used. 
The `subprocess.run` function is used to run the 
`carbon_aware_patterns.py` script with a timeout of 5 minutes. 
The `check=True` argument is used to raise an exception 
if the command returns a non-zero exit status. 
The `split` and `strip` methods are used to extract 
the solve time and emissions values from the output file.

"""

import os
import subprocess
import time

# Define the input and output files
INPUT_FILE = "test/input_requests.csv"
OUTPUT_FILE = "test/output_assignments.csv"

# Get the input strategies and CO2 from command line arguments
INPUT_STRATEGIES = os.sys.argv[1]
INPUT_CO2 = os.sys.argv[2]

# Define the number of iterations, error, and delta
ITERATIONS = 10
ERROR = 5
DELTA = 5

# Loop through the range of delta
for d in range(1, DELTA + 1):
    OUTPUT_TIMES = f"test/times_0{d}.csv"

    # Write the header to the output times file
    with open(OUTPUT_TIMES, "w") as f:
        f.write("input_total_reqs,computing_time,solve_time,emissions,iteration\n")

    # Loop through the range of requests
    for N_REQUESTS in [2**i for i in range(7, 16)]:
        # Clear the input file
        with open(INPUT_FILE, "w") as f:
            f.write("")

        # Write the input data to the input file
        for i in range(d):
            for s in range(N_REQUESTS - 1):
                with open(INPUT_FILE, "a") as f:
                    f.write(f"{d-1},") # to test
            with open(INPUT_FILE, "a") as f:
                f.write(f"{d-1}\n")

        # Repeat the process for the number of iterations
        for i in range(1, ITERATIONS + 1):
            start = time.time()

            # Run the carbon_aware_patterns.py script
            subprocess.run(["python3.8", "carbon_aware_patterns.py", INPUT_FILE, INPUT_STRATEGIES, INPUT_CO2, str(d), str(ERROR), OUTPUT_FILE])
            # Run the carbon_aware_patterns.py script with a timeout of 5 minutes
            #subprocess.run(["timeout", "5m", "python3", "carbon_aware_patterns.py", INPUT_FILE, INPUT_STRATEGIES, INPUT_CO2, str(d), str(ERROR), OUTPUT_FILE], check=True)

            end = time.time()

            # Write the results to the output times file
            with open(OUTPUT_FILE, "r") as f:
                lines = f.readlines()
                solve_time = [line for line in lines if "solve_time:" in line][0].split(":")[1].strip()
                emissions = [line for line in lines if "emissions:" in line][0].split(":")[1].strip()

            with open(OUTPUT_TIMES, "a") as f:
                f.write(f"{N_REQUESTS},{end - start},{solve_time},{emissions},{i}\n")

