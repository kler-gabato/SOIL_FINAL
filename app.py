from flask import Flask, jsonify, render_template, request, redirect, url_for, session
from flask_cors import CORS
import sqlite3
import hashlib
import os
from datetime import datetime
import json
import firebase_admin
from firebase_admin import credentials, db

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')

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

# ========== Database Setup (SQLite for backup) ==========
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
        soil_avg REAL, soil1 INTEGER, soil2 INTEGER, soil3 INTEGER, soil4 INTEGER,
        temperature REAL, humidity REAL, pump_status TEXT, mode TEXT,
        battery_voltage REAL, battery_percent REAL, current_consumed REAL,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        command_type TEXT, value TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        executed INTEGER DEFAULT 0
    )''')
    c.execute('INSERT OR IGNORE INTO sensor_current (id, esp32_online) VALUES (1, 0)')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect('soilsense.db')
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ========== Philippine Crops Database ==========
def get_current_season(month):
    return "wet" if month in [6, 7, 8, 9, 10] else "dry"

def get_season_info(month):
    if month in [6, 7, 8, 9, 10]:
        return {"season": "Wet Season", "season_type": "wet", "months": "June - October", 
                "description": "Rainy season with frequent rainfall and higher humidity", "icon": "🌧️"}
    return {"season": "Dry Season", "season_type": "dry", "months": "November - May",
            "description": "Less rainfall, good for crops needing drier conditions", "icon": "☀️"}

CROPS_DATABASE = {
    "rice": {
        "name": "Rice (Palay)", "icon": "🌾", "local_name": "Palay",
        "soil_moisture": {"min": 60, "max": 90}, "season": "wet",
        "description": "Main staple crop of the Philippines. Needs flooded paddies.",
        "tips": "Plant at start of wet season. Requires standing water for best growth."
    },
    "kangkong": {
        "name": "Kangkong", "icon": "🥬", "local_name": "Kangkong",
        "soil_moisture": {"min": 60, "max": 95}, "season": "both",
        "description": "Water spinach that thrives in wet conditions. Very easy to grow.",
        "tips": "Can grow in water or moist soil. Harvest in 21-30 days. Cut and regrow."
    },
    "kalabasa": {
        "name": "Squash (Kalabasa)", "icon": "🎃", "local_name": "Kalabasa",
        "soil_moisture": {"min": 50, "max": 80}, "season": "wet",
        "description": "Grows well with abundant rainfall. Sprawling vine needs space.",
        "tips": "Rich in Vitamin A. Fruits can be stored for months after harvest."
    },
    "gabi": {
        "name": "Taro (Gabi)", "icon": "🥔", "local_name": "Gabi",
        "soil_moisture": {"min": 65, "max": 90}, "season": "wet",
        "description": "Root crop that loves waterlogged conditions. Staple in many Filipino dishes.",
        "tips": "Plant in shaded, wet areas. Leaves (laing) are also edible."
    },
    "banana": {
        "name": "Banana (Saging)", "icon": "🍌", "local_name": "Saging",
        "soil_moisture": {"min": 55, "max": 80}, "season": "wet",
        "description": "Best planted at start of rainy season for establishment.",
        "tips": "9-12 months to first harvest. Needs consistent moisture."
    },
    "sitaw": {
        "name": "String Beans (Sitaw)", "icon": "🫛", "local_name": "Sitaw",
        "soil_moisture": {"min": 50, "max": 75}, "season": "both",
        "description": "Fast-growing legume that tolerates rainy conditions well.",
        "tips": "Provide trellis for climbing. Harvest pods when young and tender."
    },
    "tomato": {
        "name": "Tomato (Kamatis)", "icon": "🍅", "local_name": "Kamatis",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Sensitive to heavy rains. Best grown in dry season.",
        "tips": "Stake plants for support. Avoid wetting leaves to prevent disease."
    },
    "eggplant": {
        "name": "Eggplant (Talong)", "icon": "🍆", "local_name": "Talong",
        "soil_moisture": {"min": 40, "max": 65}, "season": "dry",
        "description": "Popular vegetable, thrives in warm dry weather.",
        "tips": "Harvest every 3-4 days when fruits are glossy. Avoid waterlogging."
    },
    "ampalaya": {
        "name": "Bitter Gourd (Ampalaya)", "icon": "🥒", "local_name": "Ampalaya",
        "soil_moisture": {"min": 40, "max": 65}, "season": "dry",
        "description": "Highly nutritious vine crop. Prefers dry conditions.",
        "tips": "Provide trellis for climbing. Harvest when fruits are still green."
    },
    "onion": {
        "name": "Onion (Sibuyas)", "icon": "🧅", "local_name": "Sibuyas",
        "soil_moisture": {"min": 30, "max": 55}, "season": "dry",
        "description": "Requires dry conditions for bulb formation.",
        "tips": "Stop watering 2 weeks before harvest. Cure bulbs in sun."
    },
    "garlic": {
        "name": "Garlic (Bawang)", "icon": "🧄", "local_name": "Bawang",
        "soil_moisture": {"min": 30, "max": 50}, "season": "dry",
        "description": "Needs dry weather. Plant in October-November.",
        "tips": "4-5 months to harvest. Reduce watering as plants mature."
    },
    "pechay": {
        "name": "Pechay", "icon": "🥬", "local_name": "Pechay",
        "soil_moisture": {"min": 45, "max": 65}, "season": "dry",
        "description": "Fast-growing leafy vegetable. Prefers cooler dry months.",
        "tips": "Harvest in 25-30 days. Provide partial shade if too hot."
    },
    "corn": {
        "name": "Corn (Mais)", "icon": "🌽", "local_name": "Mais",
        "soil_moisture": {"min": 40, "max": 70}, "season": "both",
        "description": "Can be planted in both seasons with proper irrigation.",
        "tips": "90-120 days to harvest. Yellow corn for feeds, white corn for food."
    },
    "monggo": {
        "name": "Mungbean (Monggo)", "icon": "🫘", "local_name": "Monggo",
        "soil_moisture": {"min": 35, "max": 60}, "season": "dry",
        "description": "Short-duration legume (60-70 days). Drought tolerant.",
        "tips": "Often planted after rice harvest. Improves soil nitrogen."
    },
    "sili": {
        "name": "Hot Pepper (Sili)", "icon": "🌶️", "local_name": "Sili",
        "soil_moisture": {"min": 40, "max": 60}, "season": "dry",
        "description": "Includes siling labuyo, siling haba. Loves hot dry weather.",
        "tips": "Multiple harvests over several months. Very heat tolerant."
    },
    "okra": {
        "name": "Okra", "icon": "🌿", "local_name": "Okra",
        "soil_moisture": {"min": 40, "max": 65}, "season": "dry",
        "description": "Heat-loving crop, drought tolerant once established.",
        "tips": "Harvest every 2 days when pods are 3-4 inches long."
    },
    "kamote": {
        "name": "Sweet Potato (Kamote)", "icon": "🍠", "local_name": "Kamote",
        "soil_moisture": {"min": 35, "max": 60}, "season": "both",
        "description": "Drought-tolerant root crop. Tops and tubers are edible.",
        "tips": "Plant cuttings at start of rainy or dry season. 3-4 months to harvest."
    },
    "papaya": {
        "name": "Papaya", "icon": "🥭", "local_name": "Papaya",
        "soil_moisture": {"min": 45, "max": 70}, "season": "both",
        "description": "Fast-growing fruit tree. Cannot tolerate waterlogging.",
        "tips": "Well-drained soil essential. Bears fruit in 9-11 months."
    }
}

def get_crop_suggestions(soil_avg, month):
    """Get crop suggestions based on soil moisture and Philippine season"""
    suggestions = []
    current_season = get_current_season(month)
    
    for crop_id, crop in CROPS_DATABASE.items():
        score = 0
        reasons = []
        
        crop_season = crop["season"]
        if crop_season == "both":
            score += 30
            reasons.append("✓ Year-round crop")
        elif crop_season == current_season:
            score += 40
            if current_season == "wet":
                reasons.append("✓ Best in wet season")
            else:
                reasons.append("✓ Best in dry season")
        else:
            continue
        
        if soil_avg is not None:
            if crop["soil_moisture"]["min"] <= soil_avg <= crop["soil_moisture"]["max"]:
                score += 40
                reasons.append("✓ Ideal soil moisture")
            elif abs(soil_avg - crop["soil_moisture"]["min"]) <= 15 or abs(soil_avg - crop["soil_moisture"]["max"]) <= 15:
                score += 20
                reasons.append("Acceptable soil moisture")
            else:
                score -= 10
        else:
            score += 20
        
        if score >= 30:
            suggestions.append({
                "id": crop_id,
                "name": crop["name"],
                "local_name": crop["local_name"],
                "icon": crop["icon"],
                "score": min(score, 100),
                "reasons": reasons,
                "description": crop["description"],
                "tips": crop["tips"],
                "ideal_soil": f"{crop['soil_moisture']['min']}-{crop['soil_moisture']['max']}%",
                "season": crop["season"]
            })
    
    suggestions.sort(key=lambda x: x["score"], reverse=True)
    return suggestions[:8]

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

@app.route('/api/test/push')
def test_push():
    """Test endpoint to push sample data to Firebase"""
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase not enabled", "firebase_enabled": False})
    
    try:
        timestamp = datetime.now().isoformat()
        test_data = {
            'soil_percent': [50, 55, 60, 65],
            'soil_status': 'NORMAL',
            'pump_status': 'OFF',
            'mode': 'AUTO',
            'temperature': 28.5,
            'humidity': 70.0,
            'battery_voltage': 12.4,
            'battery_percent': 90,
            'current_consumed': 0.35,
            'power': {"bus_voltage": 12.4, "current": 0.35},
            'timestamp': timestamp
        }
        
        sensor_ref.set(test_data)
        
        return jsonify({
            "success": True,
            "message": "Test data pushed to Firebase",
            "data": test_data
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

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
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        
        return render_template('login.html', error='Invalid username or password')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            return render_template('register.html', error='Passwords do not match')
        if len(password) < 6:
            return render_template('register.html', error='Password must be at least 6 characters')
        
        try:
            # Store in SQLite
            conn = get_db()
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
        except sqlite3.IntegrityError:
            return render_template('register.html', error='Username or email already exists')
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/account')
@login_required
def account():
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
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
        return render_template('account.html', user=user, error='Current password is incorrect')
    
    if new_password:
        if len(new_password) < 6:
            conn.close()
            return render_template('account.html', user=user, error='New password must be at least 6 characters')
        if new_password != confirm_password:
            conn.close()
            return render_template('account.html', user=user, error='New passwords do not match')
        conn.execute('UPDATE users SET username = ?, email = ?, password = ? WHERE id = ?',
                    (username, email, hash_password(new_password), session['user_id']))
    else:
        conn.execute('UPDATE users SET username = ?, email = ? WHERE id = ?',
                    (username, email, session['user_id']))
    
    conn.commit()
    conn.close()
    session['username'] = username
    
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    return render_template('account.html', user=user, success='Account updated successfully')

# ========== Dashboard Routes ==========
@app.route('/')
@login_required
def index():
    return render_template('index.html', username=session.get('username'))

@app.route('/crops')
@login_required
def crops_page():
    return render_template('crops.html', username=session.get('username'))

# ========== ESP32 API with Firebase ==========
@app.route('/api/esp32/push', methods=['POST'])
def esp32_push_data():
    """ESP32 pushes data to Firebase and SQLite"""
    try:
        data = request.get_json()
        timestamp = datetime.now().isoformat()
        
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
        conn.close()
        return jsonify(response_data)
    
    except Exception as e:
        print(f"❌ Error in esp32_push_data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

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
                        "esp32_online": True,
                        "last_update": data.get('timestamp'),
                        "source": "firebase"
                    })
            except Exception as e:
                print(f"⚠️  Firebase read error: {e}")
        
        # Fallback to SQLite
        conn = get_db()
        data = conn.execute('SELECT * FROM sensor_current WHERE id = 1').fetchone()
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
                "esp32_online": bool(data['esp32_online']),
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
    try:
        if FIREBASE_ENABLED and commands_ref:
            commands_ref.set({
                'type': 'mode',
                'value': mode.upper(),
                'executed': False,
                'timestamp': datetime.now().isoformat()
            })
        else:
            conn = get_db()
            conn.execute('INSERT INTO pump_commands (command_type, value) VALUES (?, ?)', 
                        ('mode', mode.upper()))
            conn.commit()
            conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/pump/<state>')
@login_required
def set_pump(state):
    """Send pump command via Firebase or SQLite"""
    try:
        if FIREBASE_ENABLED and commands_ref:
            commands_ref.set({
                'type': 'pump',
                'value': state.upper(),
                'executed': False,
                'timestamp': datetime.now().isoformat()
            })
        else:
            conn = get_db()
            conn.execute('INSERT INTO pump_commands (command_type, value) VALUES (?, ?)', 
                        ('pump', state.upper()))
            conn.commit()
            conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/save_reading', methods=['POST'])
@login_required
def save_reading():
    data = request.json
    conn = get_db()
    conn.execute('''INSERT INTO sensor_history 
        (soil_avg, soil1, soil2, soil3, soil4, temperature, humidity, 
         pump_status, mode, battery_voltage, battery_percent, current_consumed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (data.get('soil_avg'), data.get('soil1'), data.get('soil2'), data.get('soil3'),
         data.get('soil4'), data.get('temperature'), data.get('humidity'),
         data.get('pump_status'), data.get('mode'), data.get('battery_voltage'),
         data.get('battery_percent'), data.get('current_consumed')))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/history')
