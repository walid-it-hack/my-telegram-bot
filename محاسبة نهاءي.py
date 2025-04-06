import os
import json
import re
import tempfile
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
import speech_recognition as sr
from pydub import AudioSegment
import subprocess
from dotenv import load_dotenv 
# ------------------- مفاتيح API -------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# ------------------- تهيئة OpenAI -------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------- نظام البيانات -------------------
class DataManager:
    def __init__(self):
        self.transactions = []
    
    def load_data(self, chat_id):
        try:
            with open(f'data_{chat_id}.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data.get('transactions'), list):
                    self.transactions = data.get('transactions', [])
                else:
                    self.transactions = []
        except (FileNotFoundError, json.JSONDecodeError):
            self.transactions = []
    
    def save_data(self, chat_id):
        with open(f'data_{chat_id}.json', 'w', encoding='utf-8') as f:
            json.dump({'transactions': self.transactions}, f, ensure_ascii=False, indent=2)
    
    def get_commission_summary(self):
        total_commission = 0
        commission_details = []
        
        for trans in self.transactions:
            commission_amount = 0
            if 'العمولة' in trans and 'المبلغ' in trans:
                if isinstance(trans['العمولة'], float):
                    commission_amount = trans['المبلغ'] * trans['العمولة']
                elif isinstance(trans['العمولة'], (int, float)):
                    commission_amount = trans['العمولة']
                
            if commission_amount > 0:
                total_commission += commission_amount
                transaction_type = trans.get('النوع', 'غير محدد')
                item = trans.get('المادة', '')
                amount = trans.get('المبلغ', 0)
                
                description = f"{transaction_type}"
                if transaction_type == 'صرف':
                    dollar_amount = trans.get('مبلغ_الدولار', 0)
                    dollar_rate = trans.get('سعر_الدولار', 0)
                    description += f" {dollar_amount} دولار بسعر {dollar_rate}"
                else:
                    description += f" {item} بقيمة {amount:,.0f}"
                
                commission_details.append({
                    'تاريخ': trans.get('التاريخ', 'غير محدد'),
                    'عمولة': commission_amount,
                    'من_معاملة': description
                })
        
        return total_commission, commission_details
    
    def get_user_transactions(self, user_name, transaction_type):
        user_trans = []
        field = 'البائع' if transaction_type == 'بيع' else 'المشتري'
        
        for trans in self.transactions:
            if trans.get(field) == user_name:
                if transaction_type == 'صرف' or trans.get('النوع') == transaction_type:
                    user_trans.append(trans)
        
        return user_trans
    
    def clear_transactions(self):
        self.transactions = []

data_manager = DataManager()

# ------------------- تحويل الصوت إلى نص -------------------
async def transcribe_audio(audio_file_path):
    wav_path = None
    try:
        # تحويل الملف من OGG إلى WAV
        wav_path = audio_file_path.replace('.ogg', '.wav')
        audio = AudioSegment.from_ogg(audio_file_path)
        audio = audio.set_channels(1)  # تحويل إلى قناة واحدة
        audio = audio.set_frame_rate(16000)  # تعيين معدل العينات
        audio.export(wav_path, format="wav")

        # استخدام Google Speech Recognition
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language='ar-AR')

        return text
    except Exception as e:
        return f"خطأ في تحويل الصوت إلى نص: {str(e)}"
    finally:
        # تنظيف الملفات المؤقتة
        try:
            if wav_path and os.path.exists(wav_path):
                os.remove(wav_path)
        except:
            pass

# ------------------- معالجة النصوص باستخدام GPT -------------------
async def analyze_with_gpt(text):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": """
أنت مساعد ذكي لاستخراج بيانات المعاملات. 
استخرج المعلومات التالية من النص:
- النوع: "بيع" أو "شراء" أو "صرف"
- البائع: اسم البائع
- المشتري: اسم المشتري
- المادة: المادة المباعة/المشتراة (فقط في حالة البيع والشراء)
- المبلغ: الرقم المذكور بعد "بقيمة" أو "ب" (فقط في حالة البيع والشراء)
- مبلغ_الدولار: الرقم قبل كلمة "دولار" (فقط في حالة الصرف)
- سعر_الدولار: الرقم بعد "سعر" أو "بسعر" (فقط في حالة الصرف)
- العمولة: النسبة المئوية بعد "عمولة" أو "بعمولة" محولة إلى عدد عشري (مثلاً 5% تصبح 0.05)

