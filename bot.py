import os
import logging
import random
import aiohttp
import re
import asyncio
import time
from datetime import datetime
from collections import deque
from typing import Callable, Dict, Any, Awaitable

from aiogram import Router, Bot, Dispatcher, BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_CHAT_ID = -1002576074706

USER_MAPPING = {
    814759080: "A. H.",
    485898893: "Старый Мельник",
    1214336850: "Саня Блок",
    460174637: "Влад Блок",
    1313515064: "Булгак",
    1035739386: "Вован Крюк",
    407221863: "Некит Русанов",
    1878550901: "Егориус",
    924097351: "Александр Блок",
}

PERSONA_NAMES = list(USER_MAPPING.values())
BOT_USERNAME = "business_textbot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
CURRENT_TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))
CURRENT_CONTEXT_WINDOW = int(os.getenv("CONTEXT_WINDOW", "10"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")

admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

logger.info(f"THRESHOLD: {CURRENT_THRESHOLD}")
logger.info(f"TEMPERATURE: {CURRENT_TEMPERATURE}")
logger.info(f"CONTEXT_WINDOW: {CURRENT_CONTEXT_WINDOW}")
logger.info(f"ML_MODEL_URL: {ML_MODEL_URL}")
logger.info(f"ALLOWED CHAT ID: {ALLOWED_CHAT_ID}")

chat_histories: Dict[int, deque] = {}
api_lock = asyncio.Lock()
router = Router()

session_stats: Dict[str, Any] = {
    "user_messages": {},
    "bot_forced": 0,
    "bot_random": 0,
    "response_times": [],
    "started_at": datetime.now(),
}


async def start_dummy_server():
    app = web.Application()

    async def handle(request):
        return web.Response(text="Bot is running OK")

    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Dummy web server started on port {port}")


class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        if event.chat.id != ALLOWED_CHAT_ID:
            return

        user = event.from_user
        if user:
            logger.info(f"[ID] {user.full_name} | {user.id} | @{user.username}")

        if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            text = event.text or event.caption or ""

            if text and not text.strip().startswith("/"):
                clean_text = re.sub(f"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE).strip()
                clean_text = re.sub(r"\s+", " ", clean_text)

                if clean_text:
                    chat_id = event.chat.id
                    user_name = USER_MAPPING.get(user.id, user.full_name)

                    if chat_id not in chat_histories:
                        chat_histories[chat_id] = deque(maxlen=CURRENT_CONTEXT_WINDOW)

                    chat_histories[chat_id].append(f"[{user_name}]: {clean_text}")
                    session_stats["user_messages"][user_name] = session_stats["user_messages"].get(user_name, 0) + 1
                    logger.info(
                        f"[QUEUE] Context ({len(chat_histories[chat_id])} lines):\n"
                        + "\n".join(chat_histories[chat_id])
                    )

        return await handler(event, data)


router.message.middleware(HistoryMiddleware())


def process_model_output(generated_text: str) -> str | None:
    generated_text = generated_text.strip()
    if not generated_text:
        return None

    split_match = re.search(r"\n\[.*?\]:", generated_text)
    first_block = generated_text[: split_match.start()].strip() if split_match else generated_text.strip()

    if not first_block:
        return None

    match = re.match(r"^\[(.*?)\]:\s*(.*)", first_block, re.DOTALL)
    text = match.group(2).strip() if match else first_block

    return text.replace("@", "") or None


async def make_api_request(chat_id: int) -> str | None:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None

    context_string = "\n".join(chat_histories[chat_id]) + "\n"

    url = ML_MODEL_URL
    if not url.endswith("generate"):
        url = f"{url.rstrip('/')}/generate"

    timeout_settings = aiohttp.ClientTimeout(total=120, connect=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout_settings) as session:
            payload = {
                "prompt": context_string,
                "max_tokens": 256,
                "temperature": CURRENT_TEMPERATURE,
            }

            logger.info(f"Generating... (lock={api_lock.locked()})")
            start_time = time.time()

            async with session.post(url, json=payload) as response:
                duration = time.time() - start_time

                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    logger.info(f"Done in {duration:.2f}s. Raw: '{raw_text[:80]}...'")
                    return process_model_output(raw_text)

                logger.error(f"API Error {response.status}")
                return None

    except asyncio.TimeoutError:
        logger.error("API Timeout (>120s)")
        return None
    except Exception as e:
        logger.error(f"API Exception: {e}")
        return None


@router.message(Command("help"))
async def cmd_help(message: Message):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    text = (
        "📋 Commands:\n\n"
        "/threshold [0.0-1.0] — random response probability\n"
        "/temperature [0.0-2.0] — model creativity\n"
        "/context_window [1-30] — context size\n"
        "/status — current settings & state\n"
        "/clear — clear context\n"
        "/stats — session statistics\n"
        "/help — this message"
    )
    await message.reply(text)


@router.message(Command("threshold"))
async def cmd_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    if not command.args:
        await message.reply(f"Threshold: {CURRENT_THRESHOLD}")
        return

    try:
        value = float(command.args.replace(",", "."))
        if 0 <= value <= 1:
            CURRENT_THRESHOLD = value
            await message.reply(f"✅ Threshold: {CURRENT_THRESHOLD}")
        else:
            await message.reply("❌ 0.0 - 1.0")
    except ValueError:
        pass


@router.message(Command("temperature"))
async def cmd_temperature(message: Message, command: CommandObject):
    global CURRENT_TEMPERATURE
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    if not command.args:
        await message.reply(f"Temperature: {CURRENT_TEMPERATURE}")
        return

    try:
        value = float(command.args.replace(",", "."))
        if 0 <= value <= 2:
            CURRENT_TEMPERATURE = value
            await message.reply(f"✅ Temperature: {CURRENT_TEMPERATURE}")
        else:
            await message.reply("❌ 0.0 - 2.0")
    except ValueError:
        pass


@router.message(Command("context_window"))
async def cmd_context_window(message: Message, command: CommandObject):
    global CURRENT_CONTEXT_WINDOW
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    if not command.args:
        await message.reply(f"Context window: {CURRENT_CONTEXT_WINDOW}")
        return

    try:
        value = int(command.args)
        if 1 <= value <= 30:
            CURRENT_CONTEXT_WINDOW = value
            for cid in chat_histories:
                old = list(chat_histories[cid])
                chat_histories[cid] = deque(old[-value:], maxlen=value)
            await message.reply(f"✅ Context window: {CURRENT_CONTEXT_WINDOW}")
        else:
            await message.reply("❌ 1 - 30")
    except ValueError:
        pass


@router.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    queue_size = len(chat_histories.get(message.chat.id, []))
    lock_state = "busy" if api_lock.locked() else "free"

    text = (
        f"⚙️ Settings:\n"
        f"  Threshold: {CURRENT_THRESHOLD}\n"
        f"  Temperature: {CURRENT_TEMPERATURE}\n"
        f"  Context window: {CURRENT_CONTEXT_WINDOW}\n\n"
        f"📊 Context:\n"
        f"  Messages in queue: {queue_size}/{CURRENT_CONTEXT_WINDOW}\n"
        f"  API lock: {lock_state}"
    )
    await message.reply(text)


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    count = len(chat_histories.get(message.chat.id, []))
    chat_histories[message.chat.id] = deque(maxlen=CURRENT_CONTEXT_WINDOW)
    await message.reply(f"🗑 Cleared {count} messages from context")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    uptime = datetime.now() - session_stats["started_at"]
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)

    user_msgs = session_stats["user_messages"]
    total_user = sum(user_msgs.values())
    sorted_users = sorted(user_msgs.items(), key=lambda x: x[1], reverse=True)

    lines = [f"📈 Session stats (uptime: {hours}h {minutes}m)\n"]
    lines.append("Messages:")
    for name, count in sorted_users:
        lines.append(f"  {name}: {count}")
    lines.append(f"  Total: {total_user}\n")

    forced = session_stats["bot_forced"]
    rand = session_stats["bot_random"]
    lines.append(f"Bot responses: {forced + rand} (forced: {forced}, random: {rand})")

    times = session_stats["response_times"]
    if times:
        avg = sum(times) / len(times)
        lines.append(f"Avg response: {avg:.1f}s")
        lines.append(f"Fastest: {min(times):.1f}s | Slowest: {max(times):.1f}s")
    else:
        lines.append("No responses yet")

    await message.reply("\n".join(lines))


@router.message()
async def handle_messages(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    if message.text and message.text.strip().startswith("/"):
        return

    if (datetime.now(message.date.tzinfo) - message.date).total_seconds() > 120:
        return

    trigger_type = None
    bot_id = message.bot.id
    text = message.text or ""

    if message.reply_to_message and message.reply_to_message.from_user.id == bot_id:
        trigger_type = "forced"
    elif f"@{BOT_USERNAME}" in text.lower():
        trigger_type = "forced"
    else:
        if api_lock.locked():
            return
        if random.random() < CURRENT_THRESHOLD:
            trigger_type = "random"

    if not trigger_type:
        return

    if trigger_type == "random" and api_lock.locked():
        logger.info("Skip random: Busy")
        return

    if not (message.chat.id in chat_histories and chat_histories[message.chat.id]):
        return

    if trigger_type == "forced":
        await message.bot.send_chat_action(message.chat.id, "typing")

    start_time = time.time()
    async with api_lock:
        result_text = await make_api_request(message.chat.id)
    duration = time.time() - start_time

    if result_text:
        try:
            if trigger_type == "forced":
                await message.reply(result_text)
            else:
                await message.answer(result_text)

            persona = random.choice(PERSONA_NAMES)
            chat_histories[message.chat.id].append(f"[{persona}]: {result_text}")

            session_stats["response_times"].append(duration)
            if trigger_type == "forced":
                session_stats["bot_forced"] += 1
            else:
                session_stats["bot_random"] += 1

            logger.info("[QUEUE] Bot response added. Context:\n" + "\n".join(chat_histories[message.chat.id]))
        except Exception as e:
            logger.error(f"Failed to send: {e}")


async def main():
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)

    await start_dummy_server()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