@login_required
def get_history():
    conn = get_db()
    history = conn.execute('SELECT * FROM sensor_history ORDER BY recorded_at DESC LIMIT 100').fetchall()
    conn.close()
    return jsonify([dict(row) for row in history])

@app.route('/api/monthly_stats')
@login_required
def get_monthly_stats():
    conn = get_db()
    current_month = datetime.now().strftime('%Y-%m')
    stats = conn.execute('''SELECT 
        AVG(soil_avg) as avg_soil, AVG(temperature) as avg_temp, AVG(humidity) as avg_humidity,
        MIN(soil_avg) as min_soil, MAX(soil_avg) as max_soil,
        MIN(temperature) as min_temp, MAX(temperature) as max_temp, COUNT(*) as reading_count
        FROM sensor_history WHERE strftime('%Y-%m', recorded_at) = ?''', (current_month,)).fetchone()
    conn.close()
    return jsonify(dict(stats) if stats else {})

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
        if FIREBASE_ENABLED and sensor_ref:
            try:
                data = sensor_ref.get()
                if data:
                    soil_percent = data.get('soil_percent', [50,50,50,50])
                    soil_avg = sum(soil_percent) / len(soil_percent)
                else:
                    soil_avg = 50
            except:
                soil_avg = 50
        else:
            current = conn.execute('SELECT soil_percent FROM sensor_current WHERE id = 1').fetchone()
            if current and current['soil_percent']:
                soil_percent = json.loads(current['soil_percent'])
                soil_avg = sum(soil_percent) / len(soil_percent)
            else:
                soil_avg = 50
    
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
    app.run(host='0.0.0.0', port=port, debug=False)