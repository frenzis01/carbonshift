
Welcome to **CARBONSHIFT**! This guide will help you run different heuristic implementations and visualize results.

---

## 🚀 Quick Start

### 1. Run Multiple Instances

1. Activate venv environment
    -  source /home/ubuntu/ornela/carbonshift_/venv/bin/activate

2. Open `orchestrate_beta_blocks.py` and follow the instructions to execute various heuristics:
    - `carbonshift.py`
    - `greedy.py`

3. Aggregate average iterations values
    - move to each test_err and run with: python3.8 aggregate_carbonshift_times.py 5
    - move to test_greedy and run with: python3.8 aggregate_greedy_times.py 5

4. Generate result .csv file and splitted graphs :
    ```bash
    python3.8 post_process.py
    ```
5. Generate the all together graph
    ```bash
    python3.8 policies_results.py
    ```

6. Generate the percentage reductions graph
    ```bash
    python3.8 percentage_reductions.py
    ```

7. Generate the comparison reductions graph
    ```bash
    python3.8 comparison.py
    ```

8. Generate the normalized scatter plot
    ```bash
    python3.8 normalized_scatter_plot.py
    ```    


---

### 2. Run a Single Instance

Try out the paper example by customizing the parameters below:

| Parameter | Description                | Example Value |
|-----------|----------------------------|--------------|
| requests  | Input requests CSV         | `test_paper/input_requests.csv` |
| strategies| Strategies CSV             | `test_paper/input_strategies.csv` |
| co2       | CO2 data CSV               | `test_paper/input_co2.csv` |
| delta     | Delta parameter            | `1`, `3`     |
| beta      | Beta parameter             | `10`, `5` |
| error     | Error parameter            | `5`          |
| output    | Output CSV                 | `test_paper/output_assignment.csv` |

**Example commands:**

```bash

python3.8 greedy.py \
  test_paper/input_requests_greedy.csv \
  test_paper/input_strategies.csv \
  test_paper/input_co2.csv \
  3 \
  test_paper/output_assignment_baseline.csv \
  test_paper/output_assignment_random.csv \
  test_paper/output_assignment_n_carbon.csv \
  test_paper/output_assignment_n_err2.csv \
  test_paper/output_assignment_n_err4.csv \
  test_paper/output_assignment_n_err5.csv \
  test_paper/output_assignment_nshift.csv

python3.8 carbonshift.py \
  test_paper/input_requests_beta5.csv \
  test_paper/input_strategies.csv \
  test_paper/input_co2.csv \
  3 5 5 \
  test_paper/output_assignment_beta5.csv

python3.8 carbonshift.py \
  test_paper/input_requests_beta10.csv \
  test_paper/input_strategies.csv \
  test_paper/input_co2.csv \
  1 10 5 \
  test_paper/output_assignment_beta10.csv
```

---

## 💡 Tips

- Modify input files and parameters to experiment with different scenarios.
- Use the graph scripts to visualize and compare results.
- For more details, check comments in each script.

---

**Ready to get started?**  
Choose your workflow above and run the commands in your terminal!
