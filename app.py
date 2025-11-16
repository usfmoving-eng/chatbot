"""
USF Moving Company Chatbot Backend
Flask application with OpenAI, Twilio, Google Sheets, and Email integration
"""

from flask import Flask, request, jsonify
from flask import send_file
from werkzeug.utils import secure_filename
import time
try:
    from flask_socketio import SocketIO, emit, join_room, leave_room
    SOCKETIO_AVAILABLE = True
except Exception:
    SocketIO = None
    def emit(*args, **kwargs):
        pass
    def join_room(*args, **kwargs):
        pass
    def leave_room(*args, **kwargs):
        pass
    SOCKETIO_AVAILABLE = False
import base64
import re
from flask_cors import CORS
import os
from dotenv import load_dotenv
from openai import OpenAI
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from google.oauth2.service_account import Credentials
import googlemaps
from datetime import datetime, timedelta
import json
import logging

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
# Enable CORS specifically for WordPress site
CORS(app, resources={
    r"/*": {
        "origins": [
            "https://www.usfhoustonmoving.com",
            "https://usfhoustonmoving.com",
            "http://www.usfhoustonmoving.com",  # Fallback for HTTP
            "http://usfhoustonmoving.com"       # Fallback for HTTP
        ],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True
    }
})
socketio = SocketIO(app, cors_allowed_origins=[
    "https://www.usfhoustonmoving.com",
    "https://usfhoustonmoving.com",
    "http://www.usfhoustonmoving.com",
    "http://usfhoustonmoving.com"
]) if SOCKETIO_AVAILABLE else None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize APIs
# OpenAI client (new SDK >=1.0.0). Lazily re-created if key rotates.
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
# Cooldown window for OpenAI when hitting rate limits/quota (epoch seconds)
OPENAI_COOLDOWN_UNTIL = None
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
gmaps = googlemaps.Client(key=os.getenv('GOOGLE_MAPS_API_KEY'))
# Simple in-memory cache for distances to reduce external calls
distance_cache = {}
conversation_meta = {}  # per-session ephemeral flags (e.g., call requests)

# Realtime speech buffers: session_id -> { 'mime': str, 'ext': str, 'chunks': [bytes] }
speech_streams = {}

# Allowed audio mime types for speech upload
ALLOWED_AUDIO_MIME = {
    'audio/mpeg',
    'audio/wav',
    'audio/x-wav',
    'audio/webm',
    'audio/ogg',
    'audio/x-m4a',
    'audio/mp4',
}

