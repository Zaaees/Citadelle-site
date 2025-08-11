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
import hashlib
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

# Sacrificial draw worksheet.  Tracks sacrificial draws by user ID and date.
sheet_sacrificial_draw = get_or_create_worksheet('Tirages Sacrificiels', rows=1000, cols=2)

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

# Mapping of category names to their respective cards. Each card entry
# includes its Google Drive file ID, the original file name and the
# associated MIME type.
cards_by_category: Dict[str, List[Dict[str, str]]] = {}

# Lookup table to retrieve a file's MIME type by its Drive ID.
file_mime_types: Dict[str, str] = {}

# Expose a mapping from rarity category to a distinct border colour.  These
# values are used in the gallery template to colour‑code cards according to
# their rarity.  Feel free to adjust the hex values to better match your
# visual theme.  The order of ``ALL_CATEGORIES`` reflects descending
# rarity (Secrète being the rarest and Élèves the most common).
CATEGORY_COLORS: Dict[str, str] = {
    "Secrète": "#f1c40f",       # Gold
    "Fondateur": "#e74c3c",    # Red
    "Historique": "#1abc9c",    # Teal
    "Maître": "#9b59b6",        # Purple
    "Black Hole": "#34495e",    # Dark blue/grey
    "Architectes": "#3498db",   # Blue
    "Professeurs": "#27ae60",   # Green
    "Autre": "#95a5a6",        # Grey
    "Élèves": "#bdc3c7",       # Light grey
}

# Maintain a simple in‑memory mapping between Discord user IDs and their
# usernames.  When a user logs in via Discord OAuth we populate this map so
# that the site can display names instead of raw numeric IDs.  Note that
# usernames may change over time; this mapping will reflect the most
# recently seen name for a given ID.
usernames_map: Dict[str, str] = {}

def load_card_files() -> None:
    """Populate ``cards_by_category`` by listing image files in each Drive folder."""
    global cards_by_category, file_mime_types
    cards_by_category = {}
    file_mime_types = {}
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
                {"id": f["id"], "name": f["name"], "mimeType": f.get("mimeType")}
                for f in results.get('files', [])
            ]
            cards_by_category[category] = files
            for f in files:
                if f.get("mimeType"):
                    file_mime_types[f["id"]] = f["mimeType"]
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
        ranking = []
        for uid, cnt in user_counts.items():
            uid_str = str(uid)
            uname = usernames_map.get(uid_str) or f"Utilisateur {uid_str}"
            ranking.append({'user_id': uid_str, 'username': uname, 'count': cnt})
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
                # Unpack row values and pad missing fields with empty strings.  The
                # expected order is owner_id, category, card name, timestamp, comment.
                padded = row + [''] * (5 - len(row))
                owner_id, category, name, timestamp, comment = padded[:5]
                # Compose id based on row index (1‑based)
                offer_id = idx
                # Attempt to locate image file id
                file_id = None
                for f in cards_by_category.get(category, []):
                    if f['name'].rsplit('.', 1)[0] == name:
                        file_id = f['id']
                        break
                image_url = url_for('card_image', file_id=file_id) if file_id else ''
                # Convert owner_id to string for lookup in usernames_map
                owner_id_str = str(owner_id)
                owner_name = usernames_map.get(owner_id_str) or f"Utilisateur {owner_id_str}"
                offers.append({
                    'id': offer_id,
                    'owner_id': owner_id,
                    'owner_name': owner_name,
                    'category': category,
                    'name': name,
                    'timestamp': timestamp,
                    'comment': comment,
                    'image_url': image_url,
                })
        return offers
    except Exception as e:
        app.logger.error(f"Error retrieving exchange board: {e}")
        return []

