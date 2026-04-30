import os
import telebot
from flask import Flask, request, abort

# 1. Grab environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
# Render assigns a PORT dynamically to web services
PORT = int(os.environ.get('PORT', 5000))
# Render automatically provides this variable with your app's live URL
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL') 

if not BOT_TOKEN:
    raise ValueError("No BOT_TOKEN found. Please set it in Render's environment variables.")

# 2. Initialize Bot and Flask App
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# 3. Define the Echo Handler
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    """Echoes whatever text the user sends."""
    bot.reply_to(message, message.text)

# 4. Webhook Route for Telegram Updates
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Receives JSON updates from Telegram and passes them to the bot."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

# 5. Health Check Route for Render
@app.route('/', methods=['GET'])
def index():
    """Render pings this route to ensure the app is live."""
    return "Telegram Echo Bot is running!", 200

if __name__ == '__main__':
    # 6. Configure the Webhook
    bot.remove_webhook()
    
    if RENDER_EXTERNAL_URL:
        # Set the webhook to point to your Render app + the bot token route for security
        webhook_url = f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
        bot.set_webhook(url=webhook_url)
        print(f"Webhook set to: {webhook_url}")
    else:
        print("Warning: RENDER_EXTERNAL_URL not found. Running locally? Webhook not set.")

    # 7. Start the Flask server
    app.run(host='0.0.0.0', port=PORT)
