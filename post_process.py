# ------------------------
# 
#    RUN with
# python3.8 post_process.py 
# ------------------------ 

from argparse import ArgumentParser
import os
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

def clear_file(OUTPUT_TIME):
    with open(OUTPUT_TIME, "w") as file_out:                
        file_out.write(f"")                 
        file_out.write(f"all_requests,policy,computing_time,all_emissions,slot_emissions,avg_errors\n")
        file_out.flush()

# ------------------------    
# Aggregate all the results 
# ------------------------    

def main():    
    names           = [
        "Baseline","Random","Naive Carbon",
        "Naive Error (ε=2)","Naive Error (ε=4)","Naive Error (ε=5)","Naive Shift",
        "Carbonshift (ε=2)","Carbonshift (ε=4)","Carbonshift (ε=5)"]            
    all_policies    = [
        "baseline","random","n_carbon",
        "n_err2","n_err4","n_err5","n_shift",
        "CS_err2","CS_err4","CS_err5"]

    ALL_baseline    = "test_greedy/all_agg_times_"+all_policies[0]+".csv"
    ALL_random      = "test_greedy/all_agg_times_"+all_policies[1]+".csv"
    ALL_n_carbon    = "test_greedy/all_agg_times_"+all_policies[2]+".csv"
    ALL_n_err2      = "test_greedy/all_agg_times_"+all_policies[3]+".csv"
    ALL_n_err4      = "test_greedy/all_agg_times_"+all_policies[4]+".csv"
    ALL_n_err5      = "test_greedy/all_agg_times_"+all_policies[5]+".csv"
    ALL_n_shift     = "test_greedy/all_agg_times_"+all_policies[6]+".csv"
    ALL_CS_err2     = "test_err2/all_agg_times_carbonshift.csv"
    ALL_CS_err4     = "test_err4/all_agg_times_carbonshift.csv"
    ALL_CS_err5     = "test_err5/all_agg_times_carbonshift.csv"    

    ALL_TIMES       = [
        ALL_baseline,ALL_random,ALL_n_carbon,
        ALL_n_err2,ALL_n_err4,ALL_n_err5,ALL_n_shift,
        ALL_CS_err2,ALL_CS_err4,ALL_CS_err5]

    all_policies_result = "all_policies_results.csv"
    clear_file(all_policies_result)

    for idx, filename in enumerate(ALL_TIMES):
        if os.path.exists(filename):
            with open(filename, "r", newline='') as infile, open(all_policies_result, "a", newline='') as outfile:      
                next(infile)  # Skip header                                       
                for line in infile:
                    slot_emissions = line[line.find('['):line.find(']') + 1]
                    v = line.replace("\n", "").replace(slot_emissions, "").split(",")
                    slot_emissions  = [int(float(x)) for x in slot_emissions.strip('[]').split(',')]  # Convert elements to floats
                    #slot_emissions = str(slot_emissions)  # Convert to string for CSV output    
                    

                    all_requests        = v[0]                    
                    policy              = names[idx]  # Extract policy name from filename QUI
                    if idx > 6:  # For carbon shift policies
                        computing_time      = float(v[2])
                        all_emissions       = float(v[4])                        
                        avg_errors          = float(v[6])
                    else:  # For greedy policies
                        computing_time      = float(v[1])
                        all_emissions       = float(v[2])                        
                        avg_errors          = float(v[4])

                
                    outfile.write(
                        f"{all_requests},"
                        f"{policy},"
                        f"{computing_time:.4f},"
                        f"{all_emissions:.0f},"
                        f"\"{slot_emissions}\","
                        f"{avg_errors:.2f}\n"
                    )

    # ------------------------    
    # Build the graphs 
    # ------------------------    
    show_together = False
    horizontal_bar = False

    df = pd.read_csv(all_policies_result)                   # Load the CSV file 

    agg_df = df.groupby('policy').agg(                      # Group by policy and compute total emissions and average error
        total_emissions=('all_emissions', 'sum'),
        avg_error=('avg_errors', 'mean')
    ).reset_index()
    
    policy_order = df['policy'].drop_duplicates().tolist()  # Preserve the policy order as in the original CSV
    agg_df['policy'] = pd.Categorical(agg_df['policy'], categories=policy_order, ordered=True)
    agg_df = agg_df.sort_values('policy')
    
    agg_df['policy_label'] = agg_df['policy']    
    fig, ax1 = plt.subplots(figsize=(14, 6))                # Create the plot

    if horizontal_bar:
        # Bar chart for total emissions
        bars = ax1.bar(agg_df['policy_label'], agg_df['total_emissions'], color='#709957')
        ax1.set_xticks(np.arange(len(agg_df['policy_label'])))
        ax1.set_xticklabels(agg_df['policy_label'], fontsize=21, rotation=30, ha='right')
        #ax1.set_xlabel('Policy')
        #ax1.set_ylabel('Total Emissions', color='black')
        ax1.tick_params(axis='y', labelcolor='black', labelsize=20)
        ax1.grid(axis='y', linestyle='--', linewidth=0.5)

        # Uncomment the following lines to annotate bars with values
        """
        for bar in bars:
            height = bar.get_height()
            ax1.annotate(f'{int(height):,}',                    # Format with thousands separator
                        xy=(bar.get_x() + bar.get_width() / 2, height),
                        xytext=(0, 3),                         # Vertical offset
                        textcoords="offset points",
                        ha='center', va='bottom', fontsize=9)
        """
        
        # Uncomment the following lines to add a line plot for average error
        if show_together:
            ax2 = ax1.twinx()                                       # Line plot for average error
            ax2.plot(agg_df['policy_label'], agg_df['avg_error'], color='#295A0D', marker='o', linestyle='--')
            ax2.set_ylabel('Average Error', color='#295A0D')
            ax2.tick_params(axis='y', labelcolor='#295A0D')
            ax2.set_ylim(0, 8)
            ax2.grid(False)
            fig.tight_layout()                                      # Adjust layout to prevent overlap
            plt.savefig("all_policies_results.pdf", format='pdf')

        else:        
            fig.tight_layout()                                      # Adjust layout to prevent overlap
            plt.savefig("all_policies_emissions_H.pdf", format='pdf')
    else:
        # Vertical bar chart for total emissions
        bars = ax1.barh(agg_df['policy_label'], agg_df['total_emissions'], color='#709957')
        ax1.set_yticks(np.arange(len(agg_df['policy_label'])))
        ax1.set_yticklabels(agg_df['policy_label'], fontsize=21)
        #ax1.set_ylabel('Policy')
        #ax1.set_xlabel('Total Emissions', color='black')
        ax1.tick_params(axis='x', labelcolor='black', labelsize=20)
        ax1.grid(axis='x', linestyle='--', linewidth=0.5)

        # Uncomment the following lines to annotate bars with values
        """
        for bar in bars:
            width = bar.get_width()
            ax1.annotate(f'{int(width):,}',                        # Format with thousands separator
                         xy=(width, bar.get_y() + bar.get_height() / 2),
                         xytext=(3, 0),                             # Horizontal offset
                         textcoords="offset points",
                         ha='left', va='center', fontsize=9)
        """
        ax1.invert_yaxis()  # Reverse the y-axis so the first policy appears at the top

        fig.tight_layout()                                      # Adjust layout to prevent overlap
        plt.savefig("all_policies_emissions_V.pdf", format='pdf')
    # ------------------------    
    
    avg_errors_df = df.groupby('policy').agg(              # Group by policy and compute average error
        avg_error=('avg_errors', 'mean')
    ).reset_index()
    
    policy_order = df['policy'].drop_duplicates().tolist()  # Preserve policy order as in the CSV
    avg_errors_df['policy'] = pd.Categorical(avg_errors_df['policy'], categories=policy_order, ordered=True)
    avg_errors_df = avg_errors_df.sort_values('policy')

    avg_errors_df['policy_label'] = avg_errors_df['policy']
    
    x = np.arange(len(avg_errors_df))                       # Prepare plot
    y = avg_errors_df['avg_error'].astype(float)
    fig, ax = plt.subplots(figsize=(14, 6))
    
    if horizontal_bar:

        bars = ax.bar(x, y, color='#709957')                  # Bar chart
        ax.set_xticks(x)
        ax.set_xticklabels(avg_errors_df['policy_label'], fontsize=21, rotation=30, ha='right')
        #ax.set_ylabel("Average Error", color='black')
        #ax.set_xlabel("Policy")
        ax.tick_params(axis='y', labelcolor='black', labelsize=20)
        ax.grid(axis='y', linestyle='--', linewidth=0.5)
        
        # Uncomment the following lines to annotate bars with values
        """    
        for bar in bars:                                        # Annotate value labels above bars
            height = bar.get_height()
            ax.annotate(f'{height:.2f}',
                        xy=(bar.get_x() + bar.get_width() / 2, height),
                        xytext=(0, 3),
                        textcoords="offset points",
                        ha='center', va='bottom', fontsize=9)
        """
        fig.tight_layout()
        plt.savefig("all_policies_errors_H.pdf", format='pdf')
    else:
        bars = ax.barh(x, y, color='#709957')                  # Horizontal bar chart
        ax.set_yticks(x)
        ax.set_yticklabels(avg_errors_df['policy_label'], fontsize=21)
        #ax.set_xlabel("Average Error", color='black')
        #ax.set_ylabel("Policy")
        ax.tick_params(axis='x', labelcolor='black', labelsize=20)
        ax.grid(axis='x', linestyle='--', linewidth=0.5)

        # Uncomment the following lines to annotate bars with values
        """
        for bar in bars:                                        # Annotate value labels next to bars
            width = bar.get_width()
            ax.annotate(f'{width:.2f}',
                        xy=(width, bar.get_y() + bar.get_height() / 2),
                        xytext=(3, 0),
                        textcoords="offset points",
                        ha='left', va='center', fontsize=9)
        """
        ax.invert_yaxis()
        fig.tight_layout()
        plt.savefig("all_policies_errors_V.pdf", format='pdf')








# ------------------------
#    RUN
# ------------------------ 
# Parse command-line arguments
parser = ArgumentParser("Aggregate all policies results.")
args = parser.parse_args()
main()