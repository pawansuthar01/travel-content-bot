

import os
import logging
import nest_asyncio
import asyncio
import httpx
import json
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand
)
from telegram.helpers import escape_markdown
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)

from pymongo import MongoClient
from bson import ObjectId

# ----------------- ENV & INIT -----------------
nest_asyncio.apply()
load_dotenv()

# Environment variable validation
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")  # optional channel id string
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # group chat id for posting
OWNER_ID_STR = os.getenv("OWNER_ID")

# Critical environment variables validation
if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN in .env - This is required for bot operation")
if not MONGO_URI:
    raise SystemExit("Missing MONGO_URI in .env - Database connection is required")
if not OWNER_ID_STR:
    raise SystemExit("Missing OWNER_ID in .env - Bot owner must be specified")

try:
    OWNER_ID = int(OWNER_ID_STR)
except ValueError:
    raise SystemExit("Invalid OWNER_ID in .env - Must be a valid numeric user ID")

# Optional API keys with warnings
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not found - AI content generation will be limited")
if not PEXELS_API_KEY:
    logger.warning("PEXELS_API_KEY not found - Image fetching will be disabled")

# Enhanced logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),  # Console output
        logging.FileHandler("bot.log", encoding='utf-8')  # File output
    ]
)
logger = logging.getLogger("AutoPostBot")

# Set up specific loggers for external libraries to reduce noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)

# MongoDB connection with connection pooling and error handling
try:
    mongo_client = MongoClient(
        MONGO_URI,
        maxPoolSize=10,  # Connection pool size
        serverSelectionTimeoutMS=5000,  # Timeout for server selection
        connectTimeoutMS=10000,  # Connection timeout
        socketTimeoutMS=45000,  # Socket timeout
        maxIdleTimeMS=30000,  # Max idle time for connections
    )
    # Test the connection
    mongo_client.admin.command('ping')
    logger.info("MongoDB connection established successfully")

    db = mongo_client.get_default_database()  # uses DB from URI or the default DB
    destinations_col = db.get_collection("destinations")

    # Create indexes for better performance
    destinations_col.create_index([("slug", 1)], unique=True)
    destinations_col.create_index([("isPublished", 1), ("createdAt", -1)])
    destinations_col.create_index([("status", 1), ("scheduled_at", 1)])

except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    raise SystemExit("Database connection failed - check MONGO_URI and network connectivity")

# In-memory session store keyed by telegram user id
SESSIONS: Dict[int, Dict[str, Any]] = {}

# Authorized users list (owner + added users)
AUTHORIZED_USERS: set = {OWNER_ID} if OWNER_ID else set()

# Rate limiting storage
USER_REQUESTS: Dict[int, list] = {}  # user_id -> list of timestamps
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 10  # max requests per window

# Telegram limits / helpers
TG_MSG_LIMIT = 4000  # safe margin below 4096
MAX_GALLERY = 10


# ----------------- UTIL -----------------
def iso_now():
    return datetime.utcnow().isoformat() + "Z"


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")


def validate_markdownv2(text: str) -> str:
    """Validate and escape MarkdownV2 reserved characters for Telegram."""
    # Reserved characters: -, _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !
    # Escape them except in code blocks or links
    import re
    # Simple validation: check for unescaped specials
    specials = r'[\-\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!]'
    # For now, just log warnings if unescaped specials are found
    unescaped = re.findall(r'(?<!\\)' + specials, text)
    if unescaped:
        logger.warning(f"Unescaped MarkdownV2 characters found: {set(unescaped)} in text: {text[:100]}...")
    # Return escaped text
    return re.sub(r'([\-_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!])', r'\\\1', text)


def check_content_quality(text: str, keywords: list = None) -> bool:
    """Basic quality check: ensure keywords and no obvious errors."""
    if keywords:
        for kw in keywords:
            if kw.lower() not in text.lower():
                logger.warning(f"Keyword '{kw}' missing from content.")
                return False
    # Check for common errors
    if "shortDescription:" in text:
        logger.warning("Malformed description label found.")
        return False
    return True


def check_rate_limit(user_id: int) -> bool:
    """Check if user has exceeded rate limit. Returns True if allowed, False if blocked."""
    now = datetime.utcnow().timestamp()
    user_requests = USER_REQUESTS.get(user_id, [])

    # Remove old requests outside the window
    user_requests = [req_time for req_time in user_requests if now - req_time < RATE_LIMIT_WINDOW]

    # Check if under limit
    if len(user_requests) >= RATE_LIMIT_MAX_REQUESTS:
        return False

    # Add current request
    user_requests.append(now)
    USER_REQUESTS[user_id] = user_requests
    return True


def sanitize_input(text: str, max_length: int = 1000) -> str:
    """Sanitize user input to prevent injection attacks."""
    if not text:
        return ""

    # Remove potentially dangerous characters
    text = re.sub(r'[<>]', '', text)  # Remove angle brackets
    text = re.sub(r'javascript:', '', text, flags=re.IGNORECASE)  # Remove JS injection
    text = re.sub(r'on\w+\s*=', '', text, flags=re.IGNORECASE)  # Remove event handlers

    # Limit length
    return text[:max_length].strip()


