"""
Extended Flask application for the Citadelle card‑draw website.

This module provides a fully featured website for managing Citadelle card
collections.  Users can perform their daily draw, sacrifice cards to obtain
new ones, deposit cards on an exchange board, trade with other players and
browse their personal gallery.  A ranking page shows the top collectors.

Images are served directly from Google Drive via a dedicated route.  This
avoids embedding ``drive.google.com`` URLs in the HTML and prevents the
loading issues the user observed previously.

The site relies on Google Sheets to store inventories, daily draws and
exchange entries.  A service account defined in ``Citadelle-2.0.env`` (or
another environment file) must have access to the relevant Drive folders
and spreadsheet.  See the original ``app.py`` in the repository for basic
configuration of Discord OAuth and environment variables.

NOTE: This implementation aims to demonstrate the requested features
without replicating every nuance of the original Discord bot.  Trading
operations are simplified and concurrency aspects are not fully handled.
"""

import os
import json
import random
from datetime import datetime
from functools import wraps
from typing import List, Dict, Tuple, Optional, Any

import pytz
from flask import (Flask, render_template, redirect, url_for, session,
                   request, flash, Response)
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

###############################################################################
# Environment and application setup
###############################################################################

# Attempt to load environment variables from the repository's .env file.  If
# Citadelle-2.0.env is present one level above this file, load that instead.
BASE_DIR = os.path.dirname(__file__)
ENV_PATH = os.path.join(BASE_DIR, '..', 'Citadelle-2.0.env')
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SESSION_SECRET', os.urandom(32))

###############################################################################
# Discord OAuth configuration
###############################################################################

oauth = OAuth(app)

DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')

if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not DISCORD_REDIRECT_URI:
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

SERVICE_ACCOUNT_JSON = os.getenv('SERVICE_ACCOUNT_JSON')
GOOGLE_SHEET_ID_CARTES = os.getenv('GOOGLE_SHEET_ID_CARTES')

if not SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID_CARTES:
    raise RuntimeError(
        'Missing SERVICE_ACCOUNT_JSON or GOOGLE_SHEET_ID_CARTES in environment.\n'
        'Ensure these variables are defined in your .env file.'
    )

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

gspread_client = gspread.authorize(credentials)
drive_service = build('drive', 'v3', credentials=credentials)
spreadsheet = gspread_client.open_by_key(GOOGLE_SHEET_ID_CARTES)

# Inventory sheet (cards owned by players).  The first sheet stores inventory.
sheet_cards = spreadsheet.sheet1

# Daily draw worksheet.  A separate sheet tracks daily draws by user ID and date.
def get_or_create_worksheet(title: str, rows: int = 1000, cols: int = 4):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=str(rows), cols=str(cols))

sheet_daily_draw = get_or_create_worksheet('Tirages Journaliers', rows=1000, cols=2)

# Exchange board worksheet.  This sheet tracks exchange offers.
sheet_exchange = get_or_create_worksheet('Tableau Echanges', rows=1000, cols=4)

###############################################################################
# Card configuration and loading from Google Drive
###############################################################################

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

ALL_CATEGORIES = list(RARITY_WEIGHTS.keys())

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

cards_by_category: Dict[str, List[Dict[str, str]]] = {}

def load_card_files() -> None:
    """Populate ``cards_by_category`` by listing image files in each Drive folder."""
    global cards_by_category
    cards_by_category = {}
    for category in ALL_CATEGORIES:
        folder_id = FOLDER_IDS.get(category)
        if not folder_id:
            cards_by_category[category] = []
            continue
        try:
            results = drive_service.files().list(
                q=f"'{folder_id}' in parents and mimeType contains 'image/'",
                fields="files(id, name, mimeType)"
            ).execute()
            files = [
                {"id": f["id"], "name": f["name"]}
                for f in results.get('files', [])
                if f['name'].lower().endswith('.png')
            ]
            cards_by_category[category] = files
        except Exception as e:
            app.logger.error(f"Failed to load cards for category {category}: {e}")
            cards_by_category[category] = []

# Load cards at startup
load_card_files()

###############################################################################
# Helper functions
###############################################################################

