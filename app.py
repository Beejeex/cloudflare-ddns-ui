from flask import Flask
from routes import register_routes
from updater import start_background_updater

app = Flask(__name__)
register_routes(app)
start_background_updater()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8080)
