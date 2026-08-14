"""
Entrypoint for alfred-agent.
Starts the Telegram bot.
"""
from telegram_bot import main as start_telegram_bot

def main():
    """
    Main function to initialize and start the Telegram bot.
    """
    start_telegram_bot()

if __name__ == "__main__":
    main()
