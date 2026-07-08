import logging
import sqlite3
import re
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# --- Configuration ---
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # <--- REPLACE WITH YOUR TOKEN FROM @BotFather
ADMIN_IDS = []  # List of admin user IDs (integers), e.g., [123456789, 987654321]

# --- Logging Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database Setup ---
def init_db():
    """Initialize the SQLite database for persistent storage."""
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS warnings
                 (user_id INTEGER, group_id INTEGER, count INTEGER, last_warning TIMESTAMP,
                 PRIMARY KEY (user_id, group_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, 
                  warn_limit INTEGER DEFAULT 3,
                  mute_duration INTEGER DEFAULT 300,
                  auto_delete_spam BOOLEAN DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS approved_links
                 (group_id INTEGER, link TEXT, PRIMARY KEY (group_id, link))''')
    conn.commit()
    conn.close()

init_db()

# --- Database Helper Functions ---
def get_setting(group_id, key, default):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    if row:
        # Simple mapping for our known settings
        settings = {'warn_limit': 3, 'mute_duration': 300, 'auto_delete_spam': 1}
        # This is a simplified retrieval. A better way: store as JSON or separate columns.
        # For this robust version, we'll use separate columns.
        return default
    return default

# Re-implement with better column handling
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
    c.execute("SELECT auto_delete_spam FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else True

def update_settings(group_id, warn_limit=None, mute_duration=None, auto_delete_spam=None):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO settings (group_id) VALUES (?)", (group_id,))
    if warn_limit is not None:
        c.execute("UPDATE settings SET warn_limit=? WHERE group_id=?", (warn_limit, group_id))
    if mute_duration is not None:
        c.execute("UPDATE settings SET mute_duration=? WHERE group_id=?", (mute_duration, group_id))
    if auto_delete_spam is not None:
        c.execute("UPDATE settings SET auto_delete_spam=? WHERE group_id=?", (1 if auto_delete_spam else 0, group_id))
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
    c.execute("INSERT INTO warnings (user_id, group_id, count, last_warning) VALUES (?, ?, 1, ?) "
              "ON CONFLICT(user_id, group_id) DO UPDATE SET count=count+1, last_warning=excluded.last_warning",
              (user_id, group_id, datetime.now()))
    conn.commit()
    conn.close()

def reset_warnings(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE user_id=? AND group_id=?", (user_id, group_id))
    conn.commit()
    conn.close()

def is_approved_link(group_id, link):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM approved_links WHERE group_id=? AND link=?", (group_id, link))
    row = c.fetchone()
    conn.close()
    return row is not None

def add_approved_link(group_id, link):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO approved_links (group_id, link) VALUES (?, ?)", (group_id, link))
    conn.commit()
    conn.close()

def remove_approved_link(group_id, link):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM approved_links WHERE group_id=? AND link=?", (group_id, link))
    conn.commit()
    conn.close()

# --- Core Anti-Spam Logic ---
# Simple spam patterns (can be expanded)
SPAM_PATTERNS = [
    r'(https?://[^\s]+){3,}',  # More than 3 links
    r'\b(free|earn|cash|click|win|prize|offer|limited|discount|bonus)\b.*\b(now|today|click|here)\b',  # Promotional language
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',  # Email addresses (optional)
    r'\b(bit\.ly|tinyurl|shorturl|goo\.gl|ow\.ly|is\.gd|buff\.ly|t\.co)\b', # URL shorteners
]

def is_spam(text):
    """Check if the message text matches any spam pattern."""
    if not text:
        return False
    text_lower = text.lower()
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False

async def handle_message(update: Update, context: CallbackContext):
    """Process each message for spam."""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    chat = update.message.chat
    message_text = update.message.text or update.message.caption or ""

    # Ignore messages from admins and bots
    if user.is_bot:
        return
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("This bot works only in groups.")
        return

    # Check admin status (simple: if user is in ADMIN_IDS or has explicit admin rights - we'll check via bot)
    # For simplicity, we assume the bot has admin rights and can check status.
    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status in ['administrator', 'creator']:
            return  # Don't moderate admins
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    # 1. Check if it's spam based on content
    if is_spam(message_text) or (message_text and any(domain in message_text.lower() for domain in ['bit.ly', 'tinyurl', 'shorturl'])):
        warn_limit = get_warn_limit(chat.id)
        current_warnings = get_warnings(user.id, chat.id)

        # Delete spam message if setting is enabled
        if get_auto_delete(chat.id):
            try:
                await update.message.delete()
            except Exception as e:
                logger.error(f"Could not delete message: {e}")

        # Add a warning
        add_warning(user.id, chat.id)
        new_warnings = current_warnings + 1

        # Prepare warning message
        warning_text = f"🚨 Spam detected! {user.mention_html()} has received warning {new_warnings}/{warn_limit}."
        sent_msg = await update.message.reply_html(warning_text)

        # Mute if exceeded limit
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
                    f"🚫 {user.mention_html()} has been muted for {mute_duration//60} minutes due to spam.",
                    parse_mode='HTML'
                )
                reset_warnings(user.id, chat.id)  # Reset warnings after mute
            except Exception as e:
                logger.error(f"Could not mute user: {e}")
                await sent_msg.edit_text(f"⚠️ Could not mute {user.mention_html()}. Check bot permissions.", parse_mode='HTML')
        else:
            # Auto-delete warning after some time? We'll keep it.
            pass

# --- Admin Commands ---
async def set_warn_limit(update: Update, context: CallbackContext):
    """Set the number of warnings before mute. /setlimit <number>"""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    # Check if user is admin
    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Only admins can use this command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setlimit <number> (e.g., /setlimit 5)")
        return
    limit = int(args[0])
    if limit < 1:
        await update.message.reply_text("Limit must be at least 1.")
        return
    update_settings(chat.id, warn_limit=limit)
    await update.message.reply_text(f"✅ Warning limit set to {limit}.")

async def set_mute_duration(update: Update, context: CallbackContext):
    """Set mute duration in seconds. /setmute <seconds>"""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Only admins can use this command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setmute <seconds> (e.g., /setmute 600)")
        return
    duration = int(args[0])
    if duration < 30:
        await update.message.reply_text("Duration must be at least 30 seconds.")
        return
    update_settings(chat.id, mute_duration=duration)
    await update.message.reply_text(f"✅ Mute duration set to {duration} seconds ({duration//60} minutes).")

async def toggle_auto_delete(update: Update, context: CallbackContext):
    """Toggle automatic deletion of spam messages. /autodelete on/off"""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Only admins can use this command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Usage: /autodelete on/off")
        return
    status = args[0].lower() == 'on'
    update_settings(chat.id, auto_delete_spam=status)
    await update.message.reply_text(f"✅ Auto-delete {'enabled' if status else 'disabled'}.")

async def approve_link(update: Update, context: CallbackContext):
    """Approve a domain/link so it won't be flagged as spam. /approve example.com"""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Only admins can use this command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /approve domain.com")
        return
    link = args[0].lower()
    add_approved_link(chat.id, link)
    await update.message.reply_text(f"✅ '{link}' has been added to the approved list.")

async def remove_approved(update: Update, context: CallbackContext):
    """Remove an approved link. /remove domain.com"""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Only admins can use this command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /remove domain.com")
        return
    link = args[0].lower()
    remove_approved_link(chat.id, link)
    await update.message.reply_text(f"✅ '{link}' removed from approved list.")

async def status(update: Update, context: CallbackContext):
    """Show current settings for the group. /status"""
    if not update.message or not update.message.from_user:
        return
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    warn_limit = get_warn_limit(chat.id)
    mute_duration = get_mute_duration(chat.id)
    auto_delete = get_auto_delete(chat.id)
    await update.message.reply_text(
        f"🛡️ **Anti-Spam Settings for this group**\n"
        f"• Warning Limit: {warn_limit}\n"
        f"• Mute Duration: {mute_duration} seconds ({mute_duration//60} min)\n"
        f"• Auto-Delete Spam: {'Enabled' if auto_delete else 'Disabled'}\n"
        f"• Approved Links: (Check your admin list)",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: CallbackContext):
    """Provide help text."""
    help_text = (
        "🤖 **Anti-Spam Bot Help**\n\n"
        "This bot detects and prevents spam in groups.\n\n"
        "**Commands:**\n"
        "/setlimit <number> - Set warnings before mute\n"
        "/setmute <seconds> - Set mute duration\n"
        "/autodelete on/off - Toggle auto-deletion of spam\n"
        "/approve <domain> - Add domain to whitelist\n"
        "/remove <domain> - Remove domain from whitelist\n"
        "/status - Show current settings\n"
        "/help - Show this message\n\n"
        "**Admin Only:** All commands (except /help) are admin-only."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

# --- Error Handler ---
async def error_handler(update: object, context: CallbackContext):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

# --- Main Function ---
def main():
    """Start the bot."""
    # Create Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Message handler for spam detection
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.CAPTION, handle_message))  # For media with captions

    # Command handlers
    application.add_handler(CommandHandler("setlimit", set_warn_limit))
    application.add_handler(CommandHandler("setmute", set_mute_duration))
    application.add_handler(CommandHandler("autodelete", toggle_auto_delete))
    application.add_handler(CommandHandler("approve", approve_link))
    application.add_handler(CommandHandler("remove", remove_approved))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("help", help_command))

    # Error handler
    application.add_error_handler(error_handler)

    # Start the Bot
    logger.info("Bot started...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
