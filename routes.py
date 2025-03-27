from flask import render_template, request, redirect, url_for
from config import load_config, save_config, get_public_ip
from cloudflare_api import get_dns_record, get_zone_id, update_dns_record
from logger import log, read_recent_logs
import requests
import json

def register_routes(app):
    @app.route("/")
    def index():
        config = load_config()
        current_ip = get_public_ip()
        interval = config.get("interval", 300)

        record_data = []

        # Load stats from file
        stats = {}
        try:
            with open("logs/record_stats.json") as f:
                stats = json.load(f)
        except Exception:
            pass

        for record in config.get("records", []):
            dns = get_dns_record(config, record)
            stat = stats.get(record, {})
            last_checked = stat.get("last_checked")
            last_updated = stat.get("last_updated")
            updates = stat.get("updates", 0)
            failures = stat.get("failures", 0)

            if failures >= 3:
                health = "❌"
            elif failures > 0:
                health = "⚠️"
            else:
                health = "✅"

            is_up_to_date = dns and dns["content"] == current_ip
            status_text = f"{health} Up-to-date" if is_up_to_date else f"{health} Needs update"

            record_data.append({
                "name": record,
                "dns_ip": dns["content"] if dns else "Not Found",
                "status": status_text,
                "updates": updates,
                "failures": failures,
                "last_checked": last_checked,
                "last_updated": last_updated,
            })

        existing_records = []
        headers = {
            "Authorization": f"Bearer {config['api_token']}",
            "Content-Type": "application/json"
        }
        for domain, zone_id in config.get("zones", {}).items():
            url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
            params = {"type": "A"}
            r = requests.get(url, headers=headers, params=params)
            if r.ok:
                existing_records.extend(r.json().get("result", []))

        log_sections = read_recent_logs()
        config_logs = log_sections["config_logs"]
        api_logs = log_sections["api_logs"]

        return render_template("index.html",
            current_ip=current_ip,
            records=record_data,
            all_records=existing_records,
            api_token=config.get("api_token", ""),
            zones=json.dumps(config.get("zones", {})),
            refresh=config.get("refresh", 30),
            interval=interval,
            logs=config_logs,
            api_logs=api_logs
        )


    @app.route("/update-config", methods=["POST"])
    def update_config():
        config = load_config()
        config["api_token"] = request.form["api_token"]
        try:
            config["zones"] = json.loads(request.form["zones"])
        except Exception as e:
            log(f"❌ Failed to parse zones JSON: {e}")
            config["zones"] = {}
        config["refresh"] = int(request.form.get("refresh", 30))
        config["interval"] = int(request.form.get("interval", 300))
        save_config(config)
        log("Updated Cloudflare configuration.")
        return redirect(url_for("index"))

    @app.route("/clear-logs", methods=["POST"])
    def clear_logs():
        open("ddns.log", "w", encoding="utf-8").close()
        log("🧽 Logs cleared.")
        return redirect(url_for("index"))

    @app.route("/add-to-managed", methods=["POST"])
    def add_to_managed():
        config = load_config()
        record_name = request.form.get("record_name")
        if record_name and record_name not in config.get("records", []):
            config["records"].append(record_name)
            save_config(config)
            log(f"➕ Added '{record_name}' to managed records.")
        return redirect(url_for("index"))

    @app.route("/remove-from-managed", methods=["POST"])
    def remove_from_managed():
        config = load_config()
        record_name = request.form.get("record_name")
        if record_name and record_name in config.get("records", []):
            config["records"].remove(record_name)
            save_config(config)
            log(f"➖ Removed '{record_name}' from managed records.")
        return redirect(url_for("index"))

    @app.route("/delete-record", methods=["POST"])
    def delete_record():
        config = load_config()
        record_id = request.form.get("record_id")
        record_name = request.form.get("record_name")
        zone_id = get_zone_id(config, record_name)
        if not zone_id:
            return redirect(url_for("index"))

        headers = {
            "Authorization": f"Bearer {config['api_token']}",
            "Content-Type": "application/json"
        }
        url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
        log(f"[API] DELETE {url}")
        r = requests.delete(url, headers=headers)
        try:
            response_json = r.json()
            formatted_json = json.dumps(response_json, indent=2)
            log(f"[API] Response ({r.status_code}):\n{formatted_json}")
        except Exception:
            log(f"[API] Response ({r.status_code}):\n{r.text}")

        if r.ok and r.json().get("success"):
            log(f"🗑 Successfully deleted DNS record: {record_name}")
        else:
            log(f"❌ Failed to delete DNS record: {record_name}")

        return redirect(url_for("index"))

    @app.route("/create-managed", methods=["POST"])
    def create_managed():
        config = load_config()
        record_name = request.form.get("record_name")
        current_ip = get_public_ip()
        zone_id = get_zone_id(config, record_name)
        if not zone_id:
            return redirect(url_for("index"))

        data = {
            "type": "A",
            "name": record_name,
            "content": current_ip,
            "ttl": 1,
            "proxied": False
        }

        headers = {
            "Authorization": f"Bearer {config['api_token']}",
            "Content-Type": "application/json"
        }

        url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
        log(f"[API] POST {url} json={data}")
        r = requests.post(url, headers=headers, json=data)

        try:
            response_json = r.json()
            formatted_json = json.dumps(response_json, indent=2)
            log(f"[API] Response ({r.status_code}):\n{formatted_json}")
        except Exception:
            log(f"[API] Response ({r.status_code}):\n{r.text}")

        if record_name not in config.get("records", []):
            config["records"].append(record_name)
            save_config(config)
            log(f"📌 Added '{record_name}' to managed records after creation.")

        return redirect(url_for("index"))

    @app.route("/update", methods=["POST"])
    def update_record():
        config = load_config()
        record_name = request.form.get("record_name")
        current_ip = get_public_ip()
        dns_record = get_dns_record(config, record_name)
        if dns_record and dns_record["content"] != current_ip:
            log(f"Manual update triggered for {record_name}: {dns_record['content']} -> {current_ip}")
            response = update_dns_record(config, dns_record["id"], record_name, current_ip)
            if response.get("success"):
                log(f"✅ Manually updated {record_name} to {current_ip}.")
            else:
                log(f"❌ Manual update failed for {record_name}: {response}")
        return redirect(url_for("index"))
