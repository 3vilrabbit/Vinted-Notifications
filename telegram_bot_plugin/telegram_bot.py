"""
Telegram delivery and chat commands.

Every account that switches Telegram on with its own bot token and chat ID gets its own bot:
notifications for that account's queries go to that chat, and the chat commands manage that
account's queries and allowlist only. Bots are started and stopped as accounts change their
settings, so no restart is needed.
"""

import asyncio
from queue import Empty

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters

import core
import db
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

SYNC_INTERVAL = 10  # seconds between checks for accounts that (dis)abled or changed their bot

COMMANDS = [
    ("hello", "Verify if bot is running"),
    ("add_query", "Add a keyword to the bot"),
    ("remove_query", "Remove a keyword from the bot"),
    ("queries", "List all queries"),
    ("clear_allowlist", "Clear the allowlist"),
    ("add_country", "Add a country to the allowlist"),
    ("remove_country", "Remove a country from the allowlist"),
    ("allowlist", "List all countries in the allowlist"),
]


def _safe(action):
    """Wrap a command so an unexpected error is logged and reported to the chat."""

    def decorator(func):
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            try:
                await func(self, update, context)
            except Exception as e:
                logger.error(f"Error {action}: {str(e)}", exc_info=True)
                try:
                    await update.message.reply_text(
                        f"An error occurred while {action}. Please try again later."
                    )
                except Exception as e2:
                    logger.error(f"Error sending error message: {str(e2)}")

        return wrapper

    return decorator


