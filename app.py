from flask import Flask, jsonify, render_template, request, redirect, url_for, session
from flask_cors import CORS
import sqlite3
import hashlib
import os
from datetime import datetime, timedelta, timezone
import json
import firebase_admin
from firebase_admin import credentials, db

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')

# Keep auth cookies alive beyond default browser session to avoid sudden logouts
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# Constants
ESP32_TIMEOUT_SECONDS = 30  # ESP32 considered offline if no data for 30 seconds
DEFAULT_SOIL_MOISTURE = 50  # Default soil moisture percentage
REGISTRATION_TOKEN = "soilsense-secret-token@2025"  # Required token for registration

# ========== Firebase Setup ==========
FIREBASE_ENABLED = False
sensor_ref = None
commands_ref = None
users_ref = None

def init_firebase():
    global FIREBASE_ENABLED, sensor_ref, commands_ref, users_ref
    
    try:
        # Check if already initialized
        if firebase_admin._apps:
            print("✅ Firebase already initialized")
            FIREBASE_ENABLED = True
            sensor_ref = db.reference('sensor_data')
            commands_ref = db.reference('commands')
            users_ref = db.reference('users')
            return True
        
        # Get environment variables
        project_id = os.environ.get('FIREBASE_PROJECT_ID')
        private_key = os.environ.get('FIREBASE_PRIVATE_KEY', '')
        client_email = os.environ.get('FIREBASE_CLIENT_EMAIL')
        database_url = os.environ.get('FIREBASE_DATABASE_URL')
        
        # Debug: Print what we have (without exposing full private key)
        print(f"📋 Firebase Config Check:")
        print(f"   Project ID: {project_id}")
        print(f"   Client Email: {client_email}")
        print(f"   Database URL: {database_url}")
        print(f"   Private Key Length: {len(private_key)} chars")
        
        # Check if all required variables are present
        if not all([project_id, private_key, client_email, database_url]):
            missing = []
            if not project_id: missing.append('FIREBASE_PROJECT_ID')
            if not private_key: missing.append('FIREBASE_PRIVATE_KEY')
            if not client_email: missing.append('FIREBASE_CLIENT_EMAIL')
            if not database_url: missing.append('FIREBASE_DATABASE_URL')
            print(f"⚠️  Missing environment variables: {', '.join(missing)}")
            return False
        
        # Fix private key format - handle different formats
        if '\\n' in private_key:
            private_key = private_key.replace('\\n', '\n')
        
        # Ensure proper PEM format
        if not private_key.startswith('-----BEGIN PRIVATE KEY-----'):
            print("⚠️  Private key doesn't start with correct header")
            return False
        
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id": project_id,
            "private_key": private_key,
            "client_email": client_email,
            "token_uri": "https://oauth2.googleapis.com/token"
        })
        
        firebase_admin.initialize_app(cred, {
            'databaseURL': database_url
        })
        
        sensor_ref = db.reference('sensor_data')
        commands_ref = db.reference('commands')
        users_ref = db.reference('users')
        FIREBASE_ENABLED = True
        print("✅ Firebase initialized successfully")
        return True
        
    except Exception as e:
        print(f"⚠️  Firebase initialization error: {e}")
        import traceback
        traceback.print_exc()
        return False

# Initialize Firebase on startup
init_firebase()

