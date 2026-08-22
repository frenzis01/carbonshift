from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "tests" / "battery" / "results"

PATTERN = re.compile(
    r"^cfg_test1_([A-Za-z0-9]+)_(\d{4})_(\d{4})(?:_\d{2})?"
)

PATTERN = re.compile(
    r"^cfg_test1_([A-Za-z0-9_]+)_(\d{4})_(\d{4})"
)

DATE_LOWER_BOUND = "0820"
TIME_LOWER_BOUND = "1100"
DATE_UPPER_BOUND = None
TIME_UPPER_BOUND = None
RENAME_STRING = "cfg_test2_"


def parse_bound(value):
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def parse_folder_name(folder_name):
    match = PATTERN.match(folder_name)
    if not match:
        return None
    _, date_part, time_part, *_ = match.groups()
    return int(date_part), int(time_part)


def is_within_range(date_value, time_value):
    lower_date = parse_bound(DATE_LOWER_BOUND)
    lower_time = parse_bound(TIME_LOWER_BOUND)
    upper_date = parse_bound(DATE_UPPER_BOUND)
    upper_time = parse_bound(TIME_UPPER_BOUND)

    if lower_date is not None:
        if date_value < lower_date:
            return False
        if date_value == lower_date and lower_time is not None and time_value < lower_time:
            return False

    if upper_date is not None:
        if date_value > upper_date:
            return False
        if date_value == upper_date and upper_time is not None and time_value > upper_time:
            return False
    elif upper_time is not None and time_value > upper_time:
        return False

    if lower_date is None and lower_time is not None and time_value < lower_time:
        return False

    return True


def find_matching_folders(directory):
    matches = []
    if not directory.exists():
        return matches

    for item in sorted(directory.iterdir()):
        if not item.is_dir():
            continue

        parsed = parse_folder_name(item.name)
        if parsed is None:
            continue

        date_value, time_value = parsed
        if is_within_range(date_value, time_value):
            matches.append(item)

    return matches


def rename_matching_folders(directory):
    matches = find_matching_folders(directory)

    print(f"Trovate {len(matches)} directory corrispondenti.")
    if not matches:
        print("Nessuna directory trovata con il pattern richiesto.")
        return []

    for folder in matches:
        print(f"- {folder.name}")

    renamed = []
    for folder in matches:
        new_name = re.sub(r"^cfg_test1_", RENAME_STRING, folder.name, count=1)
        if new_name == folder.name:
            continue
        target = folder.with_name(new_name)
        if target.exists():
            print(f"Saltata: {folder.name} -> {target.name} (nome già esistente)")
            continue
        folder.rename(target)
        renamed.append(target)
        print(f"Rinominata: {folder.name} -> {target.name}")

    return renamed


if __name__ == "__main__":
    rename_matching_folders(RESULTS_DIR)