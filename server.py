"""
NeverMiss v2 — AI Receptionist for Trades
Using Twilio ConversationRelay (WebSocket) for low-latency voice AI.

ARCHITECTURE (v2):
  Customer calls Twilio number
    → Twilio sends TwiML with <ConversationRelay> 
    → ConversationRelay opens WebSocket to our server
    → Twilio handles STT/TTS natively (low latency, interruptions)
    → Our server sends/receives text via WebSocket
    → Claude AI generates responses
    → After call: extract lead → text contractor

IMPROVEMENTS OVER v1:
  - WebSocket instead of webhooks = ~10x lower latency
  - ConversationRelay handles STT/TTS = no more Polly round-trips  
  - Multi-tenant: each customer gets their own phone number + config
  - Usage logging: tracks calls, minutes, tokens per customer
"""

import os
import json
import logging
import sqlite3
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.rest import Client as TwilioClient
import anthropic

# ── CONFIG ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nevermiss")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8080")
PORT = int(os.environ.get("PORT", 8080))

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ── DATABASE (SQLite for MVP, upgrade to Postgres later) ───────────

def init_db():
    """Create tables for customer configs and usage logging."""
    conn = sqlite3.connect("nevermiss.db")
    c = conn.cursor()
    
    # Customer configs — keyed by their Twilio phone number
    c.execute("""CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        twilio_number TEXT UNIQUE NOT NULL,
        business_name TEXT NOT NULL,
        trade_type TEXT NOT NULL DEFAULT 'plumbing',
        contractor_phone TEXT NOT NULL,
        contractor_name TEXT DEFAULT '',
        service_area TEXT DEFAULT '',
        custom_greeting TEXT DEFAULT '',
        pricing_note TEXT DEFAULT '',
        specialties TEXT DEFAULT '',
        plan TEXT DEFAULT 'pro',
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Usage logging — one row per call
    c.execute("""CREATE TABLE IF NOT EXISTS call_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        call_sid TEXT,
        caller_phone TEXT,
        caller_name TEXT DEFAULT 'Unknown',
        issue TEXT DEFAULT '',
        urgency TEXT DEFAULT 'medium',
        address TEXT DEFAULT '',
        best_time TEXT DEFAULT 'ASAP',
        duration_seconds INTEGER DEFAULT 0,
        status TEXT DEFAULT 'completed',
        lead_texted INTEGER DEFAULT 0,
        booked INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    )""")
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")


def get_customer_by_number(twilio_number):
    """Look up customer config by their Twilio phone number."""
    conn = sqlite3.connect("nevermiss.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM customers WHERE twilio_number = ? AND status = 'active'", (twilio_number,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def add_customer(twilio_number, business_name, trade_type, contractor_phone, **kwargs):
    """Add a new customer config."""
    conn = sqlite3.connect("nevermiss.db")
    c = conn.cursor()
    c.execute("""INSERT INTO customers (twilio_number, business_name, trade_type, contractor_phone,
                contractor_name, service_area, custom_greeting, pricing_note, specialties)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (twilio_number, business_name, trade_type, contractor_phone,
               kwargs.get("contractor_name", ""),
               kwargs.get("service_area", ""),
               kwargs.get("custom_greeting", ""),
               kwargs.get("pricing_note", ""),
               kwargs.get("specialties", "")))
    conn.commit()
    customer_id = c.lastrowid
    conn.close()
    logger.info(f"Added customer: {business_name} ({twilio_number})")
    return customer_id