# ========== Database Setup (SQLite for backup/history) ==========
def init_db():
    conn = sqlite3.connect('soilsense.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sensor_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        soil_avg REAL, soil1 INTEGER, soil2 INTEGER, soil3 INTEGER, soil4 INTEGER,
        temperature REAL, humidity REAL, pump_status TEXT, mode TEXT,
        battery_voltage REAL, battery_percent REAL, current_consumed REAL,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sensor_current (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        soil_percent TEXT, soil_status TEXT, pump_status TEXT, mode TEXT,
        temperature REAL, humidity REAL,
        battery_voltage REAL, current_consumed REAL, battery_percent REAL,
        power_data TEXT, esp32_online INTEGER DEFAULT 0, last_update TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pump_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        command_type TEXT, value TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        executed INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS my_plants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plant_id TEXT NOT NULL,
        plant_name TEXT NOT NULL,
        plant_icon TEXT,
        status TEXT DEFAULT 'planned',
        planted_date TIMESTAMP,
        growing_date TIMESTAMP,
        harvested_date TIMESTAMP,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    # Add new columns if they don't exist (for existing databases)
    try:
        c.execute('ALTER TABLE my_plants ADD COLUMN growing_date TIMESTAMP')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE my_plants ADD COLUMN harvested_date TIMESTAMP')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE sensor_history ADD COLUMN user_id INTEGER')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE pump_commands ADD COLUMN user_id INTEGER')
    except sqlite3.OperationalError:
        pass
    
    c.execute('INSERT OR IGNORE INTO sensor_current (id, esp32_online) VALUES (1, 0)')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect('soilsense.db')
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    """Hash password using SHA256. Note: For production, consider using bcrypt."""
    return hashlib.sha256(password.encode()).hexdigest()

def format_user_for_template(user_row):
    """Convert SQLite Row to dict and format dates for template display."""
    if not user_row:
        return {}
    
    user = dict(user_row)
    
    # Format created_at date nicely
    if user.get('created_at'):
        try:
            if isinstance(user['created_at'], str):
                # Try different date formats
                try:
                    dt = datetime.fromisoformat(user['created_at'].replace('Z', '+00:00'))
                except:
                    try:
                        dt = datetime.strptime(user['created_at'], '%Y-%m-%d %H:%M:%S')
                    except:
                        dt = datetime.strptime(user['created_at'], '%Y-%m-%d')
            else:
                dt = datetime.fromisoformat(str(user['created_at']))
            user['created_at'] = dt.strftime('%B %d, %Y')
        except Exception as e:
            # If parsing fails, keep original value
            print(f"⚠️  Could not format date: {e}")
            pass
    
    return user

def get_soil_avg_from_db(conn=None):
    """Helper function to get average soil moisture from database or Firebase."""
    if FIREBASE_ENABLED and sensor_ref:
        try:
            data = sensor_ref.get()
            if data:
                soil_percent = data.get('soil_percent', [DEFAULT_SOIL_MOISTURE] * 4)
                return sum(soil_percent) / len(soil_percent) if soil_percent else DEFAULT_SOIL_MOISTURE
        except Exception as e:
            print(f"⚠️  Error reading from Firebase: {e}")
    
    # Fallback to SQLite
    if conn is None:
        conn = get_db()
        should_close = True
    else:
        should_close = False
    
    try:
        current = conn.execute('SELECT soil_percent FROM sensor_current WHERE id = 1').fetchone()
        if current and current['soil_percent']:
            try:
                soil_percent = json.loads(current['soil_percent'])
                return sum(soil_percent) / len(soil_percent) if soil_percent else DEFAULT_SOIL_MOISTURE
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                print(f"⚠️  Error parsing soil_percent: {e}")
    finally:
        if should_close:
            conn.close()
    
    return DEFAULT_SOIL_MOISTURE

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ========== Philippine Crops Database (Expanded) ==========
def get_current_season(month):
    return "wet" if month in [6, 7, 8, 9, 10] else "dry"

def get_season_info(month):
    if month in [6, 7, 8, 9, 10]:
        return {"season": "Wet Season", "season_type": "wet", "months": "June - October", 
                "description": "Rainy season with frequent rainfall and higher humidity", "icon": "🌧️"}
    return {"season": "Dry Season", "season_type": "dry", "months": "November - May",
            "description": "Less rainfall, good for crops needing drier conditions", "icon": "☀️"}

# Import the full CROPS_DATABASE from the local version
CROPS_DATABASE = {
    # ========== VEGETABLES ==========
    "rice": {
        "name": "Rice (Palay)", "icon": "🌾", "local_name": "Palay",
        "soil_moisture": {"min": 60, "max": 90}, "season": "wet",
        "description": "Main staple crop of the Philippines. Needs flooded paddies.",
        "tips": "Plant at start of wet season (June-July). Requires standing water 5-10cm deep for best growth. Transplant seedlings 20-25 days old. Space plants 20cm apart. Apply fertilizer at planting and during tillering. Harvest when 80% of grains are golden yellow. Dry harvested rice to 14% moisture before storage."
    },
    "kangkong": {
        "name": "Kangkong", "icon": "🥬", "local_name": "Kangkong",
        "soil_moisture": {"min": 60, "max": 95}, "season": "both",
        "description": "Water spinach that thrives in wet conditions. Very easy to grow.",
        "tips": "Can grow in water or moist soil. Plant cuttings 15-20cm long directly in soil or water. Space 30cm apart. Harvest in 21-30 days by cutting stems 5cm above ground. Fertilize every 2 weeks with compost or organic fertilizer. Can be harvested multiple times - regrows quickly. Keep soil consistently moist or in standing water."
    },
    "kalabasa": {
        "name": "Squash (Kalabasa)", "icon": "🎃", "local_name": "Kalabasa",
        "soil_moisture": {"min": 50, "max": 80}, "season": "wet",
        "description": "Grows well with abundant rainfall. Sprawling vine needs space.",
        "tips": "Rich in Vitamin A. Fruits can be stored for months after harvest. Plant in mounds. Provide trellis if space is limited."
    },
    "gabi": {
        "name": "Taro (Gabi)", "icon": "🥔", "local_name": "Gabi",
        "soil_moisture": {"min": 65, "max": 90}, "season": "wet",
        "description": "Root crop that loves waterlogged conditions. Staple in many Filipino dishes.",
        "tips": "Plant in shaded, wet areas. Leaves (laing) are also edible. Harvest in 8-12 months."
    },
    "banana": {
        "name": "Banana (Saging)", "icon": "🍌", "local_name": "Saging",
        "soil_moisture": {"min": 55, "max": 80}, "season": "wet",
        "description": "Best planted at start of rainy season for establishment.",
        "tips": "Plant suckers or tissue-cultured plants at start of wet season. Space plants 3-4 meters apart. Dig hole 50cm deep and wide. Water deeply 2-3 times per week. Apply organic fertilizer every 3 months. Remove old leaves regularly. Support heavy bunches with props. First harvest in 9-12 months. After harvest, cut main stem and allow new sucker to grow."
    },
    "sitaw": {
        "name": "String Beans (Sitaw)", "icon": "🫛", "local_name": "Sitaw",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Fast-growing legume that tolerates rainy conditions well.",
        "tips": "Provide trellis for climbing. Harvest pods when young and tender. 50-60 days to harvest."
    },
    "tomato": {
        "name": "Tomato (Kamatis)", "icon": "🍅", "local_name": "Kamatis",
        "soil_moisture": {"min": 40, "max": 70}, "season": "dry",
        "description": "Prefers drier season. Too much rain causes fruit cracking and diseases.",
        "tips": "Plant October-February for best results. Start seeds indoors 6-8 weeks before transplanting. Space plants 50-60cm apart. Stake or cage plants when 30cm tall. Prune lower leaves for better airflow and disease prevention. Water at base, avoid wetting leaves. Harvest when fruits are fully colored but still firm. Apply fertilizer every 2-3 weeks during growing season."
    },
    "eggplant": {
        "name": "Eggplant (Talong)", "icon": "🍆", "local_name": "Talong",
        "soil_moisture": {"min": 45, "max": 70}, "season": "dry",
        "description": "Grows well in dry season with controlled watering.",
        "tips": "60-80 days to harvest. Harvest when skin is glossy. Plant in dry season for best results."
    },
    "ampalaya": {
        "name": "Bitter Gourd (Ampalaya)", "icon": "🥒", "local_name": "Ampalaya",
        "soil_moisture": {"min": 45, "max": 75}, "season": "both",
        "description": "Year-round crop if irrigated. Very nutritious vegetable.",
        "tips": "Needs trellis. Harvest when bright green, before turning yellow. 60-70 days to harvest."
    },
    "onion": {
        "name": "Onion (Sibuyas)", "icon": "🧅", "local_name": "Sibuyas",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Needs dry conditions for bulb development. Wet season causes rot.",
        "tips": "Best planted November-January. Stop watering 2 weeks before harvest. 120-150 days to harvest."
    },
    "garlic": {
        "name": "Garlic (Bawang)", "icon": "🧄", "local_name": "Bawang",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Cool, dry conditions for best bulb formation.",
        "tips": "Plant October-December. Harvest when leaves turn yellow-brown. 120-150 days to harvest."
    },
    "pechay": {
        "name": "Pechay", "icon": "🥬", "local_name": "Pechay",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Fast-growing leafy vegetable. Adaptable to various conditions.",
        "tips": "Harvest in 25-40 days. Plant in succession for continuous supply. Provide partial shade in hot weather."
    },
    "corn": {
        "name": "Corn (Mais)", "icon": "🌽", "local_name": "Mais",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Staple crop. Sweet corn varieties popular. Needs space and full sun.",
        "tips": "Plant at start of wet season. Space plants 30cm apart. Harvest when silks turn brown. 70-90 days to harvest."
    },
    "mongo": {
        "name": "Mung Beans (Mongo)", "icon": "🫘", "local_name": "Munggo",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Short-season crop perfect for dry season. Fixes nitrogen in soil.",
        "tips": "Ready in 60-70 days. Good rotation crop after rice. Drought-tolerant."
    },
    "sili": {
        "name": "Chili Pepper (Siling Labuyo)", "icon": "🌶️", "local_name": "Sili",
        "soil_moisture": {"min": 40, "max": 70}, "season": "dry",
        "description": "Prefers less rain. Too much moisture causes flower drop.",
        "tips": "Harvest when fully colored. Wear gloves when handling hot varieties. Multiple harvests possible."
    },
    "okra": {
        "name": "Okra", "icon": "🌿", "local_name": "Okra",
        "soil_moisture": {"min": 40, "max": 65}, "season": "dry",
        "description": "Drought-tolerant once established. Thrives in hot, dry weather.",
        "tips": "Harvest pods when 7-10cm long for best tenderness. Harvest every 2 days."
    },
    "sweet_potato": {
        "name": "Sweet Potato (Kamote)", "icon": "🍠", "local_name": "Kamote",
        "soil_moisture": {"min": 40, "max": 70}, "season": "both",
        "description": "Drought-tolerant once established. Leaves are also edible.",
        "tips": "3-4 months to harvest. Cuttings can be used for next planting. Very hardy."
    },
    "papaya": {
        "name": "Papaya", "icon": "🥭", "local_name": "Papaya",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Year-round fruiting with good care. Fast-growing tree.",
        "tips": "Fruits in 9-11 months. Remove male plants if not needed for pollination. Well-drained soil essential."
    },
    "upo": {
        "name": "Bottle Gourd (Upo)", "icon": "🥒", "local_name": "Upo",
        "soil_moisture": {"min": 50, "max": 80}, "season": "wet",
        "description": "Grows vigorously in wet season. Large fruits need support.",
        "tips": "Harvest young for tender texture. Can grow very large if left. Needs trellis."
    },
    "patola": {
        "name": "Luffa (Patola)", "icon": "🥒", "local_name": "Patola",
        "soil_moisture": {"min": 50, "max": 80}, "season": "wet",
        "description": "Thrives in humid conditions. When mature, can be dried for sponge.",
        "tips": "Harvest young for eating (7-10 days after flowering). Needs trellis."
    },
    "malunggay": {
        "name": "Moringa (Malunggay)", "icon": "🌿", "local_name": "Malunggay",
        "soil_moisture": {"min": 30, "max": 65}, "season": "both",
        "description": "Extremely drought-tolerant superfood. Grows in poor soil.",
        "tips": "Cut branches regularly to promote leaf growth. Very nutritious. Easy to propagate."
    },
    "mustasa": {
        "name": "Mustard (Mustasa)", "icon": "🥬", "local_name": "Mustasa",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Fast-growing leafy green. Popular in Filipino dishes like sinigang.",
        "tips": "Harvest in 30-45 days. Can be harvested multiple times. Plant in partial shade."
    },
    "repolyo": {
        "name": "Cabbage (Repolyo)", "icon": "🥬", "local_name": "Repolyo",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Cool-season crop. Forms tight heads. Good for coleslaw and pickling.",
        "tips": "Plant October-February. Needs consistent moisture. Harvest when head is firm."
    },
    "letsugas": {
        "name": "Lettuce (Letsugas)", "icon": "🥬", "local_name": "Letsugas",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Cool-season leafy green. Perfect for salads and sandwiches.",
        "tips": "Plant October-February. Harvest outer leaves or whole head. Prefers partial shade."
    },
    "radish": {
        "name": "Radish (Labanos)", "icon": "🥕", "local_name": "Labanos",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Fast-growing root vegetable. Ready in 25-30 days. Crisp and spicy.",
        "tips": "Plant October-February. Don't let soil dry out. Harvest when roots are 2-3cm."
    },
    "carrot": {
        "name": "Carrot (Karot)", "icon": "🥕", "local_name": "Karot",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Sweet root vegetable. Rich in Vitamin A. Good for highland areas.",
        "tips": "Plant October-February. Loose, well-drained soil. Harvest in 70-80 days."
    },
    "cucumber": {
        "name": "Cucumber (Pipino)", "icon": "🥒", "local_name": "Pipino",
        "soil_moisture": {"min": 50, "max": 80}, "season": "both",
        "description": "Refreshing vegetable. Great for salads and pickling. Needs trellis.",
        "tips": "Harvest when 15-20cm long. Keep soil moist. Plant in partial shade."
    },
    "cassava": {
        "name": "Cassava (Kamoteng Kahoy)", "icon": "🌿", "local_name": "Kamoteng Kahoy",
        "soil_moisture": {"min": 40, "max": 70}, "season": "both",
        "description": "Drought-tolerant root crop. Staple food in many regions. Very hardy.",
        "tips": "Plant stem cuttings. Harvest in 8-12 months. Leaves are also edible."
    },
    "ginger": {
        "name": "Ginger (Luya)", "icon": "🫚", "local_name": "Luya",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Aromatic rhizome. Essential in Filipino cooking. Medicinal properties.",
        "tips": "Plant rhizome pieces. Needs partial shade. Harvest in 8-10 months."
    },
    "turmeric": {
        "name": "Turmeric (Luyang Dilaw)", "icon": "🫚", "local_name": "Luyang Dilaw",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Golden spice. Anti-inflammatory properties. Used in cooking and medicine.",
        "tips": "Similar to ginger. Plant rhizomes. Partial shade. Harvest in 8-10 months."
    },
    "spinach": {
        "name": "Spinach (Kulitis)", "icon": "🥬", "local_name": "Kulitis",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Nutritious leafy green. High in iron and vitamins. Fast-growing.",
        "tips": "Plant October-February. Harvest in 30-40 days. Can harvest multiple times."
    },
    "broccoli": {
        "name": "Broccoli", "icon": "🥦", "local_name": "Broccoli",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Cool-season vegetable. Rich in vitamins. Best in highland areas.",
        "tips": "Plant October-February. Needs consistent moisture. Harvest when head is tight."
    },
    "cauliflower": {
        "name": "Cauliflower", "icon": "🥦", "local_name": "Cauliflower",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Cool-season crop. White, purple, or orange varieties. Best in highlands.",
        "tips": "Plant October-February. Tie leaves over head to keep white. Harvest when head is firm."
    },
    "bell_pepper": {
        "name": "Bell Pepper (Siling Pula)", "icon": "🫑", "local_name": "Siling Pula",
        "soil_moisture": {"min": 45, "max": 70}, "season": "dry",
        "description": "Sweet pepper. Green, red, yellow varieties. Rich in Vitamin C.",
        "tips": "Plant October-February. Stake plants. Harvest when firm and fully colored."
    },
    "squash_flower": {
        "name": "Squash Flower (Bulaklak ng Kalabasa)", "icon": "🌼", "local_name": "Bulaklak ng Kalabasa",
        "soil_moisture": {"min": 50, "max": 80}, "season": "wet",
        "description": "Edible flowers from squash plant. Delicate and delicious. Popular in Filipino cuisine.",
        "tips": "Harvest male flowers in morning. Use fresh. Can be stuffed or fried."
    },
    
    # ========== FRUITS ==========
    "mango": {
        "name": "Mango (Mangga)", "icon": "🥭", "local_name": "Mangga",
        "soil_moisture": {"min": 40, "max": 70}, "season": "dry",
        "description": "Best fruiting in dry season. Philippine national fruit. Excellent for backyard growing.",
        "tips": "Prune after harvest. Best varieties: Carabao, Indian. Fruits March-June. Water regularly when young."
    },
    "calamansi": {
        "name": "Calamansi (Philippine Lime)", "icon": "🍋", "local_name": "Kalamansi",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Year-round citrus fruit. Very popular in Filipino cuisine. Easy to grow.",
        "tips": "Fruits 2-3 years from planting. Prune regularly. Good in pots. Fertilize every 3 months."
    },
    "guava": {
        "name": "Guava (Bayabas)", "icon": "🍐", "local_name": "Bayabas",
        "soil_moisture": {"min": 45, "max": 75}, "season": "both",
        "description": "Hardy fruit tree. Bears fruit twice a year. Very nutritious and high in Vitamin C.",
        "tips": "Fruits in 2-3 years. Prune to maintain shape. Pink and white varieties. Can grow from seeds or cuttings."
    },
    "pineapple": {
        "name": "Pineapple (Pinya)", "icon": "🍍", "local_name": "Pinya",
        "soil_moisture": {"min": 35, "max": 65}, "season": "dry",
        "description": "Drought-tolerant once established. Sweet varieties grow well in Philippines.",
        "tips": "Plant crown or suckers. Fruits in 18-24 months. Queen and MD2 are popular varieties."
    },
    "dragon_fruit": {
        "name": "Dragon Fruit (Pitaya)", "icon": "🐉", "local_name": "Dragon Fruit",
        "soil_moisture": {"min": 40, "max": 70}, "season": "dry",
        "description": "Cactus fruit gaining popularity. Grows well in warm climate. Beautiful flowers bloom at night.",
        "tips": "Needs support/trellis. Fruits 1-2 years from cuttings. White and red flesh varieties available."
    },
    "coconut": {
        "name": "Coconut (Niyog)", "icon": "🥥", "local_name": "Niyog",
        "soil_moisture": {"min": 50, "max": 80}, "season": "both",
        "description": "Tree of life. Every part is useful. Fruits year-round. Iconic Philippine tree.",
        "tips": "Plant in full sun. Fruits in 5-7 years. Needs space. Very drought-tolerant once established."
    },
    "jackfruit": {
        "name": "Jackfruit (Langka)", "icon": "🍈", "local_name": "Langka",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Largest tree fruit. Sweet and aromatic. Used in many Filipino dishes.",
        "tips": "Plant in wet season. Fruits in 3-4 years. Needs space. Harvest when fruit sounds hollow."
    },
    "durian": {
        "name": "Durian", "icon": "🌰", "local_name": "Durian",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "King of fruits. Strong aroma. Creamy texture. Best in Mindanao region.",
        "tips": "Needs tropical climate. Fruits in 5-7 years. Plant in wet season. Needs space."
    },
    "rambutan": {
        "name": "Rambutan", "icon": "🍒", "local_name": "Rambutan",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Hairy red fruit. Sweet and juicy. Similar to litchi. Popular in Mindanao.",
        "tips": "Plant in wet season. Fruits in 3-4 years. Needs consistent moisture. Harvest when red."
    },
    "lanzones": {
        "name": "Lanzones", "icon": "🍇", "local_name": "Lanzones",
        "soil_moisture": {"min": 55, "max": 80}, "season": "wet",
        "description": "Sweet, translucent fruit. Grows in clusters. Best in Mindanao and some Luzon areas.",
        "tips": "Needs high humidity. Fruits in 5-6 years. Plant in wet season. Harvest when yellow."
    },
    "starfruit": {
        "name": "Starfruit (Balimbing)", "icon": "⭐", "local_name": "Balimbing",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Star-shaped fruit. Sweet and tangy. Rich in Vitamin C. Easy to grow.",
        "tips": "Fruits in 2-3 years. Can grow in pots. Harvest when yellow. Prune regularly."
    },
    "soursop": {
        "name": "Soursop (Guyabano)", "icon": "🍈", "local_name": "Guyabano",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Creamy white flesh. Sweet-tart flavor. Medicinal properties. Popular fruit tree.",
        "tips": "Fruits in 3-4 years. Needs space. Harvest when slightly soft. Rich in Vitamin C."
    },
    "santol": {
        "name": "Santol", "icon": "🍈", "local_name": "Santol",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Tart fruit. Used in jams and preserves. Fast-growing tree. Very hardy.",
        "tips": "Fruits in 3-4 years. Very drought-tolerant. Harvest when yellow. Can be eaten fresh or processed."
    },
    "atis": {
        "name": "Sugar Apple (Atis)", "icon": "🍈", "local_name": "Atis",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Sweet, creamy fruit. Segmented flesh. Very popular. Easy to grow.",
        "tips": "Fruits in 2-3 years. Can grow in pots. Harvest when fruit separates easily. Rich in Vitamin C."
    },
    "chico": {
        "name": "Sapodilla (Chico)", "icon": "🍈", "local_name": "Chico",
        "soil_moisture": {"min": 45, "max": 70}, "season": "dry",
        "description": "Sweet, brown fruit. Caramel-like flavor. Very popular. Drought-tolerant.",
        "tips": "Fruits in 3-4 years. Very hardy. Harvest when soft. Can grow in various soil types."
    },
    "duhat": {
        "name": "Java Plum (Duhat)", "icon": "🍇", "local_name": "Duhat",
        "soil_moisture": {"min": 50, "max": 75}, "season": "wet",
        "description": "Small purple fruit. Astringent when unripe, sweet when ripe. Very hardy tree.",
        "tips": "Fruits in 3-4 years. Very drought-tolerant. Harvest when dark purple. Used in jams."
    },
    "macopa": {
        "name": "Rose Apple (Macopa)", "icon": "🍎", "local_name": "Macopa",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Bell-shaped fruit. Crisp and refreshing. Pink or white varieties. Easy to grow.",
        "tips": "Fruits in 2-3 years. Very hardy. Harvest when firm. Can grow in pots."
    },
    "watermelon": {
        "name": "Watermelon (Pakwan)", "icon": "🍉", "local_name": "Pakwan",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Refreshing summer fruit. High water content. Perfect for hot weather.",
        "tips": "Plant October-February. Needs space. Harvest when bottom turns yellow. 70-90 days to harvest."
    },
    "cantaloupe": {
        "name": "Cantaloupe (Melon)", "icon": "🍈", "local_name": "Melon",
        "soil_moisture": {"min": 50, "max": 75}, "season": "dry",
        "description": "Sweet, aromatic melon. Rich in Vitamin A. Perfect for hot, dry season.",
        "tips": "Plant October-February. Needs space. Harvest when stem separates easily. 80-90 days."
    },
    
    # ========== FLOWERS ==========
    "sampaguita": {
        "name": "Sampaguita (Philippine Jasmine)", "icon": "🌼", "local_name": "Sampaguita",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "National flower of the Philippines. Fragrant white flowers bloom year-round.",
        "tips": "Well-drained soil. Prune regularly. Flowers best in morning sun. Can be grown in pots."
    },
    "santan": {
        "name": "Santan (Ixora)", "icon": "🌺", "local_name": "Santan",
        "soil_moisture": {"min": 45, "max": 70}, "season": "both",
        "description": "Popular hedge plant with clusters of red, orange, yellow, or pink flowers.",
        "tips": "Full sun to partial shade. Blooms year-round. Trim after flowering for bushier growth."
    },
    "gumamela": {
        "name": "Gumamela (Hibiscus)", "icon": "🌺", "local_name": "Gumamela",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Large showy flowers in red, pink, yellow, or white. Very easy to grow.",
        "tips": "Full sun. Deadhead spent flowers. Can propagate easily from cuttings."
    },
    "rosal": {
        "name": "Rosal (Rose)", "icon": "🌹", "local_name": "Rosal",
        "soil_moisture": {"min": 45, "max": 70}, "season": "dry",
        "description": "Classic garden flower. Grows well in Philippine highlands and cooler dry season.",
        "tips": "Plant in well-drained soil. Prune in dry season. Morning watering prevents fungal diseases."
    },
    "marigold": {
        "name": "Marigold", "icon": "🌼", "local_name": "Marigold",
        "soil_moisture": {"min": 40, "max": 65}, "season": "dry",
        "description": "Bright orange or yellow flowers. Natural pest repellent for gardens.",
        "tips": "Plant October-February. Pinch tops to encourage bushy growth. Companion plant for vegetables."
    },
    "sunflower": {
        "name": "Sunflower", "icon": "🌻", "local_name": "Sunflower",
        "soil_moisture": {"min": 40, "max": 70}, "season": "dry",
        "description": "Tall, cheerful flowers that follow the sun. Grows 4-8 feet tall.",
        "tips": "Plant in dry season (Nov-Feb). Needs full sun. Support tall stems. Seeds edible when mature."
    },
    "zinnia": {
        "name": "Zinnia", "icon": "🌸", "local_name": "Zinnia",
        "soil_moisture": {"min": 40, "max": 65}, "season": "dry",
        "description": "Colorful, long-lasting blooms. Perfect for cut flowers. Heat-tolerant.",
        "tips": "Easy to grow from seeds. Deadhead for continuous blooms. Attracts butterflies."
    },
    "bougainvillea": {
        "name": "Bougainvillea", "icon": "🌺", "local_name": "Bougainvillea",
        "soil_moisture": {"min": 30, "max": 60}, "season": "dry",
        "description": "Vibrant pink, red, orange, or white bracts. Very drought-tolerant once established.",
        "tips": "Dry season triggers best blooms. Minimal watering. Prune after flowering. Great for trellises."
    },
    "adelfa": {
        "name": "Adelfa (Oleander)", "icon": "🌸", "local_name": "Adelfa",
        "soil_moisture": {"min": 35, "max": 65}, "season": "both",
        "description": "Hardy shrub with fragrant pink, white, or red flowers. Very heat and drought tolerant.",
        "tips": "Full sun. Low maintenance. WARNING: All parts toxic if ingested. Keep away from children/pets."
    },
    "kalachuchi": {
        "name": "Kalachuchi (Plumeria)", "icon": "🌺", "local_name": "Kalachuchi",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Fragrant flowers in white, pink, yellow, or red. Blooms best in dry season.",
        "tips": "Very drought-tolerant. Blooms when leaves drop in dry season. Easy to propagate from cuttings."
    },
    "waling_waling": {
        "name": "Waling-Waling (Orchid)", "icon": "🌸", "local_name": "Waling-Waling",
        "soil_moisture": {"min": 55, "max": 75}, "season": "both",
        "description": "Queen of Philippine orchids. Large, fragrant flowers. Grows in humid conditions.",
        "tips": "Needs high humidity. Partial shade. Use orchid mix, not soil. Mist regularly."
    },
    "vanda": {
        "name": "Vanda (Orchid)", "icon": "🌸", "local_name": "Vanda",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Popular orchid with stunning blooms. Grows well hanging with exposed roots.",
        "tips": "High humidity. Daily watering in dry season. No potting medium needed. Loves morning sun."
    },
    "caladium": {
        "name": "Caladium", "icon": "🌿", "local_name": "Caladium",
        "soil_moisture": {"min": 55, "max": 80}, "season": "wet",
        "description": "Colorful heart-shaped leaves in pink, red, white patterns. Foliage plant.",
        "tips": "Shade to partial sun. Loves humidity. Plant bulbs at start of wet season. Beautiful in pots."
    },
    "anthurium": {
        "name": "Anthurium", "icon": "🌺", "local_name": "Anthurium",
        "soil_moisture": {"min": 55, "max": 75}, "season": "both",
        "description": "Glossy heart-shaped 'flowers' (actually bracts) in red, pink, or white. Long-lasting.",
        "tips": "Bright indirect light. High humidity. Well-draining soil. Popular as indoor plant."
    },
    "cosmos": {
        "name": "Cosmos", "icon": "🌸", "local_name": "Cosmos",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Delicate daisy-like flowers in pink, white, or purple. Grows 2-4 feet tall.",
        "tips": "Very easy to grow from seed. Plant Nov-Jan. Self-seeds readily. Attracts bees and butterflies."
    },
    "dama_de_noche": {
        "name": "Night-Blooming Jasmine (Dama de Noche)", "icon": "🌙", "local_name": "Dama de Noche",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Fragrant white flowers that bloom at night. Intoxicating scent fills the air.",
        "tips": "Plant in partial shade. Blooms year-round. Prune regularly. Perfect for evening gardens."
    },
}

def get_crop_suggestions(soil_moisture, current_month):
    season = get_current_season(current_month)
    all_suggestions = []
    
    for crop_id, crop in CROPS_DATABASE.items():
        crop_min = crop["soil_moisture"]["min"]
        crop_max = crop["soil_moisture"]["max"]
        crop_season = crop["season"]
        
        # Calculate match score (0-100)
        season_match = crop_season == "both" or crop_season == season
        
        # Moisture score based on distance from optimal range
        if crop_min <= soil_moisture <= crop_max:
            moisture_score = 100  # Perfect match
        else:
            # Calculate how far outside the range
            if soil_moisture < crop_min:
                distance = crop_min - soil_moisture
            else:
                distance = soil_moisture - crop_max
            # Decrease score based on distance (max penalty at 30% away)
            moisture_score = max(0, 100 - (distance * 2))
        
        # Season score
        if season_match:
            season_score = 100
        elif crop_season == "both":
            season_score = 80
        else:
            season_score = 40  # Wrong season
        
        # Overall score (weighted average)
        overall_score = int((moisture_score * 0.6) + (season_score * 0.4))
        
        # Build reasons list
        reasons = []
        if season_match:
            reasons.append("✓ Perfect season")
        if crop_min <= soil_moisture <= crop_max:
            reasons.append("✓ Ideal moisture")
        if crop_season == "both":
            reasons.append("✓ Year-round")
        if overall_score >= 80:
            reasons.append("✓ Highly recommended")
        
        # Format crop data for frontend
        crop_data = {
            "id": crop_id,
            "name": crop["name"],
            "icon": crop["icon"],
            "local_name": crop["local_name"],
            "season_type": crop["season"],
            "description": crop["description"],
            "tips": crop["tips"],
            "ideal_soil": f"{crop_min}-{crop_max}%",
            "score": overall_score,
            "reasons": reasons if reasons else ["Consider with care"]
        }
        
        all_suggestions.append(crop_data)
    
    # Sort by score (highest first) and return top 15
    all_suggestions.sort(key=lambda x: x["score"], reverse=True)
    return all_suggestions[:15]

# ========== Debug/Test Endpoints ==========
@app.route('/api/test/firebase')
def test_firebase():
    """Test endpoint to check Firebase connection"""
    result = {
        "firebase_enabled": FIREBASE_ENABLED,
        "env_vars": {
            "FIREBASE_PROJECT_ID": bool(os.environ.get('FIREBASE_PROJECT_ID')),
            "FIREBASE_CLIENT_EMAIL": bool(os.environ.get('FIREBASE_CLIENT_EMAIL')),
            "FIREBASE_DATABASE_URL": bool(os.environ.get('FIREBASE_DATABASE_URL')),
            "FIREBASE_PRIVATE_KEY": bool(os.environ.get('FIREBASE_PRIVATE_KEY')),
            "FIREBASE_PRIVATE_KEY_LENGTH": len(os.environ.get('FIREBASE_PRIVATE_KEY', ''))
        }
    }
    
    if FIREBASE_ENABLED:
        try:
            # Try to read from Firebase
            test_data = sensor_ref.get()
            result["firebase_read"] = "success"
            result["current_data"] = test_data
        except Exception as e:
            result["firebase_read"] = f"error: {str(e)}"
    
    return jsonify(result)

# ========== Auth Routes ==========
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Try SQLite first
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if user and user['password'] == hash_password(password):
            # Make session cookie persistent so it doesn't expire unexpectedly
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        
        return render_template('login.html', error='Invalid username or password')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        registration_token = request.form.get('registration_token', '').strip()
        
        # Validate registration token
        if registration_token != REGISTRATION_TOKEN:
            return render_template('register.html', error='Invalid registration token. Please provide a valid token to register.')
        
        if password != confirm_password:
            return render_template('register.html', error='Passwords do not match')
        if len(password) < 6:
            return render_template('register.html', error='Password must be at least 6 characters')
        
        # Check for duplicate username and email before attempting insert
        conn = get_db()
        try:
            # Check if username already exists
            existing_username = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if existing_username:
                conn.close()
                return render_template('register.html', error='Username already exists. Please choose a different username.')
            
            # Check if email already exists
            existing_email = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
            if existing_email:
                conn.close()
                return render_template('register.html', error='Email already exists. Please use a different email address.')
            
            # Store in SQLite
            conn.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                        (username, email, hash_password(password)))
            conn.commit()
            conn.close()
            
            # Also store in Firebase if enabled
            if FIREBASE_ENABLED and users_ref:
                try:
                    users_ref.child(username).set({
                        'username': username,
                        'email': email,
                        'created_at': datetime.now().isoformat()
                    })
                    print(f"✅ User {username} also saved to Firebase")
                except Exception as e:
                    print(f"⚠️  Could not save user to Firebase: {e}")
            
            return redirect(url_for('login'))
        except Exception as e:
            if conn:
                conn.close()
            return render_template('register.html', error=f'Registration failed: {str(e)}')
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/account')
@login_required
def account():
    conn = get_db()
    user_row = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    
    user = format_user_for_template(user_row)
    return render_template('account.html', user=user)

