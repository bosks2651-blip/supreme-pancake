import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Configuration
TELEGRAM_TOKEN = "7680347417:AAH-3Pb9G-dqWjxEXOSiBYSwXkjoo48bFkg"
PLUTOS_API_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJtb2R1bGUiOiJVTVMiLCJrZXkiOiJVTVMxMjMifQ.ubQOJ0bsgth33KSGpyLBtrwwnyOxK6NeAkp0HPFeg28"
CAMPAIGN_ID = "5"
DEFAULT_MOBILE = "9277071768"
OFFERING_ID = "5d8bd23b-93a4-4c64-a364-7332c8a7163d"

# API Endpoints
BASE_UMS = "https://ums-be.plutos.one/api/v3"
BASE_PLUTOS = "https://www.plutos.one/api"

ENDPOINTS = {
    "send_otp": f"{BASE_UMS}/auth/send-otp-to-whatsapp",
    "verify_otp": f"{BASE_UMS}/auth/verify-otp",
    "whatsapp_verify": f"{BASE_UMS}/auth/whatsapp/verify",
    "vouchers": f"{BASE_PLUTOS}/vms/get-cash-voucher-by-offeringId",
    "redeem": f"{BASE_PLUTOS}/ums/redeem-locked-voucher-v3",
}

# In-memory session store
sessions = {}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def get_headers(auth_token=None):
    headers = {
        "Content-Type": "application/json",
        "api_token": PLUTOS_API_TOKEN,
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎟️ *Plutos Coupon Bot*\n\n"
        "Commands:\n"
        "/login - Send OTP to WhatsApp\n"
        "/login <mobile> - Send OTP to specific number\n"
        "/verify <otp> - Verify OTP and get token\n"
        "/checkwa - Check WhatsApp verification status\n"
        "/vouchers - List BigBasket vouchers\n"
        "/redeem <voucher_id> - Redeem a voucher\n"
        "/status - Check login status\n"
        "/help - Show this message\n"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mobile = context.args[0] if context.args else DEFAULT_MOBILE

    # Store mobile for this user
    if user_id not in sessions:
        sessions[user_id] = {}
    sessions[user_id]["mobile"] = mobile

    payload = {"mobile": mobile, "campaign_id": CAMPAIGN_ID}
    try:
        resp = requests.post(ENDPOINTS["send_otp"], json=payload, headers=get_headers())
        data = resp.json()
        if data.get("success"):
            await update.message.reply_text(
                f"✅ OTP sent to WhatsApp for *{mobile}*\n\n"
                f"📱 Check your WhatsApp and use:\n"
                f"`/verify <otp>` to complete login\n\n"
                f"Or if WhatsApp-based verification, send CONFIRM on WhatsApp and use `/checkwa`",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ Failed: {data.get('message', 'Unknown error')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def verify_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: `/verify <otp>`", parse_mode="Markdown")
        return

    otp = context.args[0]
    mobile = sessions.get(user_id, {}).get("mobile", DEFAULT_MOBILE)

    payload = {"mobile": mobile, "otp": otp, "campaign_id": CAMPAIGN_ID}
    try:
        resp = requests.post(ENDPOINTS["verify_otp"], json=payload, headers=get_headers())
        data = resp.json()
        if data.get("success"):
            token = data.get("data", {}).get("token") or data.get("token")
            if token:
                sessions[user_id] = sessions.get(user_id, {})
                sessions[user_id]["auth_token"] = token
                sessions[user_id]["mobile"] = mobile
                await update.message.reply_text(
                    f"✅ *Login successful!*\n\n"
                    f"Token stored. You can now use:\n"
                    f"/vouchers - View available vouchers\n"
                    f"/redeem <id> - Redeem a voucher",
                    parse_mode="Markdown"
                )
            else:
                # Store full response for debugging
                sessions[user_id]["last_response"] = data
                await update.message.reply_text(f"✅ Verified but no token in response.\nResponse: ```{data}```", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Verification failed: {data.get('message', 'Unknown error')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def check_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mobile = sessions.get(user_id, {}).get("mobile", DEFAULT_MOBILE)

    try:
        url = f"{ENDPOINTS['whatsapp_verify']}/{mobile}"
        resp = requests.get(url, headers=get_headers())
        data = resp.json()
        if data.get("success"):
            token = data.get("data", {}).get("token") or data.get("token")
            if token:
                if user_id not in sessions:
                    sessions[user_id] = {}
                sessions[user_id]["auth_token"] = token
                sessions[user_id]["mobile"] = mobile
                await update.message.reply_text("✅ *WhatsApp verified! Token stored.*\nUse /vouchers to continue.", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"✅ Response: ```{data}```", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⏳ Not verified yet: {data.get('message', 'Pending')}\n\nSend CONFIRM on WhatsApp and try again.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def vouchers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    auth_token = sessions.get(user_id, {}).get("auth_token")

    try:
        params = {"offering_id": OFFERING_ID}
        headers = get_headers(auth_token)
        resp = requests.post(ENDPOINTS["vouchers"], params=params, headers=headers)
        data = resp.json()

        if data.get("success") and data.get("data"):
            voucher_list = data["data"]
            if isinstance(voucher_list, list):
                msg = "🎟️ *Available Vouchers:*\n\n"
                for i, v in enumerate(voucher_list[:10], 1):
                    name = v.get("name", v.get("title", "Voucher"))
                    vid = v.get("id", v.get("_id", "N/A"))
                    amount = v.get("amount", v.get("denomination", "N/A"))
                    coins = v.get("coins_required", v.get("coins", "N/A"))
                    msg += f"{i}. *{name}*\n   💰 ₹{amount} | 🪙 {coins} coins\n   ID: `{vid}`\n\n"
                msg += "Use `/redeem <voucher_id>` to redeem"
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"📋 Response:\n```{str(data)[:3000]}```", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ {data.get('message', 'No vouchers found')}\n\nRaw: ```{str(data)[:2000]}```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    auth_token = sessions.get(user_id, {}).get("auth_token")

    if not auth_token:
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/redeem <voucher_id>`", parse_mode="Markdown")
        return

    voucher_id = context.args[0]
    payload = {
        "voucher_id": voucher_id,
        "offering_id": OFFERING_ID,
        "campaign_id": CAMPAIGN_ID,
    }

    try:
        resp = requests.post(ENDPOINTS["redeem"], json=payload, headers=get_headers(auth_token))
        data = resp.json()
        if data.get("success"):
            coupon = data.get("data", {}).get("coupon_code", data.get("data", {}).get("code", "N/A"))
            await update.message.reply_text(
                f"🎉 *Voucher Redeemed!*\n\n"
                f"🎟️ Coupon Code: `{coupon}`\n"
                f"Response: ```{str(data.get('data', {}))[:1000]}```",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ Redeem failed: {data.get('message', 'Unknown error')}\n\n```{str(data)[:2000]}```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = sessions.get(user_id, {})
    mobile = session.get("mobile", "Not set")
    has_token = "✅ Yes" if session.get("auth_token") else "❌ No"
    await update.message.reply_text(
        f"📊 *Status*\n\n"
        f"📱 Mobile: {mobile}\n"
        f"🔑 Logged in: {has_token}",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("verify", verify_otp))
    app.add_handler(CommandHandler("checkwa", check_whatsapp))
    app.add_handler(CommandHandler("vouchers", vouchers))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("status", status))

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
