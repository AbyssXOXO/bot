import os
import re
import time
import html
import secrets
import asyncio
import contextlib
import logging
import enka
from aiohttp import web
from telebot import types
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException
from enka.errors import (
    APIRequestTimeoutError,
    EnkaAPIError,
    EnkaPyError,
    GameMaintenanceError,
    PlayerDoesNotExistError,
    RateLimitedError,
    WrongUIDFormatError,
)
from uptime import get_uptime

LOGGER = logging.getLogger(__name__)

# 1. Grab environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
PORT = int(os.environ.get('PORT', 5000))
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL')
ENKA_USER_AGENT = os.environ.get(
    'ENKA_USER_AGENT',
    'TelegramZZZBot/1.0 (https://render.com)'
)

if not BOT_TOKEN:
    raise ValueError("No BOT_TOKEN found. Please set it in Render's environment variables.")

# 2. Initialize async Bot and aiohttp App
bot = AsyncTeleBot(BOT_TOKEN, parse_mode='HTML', disable_web_page_preview=True)
app = web.Application()
routes = web.RouteTableDef()

UID_PATTERN = re.compile(r'^\d{8,12}$')
CALLBACK_PREFIX = 'zzz'
SHOWCASE_SESSION_SECONDS = 15 * 60
MAX_SHOWCASE_SESSIONS = 100
SHOWCASE_SESSIONS = {}
UPDATE_TASKS = set()


def escape(value):
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=False)


def cleanup_showcase_sessions():
    now = time.time()
    expired_tokens = [
        token for token, session in SHOWCASE_SESSIONS.items()
        if session['expires_at'] <= now
    ]
    for token in expired_tokens:
        SHOWCASE_SESSIONS.pop(token, None)

    while len(SHOWCASE_SESSIONS) > MAX_SHOWCASE_SESSIONS:
        oldest_token = min(
            SHOWCASE_SESSIONS,
            key=lambda token: SHOWCASE_SESSIONS[token]['created_at']
        )
        SHOWCASE_SESSIONS.pop(oldest_token, None)


def cache_showcase(response):
    cleanup_showcase_sessions()
    token = secrets.token_urlsafe(6)
    now = time.time()
    SHOWCASE_SESSIONS[token] = {
        'created_at': now,
        'expires_at': now + SHOWCASE_SESSION_SECONDS,
        'response': response,
    }
    return token


async def fetch_zzz_showcase(uid):
    client_class = getattr(enka, 'ZZZClient', None) or enka.zzz.ZZZClient
    language = getattr(enka.zzz.Language, 'ENGLISH', 'en')
    headers = {'User-Agent': ENKA_USER_AGENT}

    async with client_class(language, headers=headers) as client:
        return await client.fetch_showcase(uid)


