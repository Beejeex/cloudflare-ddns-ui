import time
import threading
import json
import os
from datetime import datetime
from config import load_config, get_public_ip
from cloudflare_api import get_dns_record, update_dns_record
from logger import log

STATS_DIR = "logs"
STATS_FILE = os.path.join(STATS_DIR, "record_stats.json")

def ensure_stats_file():
    if not os.path.exists(STATS_DIR):
        os.makedirs(STATS_DIR)
    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w") as f:
            json.dump({}, f)

def load_stats():
    ensure_stats_file()
    try:
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"⚠️ Failed to load stats: {e}", level="ERROR")
        return {}

def save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log(f"⚠️ Failed to save stats: {e}", level="ERROR")

def check_and_update():
    log("🔁 Background updater started", level="INFO")

    while True:
        config = load_config()
        current_ip = get_public_ip()
        stats = load_stats()

        for record_name in config.get("records", []):
            try:
                dns_record = get_dns_record(config, record_name)

                if record_name not in stats:
                    stats[record_name] = {
                        "last_checked": None,
                        "last_updated": None,
                        "updates": 0,
                        "failures": 0
                    }

                stats[record_name]["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if dns_record:
                    if dns_record["content"] != current_ip:
                        log(f"IP mismatch for {record_name}: {dns_record['content']} -> {current_ip}. Updating...")
                        response = update_dns_record(config, dns_record["id"], record_name, current_ip)
                        if response.get("success"):
                            log(f"✅ Successfully updated {record_name} to {current_ip}.")
                            stats[record_name]["updates"] += 1
                            stats[record_name]["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            log(f"❌ Failed to update {record_name}: {response}", level="ERROR")
                            stats[record_name]["failures"] += 1
                    else:
                        log(f"{record_name} is already up to date.")
                else:
                    log(f"DNS record not found for {record_name}.", level="WARNING")
                    stats[record_name]["failures"] += 1

            except Exception as e:
                log(f"⚠️ Error checking {record_name}: {e}", level="ERROR")
                stats[record_name]["failures"] += 1

        save_stats(stats)

        interval = config.get("interval", 300)
        log(f"🕒 Waiting {interval} seconds until next check...")
        time.sleep(interval)

def start_background_updater():
    threading.Thread(target=check_and_update, daemon=True).start()
