import time
import threading
from config import load_config, get_public_ip
from cloudflare_api import get_dns_record, update_dns_record
from logger import log

def check_and_update():
    while True:
        config = load_config()
        current_ip = get_public_ip()

        for record_name in config.get("records", []):
            try:
                dns_record = get_dns_record(config, record_name)
                if dns_record:
                    if dns_record["content"] != current_ip:
                        log(f"IP mismatch for {record_name}: {dns_record['content']} -> {current_ip}. Updating...")
                        response = update_dns_record(config, dns_record["id"], record_name, current_ip)
                        if response.get("success"):
                            log(f"✅ Successfully updated {record_name} to {current_ip}.")
                        else:
                            log(f"❌ Failed to update {record_name}: {response}")
                    else:
                        log(f"{record_name} is already up to date.")
                else:
                    log(f"DNS record not found for {record_name}.")
            except Exception as e:
                log(f"⚠️ Error checking {record_name}: {e}")

        interval = config.get("interval", 300)
        log(f"🕒 Waiting {interval} seconds until next check...")
        time.sleep(interval)


def start_background_updater():
    threading.Thread(target=check_and_update, daemon=True).start()
