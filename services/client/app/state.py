'''
State management for the carbonshift client application.
This holds current plans and their associated state. 
In emulation there typically is only one plan.

This holds time state too.
'''


from typing import Any, Optional

from carbonshift.services.client.app.tracker import RequestTracker


# Dictionary to store the state of each plan by its plan_id.
# each plan is made by a list of of requests keyed by their request_id.
plans: dict[int, dict[str, Any]] = {}  # Mapping from plan_id to its state
current_slot: int = 0

def store_plan(requests_spec: list[dict[str, Any]], slot_minutes: float,mode: str, executor_url: Optional[str]) -> int:
    plan_id = len(plans) + 1
    plans[plan_id] = {
        "requests": requests_spec.copy(),
        "slot_minutes": slot_minutes,
        "mode": mode,
        "executor_url": executor_url
    }
    # assign to plans[plan_id] a deep copy of the requests_spec with additional metadata
    import copy
    plans[plan_id]["requests_spec"] = copy.deepcopy(requests_spec)
    
    return plan_id

def get_plans_with_reqs_in_slot(slot: int) -> list[int]:
    """Return a list of plan_ids that have requests in the given slot."""
    result = []
    for plan_id, plan in plans.items():
        # TODO: is this the correct way to determine if a plan has requests in the given slot?
        # Should we flatten the slots first
        if "requests_spec" in plan and any(req.get("slot") == slot for req in plan["requests_spec"]):
            result.append(plan_id)
    return result

def get_plan(plan_id: int) -> Optional[dict[str, Any]]:
    return plans.get(plan_id)

def get_requests_from_plan(plan_id: int) -> Optional[list[dict[str, Any]]]:
    plan = get_plan(plan_id)
    if plan is None:
        return None
    return plan.get("requests")


def get_current_slot() -> int:
    return current_slot

def advance_slot() -> int:
    global current_slot
    current_slot += 1
    return current_slot

def force_set_current_slot(slot: int) -> None:
    global current_slot
    current_slot = slot
    return current_slot