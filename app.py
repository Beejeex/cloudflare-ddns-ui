from flask import Flask
from routes import register_routes
from updater import start_background_updater
from logger import cleanup_old_logs, should_run_cleanup, log

app = Flask(__name__)

# Optional: Clean up logs on startup
if should_run_cleanup():
    cleanup_old_logs(days_to_keep=7)

# Log app startup
log("🟢 DDNS Dashboard started", level="INFO")

register_routes(app)
start_background_updater()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8080)
