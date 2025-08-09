"""
Flask-based web application to perform daily card draws for Citadelle 2.0.

This application reuses the existing data infrastructure from the original
Discord bot. Users authenticate via Discord OAuth2, after which they may
draw a limited number of cards per day. Card definitions and draw
restrictions are read from Google Drive and Google Sheets using the same
service account credentials defined in the provided `.env` file.  Drawn
cards are recorded both in the daily draw worksheet and in the user's
inventory sheet.

Before running this application you need to set up a Discord application
with OAuth2 enabled.  Define the following environment variables in
`Citadelle-2.0.env` (or your own `.env` file):

  - DISCORD_CLIENT_ID: the client ID of your Discord application
  - DISCORD_CLIENT_SECRET: the client secret of your Discord application
  - DISCORD_REDIRECT_URI: the callback URL registered with Discord

The existing `.env` file already defines the service account JSON and
Google sheet IDs used by the original bot.  These are loaded at start
time so the site can query card data and update inventories.

To install dependencies run `pip install -r requirements.txt` from the
`card_site` directory.
"""

import os
import json
import random
import hashlib
from datetime import datetime
from functools import wraps

import pytz
from flask import (Flask, redirect, url_for, session, request,
                   render_template, flash)
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

# Google APIs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

###############################################################################
# Environment setup
###############################################################################

# Load environment variables from the project's .env file.  The path is
# relative to the repository root.  If you move the application out of
# this repository adjust the path accordingly.
ENV_PATH = os.path.join(os.path.dirname(__file__), '..', 'Citadelle-2.0.env')
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
else:
    # If the default env file is missing, attempt to load from the current
    # directory.  This fallback makes local development easier.
    load_dotenv()

###############################################################################
# Flask application configuration
###############################################################################

app = Flask(__name__)

# Use a cryptographically secure secret key for session management.  For
# reproducible deployments you can define SESSION_SECRET in the .env file.
app.secret_key = os.getenv('SESSION_SECRET', os.urandom(32))

###############################################################################
# Discord OAuth configuration
###############################################################################

# Create an OAuth registry.  Each provider is configured below.
oauth = OAuth(app)

DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')

if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not DISCORD_REDIRECT_URI:
    # Warn at startup if Discord OAuth variables are missing.  Without them
    # users will not be able to authenticate and will be redirected to an
    # error page instead of the OAuth flow.
    app.logger.warning(
        'Discord OAuth environment variables are not fully configured.\n'
        'Set DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI in your .env file.'
    )

discord_oauth = oauth.register(
    name='discord',
    client_id=DISCORD_CLIENT_ID,
    client_secret=DISCORD_CLIENT_SECRET,
    access_token_url='https://discord.com/api/oauth2/token',
    authorize_url='https://discord.com/api/oauth2/authorize',
    api_base_url='https://discord.com/api/',
    client_kwargs={'scope': 'identify'},
)

###############################################################################
# Google Drive and Sheets setup
###############################################################################

# Build Google credentials from the service account JSON stored in the .env file.
SERVICE_ACCOUNT_JSON = os.getenv('SERVICE_ACCOUNT_JSON')
GOOGLE_SHEET_ID_CARTES = os.getenv('GOOGLE_SHEET_ID_CARTES')

if not SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID_CARTES:
    raise RuntimeError(
        'Missing SERVICE_ACCOUNT_JSON or GOOGLE_SHEET_ID_CARTES in environment.\n'
        'Ensure these variables are defined in your .env file. '
    )

# Load service account credentials.  The JSON string is stored directly in the
# environment to avoid persisting secrets on disk.
#
# When defined in a `.env` file the JSON is often quoted and contains escaped
# quotes (e.g. `\"`).  Those escape characters break `json.loads` and caused the
# application to crash at startup.  To make the parsing robust we attempt to
# decode the JSON and, on failure, retry after stripping the escape characters.
try:
    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
except json.JSONDecodeError:
    creds_info = json.loads(SERVICE_ACCOUNT_JSON.replace('\\', ''))

credentials = Credentials.from_service_account_info(
    creds_info,
    scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ],
)

# Initialize clients for Sheets and Drive.  gspread wraps Google Sheets API to
# simplify reading and writing cell ranges.  googleapiclient is used for
# listing Drive folders and files.
gspread_client = gspread.authorize(credentials)
drive_service = build('drive', 'v3', credentials=credentials)

# Open the spreadsheet that stores card data and draw logs.  This ID matches
# the sheet used by the Discord bot so that both systems share state.
spreadsheet = gspread_client.open_by_key(GOOGLE_SHEET_ID_CARTES)

# Access worksheets by name.  If they don't exist yet they will be created
# lazily on demand.  We avoid creating them up front because the existing
# spreadsheet might already contain them.
def get_or_create_worksheet(title: str, rows: int = 1000, cols: int = 2):
    """Return a worksheet with the given title, creating it if necessary."""
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=str(rows), cols=str(cols))