def login_required(f):
    """Decorator to ensure the user is authenticated before accessing a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_user() -> Dict[str, Any]:
    """Inject the current user into all templates."""
    return dict(user=session.get('user'))

def can_perform_daily_draw(user_id: int) -> bool:
    """Return True if the user has not drawn a card today."""
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    user_id_str = str(user_id)
    try:
        all_rows = sheet_daily_draw.get_all_values()
        for row in all_rows:
            if row and row[0] == user_id_str:
                if len(row) > 1 and row[1] == today:
                    return False
                else:
                    return True
        return True
    except Exception as e:
        app.logger.error(f"Error checking daily draw for {user_id}: {e}")
        return False

def record_daily_draw(user_id: int) -> None:
    """Record that the user has performed their daily draw today."""
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    user_id_str = str(user_id)
    try:
        all_rows = sheet_daily_draw.get_all_values()
        for idx, row in enumerate(all_rows):
            if row and row[0] == user_id_str:
                sheet_daily_draw.update(f"B{idx + 1}", today)
                return
        sheet_daily_draw.append_row([user_id_str, today])
    except Exception as e:
        app.logger.error(f"Error recording daily draw for {user_id}: {e}")

def draw_cards(number: int = 3) -> List[Tuple[str, Dict[str, str]]]:
    """Perform a weighted random draw of ``number`` cards."""
    drawn: List[Tuple[str, Dict[str, str]]] = []
    categories = list(RARITY_WEIGHTS.keys())
    weights = [RARITY_WEIGHTS[c] for c in categories]
    for _ in range(number):
        cat = random.choices(categories, weights=weights)[0]
        files = cards_by_category.get(cat, [])
        if files:
            selected = random.choice(files)
            drawn.append((cat, selected))
    return drawn

def add_card_to_user(user_id: int, category: str, name: str) -> None:
    """Add a card to the user's inventory in the sheet."""
    user_id_str = str(user_id)
    try:
        all_rows = sheet_cards.get_all_values()
        max_len = max((len(row) for row in all_rows), default=2)
        card_row_index: Optional[int] = None
        for idx, row in enumerate(all_rows):
            if len(row) >= 2 and row[0] == category and row[1] == name:
                card_row_index = idx
                break
        if card_row_index is None:
            new_row = [category, name, f"{user_id_str}:1"]
            sheet_cards.append_row(new_row)
            return
        row = all_rows[card_row_index]
        updated = False
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
                    try:
                        new_count = int(count) + 1
                    except ValueError:
                        new_count = 1
                    row[j] = f"{user_id_str}:{new_count}"
                    updated = True
                    break
        if not updated:
            row.append(f"{user_id_str}:1")
        sheet_cards.update(f"A{card_row_index + 1}", [row])
    except Exception as e:
        app.logger.error(f"Error adding card {name} for user {user_id}: {e}")

def remove_card_from_user(user_id: int, category: str, name: str) -> bool:
    """Remove one instance of the specified card from the user's inventory."""
    user_id_str = str(user_id)
    try:
        all_rows = sheet_cards.get_all_values()
        for idx, row in enumerate(all_rows):
            if len(row) >= 2 and row[0] == category and row[1] == name:
                modified = list(row)
                for j in range(2, len(modified)):
                    cell = modified[j]
                    if cell:
                        try:
                            uid, count = cell.split(':', 1)
                        except ValueError:
                            continue
                        if uid.strip() == user_id_str:
                            try:
                                count_int = int(count)
                            except ValueError:
                                return False
                            if count_int > 1:
                                modified[j] = f"{user_id_str}:{count_int - 1}"
                            else:
                                modified[j] = ''
                            sheet_cards.update(f"A{idx + 1}", [modified])
                            return True
                return False
        return False
    except Exception as e:
        app.logger.error(f"Error removing card {name} for user {user_id}: {e}")
        return False

def get_user_inventory(user_id: int) -> List[Dict[str, Any]]:
    """Return a list of cards (with counts) owned by the user."""
    user_id_str = str(user_id)
    cards: List[Dict[str, Any]] = []
    try:
        all_rows = sheet_cards.get_all_values()
        for row in all_rows:
            if len(row) >= 2:
                category, name = row[0], row[1]
                count = 0
                for cell in row[2:]:
                    if cell:
                        try:
                            uid, c = cell.split(':', 1)
                        except ValueError:
                            continue
                        if uid.strip() == user_id_str:
                            try:
                                count = int(c)
                            except ValueError:
                                count = 0
                            break
                if count > 0:
                    # Attempt to find image id in cards_by_category
                    file_id: Optional[str] = None
                    for f in cards_by_category.get(category, []):
                        if f['name'].rsplit('.', 1)[0] == name:
                            file_id = f['id']
                            break
                    image_url = url_for('card_image', file_id=file_id) if file_id else ''
                    cards.append({
                        'category': category,
                        'name': name,
                        'count': count,
                        'image_url': image_url,
                    })
        return cards
    except Exception as e:
        app.logger.error(f"Error retrieving inventory for user {user_id}: {e}")
        return []