def log_call(customer_id, call_sid, caller_phone, **kwargs):
    """Log a completed call for usage tracking."""
    conn = sqlite3.connect("nevermiss.db")
    c = conn.cursor()
    c.execute("""INSERT INTO call_log (customer_id, call_sid, caller_phone, caller_name,
                issue, urgency, address, best_time, duration_seconds, lead_texted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (customer_id, call_sid, caller_phone,
               kwargs.get("caller_name", "Unknown"),
               kwargs.get("issue", ""),
               kwargs.get("urgency", "medium"),
               kwargs.get("address", ""),
               kwargs.get("best_time", "ASAP"),
               kwargs.get("duration_seconds", 0),
               kwargs.get("lead_texted", 0)))
    conn.commit()
    conn.close()


def get_usage_stats(customer_id, month=None):
    """Get usage stats for a customer (for monitoring heavy users)."""
    conn = sqlite3.connect("nevermiss.db")
    c = conn.cursor()
    if month is None:
        month = datetime.now().strftime("%Y-%m")
    c.execute("""SELECT COUNT(*) as total_calls, 
                SUM(duration_seconds) as total_seconds,
                SUM(CASE WHEN booked = 1 THEN 1 ELSE 0 END) as booked_calls
                FROM call_log WHERE customer_id = ? AND created_at LIKE ?""",
              (customer_id, f"{month}%"))
    row = c.fetchone()
    conn.close()
    return {
        "total_calls": row[0] or 0,
        "total_minutes": round((row[1] or 0) / 60, 1),
        "booked_calls": row[2] or 0,
    }


# ── LOAD TRADE-SPECIFIC PROMPTS ────────────────────────────────────

def load_prompt(trade_type, business_name):
    """Load the AI personality prompt for a specific trade."""
    prompt_file = os.path.join(os.path.dirname(__file__), "prompts", f"{trade_type}.txt")
    
    if os.path.exists(prompt_file):
        with open(prompt_file, "r") as f:
            prompt = f.read()
        prompt = prompt.replace("{business_name}", business_name)
        return prompt
    else:
        return f"""You are a warm, friendly receptionist for {business_name}. Your name is Sarah.
You sound like a small-town office manager who genuinely cares about people.
Your job: find out what they need, how urgent it is, get their name, address,
phone number, and best callback time. Keep responses to 2-3 sentences.
Reassure them that {business_name} will reach out shortly.
Don't say you're an AI unless asked — say you're the answering service.
RESPOND WITH ONLY YOUR SPOKEN WORDS. No formatting."""


# ── ACTIVE CALL SESSIONS ───────────────────────────────────────────

sessions = {}  # call_sid -> { messages, customer, caller_phone, start_time }


# ── FASTAPI APP ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    init_db()
    
    # Add a default customer if none exist (for testing)
    if not get_customer_by_number(os.environ.get("TWILIO_PHONE_NUMBER", "")):
        default_number = os.environ.get("TWILIO_PHONE_NUMBER", "+10000000000")
        add_customer(
            twilio_number=default_number,
            business_name=os.environ.get("BUSINESS_NAME", "Demo Plumbing"),
            trade_type=os.environ.get("TRADE_TYPE", "plumbing"),
            contractor_phone=os.environ.get("CONTRACTOR_PHONE", "+10000000000"),
        )
    
    logger.info(f"NeverMiss v2 starting on port {PORT}")
    logger.info(f"Domain: {DOMAIN}")
    yield

app = FastAPI(lifespan=lifespan)


# ── TWIML ENDPOINT (tells Twilio to use ConversationRelay) ─────────

@app.post("/voice")
async def handle_voice(request: Request):
    """
    Twilio hits this when someone calls.
    We return TwiML that starts a ConversationRelay WebSocket session.
    """
    form = await request.form()
    called_number = form.get("Called", "")
    caller_phone = form.get("From", "unknown")
    
    # Look up which customer this call is for (by the Twilio number they called)
    customer = get_customer_by_number(called_number)
    
    if customer:
        greeting = customer.get("custom_greeting") or \
            f"Hi there! Thanks for calling {customer['business_name']}. This is Sarah, how can I help you today?"
    else:
        # Fallback for unknown numbers
        greeting = "Hi there! Thanks for calling. This is Sarah, how can I help you today?"
    
    logger.info(f"Incoming call to {called_number} from {caller_phone}")
    
    # Build TwiML with ConversationRelay
    ws_url = f"wss://{DOMAIN}/ws"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <ConversationRelay 
            url="{ws_url}" 
            welcomeGreeting="{greeting}"
            voice="en-US-Standard-F"
            transcriptionProvider="google"
            ttsProvider="google"
        >
            <Parameter name="called_number" value="{called_number}" />
            <Parameter name="caller_phone" value="{caller_phone}" />
        </ConversationRelay>
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="text/xml")


# ── WEBSOCKET ENDPOINT (ConversationRelay talks to us here) ────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    ConversationRelay connects here via WebSocket.
    We receive transcribed speech, send text responses.
    Twilio handles STT and TTS — we just deal with text.
    """
    await ws.accept()
    call_sid = None
    customer = None
    caller_phone = "unknown"
    
    try:
        while True:
            data = await ws.receive_text()
            message = json.loads(data)
            msg_type = message.get("type", "")
            
            # ── SETUP: First message when WebSocket connects ──
            if msg_type == "setup":
                call_sid = message.get("callSid", "unknown")
                
                # Extract custom parameters we passed in TwiML
                custom_params = message.get("customParameters", {})
                called_number = custom_params.get("called_number", "")
                caller_phone = custom_params.get("caller_phone", "unknown")
                
                # Look up customer config
                customer = get_customer_by_number(called_number)
                
                if customer:
                    system_prompt = load_prompt(customer["trade_type"], customer["business_name"])
                else:
                    system_prompt = load_prompt("plumbing", "the business")
                
                # Initialize session
                sessions[call_sid] = {
                    "messages": [],
                    "system_prompt": system_prompt,
                    "customer": customer,
                    "caller_phone": caller_phone,
                    "start_time": datetime.now(),
                }
                
                logger.info(f"WebSocket connected for call {call_sid}")
                logger.info(f"Customer: {customer['business_name'] if customer else 'Unknown'}")
            
            # ── PROMPT: Caller said something (transcribed text) ──
            elif msg_type == "prompt":
                voice_input = message.get("voicePrompt", "")
                
                if not voice_input or not call_sid or call_sid not in sessions:
                    continue
                
                logger.info(f"Caller said: {voice_input}")
                
                session = sessions[call_sid]
                session["messages"].append({
                    "role": "user",
                    "content": voice_input
                })
                
                # Get AI response
                try:
                    response = claude_client.messages.create(
                        model="claude-sonnet-4-20250514",
                        max_tokens=150,
                        system=session["system_prompt"],
                        messages=session["messages"]
                    )
                    
                    ai_text = response.content[0].text
                    
                    session["messages"].append({
                        "role": "assistant",
                        "content": ai_text
                    })
                    
                    logger.info(f"Sarah says: {ai_text}")
                    
                    # Check if conversation is wrapping up
                    is_ending = any(phrase in ai_text.lower() for phrase in [
                        "reach out", "call you back", "get back to you",
                        "hang in there", "have a great", "take care",
                        "anything else"
                    ]) and len(session["messages"]) >= 6
                    
                    # Send response back via WebSocket
                    # ConversationRelay will convert it to speech
                    await ws.send_json({
                        "type": "text",
                        "token": ai_text,
                        "last": True  # This is a complete response
                    })
                    
                    # If wrapping up and enough exchanges, end after this
                    if is_ending and len(session["messages"]) >= 8:
                        # Give a moment for the last message to play
                        await ws.send_json({
                            "type": "end",
                            "handoffData": json.dumps({
                                "reason": "conversation_complete",
                                "call_sid": call_sid
                            })
                        })
                
                except Exception as e:
                    logger.error(f"Claude API error: {e}")
                    await ws.send_json({
                        "type": "text",
                        "token": "I'm sorry, I'm having a little trouble right now. Can you give me just a moment?",
                        "last": True
                    })
            
            # ── INTERRUPT: Caller interrupted the AI ──
            elif msg_type == "interrupt":
                logger.info(f"Caller interrupted. Utterance until interrupt: {message.get('utteranceUntilInterrupt', '')}")
            
            # ── DTMF: Caller pressed a key ──
            elif msg_type == "dtmf":
                logger.info(f"DTMF received: {message.get('digit', '')}")
            
            # ── ERROR ──
            elif msg_type == "error":
                logger.error(f"ConversationRelay error: {message}")
    
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for call {call_sid}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Call ended — extract lead and send text
        if call_sid and call_sid in sessions:
            await process_call_end(call_sid)


async def process_call_end(call_sid):
    """Extract lead info and text it to the contractor."""
    session = sessions.get(call_sid)
    if not session or not session.get("messages"):
        return
    
    customer = session.get("customer")
    caller_phone = session.get("caller_phone", "Unknown")
    start_time = session.get("start_time", datetime.now())
    duration = int((datetime.now() - start_time).total_seconds())
    
    # Extract structured lead data
    convo_text = "\n".join([
        f"{'Caller' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in session["messages"]
    ])
    
    try:
        extraction = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system="""Extract lead information from this phone conversation.
Return ONLY valid JSON with these fields:
{
  "name": "caller's name or 'Unknown'",
  "issue": "brief description of the problem",
  "urgency": "emergency|high|medium|low",
  "address": "address if given or 'Not provided'",
  "best_time": "when they want a callback or 'ASAP'",
  "notes": "any other relevant details"
}""",
            messages=[{"role": "user", "content": convo_text}]
        )
        
        text = extraction.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        lead_info = json.loads(text)
    except Exception as e:
        logger.error(f"Lead extraction failed: {e}")
        lead_info = {
            "name": "Unknown",
            "issue": "Call completed but details could not be extracted",
            "urgency": "medium",
            "address": "Not provided",
            "best_time": "ASAP",
            "notes": ""
        }
    
    # Format and send text to contractor
    urgency_emoji = {
        "emergency": "\U0001F6A8 EMERGENCY",
        "high": "\U0001F534 URGENT",
        "medium": "\U0001F7E1 Medium",
        "low": "\U0001F7E2 Low priority"
    }
    
    urgency = urgency_emoji.get(lead_info.get("urgency", "medium"), "\U0001F7E1 Medium")
    
    sms_body = (
        f"{urgency}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001F4DE {lead_info.get('name', 'Unknown')}\n"
        f"\u260E\uFE0F  {caller_phone}\n"
        f"\U0001F527 {lead_info.get('issue', 'No details')}\n"
        f"\U0001F4CD {lead_info.get('address', 'No address')}\n"
        f"\U0001F550 {lead_info.get('best_time', 'ASAP')}\n"
    )
    if lead_info.get("notes"):
        sms_body += f"\U0001F4DD {lead_info['notes']}\n"
    sms_body += f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nReply BOOK to confirm or PASS to skip"
    
    # Send the text
    if customer and twilio_client:
        try:
            twilio_client.messages.create(
                body=sms_body,
                from_=customer["twilio_number"],
                to=customer["contractor_phone"]
            )
            logger.info(f"Lead text sent to {customer['contractor_phone']}")
            
            # Log the call
            log_call(
                customer_id=customer["id"],
                call_sid=call_sid,
                caller_phone=caller_phone,
                caller_name=lead_info.get("name", "Unknown"),
                issue=lead_info.get("issue", ""),
                urgency=lead_info.get("urgency", "medium"),
                address=lead_info.get("address", ""),
                best_time=lead_info.get("best_time", "ASAP"),
                duration_seconds=duration,
                lead_texted=1,
            )
        except Exception as e:
            logger.error(f"Failed to send lead text: {e}")
    else:
        logger.info(f"[DEV MODE] Would text:\n{sms_body}")
    
    # Clean up session
    del sessions[call_sid]