def generate_assistant_reply(session_id: str, user_message: str) -> str:
    """Core reply generation reused for text + speech inputs.
    Mirrors logic in /chat endpoint but accepts a plain message string.
    Returns assistant message (may be fallback).
    """
    global openai_client, OPENAI_COOLDOWN_UNTIL
    # Ensure conversation context
    if session_id not in conversations:
        conversations[session_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    conversations[session_id].append({"role": "user", "content": user_message})
    # Trim to last 12 messages (system + 11 recent)
    if len(conversations[session_id]) > 12:
        conversations[session_id] = [conversations[session_id][0]] + conversations[session_id][-11:]

    # Cooldown quick deterministic estimation reuse from main endpoint
    if OPENAI_COOLDOWN_UNTIL and OPENAI_COOLDOWN_UNTIL > datetime.utcnow().timestamp():
        remaining = int(OPENAI_COOLDOWN_UNTIL - datetime.utcnow().timestamp())
        quick = parse_quick_move_details(user_message)
        if quick:
            try:
                est = generate_estimate_logic(quick['rooms'], quick['pickup'], quick['drop'], quick.get('stairs') or False, None)
                assistant_message = (
                    f"(AI cooldown {remaining}s) Distance: {est['pickup_drop_miles']:.1f} mi. Crew: {est['crew_size']}. "
                    f"Hourly rate: ${est['hourly_rate']}/hr (+30 min travel, 3-hr minimum). Provide move date & preferred time to continue."
                )
            except Exception:
                assistant_message = f"(AI cooldown {remaining}s) Please send full move details: pickup & drop addresses, bedrooms, stairs/elevator, special items."
        else:
            assistant_message = f"(AI cooldown {remaining}s) Please send full move details to proceed (pickup/drop full addresses, bedrooms, stairs/elevator)."
        conversations[session_id].append({"role": "assistant", "content": assistant_message})
        return assistant_message

    # Try OpenAI chat
    model_candidates = ['gpt-4o-mini', 'gpt-3.5-turbo']
    if os.getenv('OPENAI_MODEL'):
        model_candidates.insert(0, os.getenv('OPENAI_MODEL'))
    assistant_message = None
    last_err = None
    for model_name in model_candidates:
        try:
            response = openai_client.chat.completions.create(
                model=model_name,
                messages=conversations[session_id],
                temperature=0.7,
                max_tokens=180,
                timeout=25
            )
            assistant_message = response.choices[0].message.content
            break
        except Exception as oe:
            last_err = oe
            if 'rate limit' in str(oe).lower() or 'quota' in str(oe).lower():
                time.sleep(1.2)
                continue

    if assistant_message is None:
        err_text = str(last_err).lower() if last_err else ''
        if 'quota' in err_text:
            OPENAI_COOLDOWN_UNTIL = datetime.utcnow().timestamp() + 60
        elif 'rate limit' in err_text:
            OPENAI_COOLDOWN_UNTIL = datetime.utcnow().timestamp() + 30
        quick = parse_quick_move_details(user_message)
        if quick:
            try:
                est = generate_estimate_logic(quick['rooms'], quick['pickup'], quick['drop'], quick.get('stairs') or False, None)
                if est['move_category'] == 'long-distance':
                    assistant_message = (
                        f"Distance ~{est['pickup_drop_miles']:.1f} miles (long-distance). For accurate pricing a manager will contact you. Please share your name, phone, email."
                    )
                else:
                    assistant_message = (
                        f"Distance ~{est['pickup_drop_miles']:.1f} miles. Crew: {est['crew_size']}. Hourly rate: ${est['hourly_rate']}/hr (+30 min travel, 3-hr minimum). "
                        "Send move date & preferred time to proceed, then name, phone, email."
                    )
            except Exception:
                assistant_message = "Please provide pickup & drop addresses plus bedrooms to start your estimate."
        else:
            assistant_message = "Please provide pickup & drop addresses plus bedrooms to start your estimate."
    conversations[session_id].append({"role": "assistant", "content": assistant_message})
    return assistant_message

def transcribe_audio_file(audio_fp, mime_type: str) -> str:
    """Transcribe an audio file using OpenAI whisper/gpt-4o-mini-transcribe.
    Returns transcript text or raises Exception."""
    global openai_client
    if mime_type not in ALLOWED_AUDIO_MIME:
        raise ValueError("Unsupported audio MIME type")
    if not openai_client:
        raise RuntimeError("OpenAI client not configured")
    # Prefer 'gpt-4o-mini-transcribe' if available, fall back to 'whisper-1'
    model_name = os.getenv('OPENAI_TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe')
    try:
        with open(audio_fp, 'rb') as f:
            transcription = openai_client.audio.transcriptions.create(
                model=model_name,
                file=f
            )
        # Different SDKs may return text field; adapt defensively
        text = getattr(transcription, 'text', None)
        if not text:
            # fallback dict style
            text = transcription.get('text') if isinstance(transcription, dict) else None
        if not text:
            raise RuntimeError("Empty transcription result")
        return text.strip()
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise

# ---------------------- Socket.IO realtime speech ----------------------
if SOCKETIO_AVAILABLE:
    @socketio.on('connect')
    def sio_connect():
        emit('connected', {'ok': True})

    @socketio.on('disconnect')
    def sio_disconnect():
        # Cleanup any streams tied to a socket if you track by request.sid
        pass

    @socketio.on('start_stream')
    def start_stream(data):
        """Initialize an audio stream.
        data: { session_id: str, mime: 'audio/webm'|'audio/wav'|'audio/ogg'|'audio/mpeg'|'audio/mp4' }
        """
        try:
            session_id = (data or {}).get('session_id') or 'default'
            mime = (data or {}).get('mime') or 'audio/webm'
            if mime not in ALLOWED_AUDIO_MIME:
                emit('error', {'message': f'Unsupported audio type: {mime}'})
                return
            ext_map = {
                'audio/webm': '.webm',
                'audio/wav': '.wav',
                'audio/x-wav': '.wav',
                'audio/ogg': '.ogg',
                'audio/mpeg': '.mp3',
                'audio/x-m4a': '.m4a',
                'audio/mp4': '.mp4',
            }
            speech_streams[session_id] = {
                'mime': mime,
                'ext': ext_map.get(mime, '.webm'),
                'chunks': []
            }
            emit('stream_started', {'session_id': session_id})
        except Exception as e:
            logger.error(f"start_stream error: {e}")
            emit('error', {'message': 'Failed to start stream'})

    @socketio.on('audio_chunk')
    def audio_chunk(data):
        """Append an audio chunk.
        data: { session_id: str, chunk: base64String }
        """
        try:
            session_id = (data or {}).get('session_id') or 'default'
            b64 = (data or {}).get('chunk')
            if not b64:
                return
            stream = speech_streams.get(session_id)
            if not stream:
                emit('error', {'message': 'Stream not initialized'})
                return
            stream['chunks'].append(b64)
            # Optionally: emit partial length
            emit('chunk_ack', {'session_id': session_id, 'chunks': len(stream['chunks'])})
        except Exception as e:
            logger.error(f"audio_chunk error: {e}")
            emit('error', {'message': 'Failed to process chunk'})

    @socketio.on('stop_stream')
    def stop_stream(data):
        """Finalize stream, transcribe, and reply.
        data: { session_id: str }
        Emits 'speech_result': { session_id, transcript, response }
        """
        try:
            session_id = (data or {}).get('session_id') or 'default'
            stream = speech_streams.pop(session_id, None)
            if not stream:
                emit('error', {'message': 'No active stream'})
                return
            # Reassemble file from base64 chunks
            raw_b64 = ''.join(stream['chunks'])
            audio_bytes = base64.b64decode(raw_b64)
            tmp_dir = os.getenv('TMP_DIR', os.getcwd())
            filename = f"realtime_{int(time.time()*1000)}{stream['ext']}"
            fp = os.path.join(tmp_dir, filename)
            with open(fp, 'wb') as f:
                f.write(audio_bytes)
            try:
                transcript = transcribe_audio_file(fp, stream['mime'])
            finally:
                try:
                    os.remove(fp)
                except Exception:
                    pass
            # Generate assistant reply
            reply = generate_assistant_reply(session_id, transcript)

            payload = {
                'session_id': session_id,
                'transcript': transcript,
                'response': reply,
            }

            emit('speech_result', payload)
        except Exception as e:
            logger.error(f"stop_stream error: {e}")
            emit('error', {'message': 'Failed to finalize speech'})

# Google Sheets setup
def get_google_sheets_client():
    """Initialize Google Sheets client"""
    try:
        creds_json = os.getenv('GOOGLE_SHEETS_CREDS')
        if not creds_json:
            logger.warning("GOOGLE_SHEETS_CREDS not configured")
            return None
        creds_dict = json.loads(creds_json)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
        return client
    except json.JSONDecodeError as je:
        logger.error(f"Error parsing Google Sheets credentials JSON: {je}")
        return None
    except Exception as e:
        logger.error(f"Error initializing Google Sheets: {e}")
        return None

# Conversation history storage (in production, use Redis or database)
conversations = {}

# System prompt for the chatbot
SYSTEM_PROMPT = """You are the USF Moving Company assistant (Houston, TX). KEEP RESPONSES CONCISE.

COMPANY INFORMATION:
- Company Name: USF Moving Company
- Phone: (281) 743-4503
- Office Address: 2800 Rolido Dr Apt 238, Houston, TX 77063
- Website: https://www.usfhoustonmoving.com/

TONE & STYLE:
- Professional, warm, calm, and confident
- Short, clear sentences
- Acknowledge customer stress and guide step-by-step
- Build trust without pressure or hype
- Be conversational and helpful

PRICING (local Houston metro):
- 2 movers + truck: $125/hr + 30 min travel time
- 3 movers + truck: $150/hr + 30 min travel time
- 4 movers + truck: $200/hr + 30 min travel time
- Included: speed pack, wardrobe boxes, stretch wrap, tapes, furniture pads
- Optional: 4 movers + 2 trucks available upon request

FLOW BASICS:

ESTIMATE REQUESTS (<50 mi):
When customer asks for an estimate, quote, or pricing:
1. Skip personal information initially - DON'T ask for name, phone, or email yet
2. Go directly to collecting move details:
   - Pickup full address (including city and ZIP)
   - Drop-off full address (including city and ZIP)
   - Home size/type and number of bedrooms
   - Stairs, elevator, or parking notes
   - Special items (piano, safe, pool table, appliances, etc.)
3. **IMPORTANT - Distance Check:**
   - If distance between pickup and drop-off is **GREATER THAN 50 MILES** (long-distance move):
     * Inform customer: "This is a long-distance move (over 50 miles). For accurate pricing, our manager needs to contact you directly."
     * Collect personal information: name, phone number, and email
     * Confirm: "Thank you! Our manager will contact you shortly to discuss your long-distance move and provide a detailed quote."
     * DO NOT provide an estimate for long-distance moves
     - If distance is **LESS THAN 50 MILES** (local move):
         * Provide the hourly rate for the recommended crew (+30 min travel time). Do NOT calculate a final price.
         * AFTER giving the hourly rate, ask: "Would you like to proceed with booking? I can collect your contact details."
     * ONLY if customer wants to book, then collect: name, phone, email, move date, and preferred time

BOOKING REQUESTS:
When customer explicitly wants to book or schedule:
1. Collect move details first (addresses, home size)
2. Collect move date and preferred time (MANDATORY - always ask for both)
3. Check availability for that date (we accept max 3 bookings per day)
   - If date is available: Proceed to collect personal information (name, phone, email)
   - If date is fully booked: Say "That date is fully booked. Available dates: [list 2-3 alternate dates]. Which works better for you?"
4. After confirming available date and collecting contact info, summarize booking briefly: "Thank you! Your booking for [date] at [time] from [pickup] to [dropoff] has been received. Estimated cost: $XXX. Our manager will contact you to finalize details."
5. Keep the confirmation message SHORT and simple

GENERAL CHAT:
- Answer questions about services, pricing, availability
- Be helpful and conversational
- Provide information without pressuring for bookings
- If conversation is off-topic, politely redirect: "I'm here to help with moving services. What would you like to know?"

**IMPORTANT - KEEP RESPONSES CONCISE:**
- Don't repeat information already provided
- Keep confirmations brief
- Avoid asking for information you already have
- When user provides date, check availability immediately

WORKFLOW (summary):
1. Greet warmly and ask how you can help
2. Identify customer intent: estimate, booking, or general inquiry
3. For estimates:
   - LOCAL moves (<50 miles): Move details → Calculate distance → Provide estimate → Offer to book
   - LONG-DISTANCE moves (>50 miles): Move details → Calculate distance → Request personal info → Manager will contact
4. For bookings: Move details → **Date & time (MANDATORY)** → Check availability → If available: collect personal info → Confirm booking; If full: propose alternates
5. Always get full addresses (not just ZIP codes) to calculate distance
6. Distance determines the flow: <50 miles = instant estimate, >50 miles = manager contact required
7. Date and time are MANDATORY for all bookings - never skip asking for them


PRICE MATCH:
- If customer mentions competitor pricing, collect full details first
- Minimum rates: 2 movers $110/hr, 3 movers $135/hr, 4 movers $185/hr
- Never confirm price changes yourself - flag to management

CALL REQUESTS:
- If customer wants to speak by phone, ask: "Call in the next few minutes or later today?"
- Provide callback number: (281) 743-4503

OFF-TOPIC:
- Redirect politely: "I'm here to help with moving services. What would you like to know?"
- Keep conversations focused on moving-related topics

IMPORTANT RULES (critical):
- Be intelligent about what to ask based on customer intent
- Don't collect unnecessary information upfront
- For estimates: Skip personal details, focus on move details
- For bookings: Collect everything needed
- Never store or mention voice recordings
- Only use information customer provides in conversation
- Always be respectful and patient
- Guide step-by-step without overwhelming
- Confirm details before finalizing booking
- Be conversational and natural, not robotic
- Do NOT tell the customer we will email them. Say a manager will contact them if follow-up is needed.
- Internal rule: Only management receives booking emails. Customer emails are disabled unless explicitly enabled.

EXAMPLES (short):

Customer: "How much for a move?"
You: "I'd be happy to give you an estimate! To calculate an accurate price, I need a few details:
- Where are you moving from? (full address)
- Where are you moving to? (full address)
- How many bedrooms?
- Any stairs or elevator?"

[If LOCAL move <50 miles - After getting details]
You: "Based on your 2-bedroom apartment moving from [pickup] to [dropoff] (25 miles), we'd recommend X movers + truck at $YYY/hr (+30 min travel, 3-hour minimum). Would you like to proceed with booking?"

[If LONG-DISTANCE move >50 miles - After calculating distance]
You: "I see this is a long-distance move - the distance between [pickup] and [dropoff] is over 50 miles. For accurate pricing on long-distance moves, our manager will need to contact you directly. May I have your name, phone number, and email address? Our manager will reach out shortly to discuss your move and provide a detailed quote."

Customer: "I want to book a move"
You: "Great! Let me help you schedule that. First, let me get the move details:
- Where are you moving from?
- Where are you moving to?
- How many bedrooms?
- What date would you like to move?
- What time works best for you?"

[When customer provides date - check availability first]
Customer: "15 november at 10 AM"
You [if available]: "Perfect! November 15th at 10 AM is available. Now I need your contact information:
- Your name
- Phone number
- Email address"

You [if fully booked]: "Unfortunately, November 15th is fully booked (we're at capacity). Here are some available dates nearby: November 16th, November 18th, or November 20th. Which works better for you?"

[After collecting all info including date/time/contact]
You: "Thank you! Your booking is confirmed for November 15th, 2025 at 10 AM. Move details: [pickup] to [dropoff], 3 bedrooms. Rate: $YYY/hr (+30 min travel, 3-hr minimum). Our manager will contact you shortly to finalize. Thank you!"
"""

# Default welcome message for new visitors/sessions
WELCOME_MESSAGE = (
    "Hello! I can help you schedule your move or get a price estimate. "
    "When you're ready, just tell me where you're moving from."
)

# ---- Pricing & Availability configuration ----
OFFICE_ADDRESS = os.getenv('OFFICE_ADDRESS', '2800 Rolido Dr Apt 238, Houston, TX 77063')
DAILY_CAPACITY = int(os.getenv('DAILY_CAPACITY', '3'))  # max bookings per day
PEAK_DATES = set([d.strip() for d in os.getenv('PEAK_DATES', '').split(',') if d.strip()])  # YYYY-MM-DD

def _safe_float_miles(distance_text):
    try:
        return float(distance_text.split()[0].replace(',', ''))
    except Exception:
        return None

def get_distance_miles_one_way(origin, destination):
    """Get distance (miles) between two addresses using Google Maps Distance Matrix with simple cache."""
    try:
        key = (origin.strip().lower(), destination.strip().lower())
        if key in distance_cache:
            return distance_cache[key]
        result = gmaps.distance_matrix(origins=origin, destinations=destination, units='imperial')
        if result['rows'][0]['elements'][0]['status'] == 'OK':
            miles = _safe_float_miles(result['rows'][0]['elements'][0]['distance']['text'])
            distance_cache[key] = miles
            return miles
        logger.error(f"Distance matrix error: {result}")
        return None
    except Exception as e:
        logger.error(f"Distance matrix exception: {e}")
        return None

def get_total_route_miles(office, pickup, drop):
    """Total route miles: Office -> Pickup -> Drop -> Office."""
    legs = [
        get_distance_miles_one_way(office, pickup),
        get_distance_miles_one_way(pickup, drop),
        get_distance_miles_one_way(drop, office),
    ]
    if any(m is None for m in legs):
        return None
    return round(sum(legs), 1)

def is_peak_date(date_str):
    return date_str in PEAK_DATES if date_str else False

def get_week_start_end(dt):
    # ISO week: Monday start
    start = dt - timedelta(days=dt.weekday())
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return start, end

def detect_call_intent(text: str) -> bool:
    """Detect if the user is requesting a phone call with a manager."""
    if not text:
        return False
    t = text.lower()
    triggers = [
        'call me', 'call back', 'call at', 'phone me',
        'speak to', 'talk to', 'speak with', 'talk with',
        'contact me', 'contact manager', 'contact with manager', 'manager contact',
        'call manager', 'manager call', 'talk with manager', 'speak with manager'
    ]
    return any(k in t for k in triggers)

def parse_call_timing(text: str) -> str:
    """Parse a simple timing phrase like '2 PM today', 'now', 'later today'. Returns a concise string."""
    try:
        t = (text or '').lower()
        # explicit now/later
        if 'now' in t or 'right now' in t:
            return 'immediate'
        if 'later today' in t or 'later' in t:
            return 'later today'
        if 'tomorrow' in t:
            # attempt to capture time with tomorrow
            m = re.search(r"(\d{1,2}(?::\d{2})?\s*(am|pm))", t)
            return f"{m.group(1)} tomorrow" if m else 'tomorrow'
        # capture times like 2 pm, 2:30 pm
        m = re.search(r"(\b\d{1,2}(?::\d{2})?\s*(am|pm)\b)", t)
        if m and 'today' in t:
            return f"{m.group(1).upper()} today"
        if m:
            return m.group(1).upper()
        return 'immediate'
    except Exception:
        return 'immediate'

def send_call_request_email(name: str, phone: str, timing: str, extra: str = "") -> bool:
    """Notify management of a call request using the existing email sender."""
    try:
        subject = f"Call Request - {name or 'Unknown'}"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>New Call Request</h2>
            <p><strong>Name:</strong> {name or 'N/A'}</p>
            <p><strong>Phone:</strong> {phone or 'N/A'}</p>
            <p><strong>Timing:</strong> {timing or 'N/A'}</p>
            {f'<p><strong>Notes:</strong> {extra}</p>' if extra else ''}
            <p><strong>Requested At:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </body>
        </html>
        """
        manager_email = os.getenv('MANAGER_EMAIL')
        logger.info(f"Call request email: to={manager_email}, name={name}, phone={phone}, timing={timing}")
        if not manager_email:
            logger.warning('MANAGER_EMAIL not set; cannot notify manager of call request')
            return False
        ok = send_email(manager_email, subject, body)
        if ok:
            logger.info("✅ Call request email sent to manager")
        else:
            logger.error("❌ Failed to send call request email")
        return ok
    except Exception as e:
        logger.error(f"Error sending call request email: {e}")
        return False

def get_weekly_jobs_count():
    """Count number of jobs created this week based on Timestamp column in Google Sheet."""
    try:
        client = get_google_sheets_client()
        if not client:
            return 0
        sheet_id = os.getenv('BOOKING_SHEET_ID')
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.sheet1
        rows = worksheet.get_all_values()
        if not rows:
            return 0
        now = datetime.now()
        week_start, week_end = get_week_start_end(now)
        count = 0
        # Skip header if present
        start_index = 1 if rows and rows[0] and rows[0][0].lower() in ('timestamp', 'time', 'date') else 0
        for r in rows[start_index:]:
            if not r or len(r) == 0:
                continue
            ts_str = r[0]
            try:
                ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                if week_start <= ts <= week_end:
                    count += 1
            except Exception:
                # skip unparsable rows
                continue
        return count
    except Exception as e:
        logger.error(f"Error counting weekly jobs: {e}")
        return 0

def count_jobs_on_date(date_str):
    """Count number of bookings for the given move date (YYYY-MM-DD)."""
    try:
        client = get_google_sheets_client()
        if not client:
            return 0
        sheet_id = os.getenv('BOOKING_SHEET_ID')
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.sheet1
        rows = worksheet.get_all_values()
        if not rows:
            return 0
        # Move Date column index based on save_to_google_sheet order (0-based index 6)
        MOVE_DATE_IDX = 6
        start_index = 1 if rows and rows[0] and rows[0][0].lower() in ('timestamp', 'time', 'date') else 0
        count = 0
        for r in rows[start_index:]:
            if len(r) > MOVE_DATE_IDX and r[MOVE_DATE_IDX] == date_str:
                count += 1
        return count
    except Exception as e:
        logger.error(f"Error counting jobs on date: {e}")
        return 0

def suggest_alternate_dates(requested_date_str, max_suggestions=3):
    try:
        requested = datetime.strptime(requested_date_str, '%Y-%m-%d').date()
    except Exception:
        return []
    suggested = []
    day = requested
    # look forward up to 14 days for availability
    for i in range(1, 15):
        day = requested + timedelta(days=i)
        ds = day.strftime('%Y-%m-%d')
        if count_jobs_on_date(ds) < DAILY_CAPACITY:
            suggested.append(ds)
        if len(suggested) >= max_suggestions:
            break
    return suggested

def compute_base_price_and_crew(rooms, stairs_elevator, weekly_jobs):
    """Return (base_price, crew_size_str, tier_label)."""
    # Determine tier by weekly jobs
    if weekly_jobs <= 2:
        tier = '0-2'
    elif weekly_jobs <= 4:
        tier = '2-4'
    else:
        tier = '5-7'

    # Pricing tables
    # 1–2 rooms, no stairs or 1–2 floors: 2 movers + truck: 100 / 125 / 150
    # 2–3 rooms with stairs/elevator: 3 movers + truck: 125 / 150 / 175
    # 3+ rooms with stairs/elevator: 4 movers + truck: 180 / 200 / 250
    base_price = None
    crew = None

    if rooms <= 2 and not stairs_elevator:
        crew = '2 movers + truck'
        base_price = {'0-2': 100, '2-4': 125, '5-7': 150}[tier]
    elif (rooms in [2, 3] and stairs_elevator) or (rooms == 3 and not stairs_elevator):
        # assume 3 rooms even without stairs typically needs 3 movers
        crew = '3 movers + truck'
        base_price = {'0-2': 125, '2-4': 150, '5-7': 175}[tier]
    else:
        # 3+ with stairs/elevator or larger
        crew = '4 movers + truck'
        base_price = {'0-2': 180, '2-4': 200, '5-7': 250}[tier]

    return base_price, crew, tier

def crew_hourly_rate(crew_size: str) -> int:
    """Map crew size string to hourly rate (includes truck) per public pricing.
    Note: We return the base published hourly rate only; travel time (30 min) and
    3-hour minimum are communicated in messaging rather than computing a flat total.
    """
    if not crew_size:
        return 0
    crew_size = crew_size.lower()
    if crew_size.startswith('2 '):
        return 125
    if crew_size.startswith('3 '):
        return 150
    if crew_size.startswith('4 '):
        return 200
    # Fallback (future expansion like 5 movers etc.)
    return 0

def generate_estimate_logic(rooms, pickup_address, drop_address, stairs_elevator=False, move_date=None):
    """Compute recommendation and pricing context without final totals.
    Returns details including: crew_size, hourly_rate (USD/hr), pickup_drop_miles, total_route_miles,
    and messaging fields for travel/minimums. No final price is calculated.
    """
    # Distances
    total_route_miles = get_total_route_miles(OFFICE_ADDRESS, pickup_address, drop_address)
    if total_route_miles is None:
        raise ValueError('Could not calculate route distance')

    # Determine local vs long-distance based on pickup->drop distance
    pickup_drop_miles = get_distance_miles_one_way(pickup_address, drop_address)
    if pickup_drop_miles is None:
        raise ValueError('Could not calculate pickup/drop distance')
    move_category = 'local' if pickup_drop_miles < 50 else 'long-distance'

    # Weekly jobs & tier
    weekly_jobs = get_weekly_jobs_count()
    base_price, crew_size, tier_label = compute_base_price_and_crew(rooms, bool(stairs_elevator), weekly_jobs)
    # Base price here is our tier-adjusted anchor; we will communicate the published
    # hourly rate for the recommended crew instead of computing a final total.
    hourly = crew_hourly_rate(crew_size)

    # Peak date surcharge
    peak_surcharge = 25 if move_date and is_peak_date(move_date) else 0

    notes = []
    if move_category == 'long-distance':
        notes.append('Packing materials are free for long-distance moves.')

    return {
        'rooms': rooms,
        'stairs_elevator': bool(stairs_elevator),
        'crew_size': crew_size,
        'base_price': base_price,
        'hourly_rate': hourly,
        'tier': tier_label,
        'total_route_miles': total_route_miles,
        'pickup_drop_miles': pickup_drop_miles,
        'mileage_charge': None,
        'peak_surcharge': peak_surcharge,
        'total_estimate': None,
        'move_category': move_category,
        'notes': notes,
        'travel_time_minutes': 30,
        'minimum_hours': 3,
    }

def calculate_distance(origin, destination):
    """Calculate distance between two addresses using Google Maps"""
    try:
        result = gmaps.distance_matrix(origins=origin, destinations=destination, units='imperial')
        
        if result['rows'][0]['elements'][0]['status'] == 'OK':
            distance_text = result['rows'][0]['elements'][0]['distance']['text']
            distance_miles = float(distance_text.split()[0].replace(',', ''))
            return distance_miles
        else:
            logger.error(f"Distance calculation failed: {result}")
            return None
    except Exception as e:
        logger.error(f"Error calculating distance: {e}")
        return None

def send_email(to_email, subject, body):
    """Send email using SMTP"""
    try:
        from_email = os.getenv('EMAIL_ADDRESS')
        password = os.getenv('EMAIL_PASSWORD')
        
        msg = MIMEMultipart('alternative')
        msg['From'] = from_email
        msg['To'] = to_email
        msg['Subject'] = subject
        
        html_part = MIMEText(body, 'html')
        msg.attach(html_part)
        
        with smtplib.SMTP(os.getenv('SMTP_SERVER'), int(os.getenv('SMTP_PORT'))) as server:
            server.starttls()
            server.login(from_email, password)
            server.send_message(msg)
        
        logger.info(f"Email sent successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return False

def save_to_google_sheet(booking_data):
    """Save booking data to Google Sheets - Bookings, Customers, and Call_Log"""
    try:
        client = get_google_sheets_client()
        if not client:
            logger.warning("Google Sheets client not available")
            return False
        
        sheet_id = os.getenv('BOOKING_SHEET_ID')
        spreadsheet = client.open_by_key(sheet_id)
        
        # Get all worksheets
        # Resolve worksheets with graceful fallback
        try:
            bookings_sheet = spreadsheet.worksheet('Bookings')
        except Exception:
            logger.warning("Worksheet 'Bookings' not found, using first sheet")
            bookings_sheet = spreadsheet.sheet1
        try:
            customers_sheet = spreadsheet.worksheet('Customers')
        except Exception:
            customers_sheet = None
            logger.warning("Worksheet 'Customers' not found; skipping customer row append")
        
        # Generate IDs
        now = datetime.now()
        booking_id = f"BOOK-{now.strftime('%Y%m%d%H%M%S')}"
        customer_id = f"CUST-{now.strftime('%Y%m%d%H%M%S')}"
        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        date_only = now.strftime('%Y-%m-%d')
        
        # Parse home size to get rooms
        rooms = ''
        home_size = str(booking_data.get('home_size', ''))
        for word in home_size.split():
            if word.isdigit():
                rooms = word
                break
        
        # Extract estimate amount
        estimate_str = str(booking_data.get('estimated_cost', ''))
        estimate_amount = ''
        if '$' in estimate_str:
            estimate_amount = estimate_str.replace('$', '').replace(',', '').strip()
        
        # 1. Save to BOOKINGS sheet
        # Columns: Booking ID, Date Created, Customer Name, Phone, Email, Move Type, 
        # Pickup Address, Pickup Type, Pickup Rooms, Pickup Stairs,
        # Dropoff Address, Dropoff Type, Dropoff Rooms, Dropoff Stairs,
        # Move Date, Time Preference, Packing Service, Special Items, Special Instructions,
        # Hourly Rate, Mileage Charge, Total Price, Crew Size, Total Miles, Status, Booking Status, [empty], [empty]
        booking_row = [
            booking_id,
            timestamp,
            booking_data.get('name', ''),
            booking_data.get('phone', ''),
            booking_data.get('email', ''),
            booking_data.get('move_type', 'Local'),
            booking_data.get('pickup_address', ''),
            'house',  # Pickup Type
            rooms,  # Pickup Rooms
            'Yes' if 'stair' in str(booking_data.get('stairs_elevator', '')).lower() else 'No',
            booking_data.get('drop_address', ''),
            'house',  # Dropoff Type
            rooms,  # Dropoff Rooms
            'No',  # Dropoff Stairs
            booking_data.get('move_date', ''),
            booking_data.get('time_preference', '10 AM'),  # Time Preference (from extraction or default)
            'No',  # Packing Service
            booking_data.get('special_items', 'no.'),
            booking_data.get('notes', 'no.'),  # Special Instructions
            '',  # Hourly Rate
            '',  # Mileage Charge
            estimate_amount,  # Total Price
            booking_data.get('crew_size', ''),  # Crew Size
            booking_data.get('distance_miles', ''),  # Total Miles
            'Confirmed',  # Status
            'CHAT-BOOKING',  # Booking Status (identifier for chat bookings)
            'Yes',  # Confirmed
            'No'  # Call recording
        ]
        bookings_sheet.append_row(booking_row)
        logger.info(f"Booking {booking_id} saved to Bookings sheet")
        
        # 2. Save to CUSTOMERS sheet
        # Columns: [ID], Name, Phone, Email, First Contact Date, Last Contact Date, Total Bookings, Notes
        if customers_sheet:
            customer_row = [
                customer_id,
                booking_data.get('name', ''),
                booking_data.get('phone', ''),
                booking_data.get('email', ''),
                timestamp,      # First Contact Date
                date_only,      # Last Contact Date (today by default)
                '1',            # Total Bookings
                ''              # Notes
            ]
            customers_sheet.append_row(customer_row)
            logger.info(f"Customer {customer_id} saved to Customers sheet")
        
        return True
    except Exception as e:
        logger.error(f"Error saving to Google Sheet: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

def send_booking_email_to_management(booking_data):
    """Send booking notification to management"""
    subject = f"New Booking Request - {booking_data.get('name', 'Unknown')}"
    
    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2 style="color: #2c3e50;">New Booking Request</h2>
        
        <h3>Customer Information:</h3>
        <ul>
            <li><strong>Name:</strong> {booking_data.get('name', 'N/A')}</li>
            <li><strong>Phone:</strong> {booking_data.get('phone', 'N/A')}</li>
            <li><strong>Email:</strong> {booking_data.get('email', 'N/A')}</li>
        </ul>
        
        <h3>Move Details:</h3>
        <ul>
            <li><strong>Pickup Address:</strong> {booking_data.get('pickup_address', 'N/A')}</li>
            <li><strong>Drop-off Address:</strong> {booking_data.get('drop_address', 'N/A')}</li>
            <li><strong>Move Date:</strong> {booking_data.get('move_date', 'N/A')}</li>
            <li><strong>Preferred Time:</strong> {booking_data.get('time_preference', 'Not specified')}</li>
            <li><strong>Home Size:</strong> {booking_data.get('home_size', 'N/A')}</li>
            <li><strong>Move Type:</strong> {booking_data.get('move_type', 'N/A')}</li>
            <li><strong>Distance:</strong> {booking_data.get('distance_miles', 'N/A')} miles</li>
        </ul>
        
        <h3>Additional Details:</h3>
        <ul>
            <li><strong>Crew Size Requested:</strong> {booking_data.get('crew_size', 'N/A')}</li>
            <li><strong>Special Items:</strong> {booking_data.get('special_items', 'None')}</li>
            <li><strong>Packing Needs:</strong> {booking_data.get('packing_needs', 'None')}</li>
            <li><strong>Stairs/Elevator/Parking:</strong> {booking_data.get('stairs_elevator', 'N/A')}</li>
        </ul>
        
        <h3>Estimated Cost:</h3>
        <p style="font-size: 18px; color: #27ae60;"><strong>{booking_data.get('estimated_cost', 'N/A')}</strong></p>
        
        <p style="margin-top: 30px;">Please confirm this booking with the customer.</p>
    </body>
    </html>
    """
    
    manager_email = os.getenv('MANAGER_EMAIL')
    logger.info(f"Sending booking email to manager: {manager_email}")
    result = send_email(manager_email, subject, body)
    if result:
        logger.info(f"✅ Manager email sent successfully to {manager_email}")
    else:
        logger.error(f"❌ Failed to send manager email to {manager_email}")
    return result

def enrich_booking_data(booking_data):
    """Populate computed pricing/crew/distance fields if possible using existing logic."""
    try:
        rooms = None
        home_size = str(booking_data.get('home_size','')).lower()
        # capture both "3 bedroom" and "bedrooms 3" styles
        import re
        m = re.search(r"(\d+)\s*bed(room)?s?", home_size)
        if m:
            rooms = int(m.group(1))
        else:
            m2 = re.search(r"bed(room)?s?\s*[:\-]?\s*(\d+)", home_size)
            if m2:
                rooms = int(m2.group(2))
        pickup = booking_data.get('pickup_address')
        drop = booking_data.get('drop_address')
        if rooms and pickup and drop:
            stairs_text = str(booking_data.get('stairs_elevator','')).lower()
            no_stairs = any(s in stairs_text for s in ['no stair', 'no stairs', 'without stair', 'without stairs'])
            has_stairs_tokens = ('stair' in stairs_text or 'stairs' in stairs_text or 'elevator' in stairs_text)
            stairs_flag = False if no_stairs else (True if has_stairs_tokens else False)
            move_date = booking_data.get('move_date')
            est = generate_estimate_logic(rooms, pickup, drop, stairs_flag, move_date)
            booking_data['crew_size'] = est.get('crew_size', booking_data.get('crew_size'))
            booking_data['distance_miles'] = est.get('pickup_drop_miles', booking_data.get('distance_miles'))
            if not booking_data.get('estimated_cost'):
                # Store hourly rate description instead of a final total
                booking_data['estimated_cost'] = f"${est['hourly_rate']}/hr (+30 min travel, 3-hr minimum)"
            booking_data['move_type'] = est.get('move_category', booking_data.get('move_type'))
            booking_data['mileage_charge'] = est.get('mileage_charge', booking_data.get('mileage_charge'))
        else:
            logger.info("Insufficient data to compute estimate (rooms/pickup/drop missing)")
    except Exception as e:
        logger.warning(f"enrich_booking_data failed: {e}")
    return booking_data

def parse_quick_move_details(text: str):
    """Best-effort parse of pickup, drop, rooms, and stairs from free text.
    Returns dict with keys: pickup, drop, rooms (int), stairs (bool) when detected, else None.
    """
    import re
    t = (text or '').strip().lower()
    if not t:
        return None
    # from ... to ... single-line pattern
    m = re.search(r"from\s+([^\n]+?)\s+to\s+([^\n]+)", t)
    pickup = drop = None
    if m:
        pickup = m.group(1).strip()
        drop = m.group(2).strip()
    # bedrooms
    rooms = None
    mr = re.search(r"(\d+)\s*bed(room)?s?", t)
    if mr:
        try:
            rooms = int(mr.group(1))
        except Exception:
            rooms = None
    stairs = None
    if 'no stair' in t or 'stairs no' in t or 'stair no' in t:
        stairs = False
    elif 'stair' in t or 'stairs' in t or 'elevator' in t:
        stairs = True
    if pickup and drop and rooms:
        return {'pickup': pickup, 'drop': drop, 'rooms': rooms, 'stairs': bool(stairs)}
    return None

def send_confirmation_email_to_customer(booking_data):
    """Send confirmation email to customer only if explicitly enabled via SEND_CUSTOMER_EMAIL=True.
    Default behavior: do NOT send customer emails."""
    if os.getenv('SEND_CUSTOMER_EMAIL', 'False').lower() != 'true':
        logger.info("Skipping customer confirmation email (SEND_CUSTOMER_EMAIL != True)")
        return False
    subject = "Booking Confirmation - USF Moving Company"
    body = f"""
    <html><body style=\"font-family: Arial, sans-serif; line-height: 1.6;\">
    <h2 style=\"color:#2c3e50;\">USF Moving Booking</h2>
    <p>Hi {booking_data.get('name','there')}, thanks for your details. Our manager will reach out to finalize your booking.</p>
    <ul>
      <li><strong>From:</strong> {booking_data.get('pickup_address','N/A')}</li>
      <li><strong>To:</strong> {booking_data.get('drop_address','N/A')}</li>
      <li><strong>Date:</strong> {booking_data.get('move_date','N/A')}</li>
      <li><strong>Estimate:</strong> {booking_data.get('estimated_cost','N/A')}</li>
      <li><strong>Crew:</strong> {booking_data.get('crew_size','N/A')}</li>
    </ul>
    <p>Questions? Call (281) 743-4503.</p>
    </body></html>
    """
    return send_email(booking_data.get('email'), subject, body)

def extract_booking_from_conversation(conversation_history):
    """Extract booking information using AI from conversation history.
    Returns booking_data dict with `ready_to_submit` flag (always present).
    """
    global openai_client
    
    # Ensure OpenAI client is available
    if not openai_client:
        logger.warning("OpenAI client not available for extraction, falling back to regex")
        return extract_booking_from_conversation_regex(conversation_history)
    
    # Join all messages for context
    full_conversation = "\n".join([
        f"{msg['role']}: {msg['content']}" 
        for msg in conversation_history 
        if msg['role'] in ['user', 'assistant']
    ])
    
    extraction_prompt = f"""Analyze this conversation and extract booking information. Return ONLY a valid JSON object with these fields (use null for missing fields):

{{
  "name": "customer's full name or null",
  "phone": "phone number (digits only) or null",
  "email": "email address or null",
  "pickup_address": "full pickup address or null",
  "drop_address": "full dropoff address or null",
  "home_size": "number of bedrooms (e.g., '3 bedroom') or null",
  "stairs_elevator": "presence of stairs/elevator (e.g., 'No stairs', 'Stairs present') or null",
  "move_date": "date in YYYY-MM-DD format or null",
  "time_preference": "preferred time (e.g., '2 PM', 'Morning') or null",
  "estimated_cost": "cost if mentioned (e.g., '$525') or null",
  "special_items": "special items mentioned or 'None'",
  "crew_size": "crew size if mentioned or null",
  "distance_miles": "distance if mentioned or null"
}}

IMPORTANT RULES:
1. Extract phone numbers without spaces, dashes, or special characters
2. For dates, convert month names to YYYY-MM-DD format (assume year 2025)
3. Return ONLY the JSON object, no other text
4. If a field is not mentioned, use null
5. For pickup_address and drop_address, include full address details from the conversation

CONVERSATION:
{full_conversation}

JSON OUTPUT:"""

    try:
        response = openai_client.chat.completions.create(
            model=os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
            messages=[{"role": "user", "content": extraction_prompt}],
            temperature=0.1,  # Low temperature for consistent extraction
            max_tokens=500,
            timeout=15
        )
        
        extraction_text = response.choices[0].message.content.strip()
        
        # Remove markdown code blocks if present
        extraction_text = extraction_text.replace('```json\n', '').replace('```\n', '').replace('```', '').strip()
        
        # Parse JSON
        booking_data = json.loads(extraction_text)
        
        # Set defaults for missing fields
        booking_data['move_type'] = booking_data.get('move_type', 'local')
        booking_data['packing_needs'] = booking_data.get('packing_needs', 'None specified')
        booking_data['special_items'] = booking_data.get('special_items', 'None')
        booking_data['crew_size'] = booking_data.get('crew_size', 'TBD')
        booking_data['distance_miles'] = booking_data.get('distance_miles', 'TBD')
        booking_data['status'] = booking_data.get('status', 'Pending')
        
        # Clean up null values
        booking_data = {k: v for k, v in booking_data.items() if v is not None and v != 'null'}

        # Detect long-distance readiness (manager handoff) separately from full booking readiness
        try:
            pickup_addr = booking_data.get('pickup_address')
            drop_addr = booking_data.get('drop_address')
            miles = None
            if pickup_addr and drop_addr:
                miles = get_distance_miles_one_way(pickup_addr, drop_addr)
            long_distance = (miles is not None and miles >= 50)
            if long_distance:
                booking_data['move_type'] = 'long-distance'
                contact_ready = all(booking_data.get(f) for f in ['name','phone','email'])
                addr_ready = all(booking_data.get(f) for f in ['pickup_address','drop_address'])
                booking_data['ready_for_long_distance'] = bool(contact_ready and addr_ready)
                booking_data['distance_miles'] = miles
            else:
                booking_data['ready_for_long_distance'] = False
        except Exception as _e:
            # On any failure, don't block the flow
            booking_data['ready_for_long_distance'] = False
        
        # Check if ready to submit
        required_fields = ['name', 'phone', 'email', 'pickup_address', 'drop_address', 'move_date', 'time_preference']
        booking_data['ready_to_submit'] = all(
            field in booking_data and booking_data[field] and booking_data[field] != 'TBD'
            for field in required_fields
        )
        
        # Log extraction results
        if not booking_data['ready_to_submit']:
            missing = [f for f in required_fields if f not in booking_data or not booking_data.get(f)]
            logger.info(f"Booking not ready. Missing fields: {missing}")
            logger.info(f"📋 Current booking_data: {booking_data}")
        else:
            logger.info(f"✅ Booking ready to submit! Data: {booking_data}")
        
        return booking_data
        
    except json.JSONDecodeError as je:
        logger.error(f"Failed to parse AI extraction JSON: {je}")
        logger.error(f"Raw response: {extraction_text}")
        return {'ready_to_submit': False}
    except Exception as e:
        logger.error(f"Error in AI-based extraction: {e}")
        return {'ready_to_submit': False}


def extract_booking_from_conversation_regex(conversation_history):
    """Fallback regex-based extraction (your original function).
    Keep this as a backup when AI extraction fails.
    """
    import re
    
    # Join all user and assistant messages
    full_text = ""
    for msg in conversation_history:
        if msg['role'] in ['user', 'assistant']:
            full_text += msg['content'] + "\n"
    
    full_text_lower = full_text.lower()
    
    # Initialize booking data
    booking_data = {}
    
    # Extract name - IMPROVED PATTERNS
    name_patterns = [
        r"name[:\s]+([a-z]+(?:\s+[a-z]+)?)",
        r"(?:i'm|i am|my name is|call me)\s+([a-z]+(?:\s+[a-z]+)?)",
        r"(?:^|\n)([a-z]+)\s+\d{10,15}",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, full_text_lower)
        if match:
            booking_data['name'] = match.group(1).strip().title()
            break
    
    # Extract phone
    phone_patterns = [
        r'phone[:\s]+(\d{10,15})',
        r'\b(\d{10,15})\b',
        r'(\d{3}[-.\s]?\d{3}[-.\s]?\d{4,})',
    ]
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_text)
        if phone_match:
            booking_data['phone'] = re.sub(r'[^\d]', '', phone_match.group(1))
            break
    
    # Extract email
    email_patterns = [
        r'(?:email|mail)[:\s]+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b',
    ]
    for pattern in email_patterns:
        email_match = re.search(pattern, full_text, re.IGNORECASE)
        if email_match:
            booking_data['email'] = email_match.group(1)
            break
    
    # ... (keep rest of your regex patterns)
    
    booking_data['ready_to_submit'] = False
    return booking_data