أجب بتنسيق JSON فقط.

أمثلة:
1. في حالة البيع أو الشراء:
{
  "النوع": "بيع",
  "البائع": "أحمد",
  "المشتري": "محمد",
  "المادة": "زيت",
  "المبلغ": 100000,
  "العمولة": 0.02
}

2. في حالة الصرف:
{
  "النوع": "صرف",
  "البائع": "أحمد",
  "المشتري": "محمد",
  "مبلغ_الدولار": 100,
  "سعر_الدولار": 10000,
  "العمولة": 0.03
}
"""
                },
                {"role": "user", "content": text}
            ]
        )
        
        gpt_response = response.choices[0].message.content
        data = json.loads(gpt_response)
        
        # التحقق من البيانات المطلوبة حسب نوع المعاملة
        if data.get('النوع') == 'صرف':
            required = ['النوع', 'البائع', 'المشتري', 'مبلغ_الدولار', 'سعر_الدولار']
        else:
            required = ['النوع', 'البائع', 'المشتري', 'المادة', 'المبلغ']
        
        if any(field not in data for field in required):
            return None, "بيانات ناقصة في الرد"
        
        return data, None
    except Exception as e:
        return None, f"خطأ: {str(e)}"

# ------------------- معالجات البوت -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""مرحباً! يمكنك:
1️⃣ إرسال معاملة نصية مثل:
   - بيع: 'بيع من أحمد إلى محمد زيت ب100000 بعمولة 2%'
   - شراء: 'شراء من محمد إلى أحمد زيت ب50000 بعمولة 3%'
   - صرف: 'صرف أحمد لمحمد 100 دولار بسعر 10000 بعمولة 3%'

2️⃣ تسجيل رسالة صوتية تحتوي على نفس المعلومات

الأوامر المتاحة:
/records - عرض جميع المعاملات
/commission - عرض سجل العمولات
/user بيع احمد - عرض معاملات البيع للمستخدم احمد
/user شراء محمد - عرض معاملات الشراء للمستخدم محمد
/user صرف احمد - عرض معاملات الصرف للمستخدم احمد
/clear - مسح جميع السجلات""")

async def view_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    data_manager.load_data(chat_id)
    
    if not data_manager.transactions:
        await update.message.reply_text("لا توجد معاملات مسجلة")
        return
    
    response = "📋 سجل المعاملات:\n\n"
    for idx, trans in enumerate(data_manager.transactions, 1):
        transaction_type = trans.get('النوع', 'غير محدد')
        
        record = f"""معاملة {idx}:
📅 {trans.get('التاريخ', 'غير محدد')}
🔸 {transaction_type}
👤 البائع: {trans.get('البائع', 'غير معروف')}
👥 المشتري: {trans.get('المشتري', 'غير معروف')}
"""
        
        if transaction_type == 'صرف':
            dollar_amount = trans.get('مبلغ_الدولار', 0)
            dollar_rate = trans.get('سعر_الدولار', 0)
            total_amount = dollar_amount * dollar_rate
            commission_rate = trans.get('العمولة', 0)
            commission_amount = total_amount * commission_rate
            net_value = total_amount - commission_amount
            
            record += f"""💵 مبلغ الدولار: {dollar_amount:,.0f} $
💹 سعر الدولار: {dollar_rate:,.0f} ل.س
💰 المبلغ الكلي: {total_amount:,.0f} ل.س
📉 العمولة: {commission_rate*100:.1f}% ({commission_amount:,.0f} ل.س)
💵 الصافي: {net_value:,.0f} ل.س"""
            
        else:  # بيع أو شراء
            record += f"""📦 {trans.get('المادة', 'غير محدد')}
💰 المبلغ: {trans.get('المبلغ', 0):,.0f} ل.س
📉 العمولة: {trans.get('العمولة', 0)*100:.1f}%
💵 الصافي: {trans.get('الصافي', 0):,.0f} ل.س"""
        
        record += "\n━━━━━━━━━━━━━━\n"
        
        if len(response + record) > 4096:
            await update.message.reply_text(response)
            response = record
        else:
            response += record
    
    if response:
        await update.message.reply_text(response)

