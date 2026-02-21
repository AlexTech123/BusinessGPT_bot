import os
import asyncio
import threading
import logging
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.fsm.storage.memory import MemoryStorage
from bot import router as bot_router

API_TOKEN = os.getenv("API_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(bot_router)

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

background_loop = asyncio.new_event_loop()
asyncio.set_event_loop(background_loop)

def start_background_loop():
    asyncio.set_event_loop(background_loop)
    background_loop.run_forever()

threading.Thread(target=start_background_loop, daemon=True).start()

@app.route("/set_webhook")
def set_webhook():
    """Установка вебхука"""
    future = asyncio.run_coroutine_threadsafe(
        bot.set_webhook(WEBHOOK_URL, allowed_updates=["message"]),
        background_loop
    )
    try:
        future.result()
        return jsonify({"status": "webhook set", "url": WEBHOOK_URL})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    """Прием обновлений от Telegram"""
    data = request.get_json(force=True)
    update = Update.model_validate(data)
    asyncio.run_coroutine_threadsafe(dp.feed_update(bot, update), background_loop)
    return "ok"

@app.route("/")
def index():
    return "Bot is running", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
