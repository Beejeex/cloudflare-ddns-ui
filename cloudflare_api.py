import json
import requests
import tldextract
from logger import log
from config import get_public_ip

def get_zone_id(config, record_name):
    ext = tldextract.extract(record_name)
    base_domain = f"{ext.domain}.{ext.suffix}"
    zone_id = config.get("zones", {}).get(base_domain)
    if not zone_id:
        log(f"❌ No zone ID configured for base domain: {base_domain}")
    return zone_id

def get_dns_record(config, record_name):
    zone_id = get_zone_id(config, record_name)
    if not zone_id:
        return None

    headers = {
        "Authorization": f"Bearer {config['api_token']}",
        "Content-Type": "application/json"
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    params = {"type": "A", "name": record_name}
    log(f"[API] GET {url} params={params}")
    r = requests.get(url, headers=headers, params=params)

    try:
        response_json = r.json()
        formatted_json = json.dumps(response_json, indent=2)
        log(f"[API] Response ({r.status_code}):\n{formatted_json}")
    except Exception:
        log(f"[API] Response ({r.status_code}):\n{r.text}")

    r_json = r.json()
    if r_json.get("result"):
        return r_json["result"][0]
    return None

def update_dns_record(config, record_id, record_name, new_ip):
    zone_id = get_zone_id(config, record_name)
    if not zone_id:
        return {"success": False, "errors": ["Zone ID not found"]}

    headers = {
        "Authorization": f"Bearer {config['api_token']}",
        "Content-Type": "application/json"
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    data = {
        "type": "A",
        "name": record_name,
        "content": new_ip,
        "ttl": 1,
        "proxied": False
    }
    log(f"[API] PUT {url} json={data}")
    r = requests.put(url, headers=headers, json=data)
    log(f"[API] Response: {r.status_code} {r.text}")
    return r.json()