@app.route('/account/update', methods=['POST'])
@login_required
def update_account():
    username = request.form.get('username')
    email = request.form.get('email')
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    
    if not current_password or user['password'] != hash_password(current_password):
        conn.close()
        user_dict = format_user_for_template(user)
        return render_template('account.html', user=user_dict, error='Current password is incorrect')
    
    if new_password:
        if len(new_password) < 6:
            conn.close()
            user_dict = format_user_for_template(user)
            return render_template('account.html', user=user_dict, error='New password must be at least 6 characters')
        if new_password != confirm_password:
            conn.close()
            user_dict = format_user_for_template(user)
            return render_template('account.html', user=user_dict, error='New passwords do not match')
        conn.execute('UPDATE users SET username = ?, email = ?, password = ? WHERE id = ?',
                    (username, email, hash_password(new_password), session['user_id']))
    else:
        conn.execute('UPDATE users SET username = ?, email = ? WHERE id = ?',
                    (username, email, session['user_id']))
    
    conn.commit()
    # Refresh user data from database
    user_row = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    session['username'] = username
    
    user_dict = format_user_for_template(user_row)
    return render_template('account.html', user=user_dict, success='Account updated successfully')

# ========== Dashboard Routes ==========
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('index.html', username=session.get('username'))

@app.route('/crops')
@login_required
def crops():
    current_month = datetime.now().month
    season_info = get_season_info(current_month)
    
    conn = get_db()
    stats = conn.execute('''SELECT AVG(soil_avg) as avg_soil FROM sensor_history 
        WHERE strftime('%Y-%m', recorded_at) = ?''', (datetime.now().strftime('%Y-%m'),)).fetchone()
    
    if stats and stats['avg_soil']:
        soil_avg = stats['avg_soil']
    else:
        soil_avg = get_soil_avg_from_db(conn)
    
    conn.close()
    
    suggestions = get_crop_suggestions(soil_avg, current_month)
    
    return render_template('crops.html', 
                         username=session.get('username'),
                         month_name=datetime.now().strftime('%B'),
                         season=season_info,
                         soil_moisture=round(soil_avg, 1),
                         suggestions=suggestions)

