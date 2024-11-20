import logging
import os
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, ConversationHandler, filters
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_PARTICIPANTS, WAITING_FOR_BILL, REMOVING_PARTICIPANT, WAITING_FOR_PURCHASER_INFO = range(4)

class MealCostBot:
    def __init__(self, telegram_token, credentials_path, spreadsheet_id):
        """
        Initialize the bot with Telegram and Google Sheets credentials
        
        :param telegram_token: Telegram Bot API token
        :param credentials_path: Path to Google Service Account JSON file
        :param spreadsheet_id: ID of the Google Spreadsheet to sync data
        """
        self.token = telegram_token
        
        # Setup Google Sheets authentication
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        
        # Load credentials
        creds = Credentials.from_service_account_file(
            credentials_path, 
            scopes=scopes
        )
        
        # Authorize and setup Google Sheets client
        self.sheets_client = gspread.authorize(creds)
        self.spreadsheet_id = spreadsheet_id
        
        # Meal tracking variables
        self.current_meal_participants = {}
        self.current_meal_total_bill = 0
        self.current_meal_date = datetime.now()
        self.current_meal_purchaser = {
            'name': '',
            'contact': '',
            'additional_info': ''
        }

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start the meal cost tracking process"""
        await update.message.reply_text(
            "Welcome to Meal Cost Distribution Bot! "
            "Use /start_meal to begin tracking a new meal's participants."
        )
        return ConversationHandler.END

    async def start_meal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Initiate a new meal tracking session"""
        # Reset previous meal data
        self.current_meal_participants.clear()
        self.current_meal_total_bill = 0
        self.current_meal_date = datetime.now()
        self.current_meal_purchaser = {
            'name': '',
        }

        await update.message.reply_text(
            "Starting a new meal. First, let's collect the purchaser's information.\n\n"
            "Please enter the purchaser's name:"
        )
        return WAITING_FOR_PURCHASER_INFO

    async def collect_purchaser_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Collect purchaser's name"""
        self.current_meal_purchaser['name'] = update.message.text.strip()
        
        await update.message.reply_text(
            f"Thanks, {self.current_meal_purchaser['name']}. "
            "Purchaser information recorded. Now, please enter the names of all participants, "
            "one name per message. Use /remove to remove a participant, "
            "/list to see current participants, or /done when finished."
        )
        return WAITING_FOR_PARTICIPANTS

    async def add_participant(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Add a participant to the current meal"""
        participant_name = update.message.text.strip()
        
        # Prevent duplicate participants (case-insensitive)
        if participant_name.lower() in [name.lower() for name in self.current_meal_participants]:
            await update.message.reply_text(f"{participant_name} is already in the participant list.")
            return WAITING_FOR_PARTICIPANTS

        self.current_meal_participants[participant_name] = 0
        await update.message.reply_text(f"Added {participant_name} to the meal.")
        return WAITING_FOR_PARTICIPANTS

    async def remove_participant(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Remove a participant from the current meal"""
        # If no participants, inform the user
        if not self.current_meal_participants:
            await update.message.reply_text("No participants to remove. Add participants first.")
            return WAITING_FOR_PARTICIPANTS

        # Create a keyboard with current participants
        participant_list = list(self.current_meal_participants.keys())
        keyboard = [participant_list[i:i+3] for i in range(0, len(participant_list), 3)]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)

        await update.message.reply_text(
            "Select a participant to remove:", 
            reply_markup=reply_markup
        )
        return REMOVING_PARTICIPANT

    async def confirm_participant_removal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Confirm and remove the selected participant"""
        participant_to_remove = update.message.text.strip()

        # Check if the participant exists (case-sensitive)
        if participant_to_remove in self.current_meal_participants:
            del self.current_meal_participants[participant_to_remove]
            await update.message.reply_text(
                f"Removed {participant_to_remove} from the meal participants.", 
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                f"Participant {participant_to_remove} not found.", 
                reply_markup=ReplyKeyboardRemove()
            )

        return WAITING_FOR_PARTICIPANTS

    async def list_participants(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """List current meal participants"""
        if not self.current_meal_participants:
            await update.message.reply_text("No participants added yet.")
        else:
            participant_list = "\n".join(self.current_meal_participants.keys())
            await update.message.reply_text(f"Current participants:\n{participant_list}")
        
        return WAITING_FOR_PARTICIPANTS

    async def finish_participants(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Finish adding participants and prepare for bill input"""
        if not self.current_meal_participants:
            await update.message.reply_text("No participants added. Please add participants first.")
            return WAITING_FOR_PARTICIPANTS

        participant_list = ", ".join(self.current_meal_participants.keys())
        await update.message.reply_text(
            f"Participants for this meal: {participant_list}\n"
            "Now, please send the total bill amount."
        )
        return WAITING_FOR_BILL

    async def process_bill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process the total bill, calculate individual shares, and sync to Google Sheets"""
        try:
            total_bill = float(update.message.text.strip())
            num_participants = len(self.current_meal_participants)
            
            # Basic equal split
            individual_share = round(total_bill / num_participants, 2)
            
            # Distribute the bill equally
            for participant in self.current_meal_participants:
                self.current_meal_participants[participant] = individual_share
            
            # Prepare result message
            result_message = "Bill Split:\n"
            for participant, share in self.current_meal_participants.items():
                result_message += f"{participant}: ${share:.2f}\n"
            
            result_message += f"\nTotal Bill: ${total_bill:.2f}"
            
            # Sync to Google Sheets
            await self.sync_to_google_sheets(total_bill, individual_share)
            
            await update.message.reply_text(result_message)
            
            return ConversationHandler.END

        except ValueError:
            await update.message.reply_text("Invalid bill amount. Please enter a valid number.")
            return WAITING_FOR_BILL

    async def sync_to_google_sheets(self, total_bill: float, individual_share: float):
        """
        Sync meal cost information to Google Sheets
        
        :param total_bill: Total bill amount
        :param individual_share: Individual share amount
        """
        try:
            # Open the spreadsheet
            spreadsheet = self.sheets_client.open_by_key(self.spreadsheet_id)
            
            # Select or create a worksheet
            try:
                worksheet = spreadsheet.worksheet("Meal Costs")
            except gspread.WorksheetNotFound:
                # If worksheet doesn't exist, create it
                worksheet = spreadsheet.add_worksheet(
                    title="Meal Costs", 
                    rows=1000, 
                    cols=20
                )
                # Add headers if it's a new sheet
                worksheet.append_row([
                    "Date", 
                    "Purchaser Name",
                    "Total Bill", 
                    "Participants", 
                    "Individual Share"
                ])
            
            # Prepare data for syncing
            participants = ", ".join(self.current_meal_participants.keys())
            
            # Append new meal cost entry
            worksheet.append_row([
                self.current_meal_date.strftime("%Y-%m-%d"),
                self.current_meal_purchaser['name'],
                total_bill,
                participants,
                individual_share
            ])
            
            logger.info(f"Synced meal cost data to Google Sheet: {total_bill}")
        
        except Exception as e:
            logger.error(f"Error syncing to Google Sheets: {e}")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel the current meal tracking"""
        await update.message.reply_text("Meal tracking cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    def setup_bot(self) -> Application:
        """Set up the Telegram bot with conversation handlers"""
        application = Application.builder().token(self.token).build()

        # Conversation handler for meal cost tracking
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', self.start),
                CommandHandler('start_meal', self.start_meal)
            ],
            states={
                WAITING_FOR_PURCHASER_INFO: [
                    # Collect purchaser information step by step
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.collect_purchaser_name),
                ],
                WAITING_FOR_PARTICIPANTS: [
                    # Handling participants
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_participant),
                    
                    # Remove participant flow
                    CommandHandler('remove', self.remove_participant),
                    
                    # List participants
                    CommandHandler('list', self.list_participants),
                    
                    # Finish adding participants
                    CommandHandler('done', self.finish_participants)
                ],
                REMOVING_PARTICIPANT: [
                    # Handle participant removal confirmation
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.confirm_participant_removal)
                ],
                WAITING_FOR_BILL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_bill)
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel)
            ]
        )

        application.add_handler(conv_handler)
        return application

def main():
    load_dotenv()
    # Load configuration from environment variables or a config file
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', os.getenv('TELEGRAM_BOT_TOKEN'))
    GOOGLE_CREDENTIALS_PATH = os.getenv('GOOGLE_CREDENTIALS_PATH', 'google_credential.json')
    GOOGLE_SPREADSHEET_ID = os.getenv('GOOGLE_SPREADSHEET_ID', os.getenv('GOOGLE_SPREADSHEET_ID'))

    # Initialize and run the bot
    bot = MealCostBot(
        telegram_token=TELEGRAM_BOT_TOKEN, 
        credentials_path=GOOGLE_CREDENTIALS_PATH,
        spreadsheet_id=GOOGLE_SPREADSHEET_ID
    )
    
    # Run the bot
    application = bot.setup_bot()
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