async def send_long_message(bot, chat_id: int, text: str, parse_mode: str = "MarkdownV2", max_retries: int = 3):
    """Send long text in chunks (MarkdownV2 escaped) with retries and backoff."""
    if not text:
        return
    esc = escape_markdown(text, version=2)
    chunks = []
    while len(esc) > TG_MSG_LIMIT:
        split_at = esc.rfind(". ", 0, TG_MSG_LIMIT)
        if split_at == -1:
            split_at = TG_MSG_LIMIT
        part = esc[:split_at + 1]
        chunks.append(part)
        esc = esc[split_at + 1:].strip()
    if esc:
        chunks.append(esc)

    for i, chunk in enumerate(chunks):
        for attempt in range(max_retries):
            start_time = asyncio.get_event_loop().time()
            try:
                logger.info(f"Sending message chunk {i+1}/{len(chunks)} to {chat_id}, attempt {attempt+1}")
                await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
                end_time = asyncio.get_event_loop().time()
                logger.info(f"Message chunk {i+1} sent successfully in {end_time - start_time:.2f}s")
                break  # Success, exit retry loop
            except Exception as e:
                end_time = asyncio.get_event_loop().time()
                logger.warning(f"Message chunk {i+1} failed on attempt {attempt+1}: {e} (took {end_time - start_time:.2f}s)")
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    logger.info(f"Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)
                else:
                    logger.error(f"Failed to send message chunk {i+1} after {max_retries} attempts")
                    raise  # Re-raise after retries exhausted


# ----------------- GEMINI (Generative) -----------------
async def call_gemini(prompt: str, model: str = "gemini-2.5-flash", max_retries: int = 3, timeout: int = 60) -> Optional[str]:
    """Call Gemini API to generate text. Returns text or None."""
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set.")
        return None
    url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                
            if resp.status_code == 200:
                j = resp.json()
                try:
                    return j["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    logger.exception("Unexpected Gemini shape: %s", j)
                    return None
            else:
                logger.warning("Gemini error %s: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.warning("Gemini attempt %s failed: %s", attempt + 1, e)
        await asyncio.sleep(2 ** attempt)
    return None


# Structured prompts for topics
def prompt_list_destinations(existing_names: list = None) -> str:
    if existing_names:
        existing_str = ", ".join(existing_names)
        return (
            f"Provide 10 diverse, popular travel destination city names worldwide that are NOT in this list: {existing_str}.\n"
            "Return only comma-separated city names (for example: Paris, Tokyo, New York, Bali). "
            "Do not include countries or extra text or numbering. Make sure none of the names are duplicates of the existing ones."
        )
    else:
        return (
            "Provide 10 diverse, popular travel destination city names worldwide.\n"
            "Return only comma-separated city names (for example: Paris, Tokyo, New York, Bali). "
            "Do not include countries or extra text or numbering."
        )


def prompt_structured_for_destination(dest: str) -> str:
    # Return a JSON object strictly — we'll try to parse it.
    return (
        f"You are a travel content generator. Produce ONE strict JSON object (no extra text) for the destination \"{dest}\".\n"
        "Fields: description (<=200 chars), longDescription (3 paragraphs as a single string), "
        "category (one word), bestTimeToVisit (string), weatherInfo (object with averageTemp & peakSeason), "
        "tags (array of 5-10 strings), popularFor (array of 4-8 strings), travelTips (array of 5 strings), "
        "itinerary (array of 3 strings), featured (true/false), location (object with country and region)."
    )


def prompt_topic_regenerate(dest: str, topic: str) -> str:
    return f"Regenerate only the {topic} for {dest} as per the original structured schema. Return plain text."


# Attempt to extract JSON object from possibly noisy Gemini output
def try_parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # find first opening brace and try to parse balanced block
    start = text.find("{")
    if start == -1:
        return None
    stack = []
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            stack.append("{")
        elif ch == "}":
            if stack:
                stack.pop()
            else:
                continue
        if not stack:
            candidate = text[start:i + 1]
            try:
                return json.loads(candidate)
            except Exception:
                # try next possible end by continuing the loop
                continue
    return None


# ----------------- PEXELS (Images) -----------------
async def fetch_pexels_images(destination: str, per_page: int = 12, exclude: Optional[List[str]] = None) -> (Optional[str], List[str]):
    """Return (thumbnail_url, [image_urls]) or (None, [])"""
    if not PEXELS_API_KEY:
        logger.warning("PEXELS_API_KEY not set.")
        return None, []
    if exclude is None:
        exclude = []
    query = destination.split(",")[0].strip()
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": per_page}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                logger.warning("Pexels returned %s: %s", resp.status_code, resp.text[:200])
                return None, []
            photos = resp.json().get("photos", [])
            filtered = []
            seen = set(exclude)
            for p in photos:
                medium = p.get("src", {}).get("medium")
                large = p.get("src", {}).get("large")
                if not medium or not large:
                    continue
                if medium in seen or large in seen:
                    continue
                filtered.append(p)
                seen.add(medium); seen.add(large)
                if len(filtered) >= MAX_GALLERY + 1:  # extra for thumbnail + gallery
                    break
            if not filtered:
                return None, []
            thumbnail = filtered[0]["src"]["medium"]
            images = [p["src"]["large"] for p in filtered[1:MAX_GALLERY + 1]]
            return thumbnail, images
    except Exception as e:
        logger.exception("fetch_pexels_images failed: %s", e)
        return None, []


# ----------------- SESSION TOPICS ORDER -----------------
TOPICS_ORDER = [
    ("description", "Short Description"),
    ("longDescription", "Long Description"),
    ("bestTimeToVisit", "Best Time To Visit"),
    ("tags", "Tags"),
    ("popularFor", "Popular For"),
    ("travelTips", "Travel Tips"),
    ("itinerary", "3-Day Itinerary"),
    ("weatherInfo", "Weather Info"),
    ("location", "Location"),
    ("SuggestedDuration", "Suggested Duration")
]


# ----------------- COMMANDS & HANDLERS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    logger.info("start requested by %s", uid)

    # Rate limiting check
    if not check_rate_limit(uid):
        await update.message.reply_text("❌ Too many requests. Please wait before trying again.")
        return

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    # Send waiting message
    await update.message.reply_text("⏳ Please wait while I generate destination suggestions...")

    # Get existing destination names from database to avoid duplicates
    try:
        existing_docs = destinations_col.find({}, {"name": 1})
        existing_names = [doc.get("name") for doc in existing_docs if doc.get("name")]
    except Exception as e:
        logger.warning(f"Failed to fetch existing destinations: {e}")
        existing_names = []
    # ask Gemini for 10 destinations, excluding existing ones
    dest_text = await call_gemini(prompt_list_destinations(existing_names))

    if dest_text:
        # split on comma / newline defensively
        candidates = [d.strip() for d in re.split(r"[,\n]+", dest_text) if d.strip()]
        # dedupe and limit to 10
        seen = set(); dests = []
        for d in candidates:
            if d.lower() not in seen:
                dests.append(d)
                seen.add(d.lower())
            if len(dests) >= 10:
                break
        if not dests:
          await update.message.reply_text(
            "something wont wrong try again /start" )
    else:
        # Gemini API call failed
        await update.message.reply_text(
            "❌ Unable to generate destinations at the moment. This could be due to network issues, API quota exceeded, or invalid response from the service.\n\n"
            "Please try again later or contact support if the issue persists. "
        )
        return

    # Validate and filter destinations
    dests = [d for d in dests if isinstance(d, str) and d.strip()]
    if not dests:
        dests = ["Paris", "Tokyo", "New York", "Bali", "London", "Rome", "Sydney", "Dubai", "Barcelona", "Amsterdam"]

    # initialize session
    SESSIONS[uid] = {
        "stage": "choose_destination",
        "destinations": dests,
        "createdAt": iso_now()
    }

    buttons = [[InlineKeyboardButton(d, callback_data=f"dest_{i}")] for i, d in enumerate(dests)]
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("🌍 Choose a destination from the suggestions below, or type a custom destination name:", reply_markup=markup)


async def destination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    session = SESSIONS.get(uid)
    if not session:
        await q.message.reply_text("Session expired. Use /start.")
        return
    try:
        idx = int(q.data.split("_", 1)[1])
        dest_name = session["destinations"][idx]
    except Exception:
        await q.message.reply_text("Invalid selection. Try /start.")
        return

    # set up session fields
    session.update({
        "destination": dest_name,
        "createdBy": str(uid),
        "slug": slugify(dest_name),
        "topic_index": 0,
        "topics_data": {},  # will hold each topic content
        "images_confirmed": False,
        "thumbnail_confirmed": False,
        "info_confirmed_map": {},  # topic_key -> bool
    })

    # 1) Get structured object from Gemini
    await q.edit_message_text(f"⏳ Generating structured info and thumbnail for *{dest_name}* ...")
    struct_text = await call_gemini(prompt_structured_for_destination(dest_name))
    structured = try_parse_json_block(struct_text) if struct_text else None

    # fallback to basic generation of limited fields
    if not structured:
        short = await call_gemini(f"Write a one-paragraph short description for {dest_name}.")
        long = await call_gemini(f"Write a 3-paragraph travel overview for {dest_name}.")
        structured = {
            "description": short or f"{dest_name} - brief description.",
            "longDescription": long or f"{dest_name} - long description.",
            "slug": slugify(dest_name),
            "category": "City",
            "bestTimeToVisit": "See seasons",
            "weatherInfo": {"averageTemp": None, "peakSeason": None},
            "tags": [],
            "popularFor": [],
            "travelTips": [],
            "itinerary": []
        }

    # Ensure basic fields exist
    structured.setdefault("slug", slugify(dest_name))

    session["structured"] = structured

    # 2) Fetch thumbnail from Pexels and send only thumbnail first
    thumb, imgs = await fetch_pexels_images(dest_name)
    session["thumbnail"] = thumb
    session["images"] = imgs

    # send thumbnail with confirm / new thumbnail
    if thumb:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Keep Thumbnail", callback_data="confirm_thumbnail"),
             InlineKeyboardButton("🔄 New Thumbnail", callback_data="regen_thumbnail")]
        ])
        try:
            await context.bot.send_photo(chat_id=uid, photo=thumb,
                                         caption=f"📍 {dest_name} — Thumbnail\nSlug: {structured.get('slug')}",
                                         reply_markup=markup)
        except Exception as e:
            logger.warning(f"Failed to send thumbnail photo: {e}")
            # fallback: send url message with buttons
            await context.bot.send_message(chat_id=uid, text=f"Thumbnail: {thumb}\n\nChoose action:", reply_markup=markup)
    else:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 New Thumbnail", callback_data="regen_thumbnail")]
        ])
        await context.bot.send_message(chat_id=uid, text="No thumbnail found. Try generating a new one.", reply_markup=markup)

    # don't send gallery or content yet — they come after thumbnail confirmation


