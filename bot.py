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

INITIAL_SIZES: Dict[int, float] = {
    # user_id: size_cm — проставить вручную после рестарта
}

PERSONA_NAMES = list(USER_MAPPING.values())
BOT_USERNAME = "business_textbot"
MAX_MESSAGE_CHARS = 700

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.08"))
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
auto_delete_seconds = 0
router = Router()


async def reply_game(message: Message, text: str):
    sent = await message.reply(text, disable_notification=True)
    if auto_delete_seconds > 0:
        logger.info(f"[AUTODELETE] Scheduled bot={sent.message_id} user={message.message_id} in {auto_delete_seconds}s")
        asyncio.create_task(_delete_later(sent, message, auto_delete_seconds))
    return sent


async def _delete_later(bot_msg: Message, user_msg: Message, delay: int):
    await asyncio.sleep(delay)
    for tag, msg in [("bot", bot_msg), ("user", user_msg)]:
        try:
            await msg.delete()
            logger.info(f"[AUTODELETE] Deleted {tag}={msg.message_id}")
        except Exception as e:
            logger.error(f"[AUTODELETE] Failed {tag}={msg.message_id}: {e}")

game_data: Dict[int, dict] = {}
cooldowns = {
    "grow": 12 * 3600,
    "fight": 10 * 60,
    "lottery": 24 * 3600,
    "buy": 3600,
}
last_lottery_global = 0.0

SHOP_ITEMS = {
    "condom": {"price_pct": 0.03, "desc": "защита в бою"},
    "viagra": {"price_pct": 0.03, "desc": "x2 забор"},
    "lube": {"price_pct": 0.02, "desc": "grow без минуса"},
}

GROW_POS = [
    "{n} вырос на {d} см! Теперь {s} см 📈",
    "{n} прибавил {d} см! Теперь {s} см 📈",
    "У {n} +{d} см! Теперь {s} см 📈",
    "{n} отрастил {d} см! Теперь {s} см 📈",
    "{n} подрос на {d} см! Теперь {s} см 📈",
    "+{d} см для {n}! Теперь {s} см 📈",
    "{n} нарастил {d} см! Теперь {s} см 📈",
    "{n} окреп на {d} см! Теперь {s} см 📈",
    "У {n} прирост {d} см! Теперь {s} см 📈",
    "{n} набрал {d} см! Теперь {s} см 📈",
]
GROW_NEG = [
    "{n} усох на {d} см. Теперь {s} см 📉",
    "{n} потерял {d} см. Теперь {s} см 📉",
    "У {n} -{d} см. Теперь {s} см 📉",
    "{n} скукожился на {d} см. Теперь {s} см 📉",
    "{n} сдулся на {d} см. Теперь {s} см 📉",
    "-{d} см у {n}. Теперь {s} см 📉",
    "{n} уменьшился на {d} см. Теперь {s} см 📉",
    "{n} просел на {d} см. Теперь {s} см 📉",
    "У {n} убыло {d} см. Теперь {s} см 📉",
    "{n} растерял {d} см. Теперь {s} см 📉",
]
GROW_ZERO = [
    "{n} остался при своём. {s} см 😐",
    "Без изменений. {s} см 😐",
    "Ничего не произошло. {s} см 😐",
    "{n} потоптался на месте. {s} см 😐",
    "Пусто. {n} всё ещё {s} см 😐",
    "{n} замер на {s} см 😐",
]
GROW_LUBE = [
    "Смазка спасла {n} от потерь! {s} см 🧴",
    "{n} намазался и не усох! {s} см 🧴",
    "Lube сработал — {n} не потерял ничего! {s} см 🧴",
    "{n} проскользнул мимо потерь! {s} см 🧴",
    "Смазка защитила {n}! Всё ещё {s} см 🧴",
]
FIGHT_CHALLENGE = [
    "{a} вызвал {d} на бой!",
    "{a} бросил вызов {d}!",
    "{a} напал на {d}!",
    "{a} наехал на {d}!",
    "{a} полез на {d}!",
    "{a} кинул перчатку {d}!",
    "Внимание! {a} против {d}!",
    "{a} решил помериться с {d}!",
]
FIGHT_WIN = [
    "{w} победил и забрал {t} см 💥",
    "{w} выиграл и отжал {t} см 💥",
    "Победа за {w}! +{t} см 💪",
    "{w} оказался сильнее! +{t} см 💪",
    "{w} доминирует! +{t} см 🔥",
    "{w} унизил соперника! +{t} см 🔥",
    "{w} забрал {t} см себе 💥",
    "{w} отобрал {t} см 👊",
]
FIGHT_CONDOM = [
    "{l} надел кондом — потери 0! 🛡️",
    "Кондом спас {l} от потерь! 🛡️",
    "{l} защитился кондомом! 🛡️",
    "Кондом {l} принял удар на себя! 🛡️",
    "{l} остался цел благодаря кондому! 🛡️",
]
FIGHT_VIAGRA = [
    "Виагра {w} удвоила добычу! 💊",
    "{w} на виагре — забрал x2! 💊",
    "Виагра сработала! Двойной улов для {w} 💊",
    "{w} под виагрой — двойная мощь! 💊",
    "Эффект виагры: {w} берёт вдвойне! 💊",
]
GIFT_PHRASES = [
    "{g} подарил {r} {a} см 🎁",
    "{g} отдал {r} {a} см 🎁",
    "{g} кинул {r} {a} см 🎁",
    "{g} пожертвовал {r} {a} см 🎁",
    "{r} получил {a} см от {g} 🎁",
]
LOTTERY_WIN = [
    "{w} забирает всё — {s} см 🎰",
    "{w} срывает куш! Теперь {s} см 🎰",
    "Джекпот у {w}! Теперь {s} см 🎰",
    "Всё уходит {w} — {s} см 🎰",
    "{w} выиграл лотерею! Теперь {s} см 🎰",
    "Удача на стороне {w} — {s} см 🎰",
]