# ── SMS WEBHOOK (contractor replies) ───────────────────────────────

@app.post("/sms")
async def handle_sms(request: Request):
    """Handle BOOK/PASS replies from contractors."""
    form = await request.form()
    from_number = form.get("From", "")
    body = form.get("Body", "").strip().upper()
    to_number = form.get("To", "")
    
    # Find which customer replied
    customer = get_customer_by_number(to_number)
    
    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    
    if customer and from_number == customer["contractor_phone"]:
        if body == "BOOK":
            resp.message("\u2705 Marked as booked! Don't forget to call them back.")
            # Update the most recent call log entry
            conn = sqlite3.connect("nevermiss.db")
            c = conn.cursor()
            c.execute("""UPDATE call_log SET booked = 1 
                        WHERE customer_id = ? ORDER BY id DESC LIMIT 1""",
                      (customer["id"],))
            conn.commit()
            conn.close()
        elif body == "PASS":
            resp.message("\U0001F44D Lead passed. We'll keep answering your calls.")
        else:
            resp.message("Reply BOOK to confirm a job or PASS to skip.")
    
    return Response(content=str(resp), media_type="text/xml")


# ── API ENDPOINTS ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "nevermiss", "version": "2.0"}


@app.get("/api/customers")
async def list_customers():
    """List all active customers."""
    conn = sqlite3.connect("nevermiss.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM customers WHERE status = 'active'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


@app.get("/api/customers/{customer_id}/usage")
async def customer_usage(customer_id: int):
    """Get usage stats for a specific customer."""
    return get_usage_stats(customer_id)


@app.get("/api/calls")
async def recent_calls():
    """Get recent calls across all customers."""
    conn = sqlite3.connect("nevermiss.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT cl.*, cu.business_name 
                FROM call_log cl 
                JOIN customers cu ON cl.customer_id = cu.id 
                ORDER BY cl.id DESC LIMIT 50""")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


@app.post("/api/customers")
async def create_customer(request: Request):
    """API endpoint to add a new customer."""
    data = await request.json()
    customer_id = add_customer(
        twilio_number=data["twilio_number"],
        business_name=data["business_name"],
        trade_type=data.get("trade_type", "plumbing"),
        contractor_phone=data["contractor_phone"],
        contractor_name=data.get("contractor_name", ""),
        service_area=data.get("service_area", ""),
        custom_greeting=data.get("custom_greeting", ""),
        pricing_note=data.get("pricing_note", ""),
        specialties=data.get("specialties", ""),
    )
    return {"id": customer_id, "status": "created"}


# ── RUN ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
