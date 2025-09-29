# ------------------------
# 
#    RUN with
# python3.8 simulated_scenario.py 
# ------------------------ 


import matplotlib.pyplot as plt

# Emissions data
x_emissions = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5]
y_emissions = [70, 119, 181, 147, 43, 70, 124, 147, 181]

# Request windows
request_windows_mixed = [
    (16384,   [0, 1, 2, 3, 4, 5], "window=0"),
    (32768,   [1, 2, 3, 4, 5, 6], "window=1"),
    (65536,   [2, 3, 4, 5, 6, 7], "window=2"),
    (131072,  [3, 4, 5, 6, 7, 8], "window=3"),
    (65536,   [4, 5, 6, 7, 8, 9], "window=4")
]

# X-axis ticks and labels
x_tick_positions = list(range(10))
slot_labels = [str(i) for i in range(10)]
midpoints = [(x_tick_positions[i] + x_tick_positions[i + 1]) / 2 for i in range(9)]

# Create figure and plot emissions
fig, ax1 = plt.subplots(figsize=(10, 6))
ax1.plot(x_emissions, y_emissions, color='green', marker='o')
ax1.set_ylabel("Carbon Intensity [gCO2-eq/kWh]", color='black', fontsize=14)
#ax1.set_xlabel("Time Slot", fontsize=16)
ax1.tick_params(axis='y', labelcolor='black', labelsize=12)
ax1.grid(True)
ax1.set_xlim(0, 9.5)

# X-ticks and centered labels
ax1.set_xticks(x_tick_positions)
ax1.set_xticklabels([""] * len(x_tick_positions))

for i, label in enumerate(slot_labels):
    xpos = midpoints[i] if i < len(midpoints) else 9.25
    ax1.text(xpos, ax1.get_ylim()[0] - 10, label, ha='center', va='top', fontsize=14)

# Time Slot label below slot numbers
ax1.text(4.75, ax1.get_ylim()[0] - 18, "Time Slot", ha='center', va='top', fontsize=16)

# Right Y-axis setup
ax2 = ax1.twinx()
ax2.set_ylabel("Requests", color='gray', fontsize=14)

# Format y-ticks in 'k'
def format_k(value, pos):
    return f'{int(value/1000)}k' if value >= 1000 else str(int(value))

ax2.yaxis.set_major_formatter(plt.FuncFormatter(format_k))
ax2.tick_params(axis='y', labelcolor='gray', labelsize=12)

# Plot request windows with [ and ] brackets
for y_val, x_vals, label in request_windows_mixed:
    xmin, xmax = min(x_vals), max(x_vals)
    ax2.hlines(y=y_val, xmin=xmin, xmax=xmax, color='gray', linestyle='--', alpha=0.6)
    ax2.text(xmin, y_val, '[', ha='center', va='center', fontsize=18, color='gray')
    ax2.text(xmax, y_val, ']', ha='center', va='center', fontsize=18, color='gray')
    
    # Add extra spacing for window=3 label
    vertical_offset = 0.01* y_val #0.05 * y_val if label == "window=3" else 0.02 * y_val
    x_center = (xmin + xmax) / 2
    ax2.text(x_center, y_val + vertical_offset, label, ha='center', va='bottom', fontsize=14, color='gray')

# Finalize and show
fig.tight_layout()
#plt.show()
plt.savefig("time_slot_emissions_requests.pdf", format='pdf')