import os
import logging
import uuid
from threading import Thread
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    JobQueue,
)
import psycopg2
from psycopg2.extras import RealDictCursor

# --- بخش پیکربندی ---
load_dotenv(dotenv_path=Path(__file__).resolve().parent / '.env')
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Health Check با Flask ---
app = Flask(__name__)

@app.route('/health')
def health_check():
    """برای اینکه Railway بفهمد ربات زنده است."""
    return "I'm alive!"

# --- پایگاه داده PostgreSQL ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def setup_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS metadata (code TEXT PRIMARY KEY, description TEXT NOT NULL, is_mix INTEGER NOT NULL DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS files (id SERIAL PRIMARY KEY, code TEXT NOT NULL, channel_message_id INTEGER NOT NULL, file_type TEXT NOT NULL, FOREIGN KEY (code) REFERENCES metadata (code))''')
    conn.commit()
    cursor.close()
    conn.close()

# --- منطق آپلود "Mix" ---
AWAITING_DESCRIPTION = 1

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if 'current_batch' not in context.user_data:
        context.user_data['current_batch'] = []
        await update.message.reply_text("فایل دریافت شد. لطفاً بقیه فایل‌ها را هم بفرستید...")

    current_jobs = context.job_queue.get_jobs_by_name(f"{user_id}_batch_timer")
    for job in current_jobs:
        job.schedule_removal()
    
    context.job_queue.run_once(ask_for_description, 7, chat_id=update.effective_chat.id, name=f"{user_id}_batch_timer")
    context.user_data['current_batch'].append(update.message)

async def ask_for_description(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    context.user_data['awaiting_description'] = True
    await context.bot.send_message(job.chat_id, "تمام شد! لطفاً یک توضیح برای این مجموعه فایل‌ها بنویسید:")

async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_description'):
        del context.user_data['awaiting_description']
        description = update.message.text
        file_batch = context.user_data.get('current_batch', [])
        
        if not file_batch:
            await update.message.reply_text("خطایی رخ داد.")
            return

        unique_code = str(uuid.uuid4()).split('-')[0].upper()
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        is_mix = len(file_batch) > 1
        cursor.execute("INSERT INTO metadata (code, description, is_mix) VALUES (%s, %s, %s)", (unique_code, description, int(is_mix)))
        
        for msg in file_batch:
            file_type = 'document'
            if msg.photo: file_type = 'photo'
            elif msg.video: file_type = 'video'
            elif msg.audio: file_type = 'audio'
            elif msg.animation: file_type = 'animation'

            sent_message = None
            if file_type == 'photo':
                sent_message = await context.bot.send_photo(CHANNEL_ID, msg.photo[-1].file_id)
            elif file_type == 'video':
                sent_message = await context.bot.send_video(CHANNEL_ID, msg.video.file_id)
            else:
                sent_message = await context.bot.send_document(CHANNEL_ID, msg.document.file_id)

            cursor.execute("INSERT INTO files (code, channel_message_id, file_type) VALUES (%s, %s, %s)", (unique_code, sent_message.message_id, file_type))

        conn.commit()
        cursor.close()
        conn.close()
        
        del context.user_data['current_batch']
        await update.message.reply_text(f"مجموعه با موفقیت ذخیره شد.\n\nکد شما: `{unique_code}`\nتوضیحات: {description}", parse_mode='Markdown')

# --- منطق دریافت و تأیید ---
AWAITING_DOWNLOAD_CONFIRMATION = 2

async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("لطفاً بعد از دستور /get کد مورد نظر را وارد کنید. مثال: /get ABC123")
        return ConversationHandler.END

    code_to_get = context.args[0].upper()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT description, is_mix FROM metadata WHERE code = %s", (code_to_get,))
    result = cursor.fetchone()
    
    if not result:
        await update.message.reply_text("کد یافت نشد.")
        cursor.close()
        conn.close()
        return ConversationHandler.END
        
    description, is_mix = result
    cursor.execute("SELECT file_type, COUNT(*) FROM files WHERE code = %s GROUP BY file_type", (code_to_get,))
    file_counts = dict(cursor.fetchall())
    cursor.close()
    conn.close()
    
    summary_text = f"کد: `{code_to_get}`\n"
    summary_text += f"توضیحات: {description}\n"
    summary_text += f"نوع: {'Mix' if is_mix else 'فایل تکی'}\n"
    summary_text += f"محتویات: {', '.join([f'{count} {type}' for type, count in file_counts.items()])}\n\n"
    summary_text += "برای دانلود، کلمه 'بله' را ارسال کنید. برای انصراف، 'خیر' را بفرستید."
    
    await update.message.reply_text(summary_text, parse_mode='Markdown')
    context.user_data['pending_code'] = code_to_get
    return AWAITING_DOWNLOAD_CONFIRMATION

async def download_confirmation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = update.message.text.lower()
    
    if response in ['بله', 'yes', 'موافقم']:
        code_to_get = context.user_data.get('pending_code')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT channel_message_id FROM files WHERE code = %s", (code_to_get,))
        message_ids = [row['channel_message_id'] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        
        if not message_ids:
            await update.message.reply_text("خطا: هیچ فایلی برای این کد پیدا نشد.")
        else:
            await update.message.reply_text("در حال ارسال فایل‌ها...")
            for msg_id in message_ids:
                try:
                    await context.bot.forward_message(chat_id=update.effective_chat.id, from_chat_id=CHANNEL_ID, message_id=msg_id)
                except Exception as e:
                    logger.error(f"Error forwarding message {msg_id}: {e}")
        
        del context.user_data['pending_code']
        
    elif response in ['خیر', 'no', 'مخالفم', 'انصراف']:
        await update.message.reply_text("درخواست شما لغو شد.")
        del context.user_data['pending_code']
        
    else:
        await update.message.reply_text("لطفاً فقط 'بله' یا 'خیر' وارد کنید.")
        return AWAITING_DOWNLOAD_CONFIRMATION

    return ConversationHandler.END

# --- تابع اصلی برای اجرای ربات تلگرام ---
def run_telegram_bot():
    """این تابع ربات تلگرام را در یک ترد جداگانه اجرا می‌کند."""
    setup_database()
    job_queue = JobQueue()
    telegram_app = Application.builder().token(TOKEN).job_queue(job_queue).build()

    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.ANIMATION | filters.Document.ALL, handle_file))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_reply))

    conv_handler_get = ConversationHandler(
        entry_points=[CommandHandler("get", get_command)],
        states={AWAITING_DOWNLOAD_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, download_confirmation_handler)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    telegram_app.add_handler(conv_handler_get)
    
    telegram_app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("ربات شخصی شما آماده است. یک فایل برایم بفرستید!")))

    print("ربات تلگرام در حال اجراست...")
    telegram_app.run_polling()

# --- شروع اجرا ---
# این ترد، ربات تلگرام را در پس‌زمینه اجرا می‌کند تا سرور Flask مسدود نشود.
bot_thread = Thread(target=run_telegram_bot)
bot_thread.start()

# این بخش فقط برای اجرای محلی (روی کامپیوتر خودتان) است
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)