sheet_cards = spreadsheet.sheet1  # inventory sheet (first sheet)
sheet_daily_draw = get_or_create_worksheet('Tirages Journaliers', rows=1000, cols=2)

###############################################################################
# Card configuration
###############################################################################

# Rarity weights and categories mirror those defined in cogs/cards/config.py.
RARITY_WEIGHTS = {
    "Secrète": 0.005,
    "Fondateur": 0.01,
    "Historique": 0.02,
    "Maître": 0.06,
    "Black Hole": 0.06,
    "Architectes": 0.07,
    "Professeurs": 0.1167,
    "Autre": 0.2569,
    "Élèves": 0.4203,
}

ALL_CATEGORIES = [
    "Secrète", "Fondateur", "Historique", "Maître", "Black Hole",
    "Architectes", "Professeurs", "Autre", "Élèves",
]

# Build a mapping from category names to Google Drive folder IDs.  The
# environment variables follow the naming convention used in the bot.  For
# example, FOLDER_SECRETE_ID points to the "Secrète" rarity images.  If an
# environment variable is missing the corresponding category will have no
# available cards.
FOLDER_IDS = {
    "Historique": os.getenv('FOLDER_PERSONNAGE_HISTORIQUE_ID'),
    "Fondateur": os.getenv('FOLDER_FONDATEUR_ID'),
    "Black Hole": os.getenv('FOLDER_BLACKHOLE_ID'),
    "Maître": os.getenv('FOLDER_MAITRE_ID'),
    "Architectes": os.getenv('FOLDER_ARCHITECTES_ID'),
    "Professeurs": os.getenv('FOLDER_PROFESSEURS_ID'),
    "Autre": os.getenv('FOLDER_AUTRE_ID'),
    "Élèves": os.getenv('FOLDER_ELEVES_ID'),
    "Secrète": os.getenv('FOLDER_SECRETE_ID'),
}

# This dictionary will be populated on application start.  Each key is a
# category name and each value is a list of dictionaries with keys ``id`` and
# ``name`` corresponding to file IDs and file names on Google Drive.
cards_by_category: dict[str, list[dict[str, str]]] = {}

def load_card_files() -> None:
    """Populate ``cards_by_category`` by listing files in each Drive folder."""
    global cards_by_category
    cards_by_category = {}
    for category in ALL_CATEGORIES:
        folder_id = FOLDER_IDS.get(category)
        if not folder_id:
            # Skip categories without configured folders
            cards_by_category[category] = []
            continue
        try:
            # Only list image files (PNG) inside the folder.  If the folder
            # contains subfolders or other file types they are ignored.
            results = drive_service.files().list(
                q=f"'{folder_id}' in parents",
                fields="files(id, name, mimeType)"
            ).execute()
            files = [
                {"id": f["id"], "name": f["name"]}
                for f in results.get('files', [])
                if f.get('mimeType', '').startswith('image/') and f['name'].lower().endswith('.png')
            ]
            cards_by_category[category] = files
        except Exception as e:
            # Log and continue on errors; categories without files will produce
            # empty draws.
            app.logger.error(f"Failed to load cards for category {category}: {e}")
            cards_by_category[category] = []

# Call at startup
load_card_files()

###############################################################################
# Helper functions for drawing and sheet manipulation
###############################################################################

def can_perform_daily_draw(user_id: int) -> bool:
    """Return True if the user has not drawn a card today."""
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    user_id_str = str(user_id)
    try:
        all_rows = sheet_daily_draw.get_all_values()
        # Find row by user ID
        for row in all_rows:
            if row and row[0] == user_id_str:
                # Existing entry for user
                if len(row) > 1 and row[1] == today:
                    return False
                else:
                    return True
        # No entry means user has not drawn yet
        return True
    except Exception as e:
        app.logger.error(f"Error checking daily draw for {user_id}: {e}")
        return False

def record_daily_draw(user_id: int) -> bool:
    """Record that the user has performed their daily draw today."""
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    user_id_str = str(user_id)
    try:
        all_rows = sheet_daily_draw.get_all_values()
        for idx, row in enumerate(all_rows):
            if row and row[0] == user_id_str:
                # Update existing row
                sheet_daily_draw.update(f"B{idx + 1}", today)
                return True
        # Append new row
        sheet_daily_draw.append_row([user_id_str, today])
        return True
    except Exception as e:
        app.logger.error(f"Error recording daily draw for {user_id}: {e}")
        return False

def draw_cards(number: int = 3) -> list[tuple[str, dict[str, str]]]:
    """
    Perform a random draw of ``number`` cards.

    Returns a list of tuples (category, file_dict) where file_dict contains
    ``id`` and ``name``.
    """
    drawn: list[tuple[str, dict[str, str]]] = []
    available_categories = list(RARITY_WEIGHTS.keys())
    category_weights = [RARITY_WEIGHTS[cat] for cat in available_categories]
    for _ in range(number):
        # Select category based on weights
        category = random.choices(available_categories, weights=category_weights)[0]
        files = cards_by_category.get(category, [])
        if not files:
            continue
        selected = random.choice(files)
        drawn.append((category, selected))
    return drawn

