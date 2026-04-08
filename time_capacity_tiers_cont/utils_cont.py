#!/usr/bin/env python3
"""
Utility functions for contiguous time slot assignment.

This module handles spanning of requests across contiguous time slots.
"""

import csv


def load_capacity_tiers(filepath):
    """Load capacity tiers from CSV file"""
    tiers = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                capacity = int(parts[0])
                factor = float(parts[1])
                tiers.append({'capacity': capacity, 'factor': factor})
    
    tiers.sort(key=lambda x: x['capacity'])
    return tiers


def get_emission_factor(load, tiers):
    """
    Calculate emission factor based on load and capacity tiers.
    
    Args:
        load: Number of requests assigned to the slot
        tiers: List of capacity tiers [{capacity, factor}, ...]
    
    Returns:
        Emission multiplication factor
    """
    if not tiers:
        return 1.0
    
    for tier in tiers:
        if load <= tier['capacity']:
            return tier['factor']
    
    return tiers[-1]['factor']


def try_assign_contiguous(block_size, strategy_duration, start_slot, residuals,
                         carbon, capacity_tiers, slot_duration_minutes, parallelism,
                         deadline_slot):
    """
    Try to assign a block starting from start_slot, spanning contiguously if needed.
    
    Args:
        block_size: Number of requests in the block
        strategy_duration: Duration of the strategy in minutes
        start_slot: Starting slot index
        residuals: List of residual time capacity per slot (in minutes)
        carbon: Carbon intensity per slot
        capacity_tiers: Capacity tier configuration
        slot_duration_minutes: Duration of each slot
        parallelism: Degree of parallelism
        deadline_slot: Latest slot allowed for this block
    
    Returns:
        (cost, feasible, slots_used, time_per_slot)
        - cost: Total cost if feasible, None if not
        - feasible: True if assignment is possible
        - slots_used: List of (slot_idx, time_used, load) tuples
        - time_per_slot: List of time used per slot (for state update)
    """
    # Calculate total duration needed
    total_duration = (block_size * strategy_duration) / parallelism
    
    # Track spanning across slots
    remaining_dur = total_duration
    slot = start_slot
    slots_used = []
    cost = 0.0
    
    # Try to fill contiguous slots
    while remaining_dur > 0 and slot < len(residuals):
        # Check deadline constraint
        if slot > deadline_slot:
            return None, False, [], []
        
        # How much capacity is available in this slot?
        available = residuals[slot]
        
        # How much do we use from this slot?
        used = min(available, remaining_dur)
        
        if used <= 0:
            # No capacity in this slot, try next
            slot += 1
            continue
        
        # Calculate load for this slot (for emission factor)
        # Load = requests being processed in this slot
        # Approximation: load ≈ used_time / slot_duration
        load = used / slot_duration_minutes * parallelism
        
        # Get emission factor based on load
        emission_factor = get_emission_factor(load, capacity_tiers)
        
        # Calculate cost contribution from this slot
        cost_contribution = carbon[slot] * used * emission_factor
        cost += cost_contribution
        
        # Record usage
        slots_used.append((slot, used, load))
        
        # Update remaining duration
        remaining_dur -= used
        slot += 1
    
    # Check if we could fit everything
    if remaining_dur > 0:
        # Could not fit within available slots
        return None, False, [], []
    
    # Success - create time_per_slot for state update
    time_per_slot = [0.0] * len(residuals)
    for slot_idx, time_used, _ in slots_used:
        time_per_slot[slot_idx] = time_used
    
    return cost, True, slots_used, time_per_slot


def apply_assignment(residuals, time_per_slot):
    """
    Apply an assignment by updating residual capacities.
    
    Args:
        residuals: List of residual capacities (modified in place)
        time_per_slot: List of time to subtract from each slot
    """
    for i in range(len(residuals)):
        residuals[i] -= time_per_slot[i]