# ----------------------------------------------------------------------------
# Sacrificial draw helpers
# ----------------------------------------------------------------------------
def can_perform_sacrificial_draw(user_id: int) -> bool:
    """
    Determine whether the user can perform a sacrificial draw today.

    Similar to ``can_perform_daily_draw`` this checks a dedicated worksheet
    (``Tirages Sacrificiels``) for an entry matching the user ID and the
    current date.  If no entry exists or the date differs from today the
    sacrifice is allowed.

    Args:
        user_id: The Discord user ID of the player.

    Returns:
        True if the user can perform a sacrificial draw, False otherwise.
    """
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    user_id_str = str(user_id)
    try:
        all_rows = sheet_sacrificial_draw.get_all_values()
        for row in all_rows:
            if row and row[0] == user_id_str:
                # If the user has already drawn today, the second column will
                # contain today's date.  Otherwise it's either empty or from a
                # previous day and can be overwritten.
                if len(row) > 1 and row[1] == today:
                    return False
                else:
                    return True
        return True
    except Exception as e:
        app.logger.error(f"Error checking sacrificial draw for {user_id}: {e}")
        return False


def record_sacrificial_draw(user_id: int) -> None:
    """
    Record that the user has performed their sacrificial draw today.

    Updates or appends an entry in the ``Tirages Sacrificiels`` worksheet with
    the current date.  The date is stored in the second column of the row
    corresponding to the user.

    Args:
        user_id: The Discord user ID of the player.
    """
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    user_id_str = str(user_id)
    try:
        all_rows = sheet_sacrificial_draw.get_all_values()
        for idx, row in enumerate(all_rows):
            if row and row[0] == user_id_str:
                sheet_sacrificial_draw.update(f"B{idx + 1}", today)
                return
        # No existing entry – append a new row
        sheet_sacrificial_draw.append_row([user_id_str, today])
    except Exception as e:
        app.logger.error(f"Error recording sacrificial draw for {user_id}: {e}")


def select_daily_sacrificial_cards(user_id: int) -> List[Tuple[str, str]]:
    """
    Deterministically select up to five cards from the user's collection for
    sacrificial draw.

    The algorithm mirrors the behaviour of the original Discord bot: it uses
    a deterministic seed derived from the user's ID and the current date
    (Paris timezone) so that the same cards are offered throughout a given
    day.  Cards are chosen from the user's inventory in proportion to the
    number of copies owned.  Only non‑Full variants are considered (this
    implementation assumes card names do not include ``(Full)`` as we work
    purely with the base names).  The resulting list contains unique
    ``(category, name)`` pairs.

    Args:
        user_id: The Discord user ID of the player.

    Returns:
        A list of up to five (category, name) tuples representing the cards
        selected for sacrifice.  The list may be shorter if the user owns
        fewer than five distinct non‑Full cards.
    """
    # Build a mapping of the user's owned cards and their counts
    inventory = get_user_inventory(user_id)
    card_counts: Dict[Tuple[str, str], int] = {}
    for item in inventory:
        category = item.get('category')
        name = item.get('name')
        count = item.get('count', 0)
        # Skip Full variants if any appear (defensive programming)
        if name and '(Full)' in name:
            continue
        if count > 0:
            card_counts[(category, name)] = count
    # No eligible cards
    if not card_counts:
        return []
    # Create a weighted list where each card appears as many times as it is owned
    weighted_cards: List[Tuple[str, str]] = []
    for card, count in card_counts.items():
        weighted_cards.extend([card] * count)
    # Create deterministic random generator based on user ID and current date
    paris_tz = pytz.timezone("Europe/Paris")
    today = datetime.now(paris_tz).strftime("%Y-%m-%d")
    seed_string = f"{user_id}-{today}"
    # Use MD5 to derive a stable integer seed
    seed = int(hashlib.md5(seed_string.encode()).hexdigest(), 16) % (2 ** 32)
    rng = random.Random(seed)
    selected: List[Tuple[str, str]] = []
    selected_set: set = set()
    attempts = 0
    max_attempts = len(weighted_cards) * 2 if weighted_cards else 0
    # Select up to five unique cards
    while len(selected) < 5 and attempts < max_attempts:
        card = rng.choice(weighted_cards)
        if card not in selected_set:
            selected.append(card)
            selected_set.add(card)
        attempts += 1
    return selected