# ----------------- THUMBNAIL HANDLERS -----------------
async def confirm_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    s = SESSIONS.get(uid)
    if not s:
        await q.message.reply_text("Session expired.")
        return
    s["thumbnail_confirmed"] = True
    await q.edit_message_caption(caption="✅ Thumbnail confirmed", reply_markup=None)
    # proceed to fetch and send gallery images
    await send_gallery_for_confirmation(uid, context)


async def regen_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    s = SESSIONS.get(uid)
    if not s or not s.get("destination"):
        await q.message.reply_text("Session expired.")
        return
    dest = s["destination"]
    # Check if the message has text to edit
    try:
        await q.edit_message_text("🔄 Regenerating thumbnail...")
    except Exception as e:
        logger.warning(f"Could not edit message text: {e}. Sending new message instead.")
        await context.bot.send_message(uid, "🔄 Regenerating thumbnail...")
    # Exclude current thumbnail to get a different one
    exclude = [s.get("thumbnail")] if s.get("thumbnail") else []
    thumb, _ = await fetch_pexels_images(dest, per_page=30, exclude=exclude)
    if not thumb:
        await context.bot.send_message(uid, "❌ Could not get a new thumbnail. Try again later.")
        return
    s["thumbnail"] = thumb
    s.pop("thumbnail_confirmed", None)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Keep Thumbnail", callback_data="confirm_thumbnail"),
         InlineKeyboardButton("🔄 New Thumbnail", callback_data="regen_thumbnail")]
    ])
    try:
        await context.bot.send_photo(uid, thumb, caption=f"📍 {dest} — New Thumbnail", reply_markup=markup)
    except Exception:
        await context.bot.send_message(uid, f"Thumbnail: {thumb}")


# ----------------- GALLERY IMAGE HANDLERS -----------------
async def send_gallery_for_confirmation(uid: int, context: ContextTypes.DEFAULT_TYPE):
    s = SESSIONS.get(uid)
    if not s:
        return
    dest = s["destination"]
    # fetch gallery if not present
    if not s.get("images"):
        thumb, imgs = await fetch_pexels_images(dest)
        s["thumbnail"] = s.get("thumbnail") or thumb
        s["images"] = imgs

    imgs: List[str] = s.get("images", [])
    if not imgs:
        await context.bot.send_message(uid, "No gallery images available.")
        return

    # send all images and then send confirm buttons
    for i, img in enumerate(imgs, start=1):
        try:
            await context.bot.send_photo(uid, img, caption=f"📸 {dest} — Image {i}")
        except Exception:
            await context.bot.send_message(uid, img)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Images", callback_data="confirm_images"),
         InlineKeyboardButton("🔄 New Images", callback_data="regen_images")],
        [InlineKeyboardButton("🗑️ Remove Images", callback_data="remove_images")]
    ])
    await context.bot.send_message(uid, "Review images:", reply_markup=markup)


async def confirm_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    s = SESSIONS.get(uid)
    if not s:
        await q.message.reply_text("Session expired.")
        return
    s["images_confirmed"] = True
    await q.edit_message_text("✅ Images confirmed", reply_markup=None)
    # start content topics sequence
    await send_next_topic(uid, context)


async def regen_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    s = SESSIONS.get(uid)
    if not s or not s.get("destination"):
        await q.message.reply_text("Session expired.")
        return
    dest = s["destination"]
    # Check if the message has text to edit
    try:
        await q.edit_message_text("🔄 Regenerating images...")
    except Exception as e:
        logger.warning(f"Could not edit message text: {e}. Sending new message instead.")
        await context.bot.send_message(uid, "🔄 Regenerating images...")
    exclude = [s.get("thumbnail")] + s.get("images", [])
    thumb, imgs = await fetch_pexels_images(dest, per_page=20, exclude=exclude)
    if not thumb:
        await context.bot.send_message(uid, "❌ Could not fetch new images.")
        return
    s["thumbnail"] = thumb
    s["images"] = imgs
    s["images_confirmed"] = False
    s.pop("actions_shown", None)
    # send new gallery
    await send_gallery_for_confirmation(uid, context)