def compute_user_ranking() -> List[Dict[str, Any]]:
    """Compute total card counts per user and return a sorted ranking."""
    user_counts: Dict[str, int] = {}
    try:
        all_rows = sheet_cards.get_all_values()
        for row in all_rows:
            for cell in row[2:]:
                if cell:
                    try:
                        uid, c = cell.split(':', 1)
                        count = int(c)
                    except Exception:
                        continue
                    user_counts[uid] = user_counts.get(uid, 0) + count
        ranking = [
            {'user_id': uid, 'count': cnt} for uid, cnt in user_counts.items()
        ]
        ranking.sort(key=lambda x: x['count'], reverse=True)
        return ranking
    except Exception as e:
        app.logger.error(f"Error computing ranking: {e}")
        return []

def get_exchange_board() -> List[Dict[str, Any]]:
    """Retrieve current exchange offers from the sheet."""
    offers: List[Dict[str, Any]] = []
    try:
        all_rows = sheet_exchange.get_all_values()
        for idx, row in enumerate(all_rows):
            # Skip header row if blank or non‑numerical index
            if idx == 0:
                continue
            if len(row) >= 4:
                owner_id, category, name, timestamp = row[:4]
                # Compose id based on row index (1‑based)
                offer_id = idx
                # Attempt to locate image file id
                file_id = None
                for f in cards_by_category.get(category, []):
                    if f['name'].rsplit('.', 1)[0] == name:
                        file_id = f['id']
                        break
                image_url = url_for('card_image', file_id=file_id) if file_id else ''
                offers.append({
                    'id': offer_id,
                    'owner_id': owner_id,
                    'category': category,
                    'name': name,
                    'timestamp': timestamp,
                    'image_url': image_url,
                })
        return offers
    except Exception as e:
        app.logger.error(f"Error retrieving exchange board: {e}")
        return []

def add_exchange_offer(user_id: int, category: str, name: str) -> None:
    """Add a new exchange offer to the board."""
    try:
        timestamp = datetime.now(pytz.timezone('Europe/Paris')).isoformat()
        sheet_exchange.append_row([str(user_id), category, name, timestamp])
    except Exception as e:
        app.logger.error(f"Error adding exchange offer: {e}")

def remove_exchange_offer(row_index: int) -> None:
    """Remove an offer from the board by its row index (1‑based)."""
    try:
        sheet_exchange.delete_rows(row_index + 1)
    except Exception as e:
        app.logger.error(f"Error removing exchange offer: {e}")

###############################################################################
# Routes
###############################################################################

@app.route('/')
def index() -> Any:
    """Landing page with hero section.  Offers daily draw button when logged in."""
    user = session.get('user')
    return render_template('home.html')

@app.route('/login')
def login() -> Any:
    """Start the Discord OAuth2 flow."""
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not DISCORD_REDIRECT_URI:
        flash('Discord OAuth2 is not configured.', 'error')
        return redirect(url_for('index'))
    redirect_uri = DISCORD_REDIRECT_URI
    return discord_oauth.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def callback() -> Any:
    """Discord OAuth2 callback: exchange code for token and fetch user info."""
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not DISCORD_REDIRECT_URI:
        flash('Discord OAuth2 is not configured.', 'error')
        return redirect(url_for('index'))
    token = discord_oauth.authorize_access_token()
    if not token:
        flash('Failed to retrieve Discord access token.', 'error')
        return redirect(url_for('index'))
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
def logout() -> Any:
    """Log out and clear the session."""
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/card_image/<file_id>')
def card_image(file_id: str) -> Any:
    """Serve an image from Google Drive by file ID."""
    try:
        file_data = drive_service.files().get_media(fileId=file_id).execute()
        return Response(file_data, mimetype='image/png')
    except Exception as e:
        app.logger.error(f"Error retrieving image {file_id}: {e}")
        return ('', 404)

@app.route('/draw')
@login_required
def draw() -> Any:
    """Perform a daily draw for the authenticated user."""
    user = session['user']
    user_id = int(user['id'])
    if not can_perform_daily_draw(user_id):
        flash('Vous avez déjà effectué votre tirage journalier aujourd\u2011hui.', 'info')
        return redirect(url_for('index'))
    drawn_cards = draw_cards(3)
    display_cards = []
    for category, file_info in drawn_cards:
        file_name = file_info['name']
        display_name = file_name.rsplit('.', 1)[0]
        add_card_to_user(user_id, category, display_name)
        # Use our card_image route for reliable loading
        img_url = url_for('card_image', file_id=file_info['id'])
        display_cards.append({
            'category': category,
            'name': display_name,
            'image_url': img_url,
        })
    record_daily_draw(user_id)
    return render_template('draw.html', cards=display_cards)