@app.route('/my-plants')
@login_required
def my_plants():
    conn = get_db()
    plants = conn.execute('''SELECT * FROM my_plants 
                            WHERE user_id = ? 
                            ORDER BY created_at DESC''', 
                         (session.get('user_id'),)).fetchall()
    conn.close()
    
    return render_template('my_plants.html', 
                         username=session.get('username'),
                         plants=[dict(p) for p in plants])

@app.route('/history')
@login_required
def history():
    """History page showing pump events and soil moisture history"""
    return render_template('history.html', username=session.get('username'))

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html', username=session.get('username'))

# ========== My Plants API ==========
@app.route('/api/my_plants/add', methods=['POST'])
@login_required
def add_my_plant():
    conn = None
    try:
        data = request.json
        conn = get_db()
        
        # Check if already added
        existing = conn.execute('''SELECT id FROM my_plants 
                                  WHERE user_id = ? AND plant_id = ?''',
                               (session.get('user_id'), data['plant_id'])).fetchone()
        
        if existing:
            return jsonify({"success": False, "error": "Plant already in your list"})
        
        conn.execute('''INSERT INTO my_plants 
                       (user_id, plant_id, plant_name, plant_icon, notes) 
                       VALUES (?, ?, ?, ?, ?)''',
                    (session.get('user_id'), 
                     data['plant_id'],
                     data['plant_name'],
                     data.get('plant_icon', '🌱'),
                     data.get('notes', '')))
        conn.commit()
        result = conn.execute('SELECT last_insert_rowid()').fetchone()
        plant_id = result[0] if result else None
        
        return jsonify({"success": True, "plant_id": plant_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/my_plants/remove/<int:plant_id>', methods=['DELETE'])
@login_required
def remove_my_plant(plant_id):
    conn = None
    try:
        conn = get_db()
        conn.execute('''DELETE FROM my_plants 
                       WHERE id = ? AND user_id = ?''',
                    (plant_id, session.get('user_id')))
        conn.commit()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/my_plants/update/<int:plant_id>', methods=['POST'])
@login_required
def update_my_plant(plant_id):
    conn = None
    try:
        data = request.json
        conn = get_db()
        
        updates = []
        params = []
        
        if 'status' in data:
            updates.append('status = ?')
            params.append(data['status'])
        
        if 'planted_date' in data:
            updates.append('planted_date = ?')
            params.append(data['planted_date'])
        
        if 'growing_date' in data:
            updates.append('growing_date = ?')
            params.append(data['growing_date'])
        
        if 'harvested_date' in data:
            updates.append('harvested_date = ?')
            params.append(data['harvested_date'])
        
        # Auto-update dates based on status changes (only if date not manually provided)
        if 'status' in data:
            current_status = data['status']
            now = datetime.now().isoformat()
            
            # Get current plant data
            existing_plant = conn.execute('SELECT planted_date, growing_date, harvested_date FROM my_plants WHERE id = ?', (plant_id,)).fetchone()
            
            # Check if we already added these fields to updates
            has_planted_update = any('planted_date' in u for u in updates)
            has_growing_update = any('growing_date' in u for u in updates)
            has_harvested_update = any('harvested_date' in u for u in updates)
            
            # If status changed to planted and no planted_date provided, set it
            if current_status == 'planted' and not has_planted_update:
                if not existing_plant or not existing_plant['planted_date']:
                    updates.append('planted_date = ?')
                    params.append(now)
            
            # If status changed to growing and no growing_date provided, set it
            if current_status == 'growing' and not has_growing_update:
                if not existing_plant or not existing_plant['growing_date']:
                    updates.append('growing_date = ?')
                    params.append(now)
            
            # If status changed to harvested and no harvested_date provided, set it
            if current_status == 'harvested' and not has_harvested_update:
                if not existing_plant or not existing_plant['harvested_date']:
                    updates.append('harvested_date = ?')
                    params.append(now)
        
        if 'notes' in data:
            updates.append('notes = ?')
            params.append(data['notes'])
        
        if updates:
            params.extend([plant_id, session.get('user_id')])
            conn.execute(f'''UPDATE my_plants 
                           SET {', '.join(updates)}
                           WHERE id = ? AND user_id = ?''', params)
            conn.commit()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/my_plants/list')
@login_required
def get_my_plants():
    conn = None
    try:
        conn = get_db()
        plants = conn.execute('''SELECT * FROM my_plants 
                                WHERE user_id = ? 
                                ORDER BY created_at DESC''',
                             (session.get('user_id'),)).fetchall()
        
        return jsonify([dict(p) for p in plants])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

# ========== ESP32 API with Firebase ==========
@app.route('/api/esp32/push', methods=['POST'])
@app.route('/esp32/push_data', methods=['POST'])
def esp32_push_data():
    """ESP32 pushes data to Firebase and SQLite"""
    conn = None
    try:
        data = request.get_json()
        # Store timestamps in UTC with timezone info to avoid display offsets
        timestamp = datetime.now(timezone.utc).isoformat()
        
        print(f"📥 Received data from ESP32: {data}")
        print(f"🔥 Firebase enabled: {FIREBASE_ENABLED}")
        
        # Store in Firebase (if enabled)
        if FIREBASE_ENABLED and sensor_ref:
            try:
                sensor_ref.set({
                    'soil_percent': data.get('soil_percent', [0,0,0,0]),
                    'soil_status': data.get('soil_status', 'UNKNOWN'),
                    'pump_status': data.get('pump_status', 'OFF'),
                    'mode': data.get('mode', 'AUTO'),
                    'temperature': data.get('temperature'),
                    'humidity': data.get('humidity'),
                    'battery_voltage': data.get('battery_voltage'),
                    'battery_percent': data.get('battery_percent'),
                    'current_consumed': data.get('current_consumed'),
                    'power': data.get('power', {}),
                    'timestamp': timestamp
                })
                print("✅ Data pushed to Firebase")
            except Exception as e:
                print(f"⚠️  Firebase write error: {e}")
        else:
            print("⚠️  Firebase not enabled, skipping Firebase write")
        
        # Also store in SQLite for backup and history
        conn = get_db()
        conn.execute('''UPDATE sensor_current SET
            soil_percent = ?, soil_status = ?, pump_status = ?, mode = ?,
            temperature = ?, humidity = ?, battery_voltage = ?, 
            current_consumed = ?, battery_percent = ?, power_data = ?, 
            esp32_online = 1, last_update = ? WHERE id = 1''', 
            (json.dumps(data.get('soil_percent', [0,0,0,0])), data.get('soil_status', 'UNKNOWN'),
             data.get('pump_status', 'OFF'), data.get('mode', 'AUTO'),
             data.get('temperature'), data.get('humidity'),
             data.get('battery_voltage'), data.get('current_consumed'),
             data.get('battery_percent'), json.dumps(data.get('power', {})), timestamp))
        
        # Add to history
        soil_percent = data.get('soil_percent', [0,0,0,0])
        soil_avg = sum(soil_percent) / len(soil_percent) if soil_percent else 0
        
        conn.execute('''INSERT INTO sensor_history 
            (soil_avg, soil1, soil2, soil3, soil4, temperature, humidity, 
             pump_status, mode, battery_voltage, battery_percent, current_consumed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (soil_avg, 
             soil_percent[0] if len(soil_percent) > 0 else 0,
             soil_percent[1] if len(soil_percent) > 1 else 0,
             soil_percent[2] if len(soil_percent) > 2 else 0,
             soil_percent[3] if len(soil_percent) > 3 else 0,
             data.get('temperature'),
             data.get('humidity'),
             data.get('pump_status', 'OFF'),
             data.get('mode', 'AUTO'),
             data.get('battery_voltage'),
             data.get('battery_percent'),
             data.get('current_consumed')))
        
        # Check for pending commands
        response_data = {"success": True}
        
        if FIREBASE_ENABLED and commands_ref:
            try:
                # Check Firebase for commands
                cmd_data = commands_ref.get()
                if cmd_data and not cmd_data.get('executed', True):
                    response_data["command"] = {
                        "type": cmd_data.get('type'),
                        "value": cmd_data.get('value')
                    }
                    commands_ref.update({'executed': True})
                    print(f"📤 Sending command to ESP32: {response_data['command']}")
            except Exception as e:
                print(f"⚠️  Firebase command read error: {e}")
        else:
            # Fallback to SQLite
            cmd = conn.execute('SELECT * FROM pump_commands WHERE executed = 0 ORDER BY created_at ASC LIMIT 1').fetchone()
            if cmd:
                response_data["command"] = {"type": cmd['command_type'], "value": cmd['value']}
                conn.execute('UPDATE pump_commands SET executed = 1 WHERE id = ?', (cmd['id'],))
        
        conn.commit()
        return jsonify(response_data)
    
    except Exception as e:
        print(f"❌ Error in esp32_push_data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/esp32/get_command', methods=['GET'])
def esp32_get_command():
    """ESP32 polls for commands (fallback method)"""
    conn = None
    try:
        if FIREBASE_ENABLED and commands_ref:
            try:
                cmd_data = commands_ref.get()
                if cmd_data and not cmd_data.get('executed', True):
                    commands_ref.update({'executed': True})
                    return jsonify({
                        "has_command": True,
                        "type": cmd_data.get('type'),
                        "value": cmd_data.get('value')
                    })
            except Exception as e:
                print(f"⚠️  Firebase command read error: {e}")
        
        # Fallback to SQLite
        conn = get_db()
        command = conn.execute(
            'SELECT * FROM pump_commands WHERE executed = 0 ORDER BY created_at DESC LIMIT 1'
        ).fetchone()
        
        if command:
            conn.execute('UPDATE pump_commands SET executed = 1 WHERE id = ?', (command['id'],))
            conn.commit()
            
            return jsonify({
                "has_command": True,
                "type": command['command_type'],
                "value": command['value']
            })
        
        return jsonify({"has_command": False})
    
    except Exception as e:
        print(f"❌ Error in esp32_get_command: {e}")
        return jsonify({"has_command": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

# ========== Dashboard API ==========
@app.route('/api/data')
@login_required
def get_data():
    """Get current sensor data from Firebase or SQLite"""
    try:
        if FIREBASE_ENABLED and sensor_ref:
            try:
                # Get from Firebase
                data = sensor_ref.get()
                if data:
                    # Check ESP32 online status
                    esp32_online = False
                    if data.get('timestamp'):
                        try:
                            last_update = datetime.fromisoformat(data['timestamp'])
                            now_dt = datetime.now(timezone.utc) if last_update.tzinfo else datetime.now()
                            time_diff = (now_dt - last_update).total_seconds()
                            esp32_online = time_diff < ESP32_TIMEOUT_SECONDS
                        except (ValueError, TypeError, AttributeError) as e:
                            print(f"⚠️  Error parsing timestamp: {e}")
                            esp32_online = False
                    
                    return jsonify({
                        "soil_percent": data.get('soil_percent', [0,0,0,0]),
                        "soil_status": data.get('soil_status', 'UNKNOWN'),
                        "pump_status": data.get('pump_status', 'OFF'),
                        "mode": data.get('mode', 'AUTO'),
                        "temperature": data.get('temperature'),
                        "humidity": data.get('humidity'),
                        "battery_voltage": data.get('battery_voltage'),
                        "battery_percent": data.get('battery_percent'),
                        "current_consumed": data.get('current_consumed'),
                        "power": data.get('power', {}),
                        "esp32_online": esp32_online,
                        "last_update": data.get('timestamp'),
                        "source": "firebase"
                    })
            except Exception as e:
                print(f"⚠️  Firebase read error: {e}")
        
        # Fallback to SQLite
        conn = get_db()
        data = conn.execute('SELECT * FROM sensor_current WHERE id = 1').fetchone()
        
        # Check if ESP32 is still online (data received within timeout period)
        esp32_online = False
        if data and data['last_update']:
            try:
                last_update = datetime.fromisoformat(data['last_update'])
                now_dt = datetime.now(timezone.utc) if last_update.tzinfo else datetime.now()
                time_diff = (now_dt - last_update).total_seconds()
                esp32_online = bool(data['esp32_online']) and time_diff < ESP32_TIMEOUT_SECONDS
                
                # Update online status if timeout
                if not esp32_online and data['esp32_online']:
                    conn.execute('UPDATE sensor_current SET esp32_online = 0 WHERE id = 1')
                    conn.commit()
            except (ValueError, AttributeError, TypeError) as e:
                print(f"⚠️  Error checking ESP32 online status: {e}")
                esp32_online = bool(data.get('esp32_online', False))
        
        conn.close()
        
        if data:
            return jsonify({
                "soil_percent": json.loads(data['soil_percent']) if data['soil_percent'] else [0,0,0,0],
                "soil_status": data['soil_status'],
                "pump_status": data['pump_status'],
                "mode": data['mode'],
                "temperature": data['temperature'],
                "humidity": data['humidity'],
                "battery_voltage": data['battery_voltage'],
                "battery_percent": data['battery_percent'],
                "current_consumed": data['current_consumed'],
                "power": json.loads(data['power_data']) if data['power_data'] else {},
                "esp32_online": esp32_online,
                "last_update": data['last_update'],
                "source": "sqlite"
            })
        
        return jsonify({"esp32_online": False})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/mode/<mode>')
@login_required
def set_mode(mode):
    """Send mode command via Firebase or SQLite"""
    conn = None
    try:
        if FIREBASE_ENABLED and commands_ref:
            commands_ref.set({
                'type': 'mode',
                'value': mode.upper(),
                'executed': False,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        else:
            conn = get_db()
            conn.execute('INSERT INTO pump_commands (command_type, value, user_id) VALUES (?, ?, ?)', 
                        ('mode', mode.upper(), session.get('user_id')))
            conn.commit()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/pump/<state>')
@login_required
def set_pump(state):
    """Send pump command via Firebase or SQLite"""
    conn = None
    try:
        if FIREBASE_ENABLED and commands_ref:
            commands_ref.set({
                'type': 'pump',
                'value': state.upper(),
                'executed': False,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        else:
            conn = get_db()
            conn.execute('INSERT INTO pump_commands (command_type, value, user_id) VALUES (?, ?, ?)', 
                        ('pump', state.upper(), session.get('user_id')))
            conn.commit()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/save_reading', methods=['POST'])
@login_required
def save_reading():
    conn = None
    try:
        data = request.json
        conn = get_db()
        conn.execute('''INSERT INTO sensor_history 
            (user_id, soil_avg, soil1, soil2, soil3, soil4, temperature, humidity, 
             pump_status, mode, battery_voltage, battery_percent, current_consumed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (session.get('user_id'),
             data.get('soil_avg'), data.get('soil1'), data.get('soil2'), data.get('soil3'),
             data.get('soil4'), data.get('temperature'), data.get('humidity'),
             data.get('pump_status'), data.get('mode'), data.get('battery_voltage'),
             data.get('battery_percent'), data.get('current_consumed')))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/history')
@login_required
def get_history():
    conn = None
    try:
        conn = get_db()
        history = conn.execute('''SELECT * FROM sensor_history 
                                 ORDER BY recorded_at DESC LIMIT 100''').fetchall()
        return jsonify([dict(row) for row in history])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/history_stats')
@login_required
def get_history_stats():
    """Get statistics for history page"""
    conn = None
    try:
        conn = get_db()
        
        # Get pump events from last 7 days
        seven_days_ago = datetime.now() - timedelta(days=7)
        
        # Get all history records from last 7 days ordered by time
        history = conn.execute('''SELECT 
            recorded_at, pump_status, mode, soil_avg
            FROM sensor_history 
            WHERE recorded_at >= ?
            ORDER BY recorded_at ASC''', 
            (seven_days_ago.isoformat(),)).fetchall()
        
        # Count actual pump state changes (events)
        pump_on_count = 0
        pump_off_count = 0
        manual_count = 0
        auto_count = 0
        prev_status = None
        
        for record in history:
            current_status = record['pump_status']
            # Count state changes (actual events) - only if status is not None
            if current_status and prev_status != current_status:
                if current_status == 'ON':
                    pump_on_count += 1
                elif current_status == 'OFF':
                    pump_off_count += 1
                
                # Count manual vs auto based on mode when state changes
                mode = record['mode']
                if mode == 'MANUAL':
                    manual_count += 1
                elif mode == 'AUTO':
                    auto_count += 1
            
            if current_status:
                prev_status = current_status
        
        # Soil moisture stats
        soil_stats = conn.execute('''SELECT 
            AVG(soil_avg) as avg_soil, COUNT(*) as total_readings
            FROM sensor_history WHERE recorded_at >= ?''', 
            (seven_days_ago.isoformat(),)).fetchone()
        
        return jsonify({
            "pump": {
                "total_on_events": pump_on_count,
                "total_off_events": pump_off_count,
                "manual_events": manual_count,
                "auto_events": auto_count
            },
            "soil": {
                "avg_soil": float(soil_stats['avg_soil']) if soil_stats and soil_stats['avg_soil'] is not None else 0,
                "total_readings": soil_stats['total_readings'] if soil_stats else 0
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/pump_events')
@login_required
def get_pump_events():
    """Get pump events from history, always including latest reading"""
    conn = None
    try:
        limit = request.args.get('limit', 100, type=int)
        conn = get_db()
        
        # Get latest reading from sensor_history for current status
        latest_reading = conn.execute('''SELECT 
            recorded_at, pump_status, mode, soil_avg, temperature, humidity
            FROM sensor_history 
            WHERE pump_status IS NOT NULL
            ORDER BY recorded_at DESC 
            LIMIT 1''').fetchone()
        
        # Get history records where pump status changed
        history = conn.execute('''SELECT 
            recorded_at, pump_status, mode, soil_avg, temperature, humidity
            FROM sensor_history 
            WHERE pump_status IS NOT NULL
            ORDER BY recorded_at DESC 
            LIMIT ?''', (limit,)).fetchall()
        
        # Convert to pump events format (only state changes)
        events = []
        prev_status = None
        for record in history:
            current_status = record['pump_status']
            if prev_status != current_status:
                events.append({
                    "recorded_at": record['recorded_at'],
                    "event_type": "PUMP_ON" if current_status == "ON" else "PUMP_OFF",
                    "mode": record['mode'] or "AUTO",
                    "triggered_by": "manual" if record['mode'] == "MANUAL" else "auto",
                    "soil_moisture": record['soil_avg']
                })
            prev_status = current_status
        
        # Always prepend latest reading as current status if it exists and is newer than last event
        if latest_reading:
            latest_timestamp = latest_reading['recorded_at']
            # Check if latest reading is newer than the most recent event (or if no events exist)
            if not events or (latest_timestamp > events[0]['recorded_at']):
                # Insert at beginning as current status (not a state change event)
                events.insert(0, {
                    "recorded_at": latest_reading['recorded_at'],
                    "event_type": "PUMP_ON" if latest_reading['pump_status'] == "ON" else "PUMP_OFF",
                    "mode": latest_reading['mode'] or "AUTO",
                    "triggered_by": "manual" if latest_reading['mode'] == "MANUAL" else "auto",
                    "soil_moisture": latest_reading['soil_avg'],
                    "is_current": True  # Flag to indicate this is current status, not a state change
                })
        
        return jsonify(events)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/soil_history')
@login_required
def get_soil_history():
    """Get soil moisture history, always including latest reading from sensor_current"""
    conn = None
    try:
        limit = request.args.get('limit', 50, type=int)
        conn = get_db()
        
        # Get history from sensor_history first (this should always work)
        history = conn.execute('''SELECT 
            recorded_at, soil_avg, soil1, soil2, soil3, soil4, 
            temperature, humidity, pump_status
            FROM sensor_history 
            ORDER BY recorded_at DESC 
            LIMIT ?''', (limit,)).fetchall()
        
        history_list = [dict(row) for row in history]
        
        # Try to get latest reading from sensor_current (optional enhancement)
        try:
            current_data = conn.execute('SELECT * FROM sensor_current WHERE id = 1').fetchone()
            
            if current_data:
                # Convert Row to dict (SQLite Row objects support dict() conversion)
                current_dict = dict(current_data)
                current_timestamp = current_dict.get('last_update')
                
                if current_timestamp:
                    # Parse soil_percent safely
                    soil_percent_str = current_dict.get('soil_percent')
                    try:
                        current_soil_percent = json.loads(soil_percent_str) if soil_percent_str else [0, 0, 0, 0]
                    except (json.JSONDecodeError, TypeError):
                        current_soil_percent = [0, 0, 0, 0]
                    
                    current_soil_avg = sum(current_soil_percent) / len(current_soil_percent) if current_soil_percent else 0
                    
                    # Check if current data is newer than latest history entry
                    should_prepend = True
                    if history_list and len(history_list) > 0 and history_list[0].get('recorded_at'):
                        latest_history_ts = history_list[0]['recorded_at']
                        try:
                            # Handle both string and datetime objects
                            if isinstance(current_timestamp, str):
                                current_dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                            else:
                                current_dt = current_timestamp
                            
                            if isinstance(latest_history_ts, str):
                                latest_dt = datetime.fromisoformat(latest_history_ts.replace('Z', '+00:00'))
                            else:
                                latest_dt = latest_history_ts
                            
                            should_prepend = current_dt > latest_dt
                        except (ValueError, AttributeError, TypeError) as e:
                            # If parsing fails, don't prepend to avoid duplicates
                            print(f"⚠️  Error comparing timestamps: {e}")
                            should_prepend = False
                    
                    if should_prepend:
                        # Create history entry from current data
                        current_entry = {
                            "recorded_at": current_timestamp,
                            "soil_avg": current_soil_avg,
                            "soil1": current_soil_percent[0] if len(current_soil_percent) > 0 else 0,
                            "soil2": current_soil_percent[1] if len(current_soil_percent) > 1 else 0,
                            "soil3": current_soil_percent[2] if len(current_soil_percent) > 2 else 0,
                            "soil4": current_soil_percent[3] if len(current_soil_percent) > 3 else 0,
                            "temperature": current_dict.get('temperature'),
                            "humidity": current_dict.get('humidity'),
                            "pump_status": current_dict.get('pump_status', 'OFF')
                        }
                        # Prepend to beginning of list
                        history_list.insert(0, current_entry)
        except Exception as e:
            # If current data processing fails, just return history (don't fail the whole request)
            print(f"⚠️  Error processing current data for soil history: {e}")
            import traceback
            traceback.print_exc()
        
        return jsonify(history_list)
    except Exception as e:
        print(f"❌ Error in get_soil_history: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/monthly_stats')
@login_required
def get_monthly_stats():
    conn = None
    try:
        conn = get_db()
        current_month = datetime.now().strftime('%Y-%m')
        stats = conn.execute('''SELECT 
            AVG(soil_avg) as avg_soil, AVG(temperature) as avg_temp, AVG(humidity) as avg_humidity,
            MIN(soil_avg) as min_soil, MAX(soil_avg) as max_soil,
            MIN(temperature) as min_temp, MAX(temperature) as max_temp, COUNT(*) as reading_count
            FROM sensor_history WHERE strftime('%Y-%m', recorded_at) = ?''', (current_month,)).fetchone()
        return jsonify(dict(stats) if stats else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/crop_suggestions')
@login_required
def get_crop_suggestions_api():
    current_month = datetime.now().month
    season_info = get_season_info(current_month)
    
    conn = get_db()
    stats = conn.execute('''SELECT AVG(soil_avg) as avg_soil FROM sensor_history 
        WHERE strftime('%Y-%m', recorded_at) = ?''', (datetime.now().strftime('%Y-%m'),)).fetchone()
    
    if stats and stats['avg_soil']:
        soil_avg = stats['avg_soil']
    else:
        soil_avg = get_soil_avg_from_db(conn)
    
    conn.close()
    
    suggestions = get_crop_suggestions(soil_avg, current_month)
    return jsonify({
        "month": current_month,
        "month_name": datetime.now().strftime('%B'),
        "season": season_info,
        "soil_moisture": round(soil_avg, 1),
        "suggestions": suggestions
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    secret_key = os.environ.get('SECRET_KEY')
    
    print("="*60)
    print("🌱 SoilSense Online Server Starting...")
    print("="*60)
    if not secret_key or secret_key == 'your-secret-key-change-this-in-production':
        print("⚠️  WARNING: Using default secret key! Set SECRET_KEY environment variable for production.")
    print(f"🔥 Firebase: {'ENABLED ✅' if FIREBASE_ENABLED else 'DISABLED ⚠️'}")
    print("💾 SQLite: Used for backup and history")
    print(f"🌐 Server: Running on port {port}")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)