@app.route('/')
def home():
    """Health check endpoint"""
    return jsonify({
        'status': 'online',
        'service': 'USF Moving Company Chatbot API',
        'version': '1.0'
    })

@app.route('/welcome', methods=['GET'])
def welcome():
    """Return the default welcome message (useful for front-ends to display on load)."""
    return jsonify({'message': WELCOME_MESSAGE})

@app.route('/chat', methods=['POST'])
def chat():
    """Main chat endpoint for WordPress integration"""
    try:
        data = request.json or {}
        user_message = data.get('message', '').strip()
        session_id = data.get('session_id', 'default')

        if not user_message:
            return jsonify({'error': 'No message provided'}), 400

        # Ensure OpenAI client exists (handle missing key or rotation)
        global openai_client
        current_key = os.getenv('OPENAI_API_KEY')
        if not current_key:
            return jsonify({'error': 'OpenAI API key not configured'}), 500
        if not openai_client or current_key != OPENAI_API_KEY:
            try:
                openai_client = OpenAI(api_key=current_key)
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                return jsonify({'error': 'OpenAI initialization failed'}), 500

        # Apply OpenAI cooldown if previously rate-limited
        global OPENAI_COOLDOWN_UNTIL
        if OPENAI_COOLDOWN_UNTIL and OPENAI_COOLDOWN_UNTIL > datetime.utcnow().timestamp():
            remaining = int(OPENAI_COOLDOWN_UNTIL - datetime.utcnow().timestamp())
            quick = parse_quick_move_details(user_message)
            if quick:
                # Provide cached/deterministic estimate without OpenAI
                try:
                    est = generate_estimate_logic(quick['rooms'], quick['pickup'], quick['drop'], quick.get('stairs') or False, None)
                    msg = (
                        f"(AI cooldown active {remaining}s) Distance: {est['pickup_drop_miles']:.1f} miles. Crew: {est['crew_size']}. "
                        f"Hourly rate: ${est['hourly_rate']}/hr (+30 min travel). "
                        "Would you like to proceed with booking? I can collect your contact details."
                    )
                    return jsonify({'response': msg, 'session_id': session_id, 'cooldown': True}), 200
                except Exception as ce:
                    logger.error(f"Cooldown estimate error: {ce}")
            else:
                msg = (
                    f"(AI cooldown active {remaining}s) Please send full move details: pickup and drop addresses, bedrooms, stairs/elevator, special items. "
                    "Then date + preferred time."
                )
                return jsonify({'response': msg, 'session_id': session_id, 'cooldown': True}), 200

        # Initialize conversation history for this session
        if session_id not in conversations:
            conversations[session_id] = [
                {"role": "system", "content": SYSTEM_PROMPT}
            ]
            logger.info(f"Session {session_id}: New conversation initialized")
            # Immediately send welcome message as assistant role so it appears before first user prompt
            conversations[session_id].append({"role": "assistant", "content": WELCOME_MESSAGE})
            # If the user only requested a blank init (no message), return welcome directly
            if not user_message:
                return jsonify({'response': WELCOME_MESSAGE, 'session_id': session_id}), 200

        # Add user message to conversation
        conversations[session_id].append({"role": "user", "content": user_message})
        
        logger.info(f"Session {session_id}: User message length: {len(user_message)}, Conversation depth: {len(conversations[session_id])}")

        # Trim conversation aggressively (keep system + last 10 messages for faster processing)
        if len(conversations[session_id]) > 12:
            conversations[session_id] = [conversations[session_id][0]] + conversations[session_id][-11:]
            logger.info(f"Session {session_id}: Trimmed conversation history to {len(conversations[session_id])} messages")

        # Call OpenAI (chat completions API in new SDK)
        # Use fastest models first
        model_candidates = ['gpt-4o-mini', 'gpt-3.5-turbo']
        if os.getenv('OPENAI_MODEL'):
            model_candidates.insert(0, os.getenv('OPENAI_MODEL'))

        assistant_message = None
        last_err = None
        # Basic backoff attempt counts
        for attempt, model_name in enumerate(model_candidates, start=1):
            try:
                response = openai_client.chat.completions.create(
                    model=model_name,
                    messages=conversations[session_id],
                    temperature=0.7,
                    max_tokens=180,  # further reduced to mitigate TPM usage
                    timeout=25  # slightly lower timeout
                )
                assistant_message = response.choices[0].message.content
                logger.info(f"OpenAI response received using {model_name}")
                break
            except Exception as oe:
                last_err = oe
                logger.error(f"OpenAI model '{model_name}' failed (attempt {attempt}): {oe}")
                # If rate limit, short sleep backoff (non-blocking long waits avoided)
                if 'rate limit' in str(oe).lower() or 'quota' in str(oe).lower():
                    import time
                    time.sleep(1.2)
                    continue

        if assistant_message is None:
            # Rate-limit or error: fall back to deterministic parser to keep UX moving
            logger.error(f"OpenAI API error for session {session_id}: {last_err}")
            # Set cooldown for subsequent requests (60s for quota, 30s for rate-limit)
            err_text = str(last_err).lower()
            if 'quota' in err_text:
                OPENAI_COOLDOWN_UNTIL = datetime.utcnow().timestamp() + 60
            elif 'rate limit' in err_text:
                OPENAI_COOLDOWN_UNTIL = datetime.utcnow().timestamp() + 30

            quick = parse_quick_move_details(user_message)
            if quick:
                try:
                    est = generate_estimate_logic(quick['rooms'], quick['pickup'], quick['drop'], quick.get('stairs') or False, None)
                    if est['move_category'] == 'long-distance':
                        msg = (
                            f"The distance between your addresses is about {est['pickup_drop_miles']:.1f} miles, which is a long-distance move (>50 miles). "
                            "For accurate pricing, our manager will contact you. Please share your name, phone, and email."
                        )
                    else:
                        msg = (
                            f"Distance is about {est['pickup_drop_miles']:.1f} miles. For a {quick['rooms']}-bedroom move, "
                            f"we'd assign {est['crew_size']}. Hourly rate: ${est['hourly_rate']}/hr (+30 min travel). "
                            "Would you like to proceed with booking? Please share your preferred move date and time, then your name, phone, and email."
                        )
                    # Append assistant reply and also try auto-submit if user already provided details earlier
                    conversations[session_id].append({"role": "assistant", "content": msg})
                    try:
                        booking_info = extract_booking_from_conversation(conversations[session_id])
                        if booking_info.get('ready_to_submit'):
                            # Check availability
                            move_date = booking_info.get('move_date')
                            if move_date and move_date != 'TBD':
                                jobs_on_date = count_jobs_on_date(move_date)
                                if jobs_on_date >= DAILY_CAPACITY:
                                    alternates = suggest_alternate_dates(move_date, max_suggestions=3)
                                    alt_str = ', '.join(alternates) if alternates else 'please contact us'
                                    msg += f"\n\nNote: {move_date} is fully booked. Available dates: {alt_str}."
                                    conversations[session_id].append({"role": "assistant", "content": msg})
                                    return jsonify({'response': msg, 'session_id': session_id, 'degraded': True, 'availability_check': 'full'}), 200
                            booking_info = enrich_booking_data(booking_info)
                            if save_to_google_sheet(booking_info):
                                send_booking_email_to_management(booking_info)
                                send_confirmation_email_to_customer(booking_info)
                    except Exception as be:
                        logger.error(f"Fallback auto-submit error: {be}")
                    return jsonify({'response': msg, 'session_id': session_id, 'degraded': True}), 200
                except Exception as fe:
                    logger.error(f"Fallback estimate failed: {fe}")

            # If we couldn't parse last message, use full conversation to guide next step deterministically
            partial = extract_booking_from_conversation(conversations[session_id])
            needed = []
            for f in ['pickup_address','drop_address','home_size','stairs_elevator','move_date','time_preference','name','phone','email']:
                if f not in partial or not partial.get(f):
                    needed.append(f)

            next_msg = None
            # Prioritize what to ask next
            if any(k not in partial for k in ['pickup_address','drop_address']):
                next_msg = "Please share your pickup full address and drop-off full address (including city and ZIP)."
            elif 'home_size' not in partial:
                next_msg = "How many bedrooms are you moving?"
            elif 'stairs_elevator' not in partial or partial.get('stairs_elevator') in (None, 'None specified'):
                next_msg = "Any stairs or elevator at either location?"
            elif 'move_date' not in partial or 'time_preference' not in partial:
                next_msg = "What date and preferred time would you like to move?"
            elif any(k not in partial for k in ['name','phone','email']):
                next_msg = "To finalize your booking, please share your name, phone number, and email."

            # If we already have addresses and bedrooms, provide estimate deterministically
            try:
                est_msg = ""
                # derive rooms int if present
                rooms_val = None
                hs = str(partial.get('home_size',''))
                import re as _re
                mr = _re.search(r"(\d+)", hs)
                if mr and partial.get('pickup_address') and partial.get('drop_address'):
                    rooms_val = int(mr.group(1))
                    stairs_text = str(partial.get('stairs_elevator','')).lower()
                    no_stairs = any(s in stairs_text for s in ['no stair','no stairs','without stair','without stairs'])
                    has_stairs = ('stair' in stairs_text) or ('stairs' in stairs_text) or ('elevator' in stairs_text)
                    stairs_flag = False if no_stairs else (True if has_stairs else False)
                    est = generate_estimate_logic(rooms_val, partial['pickup_address'], partial['drop_address'], stairs_flag, partial.get('move_date'))
                    est_msg = (
                        f"Distance is about {est['pickup_drop_miles']:.1f} miles. Crew: {est['crew_size']}. "
                        f"Hourly rate: ${est['hourly_rate']}/hr (+30 min travel). "
                    )
            except Exception as ee:
                logger.warning(f"Deterministic estimate in fallback failed: {ee}")

            fallback = (est_msg + (next_msg or "I'm here to help you book your move."))
            conversations[session_id].append({"role": "assistant", "content": fallback})
            return jsonify({'response': fallback, 'session_id': session_id, 'degraded': True}), 200

        # Add assistant response to conversation
        conversations[session_id].append({"role": "assistant", "content": assistant_message})

        # Stateful call request handling
        try:
            meta = conversation_meta.setdefault(session_id, {'call_requested': False, 'call_notified': False, 'call_time': None})
            latest = user_message
            # Detect initial intent
            if detect_call_intent(latest):
                meta['call_requested'] = True
                # Parse any timing in the same utterance
                meta['call_time'] = parse_call_timing(latest)
                logger.info(f"Session {session_id}: Call intent detected; time parsed: {meta['call_time']}")
                if not meta['call_notified']:
                    # Ask for missing contact if we don't have phone yet
                    assistant_message = "Sure, I can arrange a manager call. Please provide your name and phone number (and email if you prefer)."
                    conversations[session_id].append({"role": "assistant", "content": assistant_message})
            else:
                # If call already requested but not yet notified, try to harvest contact details from conversation
                if meta['call_requested'] and not meta['call_notified']:
                    extracted = extract_booking_from_conversation(conversations[session_id])
                    # Fallback regex if phone still missing
                    if not extracted.get('phone'):
                        try:
                            rex = extract_booking_from_conversation_regex(conversations[session_id])
                            # merge minimal fields
                            for k in ['name','phone','email']:
                                if rex.get(k) and not extracted.get(k):
                                    extracted[k] = rex[k]
                        except Exception:
                            pass
                    name = extracted.get('name')
                    phone = extracted.get('phone')
                    email = extracted.get('email')
                    # update timing if user provided time in later message
                    if not meta['call_time'] or meta['call_time'] == 'immediate':
                        maybe_time = parse_call_timing(latest)
                        if maybe_time and maybe_time != 'immediate':
                            meta['call_time'] = maybe_time
                    timing = meta['call_time'] or 'immediate'
                    if phone:
                        ok = send_call_request_email(name, phone, timing, extra="Requested via chat")
                        meta['call_notified'] = True
                        if ok:
                            assistant_message = f"Got it. Our manager will call you {('at ' + timing) if timing not in ('immediate', None) else 'shortly'}."
                        else:
                            assistant_message = "I tried to notify our manager but ran into an issue. Please call (281) 743-4503 or resend your details."
                        conversations[session_id].append({"role": "assistant", "content": assistant_message})
                    else:
                        # Prompt again if still missing phone
                        assistant_message = "To arrange the call I still need your phone number (and name if not given)."
                        conversations[session_id].append({"role": "assistant", "content": assistant_message})
        except Exception as ce:
            logger.error(f"Stateful call handling error: {ce}")

        # Auto-detect and submit booking if conversation contains all required info
        try:
            booking_info = extract_booking_from_conversation(conversations[session_id])
            if booking_info.get('ready_to_submit'):
                logger.info(f"Session {session_id}: Booking extracted; checking availability")
                
                # Check availability for the requested date
                move_date = booking_info.get('move_date')
                if move_date and move_date != 'TBD':
                    jobs_on_date = count_jobs_on_date(move_date)
                    logger.info(f"Date {move_date} has {jobs_on_date} existing bookings (capacity: {DAILY_CAPACITY})")
                    
                    if jobs_on_date >= DAILY_CAPACITY:
                        # Date is full - suggest alternates
                        alternates = suggest_alternate_dates(move_date, max_suggestions=3)
                        alt_str = ', '.join(alternates) if alternates else 'please contact us'
                        unavailable_msg = (
                            f"Unfortunately, {move_date} is fully booked (we're at capacity with {DAILY_CAPACITY} moves that day). "
                            f"Available dates nearby: {alt_str}. Which date works better for you?"
                        )
                        conversations[session_id].append({"role": "assistant", "content": unavailable_msg})
                        logger.info(f"Session {session_id}: Date unavailable, suggested alternates")
                        return jsonify({'response': unavailable_msg, 'session_id': session_id, 'availability_check': 'full'}), 200
                
                # Date is available or no date check needed - proceed with booking
                logger.info(f"Session {session_id}: Date available; enriching and saving")
                booking_info = enrich_booking_data(booking_info)
                if save_to_google_sheet(booking_info):
                    send_booking_email_to_management(booking_info)
                    # Customer email gated; will skip unless SEND_CUSTOMER_EMAIL=True
                    send_confirmation_email_to_customer(booking_info)
                    logger.info(f"Session {session_id}: Booking submitted successfully (manager notified)")
                else:
                    logger.error("Booking save failed; manager email skipped")
            else:
                # If not fully ready, still handle long-distance lead: if we have contact + addresses and distance > 50, email manager
                if booking_info.get('ready_for_long_distance') and not booking_info.get('long_distance_notified'):  # prevent duplicate emails
                    miles = booking_info.get('distance_miles')
                    logger.info(f"Session {session_id}: Long-distance lead detected (~{miles} miles); notifying manager")
                    try:
                        booking_info = enrich_booking_data(booking_info)
                    except Exception as ee:
                        logger.warning(f"Enrich long-distance lead failed: {ee}")
                    send_booking_email_to_management(booking_info)
                    booking_info['long_distance_notified'] = True
                    confirm_msg = (
                        "Thank you! This is a long-distance move (over 50 miles). "
                        "Our manager will contact you shortly to provide a detailed quote."
                    )
                    conversations[session_id].append({"role": "assistant", "content": confirm_msg})
                    return jsonify({'response': confirm_msg, 'session_id': session_id, 'manager_notified': True}), 200
        except Exception as be:
            logger.error(f"Error auto-submitting booking: {be}")

        return jsonify({'response': assistant_message, 'session_id': session_id})

    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/calculate-distance', methods=['POST'])
