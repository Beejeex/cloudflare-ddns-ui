import os
import json
import requests
from logger import log

CONFIG_FILE = "config/config.json"

def ensure_config_defaults(config):
    config.setdefault("records", [])
    config.setdefault("zones", {})
    config.setdefault("api_token", "")
    config.setdefault("refresh", 30)
    config.setdefault("interval", 300)
    config.setdefault("ui_state", {
        "settings": True,
        "all_records": True,
        "logs": True
    })
    return config

def load_config():
    # Ensure config directory exists
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        log("⚠️ config.json does not exist", level="WARNING")
        config = ensure_config_defaults({})
        try:
            save_config(config)  # attempt to create it
        except Exception as e:
            log(f"⚠️ Could not create config.json: {e}", level="ERROR")
        return config

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            return ensure_config_defaults(config)
    except json.JSONDecodeError as e:
        log(f"⚠️ Failed to load config.json (corrupt?): {e}", level="ERROR")
        return ensure_config_defaults({})
    except Exception as e:
        log(f"⚠️ Unexpected error reading config: {e}", level="ERROR")
        return ensure_config_defaults({})

import tempfile

def save_config(config):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)

        # Use unique temp file
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_FILE), prefix="config_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        log(f"📁 Writing config to temp file: {temp_path}", level="INFO")
        os.replace(temp_path, CONFIG_FILE)
        log(f"✅ Replaced config with: {temp_path}", level="INFO")

    except Exception as e:
        log(f"⚠️ Failed to write config: {e}", level="ERROR")


def get_ui_state():
    config = load_config()
    return config.get("ui_state", {
        "settings": True,
        "all_records": True,
        "logs": True
    })

def set_ui_state(new_state):
    config = load_config()
    config["ui_state"] = new_state
    save_config(config)

def get_public_ip():
    try:
        return requests.get("https://api.ipify.org").text.strip()
    except Exception:
        return "Unavailable"