async def update_message(chat_id, message_id, text, reply_markup=None):
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except ApiTelegramException:
        await bot.send_message(
            chat_id,
            text,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


def enum_label(value):
    if value is None:
        return 'Unknown'

    raw = getattr(value, 'name', None)
    if raw is None:
        raw = getattr(value, 'value', value)

    label = str(raw).replace('_', ' ').title()
    return (
        label.replace('Hp', 'HP')
        .replace('Atk', 'ATK')
        .replace('Def', 'DEF')
        .replace('Dmg', 'DMG')
        .replace('Pen', 'PEN')
        .replace('Aaa', 'AAA')
    )


def stat_value(stat):
    formatted = getattr(stat, 'formatted_value', None)
    if formatted not in (None, ''):
        return str(formatted)

    value = getattr(stat, 'value', None)
    if value is None:
        return 'Unknown'
    return str(value)


def stat_name(stat):
    name = getattr(stat, 'name', None)
    if name:
        return name
    return enum_label(getattr(stat, 'type', None))


def format_stat(stat):
    return f"{stat_name(stat)}: {stat_value(stat)}"


def ordered_agent_stats(agent):
    stats = getattr(agent, 'stats', {}) or {}
    if isinstance(stats, dict):
        values = list(stats.values())
    else:
        values = list(stats)

    preferred_order = {
        'MAX_HP': 0,
        'ATK': 1,
        'DEF': 2,
        'IMPACT': 3,
        'CRIT_RATE': 4,
        'CRIT_DMG': 5,
        'PEN_RATIO': 6,
        'PEN': 7,
        'ANOMALY_PROFICIENCY': 8,
        'ANOMALY_MASTERY': 9,
        'ENERGY_REGEN': 10,
        'PHYSICAL_DMG_BONUS': 11,
        'FIRE_DMG_BONUS': 12,
        'ICE_DMG_BONUS': 13,
        'ELECTRIC_DMG_BONUS': 14,
        'ETHER_DMG_BONUS': 15,
        'SHEER_DMG_BONUS': 16,
        'SHEER_FORCE': 17,
        'AAA': 18,
    }

    def sort_key(stat):
        stat_type = getattr(getattr(stat, 'type', None), 'name', '')
        return preferred_order.get(stat_type, 99), stat_name(stat)

    return sorted(values, key=sort_key)


def character_button_label(agent):
    name = getattr(agent, 'name', 'Unknown agent') or 'Unknown agent'
    level = getattr(agent, 'level', '?')
    mindscape = getattr(agent, 'mindscape', '?')
    rarity = getattr(agent, 'rarity', '?')
    return f"{name} {rarity} Lv{level} M{mindscape}"


def player_summary(response):
    player = response.player
    lines = [
        '<b>ZZZ Showcase Found</b>',
        f"Player: <b>{escape(player.nickname)}</b>",
        f"UID: <code>{escape(response.uid)}</code>",
        f"Inter-Knot Lv: {escape(player.level)}",
    ]

    signature = getattr(player, 'signature', '')
    if signature:
        lines.append(f"Signature: {escape(signature)}")

    lines.append('')
    lines.append('Choose an agent to view their stats:')
    return '\n'.join(lines)


def build_agent_keyboard(response, token):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []

    for index, agent in enumerate(response.agents):
        buttons.append(
            types.InlineKeyboardButton(
                text=character_button_label(agent),
                callback_data=f'{CALLBACK_PREFIX}:{token}:{index}',
            )
        )

    markup.add(*buttons)
    return markup


def format_w_engine(engine):
    lines = [
        '<b>W-Engine</b>',
        (
            f"{escape(engine.name)} {escape(engine.rarity)} | "
            f"Lv {escape(engine.level)} | Phase {escape(engine.phase)} | "
            f"Mod {escape(engine.modification)}"
        ),
    ]

    main_stat = getattr(engine, 'main_stat', None)
    if main_stat:
        lines.append(f"Main: {escape(format_stat(main_stat))}")

    sub_stat = getattr(engine, 'sub_stat', None)
    if sub_stat:
        lines.append(f"Sub: {escape(format_stat(sub_stat))}")

    return '\n'.join(lines)


def format_skills(agent):
    skills = sorted(
        getattr(agent, 'skills', []) or [],
        key=lambda skill: getattr(getattr(skill, 'type', None), 'value', 0),
    )

    if not skills:
        return '<b>Skills</b>\nNo skill data available.'

    lines = ['<b>Skills</b>']
    for skill in skills:
        lines.append(
            f"{escape(enum_label(skill.type))}: Lv {escape(skill.level)}"
        )
    return '\n'.join(lines)


def format_drive_discs(agent):
    discs = sorted(
        getattr(agent, 'discs', []) or [],
        key=lambda disc: getattr(disc, 'slot', 0),
    )

    if not discs:
        return '<b>Drive Discs</b>\nNo drive disc data available.'

    lines = ['<b>Drive Discs</b>']
    for disc in discs:
        main_stat = getattr(disc, 'main_stat', None)
        main_text = format_stat(main_stat) if main_stat else 'Unknown main stat'
        set_name = getattr(disc, 'set_name', '') or getattr(disc, 'name', '')
        lines.append(
            (
                f"{escape(disc.slot)}. {escape(set_name)} +{escape(disc.level)} "
                f"({escape(disc.rarity)}) - {escape(main_text)}"
            )
        )

        sub_stats = getattr(disc, 'sub_stats', []) or []
        if sub_stats:
            sub_text = ', '.join(format_stat(stat) for stat in sub_stats)
            lines.append(f"   {escape(sub_text)}")

    return '\n'.join(lines)


def format_agent_stats(response, agent):
    player = response.player
    elements = ', '.join(
        enum_label(element) for element in (getattr(agent, 'elements', []) or [])
    ) or 'Unknown'
    stats = ordered_agent_stats(agent)

    lines = [
        f"<b>{escape(agent.name)}</b> {escape(agent.rarity)}",
        f"Player: {escape(player.nickname)} | UID: <code>{escape(response.uid)}</code>",
        (
            f"Lv {escape(agent.level)} | Promotion {escape(agent.promotion)} | "
            f"Mindscape M{escape(agent.mindscape)} | Core {escape(agent.core_skill_level)}"
        ),
        f"Element: {escape(elements)} | Specialty: {escape(enum_label(agent.specialty))}",
        '',
        '<b>Stats</b>',
    ]

    if stats:
        lines.extend(escape(format_stat(stat)) for stat in stats)
    else:
        lines.append('No stat data available.')

    engine = getattr(agent, 'w_engine', None)
    lines.append('')
    lines.append(format_w_engine(engine) if engine else '<b>W-Engine</b>\nNo W-Engine equipped.')
    lines.append('')
    lines.append(format_skills(agent))
    lines.append('')
    lines.append(format_drive_discs(agent))

    return '\n'.join(lines)


def enka_error_message(error):
    if isinstance(error, WrongUIDFormatError):
        return 'That UID format is not valid for ZZZ. Try something like <code>/uid 1000000000</code>.'
    if isinstance(error, PlayerDoesNotExistError):
        return 'Enka could not find that ZZZ player. Check the UID and make sure the profile exists.'
    if isinstance(error, RateLimitedError):
        return 'Enka is rate-limiting requests right now. Please try again in a little while.'
    if isinstance(error, GameMaintenanceError):
        return 'ZZZ or Enka is under maintenance right now. Please try again later.'
    if isinstance(error, APIRequestTimeoutError):
        return 'The Enka request timed out. Please try again.'
    if isinstance(error, (EnkaAPIError, EnkaPyError)):
        return f'Enka returned an error: <code>{escape(error.__class__.__name__)}</code>.'
    return 'Something went wrong while fetching ZZZ data. Please try again later.'


# 3. Telegram Command Handlers
@bot.message_handler(commands=['start', 'help'])
async def help_command(message):
    await bot.reply_to(
        message,
        (
            '<b>Commands</b>\n'
            '/uptime - show how long the bot has been running\n'
            '/uid &lt;zzz uid&gt; - fetch a ZZZ showcase and choose an agent'
        ),
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=['uptime'])
async def uptime_command(message):
    """Shows how long the bot has been running in Telegram."""
    await bot.reply_to(message, f"Uptime: <code>{escape(get_uptime())}</code>")


@bot.message_handler(commands=['uid'])
async def uid_command(message):
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) != 2 or not UID_PATTERN.match(parts[1].strip()):
        await bot.reply_to(message, 'Usage: <code>/uid 1000000000</code>')
        return

    uid = parts[1].strip()
    status_message = await bot.reply_to(
        message,
        f"Fetching ZZZ showcase for UID <code>{escape(uid)}</code>...",
    )

    try:
        response = await fetch_zzz_showcase(uid)
    except Exception as error:
        LOGGER.exception("Failed to fetch ZZZ showcase for UID %s", uid)
        await update_message(
            status_message.chat.id,
            status_message.message_id,
            enka_error_message(error),
        )
        return

    if not getattr(response, 'agents', None):
        await update_message(
            status_message.chat.id,
            status_message.message_id,
            (
                f"No showcased ZZZ agents were found for UID <code>{escape(uid)}</code>.\n"
                'Make sure the player has public agents in their in-game showcase.'
            ),
        )
        return

    token = cache_showcase(response)
    await update_message(
        status_message.chat.id,
        status_message.message_id,
        player_summary(response),
        reply_markup=build_agent_keyboard(response, token),
    )


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith(f'{CALLBACK_PREFIX}:')))
async def zzz_agent_callback(call):
    cleanup_showcase_sessions()

    try:
        _, token, index_text = call.data.split(':', 2)
        index = int(index_text)
    except (TypeError, ValueError):
        await bot.answer_callback_query(call.id, 'This selection is invalid.', show_alert=True)
        return

    session = SHOWCASE_SESSIONS.get(token)
    if not session:
        await bot.answer_callback_query(
            call.id,
            'This selection expired. Run /uid again.',
            show_alert=True,
        )
        return

    response = session['response']
    agents = getattr(response, 'agents', []) or []
    if index < 0 or index >= len(agents):
        await bot.answer_callback_query(call.id, 'That agent is no longer available.', show_alert=True)
        return

    agent = agents[index]
    await bot.answer_callback_query(call.id, f"Showing {getattr(agent, 'name', 'agent')} stats")
    await bot.send_message(
        call.message.chat.id,
        format_agent_stats(response, agent),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


@bot.message_handler(func=lambda message: True)
async def fallback(message):
    """Small help fallback for unknown messages."""
    await bot.reply_to(message, 'Use /uid &lt;zzz uid&gt; or /uptime.')

# 4. Webhook Route for Telegram Updates
async def process_update(update):
    """Process a Telegram update outside the webhook response path."""
    try:
        await bot.process_new_updates([update])
    except Exception:
        LOGGER.exception("Failed to process Telegram update")


def schedule_update(update):
    task = asyncio.create_task(process_update(update))
    UPDATE_TASKS.add(task)
    task.add_done_callback(UPDATE_TASKS.discard)


@routes.post(f'/{BOT_TOKEN}')
async def webhook(request):
    """Receives JSON updates from Telegram and schedules async handling."""
    if request.content_type != 'application/json':
        raise web.HTTPForbidden()

    json_string = await request.text()
    update = types.Update.de_json(json_string)
    if update is None:
        raise web.HTTPBadRequest(text='Invalid Telegram update')

    schedule_update(update)
    return web.Response(status=200)


# 5. Health Check Route for Render
@routes.get('/')
async def index(request):
    """Render pings this route to ensure the app is live."""
    return web.Response(text="Telegram ZZZ Bot is running!")


# 5.5 Uptime Route
@routes.get('/uptime')
async def show_uptime(request):
    """Shows how long the bot has been running."""
    return web.Response(text=f"Telegram Bot Uptime: {get_uptime()}")


async def setup_webhook(app):
    await bot.remove_webhook()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}"
        await bot.set_webhook(url=webhook_url)
        LOGGER.info("Webhook set to: %s", webhook_url)
    else:
        LOGGER.warning("RENDER_EXTERNAL_URL not found. Webhook not set.")


async def cleanup(app):
    for task in list(UPDATE_TASKS):
        task.cancel()

    if UPDATE_TASKS:
        await asyncio.gather(*UPDATE_TASKS, return_exceptions=True)

    with contextlib.suppress(Exception):
        await bot.close_session()


app.router.add_routes(routes)
app.on_startup.append(setup_webhook)
app.on_shutdown.append(cleanup)


# 6. Start the aiohttp server for local testing. Gunicorn should use aiohttp's worker on Render.
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, host='0.0.0.0', port=PORT)
