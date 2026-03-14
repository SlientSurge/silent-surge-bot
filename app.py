from flask import Flask, request
import requests
import os

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, json=payload)

@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    message = f"Trading Signal:\n{data}"
    send_telegram(message)
    return {"status": "ok"}

@app.route("/", methods=["GET"])
def home():
    return "Silent Surge Bot Running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