def add_card_to_user(user_id: int, category: str, file_name: str) -> bool:
    """
    Increment the card count for the given user and card in the inventory sheet.

    The inventory sheet has the following structure:
      category | name | user1_id:count | user2_id:count | ...

    If the card does not exist yet in the inventory it will be appended.  If
    the user has no entry for this card a new cell will be appended to the
    row.  Otherwise the count will be incremented.

    This simplified implementation omits certain edge cases handled in the
    original bot (like cleaning up empty cells) but maintains the core
    behaviour.
    """
    user_id_str = str(user_id)
    try:
        all_rows = sheet_cards.get_all_values()
        # Determine maximum length of any row to pad rows uniformly
        max_len = max(len(row) for row in all_rows) if all_rows else 2
        # Search for card row
        card_row_index = None
        for idx, row in enumerate(all_rows):
            if len(row) >= 2 and row[0] == category and row[1] == file_name:
                card_row_index = idx
                break
        if card_row_index is None:
            # Card not found; append new row with user count
            new_row = [category, file_name, f"{user_id_str}:1"]
            sheet_cards.append_row(new_row)
            return True
        # Card exists; update count
        row = all_rows[card_row_index]
        updated = False
        # Extend row to max_len to avoid index errors
        if len(row) < max_len:
            row += [''] * (max_len - len(row))
        for j in range(2, len(row)):
            cell = row[j]
            if cell:
                try:
                    uid, count = cell.split(':', 1)
                except ValueError:
                    continue
                if uid.strip() == user_id_str:
                    # Increment count
                    try:
                        new_count = int(count) + 1
                    except ValueError:
                        new_count = 1
                    row[j] = f"{user_id_str}:{new_count}"
                    updated = True
                    break
        if not updated:
            # User not present in row; append new cell
            row.append(f"{user_id_str}:1")
        # Update the sheet row
        sheet_cards.update(f"A{card_row_index + 1}", [row])
        return True
    except Exception as e:
        app.logger.error(f"Error adding card {file_name} for user {user_id}: {e}")
        return False

###############################################################################
# Authentication helpers
###############################################################################

def login_required(f):
    """Decorator to ensure the user is authenticated before accessing a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

###############################################################################
# Flask routes
###############################################################################

@app.route('/')
def index():
    """Home page.  Show login status and offer draw button."""
    user = session.get('user')
    return render_template('home.html', user=user)

@app.route('/login')
def login():
    """Start the Discord OAuth2 flow or display an error if not configured."""
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not DISCORD_REDIRECT_URI:
        flash('Discord OAuth2 is not configured.  Please set the required environment variables.', 'error')
        return redirect(url_for('index'))
    redirect_uri = DISCORD_REDIRECT_URI
    return discord_oauth.authorize_redirect(redirect_uri)

@app.route('/callback')
def callback():
    """Discord OAuth2 callback: exchange code for token and fetch user info."""
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not DISCORD_REDIRECT_URI:
        flash('Discord OAuth2 is not configured.  Cannot process callback.', 'error')
        return redirect(url_for('index'))
    token = discord_oauth.authorize_access_token()
    if not token:
        flash('Failed to retrieve Discord access token.', 'error')
        return redirect(url_for('index'))
    # Fetch user profile from Discord
    resp = discord_oauth.get('users/@me')
    user_info = resp.json()
    session['user'] = {
        'id': user_info.get('id'),
        'username': user_info.get('username'),
        'discriminator': user_info.get('discriminator'),
        'avatar': user_info.get('avatar'),
    }
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    """Log the user out by clearing the session."""
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/draw')
@login_required
def draw():
    """Perform a daily draw for the authenticated user."""
    user = session.get('user')
    user_id = int(user['id'])
    if not can_perform_daily_draw(user_id):
        flash('Vous avez déjà effectué votre tirage journalier aujourd\u2011hui.', 'info')
        return redirect(url_for('index'))
    # Draw cards
    drawn_cards = draw_cards(number=3)
    # Add drawn cards to user inventory and prepare display data
    display_cards = []
    for category, file_info in drawn_cards:
        file_name = file_info['name']
        # Remove .png suffix for display
        display_name = file_name.rsplit('.', 1)[0]
        # Record in inventory
        add_card_to_user(user_id, category, display_name)
        # Build image URL using Google Drive file ID.  Using the export
        # endpoint allows embedding images directly.
        img_url = f"https://drive.google.com/uc?id={file_info['id']}"
        display_cards.append({
            'category': category,
            'name': display_name,
            'image_url': img_url,
        })
    # Record draw in daily sheet
    record_daily_draw(user_id)
    return render_template('draw.html', cards=display_cards, user=user)

###############################################################################
# Template rendering
###############################################################################

# The following templates are defined in the `templates` folder:
#   - home.html : main page
#   - draw.html : display results of a draw

# See the corresponding files under card_site/templates for the HTML.

###############################################################################

if __name__ == '__main__':
    # For local development only.  In production use a WSGI server.
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)