@app.route('/gallery')
@login_required
def gallery() -> Any:
    """Display the user's gallery of collected cards."""
    user_id = int(session['user']['id'])
    cards = get_user_inventory(user_id)
    return render_template('gallery.html', cards=cards)

@app.route('/ranking')
def ranking() -> Any:
    """Display ranking of users by total number of cards."""
    ranking_data = compute_user_ranking()
    return render_template('ranking.html', ranking=ranking_data)

@app.route('/exchange')
@login_required
def exchange() -> Any:
    """Display the exchange board and allow the user to deposit or trade cards."""
    user_id = int(session['user']['id'])
    offers = get_exchange_board()
    user_cards = get_user_inventory(user_id)
    return render_template('exchange.html', offers=offers, user_cards=user_cards)

@app.route('/exchange/deposit', methods=['POST'])
@login_required
def deposit_offer() -> Any:
    """Handle depositing a card onto the exchange board."""
    user_id = int(session['user']['id'])
    card_key = request.form.get('card_key')
    if card_key:
        category, name = card_key.split('|', 1)
        if remove_card_from_user(user_id, category, name):
            add_exchange_offer(user_id, category, name)
            flash(f'Carte déposée sur le tableau : {name} ({category})', 'success')
        else:
            flash('Impossible de déposer cette carte (non disponible ou erreur).', 'error')
    return redirect(url_for('exchange'))

@app.route('/exchange/take/<int:offer_id>', methods=['POST'])
@login_required
def take_offer(offer_id: int) -> Any:
    """Handle taking an offer from the board in exchange for one of the user's cards."""
    user_id = int(session['user']['id'])
    # Retrieve selected offer
    offers = get_exchange_board()
    offer = next((o for o in offers if o['id'] == offer_id), None)
    if not offer:
        flash('Offre introuvable.', 'error')
        return redirect(url_for('exchange'))
    # User must choose a card to offer in return
    offered_key = request.form.get('offered_card')
    if not offered_key:
        flash('Veuillez sélectionner une carte à échanger.', 'error')
        return redirect(url_for('exchange'))
    offered_category, offered_name = offered_key.split('|', 1)
    # Remove user's offered card
    if not remove_card_from_user(user_id, offered_category, offered_name):
        flash('Vous ne possédez pas cette carte ou une erreur est survenue.', 'error')
        return redirect(url_for('exchange'))
    # Give board card to the user
    add_card_to_user(user_id, offer['category'], offer['name'])
    # Give offered card to the offer owner
    try:
        owner_id = int(offer['owner_id'])
    except ValueError:
        owner_id = None
    if owner_id:
        add_card_to_user(owner_id, offered_category, offered_name)
    # Remove the offer from the board
    remove_exchange_offer(offer_id)
    flash('Échange réalisé avec succès.', 'success')
    return redirect(url_for('exchange'))

@app.route('/sacrifice', methods=['GET', 'POST'])
@login_required
def sacrifice() -> Any:
    """Allow the user to sacrifice a card to draw a new one."""
    user_id = int(session['user']['id'])
    user_cards = get_user_inventory(user_id)
    new_card: Optional[Dict[str, Any]] = None
    if request.method == 'POST':
        card_key = request.form.get('card_key')
        if card_key:
            cat, name = card_key.split('|', 1)
            if remove_card_from_user(user_id, cat, name):
                # Draw one random card; use weighted distribution
                drawn = draw_cards(1)
                if drawn:
                    new_cat, file_info = drawn[0]
                    file_name = file_info['name']
                    display_name = file_name.rsplit('.', 1)[0]
                    add_card_to_user(user_id, new_cat, display_name)
                    img_url = url_for('card_image', file_id=file_info['id'])
                    new_card = {'category': new_cat, 'name': display_name, 'image_url': img_url}
                    flash('Vous avez obtenu une nouvelle carte !', 'success')
                else:
                    flash('Aucune carte disponible à tirer.', 'error')
            else:
                flash('Impossible de sacrifier cette carte.', 'error')
        else:
            flash('Veuillez sélectionner une carte à sacrifier.', 'error')
    return render_template('sacrifice.html', user_cards=user_cards, new_card=new_card)

###############################################################################
# Run the application
###############################################################################

if __name__ == '__main__':
    # For local development only
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)