async def view_commission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    data_manager.load_data(chat_id)
    
    total_commission, commission_details = data_manager.get_commission_summary()
    
    if not commission_details:
        await update.message.reply_text("لا توجد عمولات مسجلة")
        return
    
    response = f"""💰 سجل العمولات:
━━━━━━━━━━━━━━
المجموع الكلي: {total_commission:,.0f} ل.س
\nتفاصيل العمولات:\n"""
    
    for detail in commission_details:
        response += f"""
📅 {detail['تاريخ']}
💵 العمولة: {detail['عمولة']:,.0f} ل.س
📝 {detail['من_معاملة']}
━━━━━━━━━━━━━━"""
    
    await update.message.reply_text(response)

async def view_user_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("الرجاء إدخال نوع المعاملة (بيع/شراء/صرف) واسم المستخدم")
            return
        
        transaction_type = args[0]
        user_name = ' '.join(args[1:])
        
        if transaction_type not in ['بيع', 'شراء', 'صرف']:
            await update.message.reply_text("نوع المعاملة يجب أن يكون 'بيع' أو 'شراء' أو 'صرف'")
            return
        
        chat_id = update.message.chat_id
        data_manager.load_data(chat_id)
        
        transactions = data_manager.get_user_transactions(user_name, transaction_type)
        
        if not transactions:
            await update.message.reply_text(f"لا توجد معاملات {transaction_type} لـ {user_name}")
            return
        
        response = f"📋 سجل معاملات {transaction_type} لـ {user_name}:\n\n"
        for trans in transactions:
            transaction_type = trans.get('النوع', 'غير محدد')
            record = f"""📅 {trans.get('التاريخ', 'غير محدد')}\n"""
            
            if transaction_type == 'صرف':
                dollar_amount = trans.get('مبلغ_الدولار', 0)
                dollar_rate = trans.get('سعر_الدولار', 0)
                total_amount = dollar_amount * dollar_rate
                
                record += f"""💵 مبلغ الدولار: {dollar_amount:,.0f} $
💹 سعر الدولار: {dollar_rate:,.0f} ل.س
💰 المبلغ الكلي: {total_amount:,.0f} ل.س"""
            else:
                record += f"""📦 {trans.get('المادة', 'غير محدد')}
💰 المبلغ: {trans.get('المبلغ', 0):,.0f} ل.س"""
            
            record += f"\n💵 الصافي: {trans.get('الصافي', 0):,.0f} ل.س\n━━━━━━━━━━━━━━"
            
            if len(response + record) > 4096:
                await update.message.reply_text(response)
                response = record
            else:
                response += record
        
        if response:
            await update.message.reply_text(response)
        
    except Exception as e:
        await update.message.reply_text(f"حدث خطأ: {str(e)}")

