from datetime import datetime, timedelta
import os
import re

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "ddns.log")
CLEANUP_STATE_FILE = os.path.join(LOG_DIR, ".last_log_cleanup")

def log(message, level="INFO"):
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] {message}"

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

    print(log_line)

# --- Daily Cleanup Logic ---

def should_run_cleanup():
    if not os.path.exists(CLEANUP_STATE_FILE):
        return True
    try:
        with open(CLEANUP_STATE_FILE, "r") as f:
            last = datetime.fromisoformat(f.read().strip())
        return (datetime.now() - last).days >= 1
    except Exception:
        return True

def update_last_cleanup_timestamp():
    with open(CLEANUP_STATE_FILE, "w") as f:
        f.write(datetime.now().isoformat())

def cleanup_old_logs(days_to_keep=7):
    if not os.path.exists(LOG_FILE):
        return

    cutoff = datetime.now() - timedelta(days=days_to_keep)
    line_pattern = re.compile(r'^\[(?P<timestamp>[0-9:\-\s]+)\]')

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    kept_lines = []
    current_entry = []

    for line in lines:
        match = line_pattern.match(line)
        if match:
            if current_entry:
                kept_lines.extend(current_entry)
                current_entry = []

            try:
                timestamp_str = match.group("timestamp")
                entry_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                if entry_time >= cutoff:
                    current_entry = [line]
            except ValueError:
                continue
        else:
            if current_entry:
                current_entry.append(line)

    if current_entry:
        kept_lines.extend(current_entry)

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(kept_lines) + "\n")

    update_last_cleanup_timestamp()

# --- Log Reader ---

def read_recent_logs(config_log_lines=30):
    # ✅ Trigger cleanup once per day
    if should_run_cleanup():
        cleanup_old_logs(days_to_keep=7)

    if not os.path.exists(LOG_FILE):
        return {"config_logs": [], "api_logs": {}}

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        all_lines = f.read().splitlines()

    line_pattern = re.compile(
        r'^\[(?P<timestamp>[0-9:\-\s]+)\]\s*\[(?P<level>[^\]]+)\]\s*(?P<rest>.*)'
    )

    parsed_entries = []
    current_entry = None

    for line in all_lines:
        match = line_pattern.match(line)
        if match:
            if current_entry:
                current_entry["message"] = current_entry["message"].strip()
                parsed_entries.append(current_entry)

            timestamp = match.group("timestamp").strip()
            level = match.group("level").strip()
            rest = match.group("rest").strip()

            current_entry = {
                "timestamp": timestamp,
                "level": level,
                "is_api": False,
                "api_type": None,
                "record_name": None,
                "status_code": None,
                "message": rest,
                "raw_line": line,
            }

            if rest.startswith("[API] "):
                current_entry["is_api"] = True
                api_rest = rest[6:].strip()

                if api_rest.startswith("GET"):
                    current_entry["api_type"] = "GET"
                    current_entry["message"] = "API GET → " + api_rest[4:].strip()

                    # Extract record name from GET
                    name_match = re.search(r"name':\s*'([^']+)'", api_rest)
                    if name_match:
                        current_entry["record_name"] = name_match.group(1)

                elif api_rest.startswith("Response"):
                    current_entry["api_type"] = "RESPONSE"
                    status_match = re.search(r"\((\d+)\)", api_rest)
                    if status_match:
                        current_entry["status_code"] = int(status_match.group(1))
                    current_entry["message"] = "API " + api_rest
        else:
            if current_entry:
                cleaned_line = line.strip()
                if cleaned_line not in ("", "null", "{}", "[]"):
                    current_entry["message"] = (current_entry["message"].rstrip() + "\n" + cleaned_line).strip()
                    current_entry["raw_line"] += "\n" + cleaned_line

    if current_entry:
        current_entry["message"] = current_entry["message"].strip()
        parsed_entries.append(current_entry)

    # Split logs
    api_logs = {}
    config_logs = []

    for entry in reversed(parsed_entries):
        if entry["is_api"]:
            if entry["api_type"] == "RESPONSE":
                # Match with latest record_name from previous GET
                for prior in reversed(parsed_entries):
                    if (
                        prior["is_api"]
                        and prior["api_type"] == "GET"
                        and prior["timestamp"] < entry["timestamp"]
                        and prior["record_name"]
                    ):
                        record_name = prior["record_name"]
                        if record_name not in api_logs:
                            api_logs[record_name] = {
                                "get": prior,
                                "response": entry
                            }
                        break
        else:
            config_logs.append(entry)
            if len(config_logs) >= config_log_lines:
                break

    config_logs.reverse()
    return {"config_logs": config_logs, "api_logs": api_logs}
