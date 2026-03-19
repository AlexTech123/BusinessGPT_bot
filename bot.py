import os
import logging
import random
import math
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
from aiogram.utils.chat_action import ChatActionSender
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
MAX_MESSAGE_CHARS = 700

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
silent_mode = False
router = Router()

game_data: Dict[int, dict] = {}
GROW_COOLDOWN = 12 * 3600
FIGHT_COOLDOWN = 5 * 60
LOTTERY_COOLDOWN = 24 * 3600
last_lottery_global = 0.0


def get_or_create_player(user_id: int, name: str) -> dict:
    if user_id not in game_data:
        game_data[user_id] = {
            "name": name,
            "size": round(random.uniform(5.0, 15.0), 1),
            "wins": 0,
            "losses": 0,
            "last_grow": 0.0,
            "last_fight": 0.0,
        }
    return game_data[user_id]


def make_dick_visual(size: float) -> str:
    length = max(1, int(size / 3))
    return "8" + "=" * length + "D"

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

        text = event.text or ""
        is_silent_cmd = text.strip().lower() == "/silent"

        if silent_mode and not is_silent_cmd:
            return

        if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            raw = event.text or event.caption or ""

            if raw and not raw.strip().startswith("/"):
                clean_text = re.sub(f"@{BOT_USERNAME}", "", raw, flags=re.IGNORECASE).strip()
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

    match = re.match(r"^\[.*?\]:\s*(.*)", generated_text, re.DOTALL)
    text = match.group(1).strip() if match else generated_text.strip()

    return text.replace("@", "") or None