def batch_remove_cards_from_user(user_id: int, cards: List[Tuple[str, str]]) -> bool:
    """
    Remove one instance of each specified card from the user's inventory.

    This helper iterates over the list of ``(category, name)`` tuples and
    removes one copy via ``remove_card_from_user``.  If any removal fails,
    the function returns False.  It does not perform rollback on partial
    failure; however the sacrificial draw route uses this helper only when
    the inventory has already been validated.

    Args:
        user_id: The Discord user ID.
        cards: A list of (category, name) pairs to remove.

    Returns:
        True if all removals succeed, False otherwise.
    """
    for category, name in cards:
        if not remove_card_from_user(user_id, category, name):
            return False
    return True


def handle_sacrifice() -> Any:
    """
    Core implementation of the sacrificial draw route.

    This helper encapsulates the full logic required for the sacrificial draw
    without exposing the legacy behaviour.  It is called by the public
    ``sacrifice`` view, which simply delegates to this function and returns
    its result.

    Returns:
        A rendered template response for the sacrificial draw page.
    """
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))
    user_id = int(user['id'])
    # Check if the user has already performed a sacrificial draw today
    if not can_perform_sacrificial_draw(user_id):
        return render_template(
            'sacrifice.html',
            daily_cards=None,
            new_cards=None,
            sacrifice_done=True,
            error_message="Vous avez déjà effectué votre tirage sacrificiel aujourd'hui. Revenez demain !",
            category_colors=CATEGORY_COLORS,
        )
    # Determine the set of cards selected for today's sacrifice
    selected_pairs = select_daily_sacrificial_cards(user_id)
    # Require at least five cards for the sacrificial draw
    if len(selected_pairs) < 5:
        error_message = (
            f"Vous devez avoir au moins 5 cartes (hors variantes Full) pour effectuer un tirage sacrificiel. "
            f"Vous en avez {len(selected_pairs)}."
        )
        return render_template(
            'sacrifice.html',
            daily_cards=None,
            new_cards=None,
            sacrifice_done=False,
            error_message=error_message,
            category_colors=CATEGORY_COLORS,
        )
    # Build display data for the selected cards: include count and image URL
    inventory = get_user_inventory(user_id)
    counts_map: Dict[Tuple[str, str], int] = {}
    for item in inventory:
        counts_map[(item['category'], item['name'])] = item['count']
    daily_cards: List[Dict[str, Any]] = []
    for (cat, name) in selected_pairs:
        count = counts_map.get((cat, name), 0)
        file_id: Optional[str] = None
        for f in cards_by_category.get(cat, []):
            if f['name'].rsplit('.', 1)[0] == name:
                file_id = f['id']
                break
        img_url = url_for('card_image', file_id=file_id) if file_id else ''
        daily_cards.append({'category': cat, 'name': name, 'count': count, 'image_url': img_url})
    # On POST with confirmation perform the sacrifice
    if request.method == 'POST' and request.form.get('confirm') == '1':
        # Remove one instance of each selected card
        if not batch_remove_cards_from_user(user_id, selected_pairs):
            flash('Erreur lors du retrait des cartes sacrifiées.', 'error')
            return render_template(
                'sacrifice.html',
                daily_cards=daily_cards,
                new_cards=None,
                sacrifice_done=False,
                error_message=None,
                category_colors=CATEGORY_COLORS,
            )
        # Draw three new cards using the weighted distribution
        drawn = draw_cards(3)
        new_cards: List[Dict[str, Any]] = []
        for category, file_info in drawn:
            file_name = file_info['name']
            display_name = file_name.rsplit('.', 1)[0]
            add_card_to_user(user_id, category, display_name)
            img_url = url_for('card_image', file_id=file_info['id'])
            new_cards.append({'category': category, 'name': display_name, 'image_url': img_url})
        # Record the sacrificial draw and notify the user
        record_sacrificial_draw(user_id)
        flash('Sacrifice accompli ! Voici vos nouvelles cartes.', 'success')
        return render_template(
            'sacrifice.html',
            daily_cards=None,
            new_cards=new_cards,
            sacrifice_done=True,
            error_message=None,
            category_colors=CATEGORY_COLORS,
        )
    # Otherwise (GET request) show the selected cards awaiting confirmation
    return render_template(
        'sacrifice.html',
        daily_cards=daily_cards,
        new_cards=None,
        sacrifice_done=False,
        error_message=None,
        category_colors=CATEGORY_COLORS,
    )