async def clear_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    data_manager.load_data(chat_id)
    data_manager.clear_transactions()
    data_manager.save_data(chat_id)
    await update.message.reply_text("تم مسح جميع السجلات")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    temp_file = None
    try:
        await update.message.reply_text("جاري معالجة الرسالة الصوتية...")
        voice = update.message.voice
        
        temp_file = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
        temp_file.close()
        
        voice_file = await context.bot.get_file(voice.file_id)
        await voice_file.download_to_drive(temp_file.name)
        
        text = await transcribe_audio(temp_file.name)
        
        if text.startswith("خطأ"):
            await update.message.reply_text(text)
            return
            
        await update.message.reply_text(f"🎤 تم تحويل الرسالة الصوتية إلى:\n{text}")
        await handle_message(update, context, text)
        
    except Exception as e:
        await update.message.reply_text(f"❌ حدث خطأ أثناء معالجة الرسالة الصوتية: {str(e)}")
    finally:
        try:
            if temp_file and os.path.exists(temp_file.name):
                os.remove(temp_file.name)
        except:
            pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, external_text=None):
    text = external_text if external_text else update.message.text
    if text.startswith('/'):
        return
    
    data, error = await analyze_with_gpt(text)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    
    transaction_type = data.get('النوع')
    
    if transaction_type not in ['بيع', 'شراء', 'صرف']:
        await update.message.reply_text("❌ نوع المعاملة غير معروف. الأنواع المتاحة: بيع، شراء، صرف")
        return
    
    commission_rate = data.get('العمولة', 0)
    
    # إنشاء معاملة جديدة
    new_transaction = {
        "التاريخ": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "النوع": transaction_type,
        "البائع": data.get('البائع', 'غير معروف'),
        "المشتري": data.get('المشتري', 'غير معروف'),
        "العمولة": commission_rate
    }
    
    # معالجة حسب نوع المعاملة
    if transaction_type == 'صرف':
        if 'مبلغ_الدولار' not in data or 'سعر_الدولار' not in data:
            await update.message.reply_text("❌ معلومات غير كاملة لمعاملة الصرف")
            return
            
        dollar_amount = data['مبلغ_الدولار']
        dollar_rate = data['سعر_الدولار']
        total_amount = dollar_amount * dollar_rate
        commission_amount = total_amount * commission_rate
        net_value = total_amount - commission_amount
        
        new_transaction.update({
            "مبلغ_الدولار": dollar_amount,
            "سعر_الدولار": dollar_rate,
            "المبلغ": total_amount,
            "الصافي": net_value
        })
        
        response = f"""
✅ تم تسجيل معاملة صرف:
━━━━━━━━━━━━━━
📅 التاريخ: {new_transaction['التاريخ']}
👤 البائع: {new_transaction['البائع']}
👥 المشتري: {new_transaction['المشتري']}
💵 مبلغ الدولار: {dollar_amount:,.0f} $
💹 سعر الدولار: {dollar_rate:,.0f} ل.س
💰 المبلغ الكلي: {total_amount:,.0f} ل.س
📉 العمولة: {commission_rate*100:.1f}% ({commission_amount:,.0f} ل.س)
💵 الصافي: {net_value:,.0f} ل.س
"""
        
    else:  # بيع أو شراء
        if 'المادة' not in data or 'المبلغ' not in data:
            await update.message.reply_text("❌ معلومات غير كاملة للمعاملة")
            return
            
        amount = data['المبلغ']
        commission_amount = amount * commission_rate
        net_value = amount - commission_amount
        
        new_transaction.update({
            "المادة": data['المادة'],
            "المبلغ": amount,
            "الصافي": net_value
        })
        
        response = f"""
✅ تم تسجيل معاملة {transaction_type}:
━━━━━━━━━━━━━━
📅 التاريخ: {new_transaction['التاريخ']}
👤 البائع: {new_transaction['البائع']}
👥 المشتري: {new_transaction['المشتري']}
📦 المادة: {data['المادة']}
💰 المبلغ: {amount:,.0f} ل.س
📉 العمولة: {commission_rate*100:.1f}% ({commission_amount:,.0f} ل.س)
💵 الصافي: {net_value:,.0f} ل.س
"""
    
    # حفظ المعاملة
    data_manager.load_data(update.message.chat_id)
    data_manager.transactions.append(new_transaction)
    data_manager.save_data(update.message.chat_id)
    
    await update.message.reply_text(response)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("records", view_records))
    app.add_handler(CommandHandler("commission", view_commission))
    app.add_handler(CommandHandler("user", view_user_transactions))
    app.add_handler(CommandHandler("clear", clear_records))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    
    print("البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()