def format_solution(assignments, strategies, blocks, carbon, capacity_tiers,
                   slot_duration_minutes, parallelism):
    """
    Format solution for display.
    
    Args:
        assignments: List of (block_idx, strategy_idx, start_slot, slots_used) tuples
        strategies: List of strategy dicts
        blocks: List of block dicts
        carbon: Carbon intensity per slot
        capacity_tiers: Capacity tier configuration
        slot_duration_minutes: Duration of each slot
        parallelism: Degree of parallelism
    
    Returns:
        Dictionary with formatted solution details
    """
    total_cost = 0.0
    total_error = 0.0
    slot_details = {}
    
    for block_idx, strategy_idx, start_slot, slots_used in assignments:
        block = blocks[block_idx]
        strategy = strategies[strategy_idx]
        block_size = block['size']
        
        # Track error
        total_error += strategy['error'] * block_size
        
        # Track cost and slot usage
        for slot_idx, time_used, load in slots_used:
            if slot_idx not in slot_details:
                slot_details[slot_idx] = {
                    'time_used': 0.0,
                    'requests': 0,
                    'blocks': []
                }
            
            slot_details[slot_idx]['time_used'] += time_used
            slot_details[slot_idx]['requests'] += load  # Approximation
            slot_details[slot_idx]['blocks'].append({
                'block_idx': block_idx,
                'time': time_used,
                'strategy': strategy['name'] if 'name' in strategy else f"S{strategy_idx}"
            })
            
            # Calculate cost
            emission_factor = get_emission_factor(load, capacity_tiers)
            cost_contribution = carbon[slot_idx] * time_used * emission_factor
            total_cost += cost_contribution
    
    return {
        'cost': total_cost,
        'error': total_error,
        'slot_details': slot_details,
        'assignments': assignments
    }


def calculate_initial_residuals(delta, slot_duration_minutes, parallelism):
    """
    Calculate initial residual capacities for all slots.
    
    Args:
        delta: Number of time slots
        slot_duration_minutes: Duration of each slot in minutes
        parallelism: Degree of parallelism
    
    Returns:
        List of residual capacities (all equal initially)
    """
    capacity_per_slot = slot_duration_minutes * parallelism
    return [capacity_per_slot] * delta


def validate_solution(assignments, blocks, strategies, delta, error_threshold,
                     slot_duration_minutes, parallelism):
    """
    Validate a solution for correctness.
    
    Checks:
    1. All blocks assigned exactly once
    2. No slot capacity violations
    3. Error threshold not exceeded
    4. No deadline violations
    
    Returns:
        (valid, error_messages)
    """
    errors = []
    
    # Check all blocks assigned
    assigned_blocks = set()
    for block_idx, _, _, _ in assignments:
        if block_idx in assigned_blocks:
            errors.append(f"Block {block_idx} assigned multiple times")
        assigned_blocks.add(block_idx)
    
    if len(assigned_blocks) != len(blocks):
        errors.append(f"Not all blocks assigned: {len(assigned_blocks)}/{len(blocks)}")
    
    # Check capacity violations
    slot_usage = [0.0] * delta
    for block_idx, strategy_idx, _, slots_used in assignments:
        for slot_idx, time_used, _ in slots_used:
            slot_usage[slot_idx] += time_used
    
    capacity_per_slot = slot_duration_minutes * parallelism
    for slot_idx, used in enumerate(slot_usage):
        if used > capacity_per_slot + 1e-6:  # Small tolerance for floating point
            errors.append(f"Slot {slot_idx} over capacity: {used:.2f} > {capacity_per_slot:.2f}")
    
    # Check error threshold
    total_requests = sum(b['size'] for b in blocks)
    total_error = 0.0
    for block_idx, strategy_idx, _, _ in assignments:
        block = blocks[block_idx]
        strategy = strategies[strategy_idx]
        total_error += strategy['error'] * block['size']
    
    avg_error = total_error / total_requests if total_requests > 0 else 0
    if avg_error > error_threshold + 1e-6:
        errors.append(f"Error threshold exceeded: {avg_error:.4f} > {error_threshold:.4f}")
    
    # Check deadlines
    for block_idx, _, start_slot, slots_used in assignments:
        block = blocks[block_idx]
        deadline = block['deadline']
        
        # Find last slot used
        if slots_used:
            last_slot = max(s[0] for s in slots_used)
            if last_slot > deadline:
                errors.append(f"Block {block_idx} deadline violated: last_slot={last_slot} > deadline={deadline}")
    
    return len(errors) == 0, errors