def calculate_distance_endpoint():
    """Calculate distance between two addresses"""
    try:
        data = request.json
        origin = data.get('origin')
        destination = data.get('destination')
        
        if not origin or not destination:
            return jsonify({'error': 'Origin and destination required'}), 400
        
        distance = calculate_distance(origin, destination)
        
        if distance is None:
            return jsonify({'error': 'Could not calculate distance'}), 400
        
        move_type = 'local' if distance < 50 else 'long-distance'
        
        return jsonify({
            'distance_miles': distance,
            'move_type': move_type
        })
    
    except Exception as e:
        logger.error(f"Error calculating distance: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# UPDATED FIX - Replace your speech_chat() function with this version
# This accepts BOTH 'file' and 'audio' as the form field name

@app.route('/speech-chat', methods=['POST'])
def speech_chat():
    """Accept an audio file upload and return transcript + assistant reply.
    
    Supports two input formats:
    1. Form-data with file upload:
       - file: audio file (wav/mp3/webm/ogg/m4a)  OR
       - audio: audio file (alternative field name)
       - session_id: optional conversation session id
    
    2. JSON with base64 audio:
       - audio: base64-encoded audio data
       - mime_type: audio MIME type
       - session_id: optional conversation session id
    
    Returns JSON: { transcript, response, session_id }
    """
    try:
        session_id = None
        audio_file = None
        mime_type = None
        tmp_path = None
        
        # Check if this is a file upload (form-data)
        # Accept BOTH 'file' and 'audio' as field names
        if 'file' in request.files or 'audio' in request.files:
            logger.info("Processing file upload")
            
            # Try 'file' first, then 'audio'
            audio_file = request.files.get('file') or request.files.get('audio')
            session_id = request.form.get('session_id', 'default')
            mime_type = audio_file.mimetype or audio_file.content_type
            
            if not mime_type:
                # Try to detect from filename
                filename = audio_file.filename.lower()
                if filename.endswith('.wav'):
                    mime_type = 'audio/wav'
                elif filename.endswith('.mp3'):
                    mime_type = 'audio/mpeg'
                elif filename.endswith('.webm'):
                    mime_type = 'audio/webm'
                elif filename.endswith('.ogg'):
                    mime_type = 'audio/ogg'
                elif filename.endswith('.m4a'):
                    mime_type = 'audio/x-m4a'
                else:
                    logger.error(f"Cannot determine MIME type for file: {filename}")
                    return jsonify({'error': 'Cannot determine audio format'}), 400
            
            logger.info(f"File upload - MIME type: {mime_type}, filename: {audio_file.filename}")
            
            if mime_type not in ALLOWED_AUDIO_MIME:
                logger.error(f"Unsupported MIME type: {mime_type}")
                return jsonify({'error': f'Unsupported audio type: {mime_type}. Allowed types: {list(ALLOWED_AUDIO_MIME)}'}), 400
            
            # Save temp file
            tmp_dir = os.getenv('TMP_DIR', os.getcwd())
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(tmp_dir, f"upload_{int(time.time()*1000)}_{secure_filename(audio_file.filename)}")
            audio_file.save(tmp_path)
            logger.info(f"Audio saved to: {tmp_path}")
        
        # Check if this is JSON with base64 audio
        elif request.is_json:
            logger.info("Processing JSON with base64 audio")
            data = request.json
            audio_b64 = data.get('audio')
            mime_type = data.get('mime_type') or data.get('mimeType')
            session_id = data.get('session_id', 'default')
            
            if not audio_b64:
                logger.error("No audio data in JSON request")
                return jsonify({'error': 'No audio data provided in JSON'}), 400
            
            if not mime_type:
                logger.error("No MIME type specified in JSON request")
                return jsonify({'error': 'mime_type required in JSON request'}), 400
            
            logger.info(f"JSON upload - MIME type: {mime_type}")
            
            if mime_type not in ALLOWED_AUDIO_MIME:
                logger.error(f"Unsupported MIME type: {mime_type}")
                return jsonify({'error': f'Unsupported audio type: {mime_type}. Allowed types: {list(ALLOWED_AUDIO_MIME)}'}), 400
            
            # Decode base64 and save to temp file
            try:
                audio_bytes = base64.b64decode(audio_b64)
                tmp_dir = os.getenv('TMP_DIR', os.getcwd())
                os.makedirs(tmp_dir, exist_ok=True)
                
                # Determine extension from MIME type
                ext_map = {
                    'audio/webm': '.webm',
                    'audio/wav': '.wav',
                    'audio/x-wav': '.wav',
                    'audio/ogg': '.ogg',
                    'audio/mpeg': '.mp3',
                    'audio/x-m4a': '.m4a',
                    'audio/mp4': '.mp4',
                }
                ext = ext_map.get(mime_type, '.webm')
                tmp_path = os.path.join(tmp_dir, f"upload_{int(time.time()*1000)}{ext}")
                
                with open(tmp_path, 'wb') as f:
                    f.write(audio_bytes)
                logger.info(f"Base64 audio decoded and saved to: {tmp_path}")
            except Exception as e:
                logger.error(f"Failed to decode base64 audio: {e}")
                return jsonify({'error': f'Failed to decode audio data: {str(e)}'}), 400
        
        else:
            logger.error("No audio file or JSON data found in request")
            logger.error(f"Content-Type: {request.content_type}")
            logger.error(f"Form keys: {list(request.form.keys())}")
            logger.error(f"Files keys: {list(request.files.keys())}")
            return jsonify({
                'error': 'No audio file provided. Send form-data with "file" or "audio" field, or JSON with "audio" (base64) field',
                'received_content_type': request.content_type,
                'form_keys': list(request.form.keys()),
                'files_keys': list(request.files.keys())
            }), 400
        
        # Transcribe audio
        try:
            logger.info(f"Transcribing audio file: {tmp_path}")
            transcript = transcribe_audio_file(tmp_path, mime_type)
            logger.info(f"Transcription successful: {transcript[:100]}...")
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return jsonify({'error': f'Transcription failed: {str(e)}'}), 500
        finally:
            # Clean up temp file
            if tmp_path:
                try:
                    os.remove(tmp_path)
                    logger.info(f"Temp file removed: {tmp_path}")
                except Exception as e:
                    logger.warning(f"Failed to remove temp file: {e}")
        
        # Generate reply using transcript as user message
        logger.info(f"Generating reply for session: {session_id}")
        reply = generate_assistant_reply(session_id, transcript)
        logger.info(f"Reply generated successfully")
        
        return jsonify({
            'transcript': transcript,
            'response': reply,
            'session_id': session_id
        })
    
    except Exception as e:
        logger.error(f"Speech chat error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Speech processing failed: {str(e)}'}), 500


@app.route('/chat/speech', methods=['POST'])
def speech_chat_alias():
    """Alias endpoint for legacy/front-end expectation (/chat/speech -> /speech-chat)."""
    return speech_chat()

@app.route('/generate-estimate', methods=['POST'])
def generate_estimate_endpoint():
    """Generate an estimate using pricing tiers, mileage, and optional peak surcharge.
    Expected JSON: {
      rooms: int,
      pickup_address: str,
      drop_address: str,
      stairs_elevator: bool,
      move_date: 'YYYY-MM-DD' (optional)
    }
    """
    try:
        data = request.json or {}
        rooms = int(data.get('rooms')) if data.get('rooms') is not None else None
        pickup_address = data.get('pickup_address')
        drop_address = data.get('drop_address')
        stairs_elevator = bool(data.get('stairs_elevator', False))
        move_date = data.get('move_date')

        if not rooms or not pickup_address or not drop_address:
            return jsonify({'error': 'rooms, pickup_address, and drop_address are required'}), 400

        estimate = generate_estimate_logic(rooms, pickup_address, drop_address, stairs_elevator, move_date)
        # Provide a simplified response focused on hourly rate, travel time, and crew recommendation.
        slim = {
            'rooms': estimate['rooms'],
            'crew_size': estimate['crew_size'],
            'hourly_rate': estimate['hourly_rate'],
            'travel_time_minutes': estimate['travel_time_minutes'],
            'minimum_hours': estimate['minimum_hours'],
            'pickup_drop_miles': estimate['pickup_drop_miles'],
            'move_category': estimate['move_category'],
            'notes': estimate['notes'],
        }
        return jsonify({'success': True, 'estimate': slim})
    except ValueError as ve:
        logger.error(f"Estimate error: {ve}")
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        logger.error(f"Error generating estimate: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/submit-booking', methods=['POST'])
def submit_booking():
    """Submit a booking request"""
    try:
        booking_data = request.json or {}
        
        # Validate required fields
        required_fields = ['name', 'email', 'phone', 'pickup_address', 'drop_address']
        for field in required_fields:
            if not booking_data.get(field):
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Optional: auto-generate estimate if not provided and required fields are present
        if not booking_data.get('estimated_cost') and booking_data.get('home_size') and booking_data.get('pickup_address') and booking_data.get('drop_address'):
            # Attempt to parse rooms from home_size (e.g., "2 bedroom apartment" -> 2)
            rooms = None
            try:
                for token in str(booking_data.get('home_size')).split():
                    if token.isdigit():
                        rooms = int(token)
                        break
            except Exception:
                rooms = None
            stairs_flag = 'stairs' in str(booking_data.get('stairs_elevator', '')).lower() or 'elevator' in str(booking_data.get('stairs_elevator', '')).lower()
            move_date = booking_data.get('move_date')
            if rooms:
                est = generate_estimate_logic(rooms, booking_data.get('pickup_address'), booking_data.get('drop_address'), stairs_flag, move_date)
                # Store hourly rate description instead of a final total
                booking_data['estimated_cost'] = f"${est['hourly_rate']}/hr (+30 min travel, 3-hr minimum)"
                booking_data['move_type'] = est['move_category']
                booking_data['distance_miles'] = est['pickup_drop_miles']
                booking_data['crew_size'] = est['crew_size']

        # Availability check
        move_date = booking_data.get('move_date')
        if move_date:
            jobs_on_day = count_jobs_on_date(move_date)
            if jobs_on_day >= DAILY_CAPACITY:
                suggestions = suggest_alternate_dates(move_date)
                return jsonify({
                    'success': False,
                    'message': 'Requested date is fully booked',
                    'suggested_dates': suggestions
                }), 200

        # Save to Google Sheet
        save_to_google_sheet(booking_data)

        # Send emails
        send_booking_email_to_management(booking_data)
        send_confirmation_email_to_customer(booking_data)

        return jsonify({'success': True, 'message': 'Booking submitted successfully'})
    
    except Exception as e:
        logger.error(f"Error submitting booking: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/request-call', methods=['POST'])
def request_call():
    """Handle call request from customer"""
    try:
        data = request.json
        customer_name = data.get('name', 'Unknown')
        customer_phone = data.get('phone')
        timing = data.get('timing', 'immediate')  # immediate or later
        
        if not customer_phone:
            return jsonify({'error': 'Phone number required'}), 400
        
        # Send email to management
        subject = f"Call Request - {customer_name}"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>New Call Request</h2>
            <p><strong>Customer Name:</strong> {customer_name}</p>
            <p><strong>Phone:</strong> {customer_phone}</p>
            <p><strong>Timing:</strong> {timing}</p>
            <p><strong>Time of Request:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </body>
        </html>
        """
        
        send_email(os.getenv('MANAGER_EMAIL'), subject, body)
        
        return jsonify({
            'success': True,
            'message': 'Call request submitted'
        })
    
    except Exception as e:
        logger.error(f"Error requesting call: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/twilio/voice', methods=['POST'])
def twilio_voice():
    """Handle incoming Twilio voice calls"""
    try:
        response = VoiceResponse()
        response.say(
            "Thank you for calling U S F Moving Company. "
            "A representative will be with you shortly.",
            voice='Polly.Joanna'
        )
        response.dial(os.getenv('COMPANY_PHONE'))
        
        return str(response)
    
    except Exception as e:
        logger.error(f"Error in Twilio voice handler: {e}")
        return str(VoiceResponse())

@app.route('/reset-conversation', methods=['POST'])
def reset_conversation():
    """Reset conversation history for a session"""
    try:
        data = request.json
        session_id = data.get('session_id', 'default')
        
        if session_id in conversations:
            del conversations[session_id]
        
        return jsonify({
            'success': True,
            'message': 'Conversation reset'
        })
    
    except Exception as e:
        logger.error(f"Error resetting conversation: {e}")
        return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug_flag = os.getenv('FLASK_DEBUG', 'False') == 'True'
    # Prefer SocketIO server if available for realtime features
    if socketio:
        try:
            socketio.run(app, host='0.0.0.0', port=port, debug=debug_flag, allow_unsafe_werkzeug=True)
        except Exception as e:
            logger.error(f"SocketIO run failed, falling back to Flask: {e}")
            app.run(host='0.0.0.0', port=port, debug=debug_flag)
    else:
        app.run(host='0.0.0.0', port=port, debug=debug_flag)
