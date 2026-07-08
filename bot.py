import os
import logging
import sqlite3
import re
import sys
import time
from datetime import datetime, timedelta
from telegram import Update, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Conflict

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN environment variable not set!")
    sys.exit(1)

ADMIN_IDS = []
admin_ids_str = os.environ.get("ADMIN_IDS", "")
if admin_ids_str:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database ---
def init_db():
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS warnings
                 (user_id INTEGER, group_id INTEGER, count INTEGER, last_warning TIMESTAMP,
                 PRIMARY KEY (user_id, group_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, 
                  warn_limit INTEGER DEFAULT 3,
                  mute_duration INTEGER DEFAULT 300,
                  auto_delete INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS approved_links
                 (group_id INTEGER, link TEXT, PRIMARY KEY (group_id, link))''')
    conn.commit()
    conn.close()
    logger.info("Database initialized")

init_db()

def get_warn_limit(group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT warn_limit FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 3

def get_mute_duration(group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT mute_duration FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 300

def get_auto_delete(group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT auto_delete FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else True

def update_settings(group_id, warn_limit=None, mute_duration=None, auto_delete=None):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO settings (group_id) VALUES (?)", (group_id,))
    if warn_limit is not None:
        c.execute("UPDATE settings SET warn_limit=? WHERE group_id=?", (warn_limit, group_id))
    if mute_duration is not None:
        c.execute("UPDATE settings SET mute_duration=? WHERE group_id=?", (mute_duration, group_id))
    if auto_delete is not None:
        c.execute("UPDATE settings SET auto_delete=? WHERE group_id=?", (1 if auto_delete else 0, group_id))
    conn.commit()
    conn.close()

def get_warnings(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT count FROM warnings WHERE user_id=? AND group_id=?", (user_id, group_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def add_warning(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT INTO warnings (user_id, group_id, count, last_warning) VALUES (?, ?, 1, ?) "
              "ON CONFLICT(user_id, group_id) DO UPDATE SET count=count+1, last_warning=?",
              (user_id, group_id, now, now))
    conn.commit()
    conn.close()

def reset_warnings(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE user_id=? AND group_id=?", (user_id, group_id))
    conn.commit()
    conn.close()

# --- Spam Detection ---
SPAM_PATTERNS = [
    r'(https?://[^\s]+){3,}',
    r'\b(free|earn|cash|click|win|prize|offer|limited|discount|bonus|gift)\b.*\b(now|today|click|here|link)\b',
    r'\b(bit\.ly|tinyurl|shorturl|goo\.gl|ow\.ly|is\.gd|buff\.ly|t\.co|cutt\.ly|rb\.gy)\b',
]

def is_spam(text):
    if not text:
        return False
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
    
    user = update.message.from_user
    chat = update.message.chat
    
    if user.is_bot or chat.type not in ['group', 'supergroup']:
        return

    message_text = update.message.text or update.message.caption or ""

    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status in ['administrator', 'creator']:
            return
    except Exception as e:
        logger.error(f"Error checking admin: {e}")
        return

    if is_spam(message_text):
        warn_limit = get_warn_limit(chat.id)
        current_warnings = get_warnings(user.id, chat.id)

        if get_auto_delete(chat.id):
            try:
                await update.message.delete()
            except Exception as e:
                logger.error(f"Delete error: {e}")

        add_warning(user.id, chat.id)
        new_warnings = current_warnings + 1

        warning_text = f"🚨 Spam! {user.mention_html()} warning {new_warnings}/{warn_limit}"
        sent_msg = await update.message.reply_html(warning_text)

        if new_warnings >= warn_limit:
            mute_duration = get_mute_duration(chat.id)
            try:
                until_date = datetime.now() + timedelta(seconds=mute_duration)
                await context.bot.restrict_chat_member(
                    chat.id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await sent_msg.edit_text(
                    f"🚫 {user.mention_html()} muted for {mute_duration//60} min",
                    parse_mode='HTML'
                )
                reset_warnings(user.id, chat.id)
            except Exception as e:
                logger.error(f"Mute error: {e}")

# --- Admin Commands ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message:
        return False
    
    user = update.message.from_user
    chat = update.message.chat
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use in a group")
        return False
    
    if user.id in ADMIN_IDS:
        return True
    
    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status in ['administrator', 'creator']:
            return True
        await update.message.reply_text("⚠️ Admin only")
        return False
    except Exception:
        return False

async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setlimit <number>")
        return
    limit = int(args[0])
    if limit < 1:
        await update.message.reply_text("Minimum 1")
        return
    update_settings(update.message.chat.id, warn_limit=limit)
    await update.message.reply_text(f"✅ Limit set to {limit}")

async def set_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setmute <seconds>")
        return
    duration = int(args[0])
    if duration < 30:
        await update.message.reply_text("Minimum 30 seconds")
        return
    update_settings(update.message.chat.id, mute_duration=duration)
    await update.message.reply_text(f"✅ Mute set to {duration}s")

async def toggle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Usage: /autodelete on/off")
        return
    status = args[0].lower() == 'on'
    update_settings(update.message.chat.id, auto_delete=status)
    await update.message.reply_text(f"✅ Auto-delete {'enabled' if status else 'disabled'}")

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use in a group")
        return
    
    warn_limit = get_warn_limit(chat.id)
    mute_duration = get_mute_duration(chat.id)
    auto_delete = get_auto_delete(chat.id)
    
    await update.message.reply_text(
        f"🛡️ **Settings**\n"
        f"• Warning Limit: {warn_limit}\n"
        f"• Mute Duration: {mute_duration}s ({mute_duration//60}m)\n"
        f"• Auto-Delete: {'✅' if auto_delete else '❌'}",
        parse_mode='Markdown'
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Anti-Spam Bot**\n\n"
        "**Admin Commands:**\n"
        "/setlimit <num> - Warnings before mute\n"
        "/setmute <sec> - Mute duration\n"
        "/autodelete on/off - Toggle auto-delete\n"
        "/status - Show settings\n"
        "/help - This message",
        parse_mode='Markdown'
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Anti-Spam Bot is running!")

# --- Main ---
def main():
    logger.info("🚀 Starting Anti-Spam Bot...")
    
    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setlimit", set_limit))
    app.add_handler(CommandHandler("setmute", set_mute))
    app.add_handler(CommandHandler("autodelete", toggle_delete))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.CAPTION, handle_message))
    
    logger.info("✅ Bot is ready!")
    
    # Run with retry logic for conflicts
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            # Clear webhook before starting
            app.bot.delete_webhook()
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                stop_signals=None  # Prevent signal issues on Railway
            )
            break
        except Conflict as e:
            logger.warning(f"Conflict error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                logger.info(f"Waiting {retry_delay} seconds before retry...")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                logger.error("Max retries exceeded. Exiting...")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