async def make_api_request(chat_id: int) -> str | None:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None

    context_lines = "\n".join(chat_histories[chat_id])
    if len(context_lines) > MAX_MESSAGE_CHARS:
        context_lines = context_lines[-MAX_MESSAGE_CHARS:]
        newline_pos = context_lines.find("\n")
        if newline_pos != -1:
            context_lines = context_lines[newline_pos + 1:]
    context_string = (
        f"<|im_start|>user\n{context_lines}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )

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
        "⚙️ Admin:\n"
        "/threshold [0.0-1.0] — random response probability\n"
        "/temperature [0.0-2.0] — model creativity\n"
        "/context_window [1-30] — context size\n"
        "/silent — заглушить/разглушить бота целиком\n"
        "/status — current settings & state\n"
        "/clear — clear context\n"
        "/stats — session statistics\n"
        "/help — this message\n\n"
        "🍆 Growing Dick:\n"
        "/dick — показать свой размер\n"
        "/grow — попробовать вырастить (раз в 12ч)\n"
        "/fight — дуэль (реплай, кулдаун 5м)\n"
        "/gift [см] — подарить (реплай)\n"
        "/lottery — лотерея (раз в 24ч, все скидываются)\n"
        "/top — таблица лидеров"
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

    silent_state = "ON 🔇" if silent_mode else "OFF 🔊"

    text = (
        f"⚙️ Settings:\n"
        f"  Threshold: {CURRENT_THRESHOLD}\n"
        f"  Temperature: {CURRENT_TEMPERATURE}\n"
        f"  Context window: {CURRENT_CONTEXT_WINDOW}\n"
        f"  Silent mode: {silent_state}\n\n"
        f"📊 Context:\n"
        f"  Messages in queue: {queue_size}/{CURRENT_CONTEXT_WINDOW}\n"
        f"  API lock: {lock_state}"
    )
    await message.reply(text)


@router.message(Command("silent"))
async def cmd_silent(message: Message):
    global silent_mode
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return

    silent_mode = not silent_mode
    state = "ON 🔇" if silent_mode else "OFF 🔊"
    await message.reply(f"Silent mode: {state}")


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


@router.message(Command("dick"))
async def cmd_dick(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    user = message.from_user
    name = USER_MAPPING.get(user.id, user.full_name)
    player = get_or_create_player(user.id, name)

    visual = make_dick_visual(player["size"])
    await message.reply(
        f"🍆 {player['name']}\n"
        f"Размер: {player['size']} см\n"
        f"{visual}\n"
        f"Победы: {player['wins']} | Поражения: {player['losses']}"
    )


@router.message(Command("grow"))
async def cmd_grow(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    user = message.from_user
    name = USER_MAPPING.get(user.id, user.full_name)
    player = get_or_create_player(user.id, name)

    now = time.time()
    elapsed = now - player["last_grow"]
    if elapsed < GROW_COOLDOWN:
        remaining = GROW_COOLDOWN - elapsed
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        await message.reply(f"⏳ Кулдаун! Попробуй через {hours}ч {minutes}м")
        return

    base = random.uniform(-3.0, 5.0)
    multiplier = random.choice([0.5, 1.0, 1.0, 1.0, 1.5, 2.0])
    change = round(base * multiplier, 1)

    old_size = player["size"]
    player["size"] = max(1.0, round(old_size + change, 1))
    player["last_grow"] = now

    actual_change = round(player["size"] - old_size, 1)
    visual = make_dick_visual(player["size"])

    if actual_change > 0:
        emoji = "📈"
        sign = "+"
    elif actual_change < 0:
        emoji = "📉"
        sign = ""
    else:
        emoji = "😐"
        sign = ""

    mult_str = f" (x{multiplier})" if multiplier != 1.0 else ""

    await message.reply(
        f"{emoji} {player['name']}\n"
        f"Бросок: {sign}{actual_change} см{mult_str}\n"
        f"Размер: {player['size']} см\n"
        f"{visual}"
    )


@router.message(Command("fight"))
async def cmd_fight(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    user = message.from_user
    attacker_name = USER_MAPPING.get(user.id, user.full_name)
    attacker = get_or_create_player(user.id, attacker_name)

    now = time.time()
    elapsed = now - attacker["last_fight"]
    if elapsed < FIGHT_COOLDOWN:
        remaining = int(FIGHT_COOLDOWN - elapsed)
        minutes = remaining // 60
        seconds = remaining % 60
        await message.reply(f"⏳ Кулдаун! Попробуй через {minutes}м {seconds}с")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("⚔️ Реплайни на сообщение соперника!")
        return

    opp = message.reply_to_message.from_user
    if opp.is_bot:
        await message.reply("🤖 С ботом драться нельзя!")
        return

    opponent_id = opp.id
    opponent_name = USER_MAPPING.get(opp.id, opp.full_name)

    if opponent_id == user.id:
        await message.reply("🤦 Нельзя драться с самим собой!")
        return

    defender = get_or_create_player(opponent_id, opponent_name)

    atk_roll = round(random.uniform(0, 100), 1)
    def_roll = round(random.uniform(0, 100), 1)
    atk_bonus = round(math.log2(max(attacker["size"], 1)) * 5, 1)
    def_bonus = round(math.log2(max(defender["size"], 1)) * 5, 1)
    atk_power = round(atk_roll + atk_bonus, 1)
    def_power = round(def_roll + def_bonus, 1)

    transfer = round(random.uniform(1.0, 3.0), 1)

    if atk_power >= def_power:
        winner, loser = attacker, defender
        winner_label, loser_label = attacker["name"], defender["name"]
    else:
        winner, loser = defender, attacker
        winner_label, loser_label = defender["name"], attacker["name"]

    loser_old = loser["size"]
    loser["size"] = max(1.0, round(loser["size"] - transfer, 1))
    actual_loss = round(loser_old - loser["size"], 1)
    winner["size"] = round(winner["size"] + actual_loss, 1)

    winner["wins"] += 1
    loser["losses"] += 1
    attacker["last_fight"] = now

    await message.reply(
        f"⚔️ {attacker['name']} 🎲{atk_roll}+{atk_bonus}={atk_power}\n"
        f"⚔️ {defender['name']} 🎲{def_roll}+{def_bonus}={def_power}\n\n"
        f"🏆 Победил {winner_label}!\n"
        f"  +{actual_loss} см → {winner['size']} см {make_dick_visual(winner['size'])}\n"
        f"😢 Проиграл {loser_label}:\n"
        f"  -{actual_loss} см → {loser['size']} см {make_dick_visual(loser['size'])}"
    )


@router.message(Command("gift"))
async def cmd_gift(message: Message, command: CommandObject):
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    user = message.from_user
    name = USER_MAPPING.get(user.id, user.full_name)
    player = get_or_create_player(user.id, name)

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("🎁 Реплайни на сообщение получателя: /gift 3")
        return

    opp = message.reply_to_message.from_user
    if opp.is_bot:
        await message.reply("🤖 Боту дарить нельзя!")
        return

    if opp.id == user.id:
        await message.reply("🤦 Нельзя дарить самому себе!")
        return

    if not command.args:
        await message.reply("🎁 Напиши сколько дарить: /gift 3")
        return

    try:
        amount = round(float(command.args.replace(",", ".")), 1)
    except ValueError:
        await message.reply("❌ Напиши число: /gift 3")
        return

    if amount <= 0:
        await message.reply("❌ Количество должно быть больше 0")
        return

    max_gift = round(player["size"] - 1.0, 1)
    if amount > max_gift:
        await message.reply(f"❌ Максимум: {max_gift} см (минимум 1.0 см остаётся)")
        return

    recipient_name = USER_MAPPING.get(opp.id, opp.full_name)
    recipient = get_or_create_player(opp.id, recipient_name)

    player["size"] = round(player["size"] - amount, 1)
    recipient["size"] = round(recipient["size"] + amount, 1)

    await message.reply(
        f"🎁 {player['name']} → {recipient['name']}\n"
        f"Передано: {amount} см\n\n"
        f"{player['name']}: {player['size']} см {make_dick_visual(player['size'])}\n"
        f"{recipient['name']}: {recipient['size']} см {make_dick_visual(recipient['size'])}"
    )


@router.message(Command("lottery"))
async def cmd_lottery(message: Message):
    global last_lottery_global
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    if len(game_data) < 2:
        await message.reply("🎰 Нужно минимум 2 игрока! Пусть все напишут /dick")
        return

    now = time.time()
    elapsed = now - last_lottery_global
    if elapsed < LOTTERY_COOLDOWN:
        remaining = LOTTERY_COOLDOWN - elapsed
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        await message.reply(f"⏳ Лотерея уже была! Следующая через {hours}ч {minutes}м")
        return

    last_lottery_global = now

    entry_fee = 1.0
    pot = 0.0
    contributors = []

    for uid, p in game_data.items():
        if p["size"] > 1.0:
            take = min(entry_fee, round(p["size"] - 1.0, 1))
            p["size"] = round(p["size"] - take, 1)
            pot = round(pot + take, 1)
            contributors.append(p["name"])

    if pot == 0:
        await message.reply("🎰 Ни у кого нет лишних см для взноса!")
        return

    winner_id = random.choice(list(game_data.keys()))
    winner = game_data[winner_id]
    winner["size"] = round(winner["size"] + pot, 1)

    lines = [
        f"🎰 ЛОТЕРЕЯ!\n",
        f"Взнос: {entry_fee} см с каждого",
        f"Участники: {len(contributors)}",
        f"Банк: {pot} см\n",
        f"🏆 Победитель: {winner['name']}!",
        f"  +{pot} см → {winner['size']} см {make_dick_visual(winner['size'])}",
    ]
    await message.reply("\n".join(lines))


@router.message(Command("top"))
async def cmd_top(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    if not game_data:
        await message.reply("🏆 Ещё никто не играет! Напиши /dick чтобы начать")
        return

    sorted_players = sorted(game_data.values(), key=lambda p: p["size"], reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Топ игроков:\n"]
    for i, p in enumerate(sorted_players):
        medal = medals[i] if i < 3 else f"{i + 1}."
        lines.append(
            f"{medal} {p['name']} — {p['size']} см "
            f"{make_dick_visual(p['size'])} "
            f"({p['wins']}W/{p['losses']}L)"
        )

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

    start_time = time.time()
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
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
