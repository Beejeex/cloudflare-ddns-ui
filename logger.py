from datetime import datetime
import os

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "ddns.log")

def log(message):
    # Make sure the logs directory exists first
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    # Make sure the log file exists
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")

    # Now we can safely write to the log
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    print(message)
    
def read_recent_logs(lines=20):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return f.read().splitlines()[-lines:]