class UserBot:
    """The bot of one account."""

    def __init__(self, user_id, token, chat_id):
        self.user_id = user_id
        self.credentials = (token, chat_id)
        self.chat_id = chat_id
        self.app = ApplicationBuilder().token(token).build()

        # Only the account's own chat may talk to its bot
        try:
            chat_filter = filters.Chat(chat_id=int(chat_id))
        except ValueError:
            chat_filter = filters.Chat(username=chat_id.lstrip("@"))
        handlers = {
            "hello": self.hello,
            "add_query": self.add_query,
            "remove_query": self.remove_query,
            "queries": self.queries,
            "clear_allowlist": self.clear_allowlist,
            "add_country": self.add_country,
            "remove_country": self.remove_country,
            "allowlist": self.allowlist,
        }
        for name, handler in handlers.items():
            self.app.add_handler(CommandHandler(name, handler, filters=chat_filter))

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        try:
            await self.app.bot.set_my_commands(COMMANDS)
        except Exception as e:
            logger.error(f"Error setting bot commands: {str(e)}", exc_info=True)
        logger.info(f"Telegram bot started for account {self.user_id}")

    async def stop(self):
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as e:
            logger.error(f"Error stopping bot of account {self.user_id}: {str(e)}")
        logger.info(f"Telegram bot stopped for account {self.user_id}")

    ### COMMANDS ###

    async def hello(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ver = db.get_parameter("version")
        await update.message.reply_text(
            f"Hello {update.effective_user.first_name}! Vinted-Notifications is running under version {ver}.\n"
        )

    @_safe("adding the query")
    async def add_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = context.args
        if not query:
            await update.message.reply_text("No query provided.")
            return
        # name=url, or just the url
        if "=http" in query[0]:
            name, url = query[0].split("=", 1)
        else:
            name, url = None, query[0]
        message, is_new_query = core.process_query(url, name, user_id=self.user_id)
        if is_new_query:
            query_list = core.get_formatted_query_list(self.user_id)
            await update.message.reply_text(f"{message} \nCurrent queries: \n{query_list}")
        else:
            await update.message.reply_text(message)

    @_safe("removing the query")
    async def remove_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args
        if not args:
            await update.message.reply_text("No number provided.")
            return
        target = args[0]
        if target != "all":
            if not target.isdigit():
                await update.message.reply_text("Invalid number.")
                return
            target = db.get_query_id_by_rowid(target, self.user_id)
            if target is None:
                await update.message.reply_text("Query not found.")
                return
        message, success = core.process_remove_query(str(target), self.user_id)
        if success and args[0] != "all":
            query_list = core.get_formatted_query_list(self.user_id)
            await update.message.reply_text(f"{message} \nCurrent queries: \n{query_list}")
        else:
            await update.message.reply_text(message)

    @_safe("retrieving the queries")
    async def queries(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query_list = core.get_formatted_query_list(self.user_id)
        await update.message.reply_text(f"Current queries: \n{query_list}")

    @_safe("clearing the allowlist")
    async def clear_allowlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        db.clear_allowlist(self.user_id)
        await update.message.reply_text("Allowlist cleared. All countries are allowed.")

    @_safe("adding the country to the allowlist")
    async def add_country(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("No country provided")
            return
        message, country_list = core.process_add_country(" ".join(context.args), self.user_id)
        await update.message.reply_text(f"{message} Current allowlist: {country_list}")

    @_safe("removing the country from the allowlist")
    async def remove_country(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("No country provided")
            return
        message, country_list = core.process_remove_country(" ".join(context.args), self.user_id)
        await update.message.reply_text(f"{message} Current allowlist: {country_list}")

    @_safe("retrieving the allowlist")
    async def allowlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        countries = db.get_allowlist(self.user_id)
        if countries == 0:
            await update.message.reply_text("No allowlist set. All countries are allowed.")
        else:
            await update.message.reply_text(f"Current allowlist: {countries}")

    ### DELIVERY ###

    async def send_new_post(self, content, url, text, buy_url=None, buy_text=None):
        try:
            buttons = [[InlineKeyboardButton(text=text, url=url)]]
            if buy_url and buy_text:
                buttons.append([InlineKeyboardButton(text=buy_text, url=buy_url)])
            await self.app.bot.send_message(
                self.chat_id,
                content,
                parse_mode="HTML",
                read_timeout=40,
                write_timeout=40,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except RetryAfter as e:
            logger.error(f"Flood control exceeded. Retrying in {e.retry_after + 2} seconds")
            await asyncio.sleep(e.retry_after + 2)
            await self.send_new_post(content, url, text, buy_url, buy_text)
        except Exception as e:
            logger.error(f"Error sending new post: {str(e)}", exc_info=True)


def _wanted_bots():
    """{account: (token, chat id)} for the accounts that switched Telegram on."""
    wanted = {}
    for user_id in db.users_with_setting("telegram_enabled"):
        token = db.get_user_setting(user_id, "telegram_token")
        chat_id = db.get_user_setting(user_id, "telegram_chat_id")
        if token and chat_id:
            wanted[user_id] = (token.strip(), str(chat_id).strip())
    return wanted


async def run_bots(queue):
    """Keep one bot per enabled account running and deliver queued notifications."""
    bots = {}

    async def sync_loop():
        while True:
            try:
                wanted = _wanted_bots()
                for user_id in list(bots):
                    if wanted.get(user_id) != bots[user_id].credentials:
                        await bots.pop(user_id).stop()
                for user_id, (token, chat_id) in wanted.items():
                    if user_id not in bots:
                        bot = UserBot(user_id, token, chat_id)
                        try:
                            await bot.start()
                            bots[user_id] = bot
                        except Exception as e:
                            logger.error(f"Could not start the bot of account {user_id}: {e}")
                            await bot.stop()
            except Exception as e:
                logger.error(f"Error syncing Telegram bots: {str(e)}", exc_info=True)
            await asyncio.sleep(SYNC_INTERVAL)

    async def delivery_loop():
        while True:
            try:
                owner, content, url, text, buy_url, buy_text = queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.2)
                continue
            except Exception as e:
                logger.error(f"Error reading the Telegram queue: {str(e)}", exc_info=True)
                await asyncio.sleep(1)
                continue
            bot = bots.get(owner)
            if bot is not None:
                await bot.send_new_post(content, url, text, buy_url, buy_text)

    await asyncio.gather(sync_loop(), delivery_loop())