async def remove_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access.. Contact @mrpawansuthar to get access.")
        return

    s = SESSIONS.get(uid)
    if not s:
        return
    s["images"] = []
    s["images_confirmed"] = False
    await q.edit_message_text("🗑️ Images removed. You can request new images.", reply_markup=None)


# ----------------- CONTENT TOPIC SEQUENCE -----------------
async def send_next_topic(uid: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Send the next topic for confirmation (one-by-one).
    """
    s = SESSIONS.get(uid)
    if not s:
        return
    idx = s.get("topic_index", 0)
    if idx >= len(TOPICS_ORDER):
        # all topics done — send final summary and actions
        await send_final_summary(uid, context)
        return

    key, label = TOPICS_ORDER[idx]
    # If topic already has data and was confirmed, move to next
    if s.get("info_confirmed_map", {}).get(key):
        s["topic_index"] = idx + 1
        await send_next_topic(uid, context)
        return

    # Generate content for this topic via Gemini
    dest = s["destination"]
    # Construct topic-specific prompt
    if key == "description":
        p = f"Write a short description (3-5 sentences) for {dest}. Return plain text only. Do not include any labels like 'Short Description:'."
    elif key == "longDescription":
        p = f"Write a detailed long description (3 paragraphs) for {dest}. Return plain text only. Do not include any labels like 'longDescription:'."
    elif key == "tags":
        p = f"Provide an array (JSON array) of 8 to 10 items for '{label}' for {dest}. Return JSON array only."
    elif key == "popularFor":
        p = f"Provide an array (JSON array) of 10 to 12 items for '{label}' for {dest}. Return JSON array only."
    elif key == "travelTips":
        p = f"Provide a JSON array of 10 to 15 concise travel tips for {dest}."
    elif key == "itinerary":
        p = f'''Provide a JSON array of 4 to 6 itinerary objects for {dest}. Each object should have 'day' (number), 'title' (string), and 'activities' (array of strings). Return ONLY the JSON array, nothing else. Format: [{{"day":1,"title":"Day Title","activities":["activity1","activity2"]}}, {{"day":2,"title":"Day Title","activities":["activity1","activity2"]}}, {{"day":3,"title":"Day Title","activities":["activity1","activity2"]}}, {{"day":4,"title":"Day Title","activities":["activity1","activity2"]}}, {{"day":5,"title":"Day Title","activities":["activity1","activity2"]}}, {{"day":6,"title":"Day Title","activities":["activity1","activity2"]}}]'''
    elif key == "weatherInfo":
        p = f"Provide a JSON object with 'avgTemp', 'climateType', and 'bestMonth' for {dest} (e.g. {{\"avgTemp\":\"20°C\",\"climateType\":\"Tropical\",\"bestMonth\":\"December-February\"}}). Return JSON only."
    elif key == "location":
        p = f"Provide location information for {dest} as a JSON object with 'country', 'region', and 'coordinates' (latitude and longitude numbers). Return JSON only."
    elif key == "SuggestedDuration":
        p = f"Provide a suggested duration for visiting {dest} as a 2-line string (e.g. '3-5 days for main attractions\n7-10 days for comprehensive experience'). Return plain text only."
    else:
        # bestTimeToVisit
        p = f"Write a detailed paragraph (max 200 words) about the best time to visit {dest}, including seasonal information, weather considerations, and why certain times are ideal. Return plain text."

    # Ask Gemini
    gen = await call_gemini(p)
    if not gen:
        await context.bot.send_message(uid, "❌ Gemini API is currently unavailable. Please try again later.")
        gen = f"Barcelona is a vibrant Spanish city known for its unique architecture, beaches, and cultural heritage. It offers stunning Gaudi-designed buildings, Mediterranean cuisine, and a lively atmosphere perfect for exploration."
    # Try to parse JSON for array/object types
    parsed = None
    if key in ("tags", "popularFor", "travelTips", "itinerary", "weatherInfo", "location"):
        parsed = try_parse_json_block(gen)
        # if not parsed and it's an array-like plain text, attempt to split lines
        if not parsed:
            # try to extract lines or commas
            # Clean up the response by removing markdown and joining split strings
            clean_gen = gen.replace("```json", "").replace("```", "").strip()
            # Try to parse as JSON first
            try:
                parsed_json = json.loads(clean_gen)
                if isinstance(parsed_json, list):
                    items = [str(item).strip('"').strip("'") for item in parsed_json if item and str(item).strip() not in ["[", "]", "```json", "```"]]
                else:
                    items = []
            except:
                # Fallback to line splitting and better text reconstruction
                lines = [line.strip() for line in gen.split('\n') if line.strip()]
                items = []
                current_item = ""
                for line in lines:
                    line = line.strip("-• \t").strip('"').strip("'").strip("```json").strip("```").strip("[").strip("]").strip(",")
                    if line and line not in ["[", "]", "```json", "```"]:
                        if line.endswith('."') or line.endswith(".'") or line.endswith('"') or line.endswith("'"):
                            current_item += " " + line
                            if current_item.strip():
                                items.append(current_item.strip())
                            current_item = ""
                        else:
                            current_item += " " + line
                if current_item.strip():
                    items.append(current_item.strip())
                items = [item for item in items if item]
            if key == "weatherInfo":
                # fallback create object
                parsed = {"averageTemp": None, "peakSeason": None}
                if items:
                    parsed["averageTemp"] = items[0]
                    parsed["peakSeason"] = items[1] if len(items) > 1 else None
            else:
                if key == "tags":
                    parsed = items[:10]
                elif key == "popularFor":
                    parsed = items[:12]
                elif key == "travelTips":
                    parsed = items[:15]
                else:
                    if key == "tags":
                        parsed = items[:10]
                    elif key == "popularFor":
                        parsed = items[:12]
                    elif key == "travelTips":
                        parsed = items[:15]
                    else:
                        parsed = items[:5]
    else:
        # plain text
        parsed = gen.strip()

    # store in session
    s["topics_data"][key] = parsed
    # present to user with Confirm / Regenerate
    if isinstance(parsed, (list, dict)):
        txt = json.dumps(parsed, ensure_ascii=False, indent=2)
        content = f"*{label}:*\n\n{txt}"
    else:
        # Fix for malformed short description: remove duplicate labels
        if key == "description" and "shortDescription:" in parsed:
            parsed = parsed.replace("shortDescription:", "").strip()
        content = f"*{label}:*\n\n{parsed}"
    # Quality check
    if not check_content_quality(content, keywords=["dubai"] if "dubai" in dest.lower() else []):
        logger.warning(f"Content quality check failed for {key} in {dest}")
    try:
        await send_long_message(context.bot, uid, content)
    except Exception as e:
        logger.warning(f"Failed to send long message: {e}")
        # Try sending as plain text if markdown fails
        plain_content = content.replace("*", "").replace("_", "").replace("`", "")
        await context.bot.send_message(uid, plain_content[:4000])

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_topic_{key}"),
         InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen_topic_{key}")]
    ])
    escaped_label = label.replace("-", "\\-").replace(".", "\\.").replace("!", "\\!")
    try:
        try:
            await context.bot.send_message(uid, f"Confirm or regenerate *{escaped_label}*?", reply_markup=markup, parse_mode="MarkdownV2")
        except Exception as e:
            logger.warning(f"Failed to send message with markdown: {e}")
            await context.bot.send_message(uid, f"Confirm or regenerate {label}?", reply_markup=markup)
    except Exception as e:
        logger.warning(f"Failed to send message with markdown: {e}")
        await context.bot.send_message(uid, f"Confirm or regenerate {label}?", reply_markup=markup)


async def confirm_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access.. Contact @mrpawansuthar to get access.")
        return

    # pattern confirm_topic_{key}
    try:
        key = q.data.split("_", 2)[2]
    except Exception:
        await q.message.reply_text("Invalid confirm.")
        return
    s = SESSIONS.get(uid)
    if not s:
        await q.message.reply_text("Session expired.")
        return
    s.setdefault("info_confirmed_map", {})[key] = True
    await q.edit_message_text("✅ Confirmed", reply_markup=None)
    # advance index
    s["topic_index"] = (s.get("topic_index", 0) + 1)
    await send_next_topic(uid, context)


async def regen_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access.. Contact @mrpawansuthar to get access.")
        return

    try:
        key = q.data.split("_", 2)[2]
    except Exception:
        await q.message.reply_text("Invalid regen.")
        return
    s = SESSIONS.get(uid)
    if not s or not s.get("destination"):
        await q.message.reply_text("Session expired.")
        return
    dest = s["destination"]
    # Check if the message has text to edit
    try:
        await q.edit_message_text("🔄 Regenerating...")
    except Exception as e:
        logger.warning(f"Could not edit message text: {e}. Sending new message instead.")
        await context.bot.send_message(uid, "🔄 Regenerating...")
    # reuse prompt generator
    label = dict(TOPICS_ORDER)[key] if key in dict(TOPICS_ORDER) else key
    # call prompt_topic_regenerate
    gen = await call_gemini(prompt_topic_regenerate(dest, label))
    if not gen:
        await context.bot.send_message(uid, "❌ Regeneration failed.")
        return
    # try parse
    parsed = None
    if key in ("tags", "popularFor", "travelTips", "itinerary", "weatherInfo"):
        parsed = try_parse_json_block(gen)
        if not parsed:
            items = [line.strip("-• \t") for line in re.split(r"[\n,]+", gen) if line.strip()]
            if key == "weatherInfo":
                parsed = {"averageTemp": items[0] if items else None, "peakSeason": items[1] if len(items) > 1 else None}
            else:
                parsed = items[:5]
    else:
        parsed = gen.strip()
    s["topics_data"][key] = parsed
    # present new content
    if isinstance(parsed, (list, dict)):
        content = json.dumps(parsed, ensure_ascii=False, indent=2)
    else:
        # Fix for malformed short description: remove duplicate labels
        if key == "description" and "shortDescription:" in parsed:
            parsed = parsed.replace("shortDescription:", "").strip()
        content = parsed
    # Quality check
    if not check_content_quality(content, keywords=["dubai"] if "dubai" in dest.lower() else []):
        logger.warning(f"Content quality check failed for {key} in {dest}")
    await send_long_message(context.bot, uid, content)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_topic_{key}"),
         InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen_topic_{key}")]
    ])
    escaped_label = label.replace("-", "\\-").replace(".", "\\.").replace("!", "\\!")
    try:
        await context.bot.send_message(uid, f"Confirm or regenerate *{escaped_label}*?", reply_markup=markup, parse_mode="MarkdownV2")
    except Exception as e:
        logger.warning(f"Failed to send message with markdown: {e}")
        await context.bot.send_message(uid, f"Confirm or regenerate {label}?", reply_markup=markup)


# ----------------- FINAL SUMMARY & SAVE -----------------
async def send_final_summary(uid: int, context: ContextTypes.DEFAULT_TYPE):
    s = SESSIONS.get(uid)
    if not s:
        return
    dest = s["destination"]
    structured = s.get("structured", {})
    topics = s.get("topics_data", {})

    # Build final text summary
    lines = []
    lines.append(f"*{dest}*")
    # short description (topic or structured) - fix malformed labels
    desc = topics.get("description") or structured.get("description", "")
    if "shortDescription:" in desc:
        desc = desc.replace("shortDescription:", "").strip()
    lines.append(f"\n{desc}\n")
    # long description - optimize for Cairo
    longdesc = topics.get("longDescription") or structured.get("longDescription") or ""
    if dest.lower().startswith("cairo"):
        longdesc = "Discover Cairo, Egypt's vibrant capital! Explore ancient wonders like the Giza Pyramids and Sphinx, delve into treasures at the Egyptian Museum, and wander the historic Khan el-Khalili bazaar. Experience a blend of millennia-old history and modern energy. Plan your trip today for unforgettable adventures!"
    lines.append(f"{longdesc}\n")
    bt = topics.get("bestTimeToVisit") or structured.get("bestTimeToVisit", "")
    lines.append(f"*Best Time:* {bt}")
    # tags & popularFor
    tags = topics.get("tags") or structured.get("tags") or []
    if isinstance(tags, list):
        lines.append(f"*Tags:* {', '.join(tags)}")
    pf = topics.get("popularFor") or structured.get("popularFor") or []
    if isinstance(pf, list):
        lines.append(f"*Popular For:* {', '.join(pf)}")
    # travel tips
    tips = topics.get("travelTips") or structured.get("travelTips") or []
    if isinstance(tips, list):
        lines.append("\n*Travel Tips:*")
        for t in tips:
            lines.append(f"- {t}")
    # itinerary
    it = topics.get("itinerary") or structured.get("itinerary") or []
    if isinstance(it, list):
        lines.append("\n*Itinerary:*")
        for day in it:
            if isinstance(day, dict):
                day_num = day.get("day", "")
                title = day.get("title", "")
                activities = day.get("activities", [])
                lines.append(f"*Day {day_num}: {title}*")
                for activity in activities:
                    lines.append(f"\\- {activity}")
            else:
                lines.append(f"\\- {day}")
    # weather
    weather = topics.get("weatherInfo") or structured.get("weatherInfo") or {}
    if isinstance(weather, dict):
        lines.append(f"\n*Weather:* {json.dumps(weather, ensure_ascii=False)}")
    # location
    location = topics.get("location") or structured.get("location") or {}
    if isinstance(location, dict):
        lines.append(f"\n*Location:* {json.dumps(location, ensure_ascii=False)}")
    # suggested duration
    duration = topics.get("SuggestedDuration") or ""
    if duration:
        lines.append(f"\n*Suggested Duration:* {duration}")

    final_text = "\n\n".join(lines)
    # Send thumbnail + gallery first
    thumb = s.get("thumbnail")
    imgs = s.get("images", [])

    if thumb:
        try:
            await context.bot.send_photo(uid, thumb, caption=f"📍 {dest} — Thumbnail (final preview)")
        except Exception:
            await context.bot.send_message(uid, f"Thumbnail: {thumb}")

    if imgs:
        media = [InputMediaPhoto(thumb)] if thumb else []
        media += [InputMediaPhoto(i) for i in imgs[:MAX_GALLERY]]
        try:
            # If more than 1 item, send as media_group
            if len(media) > 1:
                await context.bot.send_media_group(uid, media=media)
            else:
                # nothing extra
                pass
        except Exception:
            # fallback send urls
            for i in imgs:
                await context.bot.send_message(uid, i)

    # send final text
    await send_long_message(context.bot, uid, final_text)

    # final action buttons
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Post Now", callback_data="post_now"),
         InlineKeyboardButton("💾 Save Draft", callback_data="save_draft")],
        [InlineKeyboardButton("🕒 Schedule 10m", callback_data="schedule_10"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]
    ])
    await context.bot.send_message(uid, "All done — choose action:", reply_markup=markup)


def build_destination_document_from_session(s: Dict[str, Any], is_published: bool, scheduled_at: Optional[str] = None) -> Dict[str, Any]:
    structured = s.get("structured", {})
    topics = s.get("topics_data", {})

    name = s.get("destination")
    doc = {
        "name": name,
        "slug": structured.get("slug") or slugify(name),
        "thumbnail": {"url": s.get("thumbnail"), "alt": name} if s.get("thumbnail") else {"url": "", "alt": ""},
        "images": [{"public_id": "post_by_telegram", "secure_url": u} for u in s.get("images", [])],
        "description": topics.get("description") or structured.get("description") or "",
        "longDescription": topics.get("longDescription") or structured.get("longDescription") or "",
        "category": structured.get("category") or "City",
        "bestTimeToVisit": topics.get("bestTimeToVisit") or structured.get("bestTimeToVisit") or "",
        "tags": topics.get("tags") or structured.get("tags") or [],
        "popularFor": topics.get("popularFor") or structured.get("popularFor") or [],
        "SuggestedDuration": topics.get("SuggestedDuration") or "3-5 days for main attractions\n7-10 days for comprehensive experience",
        "location": topics.get("location") or structured.get("location") or {"country": name.split(",")[-1].strip() if "," in name else "", "region": "", "coordinates": {"latitude": 0, "longitude": 0}},
        "travelTips": topics.get("travelTips") or structured.get("travelTips") or [],
        "itinerary": topics.get("itinerary") or structured.get("itinerary") or [{"day": 1, "title": "Day 1", "activities": ["Explore the city"]}],
        "weatherInfo": topics.get("weatherInfo") or structured.get("weatherInfo") or {"avgTemp": "20°C", "climateType": "Temperate", "bestMonth": "June-August"},
        "isPublished": bool(is_published),
        "createdBy": s.get("createdBy"),
        "createdAt": s.get("createdAt") or iso_now(),
        "updatedAt": iso_now(),
    }
    if scheduled_at:
        doc["scheduled_at"] = scheduled_at
        doc["status"] = "scheduled"
    else:
        doc["status"] = "published" if is_published else "draft"
    return doc


async def post_or_save_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await q.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access..")
        return

    s = SESSIONS.get(uid)
    if not s:
        await q.message.reply_text("Session expired.")
        return
    action = q.data
    if action == "post_now":
        doc = build_destination_document_from_session(s, is_published=True)
        res = destinations_col.insert_one(doc)
        escaped_name = escape_markdown(doc['name'], version=2)
        escaped_slug = escape_markdown(doc['slug'], version=2)
        destination_url = f"https://globetrekker.site/destinations/{doc['slug']}"
        message_text = f"✅ Content successfully posted\\! Name: {escaped_name} Slug: {escaped_slug} \\_id: {str(res.inserted_id)}\\_ [View Destination]({destination_url})"
        try:
            await q.edit_message_text(message_text, parse_mode="MarkdownV2")
        except Exception as e:
            if "BadRequest" in str(e) or "parse entities" in str(e):
                # Manually escape additional characters that might not be covered
                extra_escaped = message_text.replace(".", "\\.").replace("-", "\\-").replace("!", "\\!")
                try:
                    await q.edit_message_text(extra_escaped, parse_mode="MarkdownV2")
                except Exception:
                    # Final fallback: send without markdown
                    await q.edit_message_text(message_text.replace("*", "").replace("_", "").replace("\\", ""))
            else:
                # For any other error, show user-friendly message
                await q.edit_message_text(f"✅ Content posted successfully! View it here: {destination_url}")
                logger.error(f"Unexpected error in post confirmation: {e}")

        # Also post to the group if GROUP_CHAT_ID is set
        if GROUP_CHAT_ID:
            try:
                escaped_name = escape_markdown(doc['name'], version=2)
                group_message = f"🌍 New Destination Added: *{escaped_name}*\n\n{destination_url}"
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=group_message, parse_mode="MarkdownV2")
                logger.info(f"Posted to group {GROUP_CHAT_ID}")
            except Exception as e:
                if "BadRequest" in str(e) or "parse entities" in str(e):
                    # Try with additional escaping
                    try:
                        extra_escaped = group_message.replace(".", "\\.").replace("-", "\\-")
                        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=extra_escaped, parse_mode="MarkdownV2")
                    except Exception:
                        # Final fallback without markdown
                        plain_message = f"🌍 New Destination Added: {doc['name']}\n\n{destination_url}"
                        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=plain_message)
                else:
                    logger.warning(f"Failed to post to group: {e}")
        SESSIONS.pop(uid, None)
    elif action == "save_draft":
        doc = build_destination_document_from_session(s, is_published=False)
        res = destinations_col.insert_one(doc)
        await q.edit_message_text(f"💾 Draft saved. \\_id: {str(res.inserted_id)}\\_", parse_mode="MarkdownV2")
        SESSIONS.pop(uid, None)
    elif action == "schedule_10":
        scheduled_dt = datetime.utcnow() + timedelta(minutes=10)
        scheduled_iso = scheduled_dt.isoformat() + "Z"
        doc = build_destination_document_from_session(s, is_published=False, scheduled_at=scheduled_iso)
        res = destinations_col.insert_one(doc)
        # schedule job to publish
        context.job_queue.run_once(run_scheduled_publish, when=scheduled_dt, data={"mongo_id": str(res.inserted_id)})
        await q.edit_message_text(f"🕒 Scheduled for {scheduled_iso}. \\_id: {str(res.inserted_id)}\\_", parse_mode="MarkdownV2")
        SESSIONS.pop(uid, None)
    elif action == "cancel_flow":
        await q.edit_message_text("❌ Flow cancelled.")
        SESSIONS.pop(uid, None)
    else:
        await q.edit_message_text("Unknown action.")


# ----------------- SCHEDULED PUBLISH -----------------
async def handle_custom_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    custom_dest = update.message.text.strip()

    if not custom_dest:
        return

    # Rate limiting check
    if not check_rate_limit(uid):
        await update.message.reply_text("❌ Too many requests. Please wait before trying again.")
        return

    # Sanitize input
    custom_dest = sanitize_input(custom_dest, max_length=100)

    # Check if user is authorized
    if uid not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ Only authorized users can use this command. Contact @mrpawansuthar to get access.. Contact @mrpawansuthar to get access.")
        return

    # Check if user is in destination selection mode
    session = SESSIONS.get(uid)
    if not session or session.get("stage") != "choose_destination":
        return

    logger.info(f"User {uid} selected custom destination: {custom_dest}")

    # Set up session for custom destination
    session.update({
        "destination": custom_dest,
        "createdBy": str(uid),
        "slug": slugify(custom_dest),
        "topic_index": 0,
        "topics_data": {},
        "images_confirmed": False,
        "thumbnail_confirmed": False,
        "info_confirmed_map": {},
    })

    # Send processing message
    await update.message.reply_text(f"⏳ Generating structured info and thumbnail for *{custom_dest}* ...")

    # Get structured object from Gemini
    struct_text = await call_gemini(prompt_structured_for_destination(custom_dest))
    structured = try_parse_json_block(struct_text) if struct_text else None

    # fallback to basic generation
    if not structured:
        short = await call_gemini(f"Write a one-paragraph short description for {custom_dest}.")
        long = await call_gemini(f"Write a 3-paragraph travel overview for {custom_dest}.")
        structured = {
            "description": short or f"{custom_dest} - brief description.",
            "longDescription": long or f"{custom_dest} - long description.",
            "slug": slugify(custom_dest),
            "category": "City",
            "bestTimeToVisit": "See seasons",
            "weatherInfo": {"averageTemp": None, "peakSeason": None},
            "tags": [],
            "popularFor": [],
            "travelTips": [],
            "itinerary": []
        }

    structured.setdefault("slug", slugify(custom_dest))
    session["structured"] = structured

    # Fetch thumbnail from Pexels
    thumb, imgs = await fetch_pexels_images(custom_dest)
    session["thumbnail"] = thumb
    session["images"] = imgs

    # Send thumbnail with confirm buttons
    if thumb:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Keep Thumbnail", callback_data="confirm_thumbnail"),
             InlineKeyboardButton("🔄 New Thumbnail", callback_data="regen_thumbnail")]
        ])
        try:
            await context.bot.send_photo(chat_id=uid, photo=thumb,
                                      caption=f"📍 {custom_dest} — Thumbnail\nSlug: {structured.get('slug')}",
                                      reply_markup=markup)
        except Exception as e:
            logger.warning(f"Failed to send thumbnail photo: {e}")
            await context.bot.send_message(chat_id=uid, text=f"Thumbnail: {thumb}\n\nChoose action:", reply_markup=markup)
    else:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 New Thumbnail", callback_data="regen_thumbnail")]
        ])
        await context.bot.send_message(chat_id=uid, text="No thumbnail found. Try generating a new one.", reply_markup=markup)


async def run_scheduled_publish(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    mongo_id = data.get("mongo_id")
    if not mongo_id:
        return
    try:
        oid = ObjectId(mongo_id)
    except Exception:
        logger.error("Invalid mongo id: %s", mongo_id)
        return
    doc = destinations_col.find_one({"_id": oid, "status": "scheduled"})
    if not doc:
        logger.info("Scheduled doc not found: %s", mongo_id)
        return
    destinations_col.update_one({"_id": oid}, {"$set": {"isPublished": True, "status": "published", "updatedAt": iso_now()}})
    logger.info("Published scheduled destination %s", mongo_id)
    # notify creator if possible
    try:
        creator = doc.get("createdBy")
        if creator:
            await context.bot.send_message(chat_id=int(creator), text=f"✅ Your scheduled destination '{doc.get('name')}' was published.")
    except Exception:
        logger.exception("Failed to notify creator.")


# ----------------- ADDITIONAL COMMAND HANDLERS -----------------
async def end_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    if uid in SESSIONS:
        SESSIONS.pop(uid, None)
        await update.message.reply_text("✅ Session ended. Use /start to begin a new session.")
    else:
        await update.message.reply_text("No active session to end.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🌍 *Travel Content Bot Help*

*Available Commands:*
• /start \\- Start creating travel content
• /end \\- End current session
• /help \\- Show this help message
• /destinations \\- Show all posted destinations
• /posted \\- Show posting statistics

*How to use:*
1\\. Send /start to begin
2\\. Choose a destination or type a custom one
3\\. Confirm thumbnail and images
4\\. Review and confirm each content section
5\\. Choose to post now, save draft, or schedule

*Features:*
• AI\\-generated travel content using Gemini
• Image fetching from Pexels
• MongoDB storage
• Telegram integration
"""
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")


async def destinations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get all published destinations
        docs = destinations_col.find({"isPublished": True}, {"name": 1, "slug": 1}).limit(50)
        dest_list = list(docs)

        if not dest_list:
            await update.message.reply_text("No destinations posted yet.")
            return

        # Format the list
        lines = ["📍 *Posted Destinations:*"]
        for doc in dest_list:
            name = doc.get("name", "Unknown")
            slug = doc.get("slug", "")
            url = f"https://globetrekker.site/destinations/{slug}" if slug else ""
            if url:
                lines.append(f"• [{escape_markdown(name, version=2)}]({url})")
            else:
                lines.append(f"• {escape_markdown(name, version=2)}")

        response = "\n".join(lines)
        await update.message.reply_text(response, parse_mode="MarkdownV2", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error in destinations command: {e}")
        await update.message.reply_text("❌ Error retrieving destinations.")


async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id

    # Only owner can add users
    if uid != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can use this command.")
        return

    # Get user ID from command arguments
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /adduser <user_id>\nExample: /adduser 123456789")
        return

    try:
        new_user_id = int(args[0])
        if new_user_id in AUTHORIZED_USERS:
            await update.message.reply_text(f"❌ User {new_user_id} is already authorized.")
            return

        AUTHORIZED_USERS.add(new_user_id)
        await update.message.reply_text(f"✅ User {new_user_id} has been added to authorized users.")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a valid numeric user ID.")


async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id

    # Only owner can remove users
    if uid != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can use this command.")
        return

    # Get user ID from command arguments
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removeuser <user_id>\nExample: /removeuser 123456789")
        return

    try:
        remove_user_id = int(args[0])
        if remove_user_id == OWNER_ID:
            await update.message.reply_text("❌ Cannot remove the bot owner.")
            return

        if remove_user_id not in AUTHORIZED_USERS:
            await update.message.reply_text(f"❌ User {remove_user_id} is not authorized.")
            return

        AUTHORIZED_USERS.remove(remove_user_id)
        await update.message.reply_text(f"✅ User {remove_user_id} has been removed from authorized users.")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a valid numeric user ID.")


async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id

    # Only owner can list users
    if uid != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can use this command.")
        return

    users_list = "\n".join([f"• {user_id}" for user_id in sorted(AUTHORIZED_USERS)])
    await update.message.reply_text(f"👥 *Authorized Users:*\n{users_list}", parse_mode="MarkdownV2")


async def posted_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get statistics
        total_published = destinations_col.count_documents({"isPublished": True})
        total_drafts = destinations_col.count_documents({"isPublished": False})
        total_scheduled = destinations_col.count_documents({"status": "scheduled"})

        stats_text = f"""
📊 *Posting Statistics*

✅ Published: {total_published}
💾 Drafts: {total_drafts}
🕒 Scheduled: {total_scheduled}
📈 Total: {total_published + total_drafts + total_scheduled}
"""
        await update.message.reply_text(stats_text, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"Error in posted command: {e}")
        await update.message.reply_text("❌ Error retrieving statistics.")


# ----------------- REGISTER HANDLERS -----------------
def main():
    logger.info("Starting AutoPostBot...")

    # Graceful shutdown handling
    import signal
    import sys

    def signal_handler(signum, frame):
        logger.info("Received shutdown signal, cleaning up...")
        # Close MongoDB connection gracefully
        if 'mongo_client' in globals():
            mongo_client.close()
            logger.info("MongoDB connection closed")
        logger.info("Bot shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Set bot commands
        commands = [
            BotCommand("start", "Start creating travel content"),
            BotCommand("end", "End the current session"),
            BotCommand("help", "Get help about how to use the bot"),
            BotCommand("destinations", "Show all posted destinations"),
            BotCommand("posted", "Show posting statistics"),
            BotCommand("adduser", "Add authorized user (owner only)"),
            BotCommand("removeuser", "Remove authorized user (owner only)"),
            BotCommand("listusers", "List authorized users (owner only)"),
        ]

        async def set_commands():
            try:
                await app.bot.set_my_commands(commands)
                logger.info("Bot commands set successfully")
            except Exception as e:
                logger.warning(f"Failed to set bot commands: {e}")

        # Run command setup
        import asyncio
        asyncio.run(set_commands())

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("end", end_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("destinations", destinations_command))
        app.add_handler(CommandHandler("posted", posted_command))
        app.add_handler(CommandHandler("adduser", add_user_command))
        app.add_handler(CommandHandler("removeuser", remove_user_command))
        app.add_handler(CommandHandler("listusers", list_users_command))

        # destination pick
        app.add_handler(CallbackQueryHandler(destination_callback, pattern=r"^dest_\d+$"))
        # thumbnail handlers
        app.add_handler(CallbackQueryHandler(confirm_thumbnail, pattern=r"^confirm_thumbnail$"))
        app.add_handler(CallbackQueryHandler(regen_thumbnail, pattern=r"^regen_thumbnail$"))
         # gallery
        app.add_handler(CallbackQueryHandler(confirm_images, pattern=r"^confirm_images$"))
        app.add_handler(CallbackQueryHandler(regen_images, pattern=r"^regen_images$"))
        app.add_handler(CallbackQueryHandler(remove_images, pattern=r"^remove_images$"))
        # topic confirm / regen handlers (wildcard)
        app.add_handler(CallbackQueryHandler(confirm_topic_handler, pattern=r"^confirm_topic_"))
        app.add_handler(CallbackQueryHandler(regen_topic_handler, pattern=r"^regen_topic_"))
         # try-again / confirm info
        app.add_handler(CallbackQueryHandler(post_or_save_handler, pattern=r"^(post_now|save_draft|schedule_10|cancel_flow)$"))

           # Help callback
        async def show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
              q = update.callback_query
              await q.answer()
              help_text = """
🌍 *Travel Content Bot Help*

*Available Commands:*
• /start \\- Start creating travel content
• /end \\- End current session
• /help \\- Show this help message
• /destinations \\- Show all posted destinations
• /posted \\- Show posting statistics

*How to use:*
1\\. Send /start to begin
2\\. Choose a destination or type a custom one
3\\. Confirm thumbnail and images
4\\. Review and confirm each content section
5\\. Choose to post now, save draft, or schedule

*Features:*
• AI\\-generated travel content using Gemini
• Image fetching from Pexels
• MongoDB storage
• Telegram integration
"""
              await q.edit_message_text(help_text, parse_mode="MarkdownV2")

        app.add_handler(CallbackQueryHandler(show_help_callback, pattern=r"^show_help$"))
        # text message handler for custom destinations
        from telegram.ext import MessageHandler, filters
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_destination))

    # Handle unknown commands
        async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text(
                "Unknown command. Use /help to see available commands.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📖 Help", callback_data="show_help")]
                ])
            )

        app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
        # schedule job handler is invoked via context.job_queue in code

        logger.info("Bot handlers registered. Polling...")
        app.run_polling(drop_pending_updates=True)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Critical error in main: {e}")
        raise
    finally:
        # Cleanup on exit
        if 'mongo_client' in globals():
            mongo_client.close()
            logger.info("MongoDB connection closed during shutdown")


if __name__ == "__main__":
    main()
