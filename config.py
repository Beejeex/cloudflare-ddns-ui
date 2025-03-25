import os
import json
import requests

CONFIG_FILE = "config/config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"records": [], "api_token": "", "zones": {}, "refresh": 30}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def get_public_ip():
    return requests.get("https://api.ipify.org").text.strip()