def get_or_create_player(user_id: int, name: str) -> dict:
    if user_id not in game_data:
        game_data[user_id] = {
            "name": name,
            "size": INITIAL_SIZES.get(user_id, round(random.uniform(5.0, 15.0), 1)),
            "last_grow": 0.0,
            "last_fight": 0.0,
            "last_buy": 0.0,
            "item": None,
        }
    return game_data[user_id]


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
        "⚙️ Admin:\n"
        "/threshold [0.0-1.0]\n"
        "/temperature [0.0-2.0]\n"
        "/context_window [1-30]\n"
        "/silent — заглушить бота\n"
        "/autodelete [сек] — автоудаление (-1=выкл)\n"
        "/cd [grow|fight|lottery|buy] [сек]\n"
        "/setsize [размер] — реплай на игрока\n"
        "/resetgame — сброс всей игры\n"
        "/status /clear /stats"
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

    ad = f"{auto_delete_seconds}s" if auto_delete_seconds > 0 else "выкл"
    cd_lines = ", ".join(f"{k}={fmt_cd(v)}" for k, v in cooldowns.items())

    text = (
        f"⚙️ Settings:\n"
        f"  Threshold: {CURRENT_THRESHOLD}\n"
        f"  Temperature: {CURRENT_TEMPERATURE}\n"
        f"  Context window: {CURRENT_CONTEXT_WINDOW}\n"
        f"  Silent mode: {silent_state}\n"
        f"  Auto-delete: {ad}\n"
        f"  CD: {cd_lines}\n\n"
        f"📊 Context:\n"
        f"  Messages in queue: {queue_size}/{CURRENT_CONTEXT_WINDOW}\n"
        f"  API lock: {lock_state}\n"
        f"  Players: {len(game_data)}"
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


@router.message(Command("autodelete"))
async def cmd_autodelete(message: Message, command: CommandObject):
    global auto_delete_seconds
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return
    if not command.args:
        state = f"{auto_delete_seconds}s" if auto_delete_seconds > 0 else "выкл"
        await message.reply(f"Auto-delete: {state}")
        return
    try:
        value = int(command.args)
        if value <= 0:
            auto_delete_seconds = 0
            await message.reply("✅ Auto-delete: выкл")
        elif value <= 300:
            auto_delete_seconds = value
            await message.reply(f"✅ Auto-delete: {value}s")
        else:
            await message.reply("❌ 1-300 или -1")
    except ValueError:
        pass


def fmt_cd(seconds: int) -> str:
    if seconds >= 3600:
        h, r = divmod(seconds, 3600)
        m = r // 60
        return f"{h}ч {m}м" if m else f"{h}ч"
    if seconds >= 60:
        return f"{seconds // 60}м {seconds % 60}с" if seconds % 60 else f"{seconds // 60}м"
    return f"{seconds}с"


@router.message(Command("cd"))
async def cmd_cd(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return
    if not command.args:
        lines = [f"  {k}: {fmt_cd(v)}" for k, v in cooldowns.items()]
        await message.reply("⏱ Кулдауны:\n" + "\n".join(lines))
        return
    parts = command.args.strip().split()
    if len(parts) != 2 or parts[0] not in cooldowns:
        await message.reply("❌ /cd grow|fight|lottery|buy [сек]")
        return
    try:
        value = int(parts[1])
        if value < 0:
            await message.reply("❌ >= 0")
            return
        cooldowns[parts[0]] = value
        await message.reply(f"✅ {parts[0]}: {fmt_cd(value)}")
    except ValueError:
        pass


@router.message(Command("setsize"))
async def cmd_setsize(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("❌ Реплайни на игрока")
        return
    if not command.args:
        await message.reply("❌ /setsize 15.0")
        return
    try:
        value = round(float(command.args.replace(",", ".")), 1)
        if value < 1.0:
            await message.reply("❌ >= 1.0")
            return
    except ValueError:
        return
    target = message.reply_to_message.from_user
    p = get_or_create_player(target.id, USER_MAPPING.get(target.id, target.full_name))
    old = p["size"]
    p["size"] = value
    await message.reply(f"✅ {p['name']}: {old} → {value} см")


@router.message(Command("resetgame"))
async def cmd_resetgame(message: Message):
    if message.from_user.id not in ADMIN_IDS or message.chat.id != ALLOWED_CHAT_ID:
        return
    count = len(game_data)
    game_data.clear()
    await message.reply(f"🗑 Игра сброшена ({count} игроков)")


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


@router.message(Command("game"))
async def cmd_game(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    await reply_game(message,
        "/dick — твой размер\n"
        "/grow — вырастить (кд 12ч)\n"
        "/fight — бой реплаем (кд 10м)\n"
        "/fight 5 — бой со ставкой\n"
        "/gift 3 — подарить (реплай)\n"
        "/lottery — лотерея (кд 24ч)\n"
        "/shop — магазин\n"
        "/buy condom|viagra|lube (кд 1ч)\n"
        "/top — рейтинг"
    )


@router.message(Command("dick"))
async def cmd_dick(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    user = message.from_user
    p = get_or_create_player(user.id, USER_MAPPING.get(user.id, user.full_name))
    item_str = f" [{p['item']}]" if p["item"] else ""
    await reply_game(message, f"{p['name']} — {p['size']} см 🍆{item_str}")


@router.message(Command("grow"))
async def cmd_grow(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    user = message.from_user
    p = get_or_create_player(user.id, USER_MAPPING.get(user.id, user.full_name))

    now = time.time()
    if now - p["last_grow"] < cooldowns["grow"]:
        rem = cooldowns["grow"] - (now - p["last_grow"])
        await reply_game(message, f"Подожди ещё {int(rem // 3600)}ч {int((rem % 3600) // 60)}м ⏳")
        return

    base = p["size"] * random.uniform(-0.15, 0.25)
    multiplier = random.choice([0.5, 1.0, 1.0, 1.0, 1.5, 2.0])
    change = round(base * multiplier, 1)

    lube_used = False
    if p["item"] == "lube" and change < 0:
        change = 0
        p["item"] = None
        lube_used = True

    old = p["size"]
    p["size"] = max(1.0, round(old + change, 1))
    p["last_grow"] = now
    actual = round(p["size"] - old, 1)

    if p["item"] == "lube":
        p["item"] = None

    n = p["name"]
    s = p["size"]
    if lube_used:
        text = random.choice(GROW_LUBE).format(n=n, s=s)
    elif actual > 0:
        text = random.choice(GROW_POS).format(n=n, d=actual, s=s)
    elif actual < 0:
        text = random.choice(GROW_NEG).format(n=n, d=abs(actual), s=s)
    else:
        text = random.choice(GROW_ZERO).format(n=n, s=s)
    await reply_game(message, text)


@router.message(Command("fight"))
async def cmd_fight(message: Message, command: CommandObject):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    user = message.from_user
    atk = get_or_create_player(user.id, USER_MAPPING.get(user.id, user.full_name))

    now = time.time()
    if now - atk["last_fight"] < cooldowns["fight"]:
        rem = int(cooldowns["fight"] - (now - atk["last_fight"]))
        await reply_game(message, f"Подожди ещё {rem // 60}м {rem % 60}с ⏳")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await reply_game(message, "Реплайни на соперника 👊")
        return
    opp = message.reply_to_message.from_user
    if opp.is_bot:
        await reply_game(message, "С ботом нельзя драться 🤖")
        return
    if opp.id == user.id:
        await reply_game(message, "Нельзя драться с собой 🤦")
        return

    dfn = get_or_create_player(opp.id, USER_MAPPING.get(opp.id, opp.full_name))

    bet = None
    if command.args:
        try:
            bet = round(float(command.args.replace(",", ".")), 1)
            max_bet = min(dfn["size"] * 0.25, atk["size"] - 1.0)
            max_bet = max(0.1, round(max_bet, 1))
            if bet <= 0 or bet > max_bet:
                await reply_game(message, f"Ставка от 0.1 до {max_bet} 💰")
                return
        except ValueError:
            await reply_game(message, "/fight или /fight 5 👊")
            return

    atk_power = random.uniform(0, 100) + min(atk["size"] * 0.5, 15)
    def_power = random.uniform(0, 100) + min(dfn["size"] * 0.5, 15)

    if atk_power >= def_power:
        winner, loser = atk, dfn
    else:
        winner, loser = dfn, atk

    if bet:
        transfer = bet
    else:
        transfer = max(0.5, round(loser["size"] * random.uniform(0.1, 0.25), 1))

    viagra_used = False
    condom_used = False

    if winner["item"] == "viagra":
        transfer = min(round(transfer * 2, 1), round(loser["size"] * 0.3, 1))
        winner["item"] = None
        viagra_used = True
    elif winner["item"] in ("condom", "lube"):
        winner["item"] = None

    if loser["item"] == "condom":
        transfer = 0
        loser["item"] = None
        condom_used = True
    elif loser["item"] in ("viagra", "lube"):
        loser["item"] = None

    loser_old = loser["size"]
    loser["size"] = max(1.0, round(loser["size"] - transfer, 1))
    actual = round(loser_old - loser["size"], 1)
    winner["size"] = round(winner["size"] + actual, 1)
    atk["last_fight"] = now

    lines = [random.choice(FIGHT_CHALLENGE).format(a=atk["name"], d=dfn["name"])]
    if bet:
        lines.append(f"Ставка: {bet} см 💰")
    lines.append(random.choice(FIGHT_WIN).format(w=winner["name"], t=actual))
    if condom_used:
        lines.append(random.choice(FIGHT_CONDOM).format(l=loser["name"]))
    if viagra_used:
        lines.append(random.choice(FIGHT_VIAGRA).format(w=winner["name"]))
    lines.append(f"📊 {winner['name']} → {winner['size']} см")
    lines.append(f"📊 {loser['name']} → {loser['size']} см")
    await reply_game(message, "\n".join(lines))


@router.message(Command("gift"))
async def cmd_gift(message: Message, command: CommandObject):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    user = message.from_user
    p = get_or_create_player(user.id, USER_MAPPING.get(user.id, user.full_name))

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await reply_game(message, "/gift 3 — реплайни на получателя 🎁")
        return
    opp = message.reply_to_message.from_user
    if opp.is_bot or opp.id == user.id:
        return
    if not command.args:
        await reply_game(message, "/gift 3 — укажи сколько 🎁")
        return
    try:
        amount = round(float(command.args.replace(",", ".")), 1)
    except ValueError:
        return
    if amount <= 0:
        return
    max_gift = round(p["size"] - 1.0, 1)
    if amount > max_gift:
        await reply_game(message, f"Максимум {max_gift} см 🎁")
        return

    r = get_or_create_player(opp.id, USER_MAPPING.get(opp.id, opp.full_name))
    p["size"] = round(p["size"] - amount, 1)
    r["size"] = round(r["size"] + amount, 1)
    text = random.choice(GIFT_PHRASES).format(g=p["name"], r=r["name"], a=amount)
    await reply_game(message, text)


@router.message(Command("lottery"))
async def cmd_lottery(message: Message):
    global last_lottery_global
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    if len(game_data) < 2:
        await reply_game(message, "Нужно 2+ игрока 🎰")
        return

    now = time.time()
    if now - last_lottery_global < cooldowns["lottery"]:
        rem = cooldowns["lottery"] - (now - last_lottery_global)
        await reply_game(message, f"Подожди ещё {int(rem // 3600)}ч {int((rem % 3600) // 60)}м ⏳")
        return

    last_lottery_global = now
    pot = 0.0
    for p in game_data.values():
        if p["size"] > 1.0:
            take = round(p["size"] * 0.05, 1)
            take = min(take, round(p["size"] - 1.0, 1))
            p["size"] = round(p["size"] - take, 1)
            pot = round(pot + take, 1)

    if pot == 0:
        return

    w = game_data[random.choice(list(game_data.keys()))]
    w["size"] = round(w["size"] + pot, 1)
    line2 = random.choice(LOTTERY_WIN).format(w=w["name"], s=w["size"])
    await reply_game(message, f"Лотерея! Банк: {pot} см\n{line2}")


@router.message(Command("shop"))
async def cmd_shop(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    user = message.from_user
    p = get_or_create_player(user.id, USER_MAPPING.get(user.id, user.full_name))

    lines = []
    for name, info in SHOP_ITEMS.items():
        price = max(0.1, round(p["size"] * info["price_pct"], 1))
        lines.append(f"🛒 {name} — {info['desc']} (цена: {price} см)")
    if p["item"]:
        lines.append(f"Есть: {p['item']} 🎒")
    now = time.time()
    if now - p["last_buy"] < cooldowns["buy"]:
        rem = cooldowns["buy"] - (now - p["last_buy"])
        h, m = int(rem // 3600), int((rem % 3600) // 60)
        cd = f"{h}ч {m}м" if h > 0 else f"{m}м"
        lines.append(f"Подожди ещё {cd} ⏳")
    await reply_game(message, "\n".join(lines))


@router.message(Command("buy"))
async def cmd_buy(message: Message, command: CommandObject):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    user = message.from_user
    p = get_or_create_player(user.id, USER_MAPPING.get(user.id, user.full_name))

    if not command.args or command.args.strip().lower() not in SHOP_ITEMS:
        await reply_game(message, "/buy condom|viagra|lube 🛒")
        return
    if p["item"]:
        await reply_game(message, f"Уже есть: {p['item']} 🛒")
        return

    now = time.time()
    if now - p["last_buy"] < cooldowns["buy"]:
        rem = cooldowns["buy"] - (now - p["last_buy"])
        h, m = int(rem // 3600), int((rem % 3600) // 60)
        cd = f"{h}ч {m}м" if h > 0 else f"{m}м"
        await reply_game(message, f"Подожди ещё {cd} ⏳")
        return

    item_name = command.args.strip().lower()
    price = max(0.1, round(p["size"] * SHOP_ITEMS[item_name]["price_pct"], 1))

    if p["size"] - price < 1.0:
        await reply_game(message, "Не хватает см 🛒")
        return

    p["size"] = round(p["size"] - price, 1)
    p["item"] = item_name
    p["last_buy"] = now
    await reply_game(message, f"{p['name']} купил {item_name} за {price} см ✅")


@router.message(Command("top"))
async def cmd_top(message: Message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return
    if not game_data:
        await reply_game(message, "/dick чтобы начать 🏆")
        return

    sorted_p = sorted(game_data.values(), key=lambda x: x["size"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(sorted_p):
        m = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{m} {p['name']} — {p['size']} см")
    await reply_game(message, "\n".join(lines))


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