def add_exchange_offer(user_id: int, category: str, name: str, comment: str | None = None) -> None:
    """Add a new exchange offer to the board.

    A new offer consists of the user ID, card category, card name, timestamp and
    an optional comment.  The comment allows players to leave a short note
    describing their offer or desired trade.  It is stored in the fifth
    column of the ``Tableau Echanges`` sheet.

    Args:
        user_id: Discord user ID of the offer owner.
        category: Rarity category of the offered card.
        name: Name of the offered card.
        comment: Optional text comment associated with the offer.
    """
    try:
        timestamp = datetime.now(pytz.timezone('Europe/Paris')).isoformat()
        row = [str(user_id), category, name, timestamp]
        # Append comment as fifth column if provided
        if comment is not None:
            row.append(comment)
        sheet_exchange.append_row(row)
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
    # Persist the user's basic info in the session
    session['user'] = {
        'id': user_info.get('id'),
        'username': user_info.get('username'),
        'discriminator': user_info.get('discriminator'),
        'avatar': user_info.get('avatar'),
    }
    # Record the username for this ID in our global mapping.  This allows us
    # to display human‑readable names in the ranking and exchange views.
    try:
        uid = str(user_info.get('id'))
        uname = user_info.get('username') or f"User {uid}"
        usernames_map[uid] = uname
    except Exception:
        pass
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
        mime_type = file_mime_types.get(file_id)
        if not mime_type:
            meta = drive_service.files().get(fileId=file_id, fields="mimeType").execute()
            mime_type = meta.get("mimeType", "application/octet-stream")
            file_mime_types[file_id] = mime_type
        return Response(file_data, mimetype=mime_type)
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
    # Provide category colours so the draw template can colour‑code cards
    return render_template('draw.html', cards=display_cards, category_colors=CATEGORY_COLORS)

@app.route('/gallery')
@login_required
def gallery() -> Any:
    """Display the user's gallery of collected cards."""
    user_id = int(session['user']['id'])
    cards = get_user_inventory(user_id)
    # Group cards by category for easier display
    cards_by_category: Dict[str, List[Dict[str, Any]]] = {}
    for card in cards:
        cards_by_category.setdefault(card['category'], []).append(card)
    # Sort each category alphabetically by card name
    for cat_cards in cards_by_category.values():
        cat_cards.sort(key=lambda c: c['name'])
    # Render the gallery grouped by category.  The Jinja template expects
    # ``cards_by_category`` keyed by category along with the ordered list
    # ``categories`` and the colour mapping ``category_colors``.
    return render_template(
        'gallery.html',
        cards_by_category=cards_by_category,
        categories=ALL_CATEGORIES,
        category_colors=CATEGORY_COLORS,
    )

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
    # Pass the category colour mapping so that offers can be colour‑coded
    return render_template(
        'exchange.html',
        offers=offers,
        user_cards=user_cards,
        category_colors=CATEGORY_COLORS,
    )

@app.route('/exchange/deposit', methods=['POST'])
@login_required
def deposit_offer() -> Any:
    """Handle depositing a card onto the exchange board."""
    user_id = int(session['user']['id'])
    card_key = request.form.get('card_key')
    comment = request.form.get('comment')
    if card_key:
        category, name = card_key.split('|', 1)
        if remove_card_from_user(user_id, category, name):
            # Record the offer with the optional comment (empty string treated as None)
            add_exchange_offer(user_id, category, name, comment if comment else None)
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
    """
    This function delegates all sacrificial draw logic to ``handle_sacrifice``.
    The legacy implementation remains below for reference but will never be
    executed because of the immediate return.
    """
    return handle_sacrifice()
    # -------------------------------------------------------------------------
    # Legacy implementation (no longer used)
    # -------------------------------------------------------------------------
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