"""
Online2: Batch Scheduler for Carbon-Aware Scheduling

A production-ready system for scheduling requests in batches,
considering carbon emissions, capacity constraints, and error budgets.
"""

from shared_state import SharedSchedulerState, Request, Assignment
from request_generator import RequestGenerator
from scheduler import BatchScheduler
from main import Online2System

__all__ = [
    'SharedSchedulerState',
    'Request',
    'Assignment',
    'RequestGenerator',
    'BatchScheduler',
    'Online2System',
]

__version__ = '0.1.0'
