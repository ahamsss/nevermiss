"""
NeverMiss v2.1 — AI Receptionist for Trades
Webhook-based voice (works on Twilio trial) + multi-tenant + usage logging.
Will upgrade to ConversationRelay when on paid Twilio account.
"""

import os
import json
import logging
import sqlite3
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
import anthropic

# ── CONFIG ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nevermiss")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

twilio_client = None
claude_client = None


def init_clients():
    global twilio_client, claude_client
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if ANTHROPIC_API_KEY:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── DATABASE ───────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("nevermiss.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        twilio_number TEXT UNIQUE NOT NULL,
        business_name TEXT NOT NULL,
        trade_type TEXT NOT NULL DEFAULT 'plumbing',
        contractor_phone TEXT NOT NULL,
        contractor_name TEXT DEFAULT '',
        service_area TEXT DEFAULT '',
        custom_greeting TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
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
        lead_texted INTEGER DEFAULT 0,
        booked INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    )""")
    conn.commit()
    conn.close()
    logger.info("Database initialized")


def get_customer_by_number(twilio_number):
    conn = sqlite3.connect("nevermiss.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM customers WHERE twilio_number = ? AND status = 'active'", (twilio_number,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def add_customer(twilio_number, business_name, trade_type, contractor_phone, **kwargs):
    conn = sqlite3.connect("nevermiss.db")
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO customers (twilio_number, business_name, trade_type, contractor_phone,
                contractor_name, service_area, custom_greeting)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (twilio_number, business_name, trade_type, contractor_phone,
               kwargs.get("contractor_name", ""),
               kwargs.get("service_area", ""),
               kwargs.get("custom_greeting", "")))
    conn.commit()
    customer_id = c.lastrowid
    conn.close()
    logger.info(f"Added customer: {business_name} ({twilio_number})")
    return customer_id


def log_call(customer_id, call_sid, caller_phone, **kwargs):
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


# ── AI PROMPTS ─────────────────────────────────────────────────────

def load_prompt(trade_type, business_name):
    prompt_file = os.path.join(os.path.dirname(__file__), "prompts", f"{trade_type}.txt")
    if os.path.exists(prompt_file):
        with open(prompt_file, "r") as f:
            prompt = f.read()
        return prompt.replace("{business_name}", business_name)
    else:
        return f"""You are a warm, friendly receptionist for {business_name}. Your name is Sarah.
You sound like a small-town office manager who genuinely cares about people.
Your job: find out what they need, how urgent it is, get their name, address,
phone number, and best callback time. Keep responses to 2-3 sentences.
Reassure them that {business_name} will reach out shortly.
Don't say you're an AI unless asked — say you're the answering service.
RESPOND WITH ONLY YOUR SPOKEN WORDS. No formatting."""


# ── ACTIVE CALLS ───────────────────────────────────────────────────

calls = {}  # call_sid -> { messages, customer, caller_phone, start_time }


def get_ai_response(call_sid, caller_message, system_prompt):
    if call_sid not in calls:
        calls[call_sid] = {"messages": [], "start_time": datetime.now()}

    calls[call_sid]["messages"].append({"role": "user", "content": caller_message})

    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        system=system_prompt,
        messages=calls[call_sid]["messages"]
    )

    ai_text = response.content[0].text
    calls[call_sid]["messages"].append({"role": "assistant", "content": ai_text})
    return ai_text


def extract_lead_info(call_sid):
    conversation = calls.get(call_sid, {}).get("messages", [])
    if not conversation:
        return None

    convo_text = "\n".join([
        f"{'Caller' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in conversation
    ])

    response = claude_client.messages.create(
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

    try:
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        logger.error(f"Failed to parse lead info for call {call_sid}")
        return None


def send_lead_text(customer, caller_phone, lead_info):
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

    if twilio_client:
        try:
            twilio_client.messages.create(
                body=sms_body,
                from_=customer["twilio_number"],
                to=customer["contractor_phone"]
            )
            logger.info(f"Lead text sent to {customer['contractor_phone']}")
            return True
        except Exception as e:
            logger.error(f"Failed to send text: {e}")
            return False
    else:
        logger.info(f"[DEV MODE] Would text:\n{sms_body}")
        return False


# ── FASTAPI APP ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    init_clients()
    init_db()

    default_number = os.environ.get("TWILIO_PHONE_NUMBER", "")
    if default_number and not get_customer_by_number(default_number):
        add_customer(
            twilio_number=default_number,
            business_name=os.environ.get("BUSINESS_NAME", "Demo Plumbing"),
            trade_type=os.environ.get("TRADE_TYPE", "plumbing"),
            contractor_phone=os.environ.get("CONTRACTOR_PHONE", "+10000000000"),
        )

    logger.info(f"NeverMiss v2.1 starting on port {PORT}")
    yield

app = FastAPI(lifespan=lifespan)


# ── VOICE WEBHOOKS ─────────────────────────────────────────────────

@app.post("/voice")
async def handle_voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller_phone = form.get("From", "unknown")
    called_number = form.get("Called", form.get("To", ""))

    logger.info(f"Incoming call: {call_sid} from {caller_phone} to {called_number}")

    customer = get_customer_by_number(called_number)

    if customer:
        system_prompt = load_prompt(customer["trade_type"], customer["business_name"])
        calls[call_sid] = {
            "messages": [],
            "customer": customer,
            "caller_phone": caller_phone,
            "system_prompt": system_prompt,
            "start_time": datetime.now(),
        }
        greeting = get_ai_response(call_sid, "[Caller just connected — greet them warmly]", system_prompt)
    else:
        greeting = "Hi there! Thanks for calling. How can I help you today?"
        calls[call_sid] = {
            "messages": [],
            "customer": None,
            "caller_phone": caller_phone,
            "system_prompt": load_prompt("plumbing", "the business"),
            "start_time": datetime.now(),
        }

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/voice/continue?call_sid={call_sid}",
        timeout=5,
        speech_timeout="auto",
        language="en-US",
    )
    gather.say(greeting, voice="Polly.Joanna")
    response.append(gather)
    response.say("Are you still there? I'm here to help whenever you're ready.", voice="Polly.Joanna")
    response.redirect(f"/voice?CallSid={call_sid}&From={caller_phone}&Called={called_number}")

    return Response(content=str(response), media_type="text/xml")


@app.post("/voice/continue")
async def handle_continue(request: Request):
    form = await request.form()
    call_sid = form.get("call_sid", request.query_params.get("call_sid", "unknown"))
    caller_speech = form.get("SpeechResult", "")

    logger.info(f"Caller said: {caller_speech}")

    if not caller_speech:
        response = VoiceResponse()
        response.say("Sorry, I didn't catch that. Could you repeat that?", voice="Polly.Joanna")
        gather = Gather(
            input="speech",
            action=f"/voice/continue?call_sid={call_sid}",
            timeout=5,
            speech_timeout="auto",
        )
        response.append(gather)
        return Response(content=str(response), media_type="text/xml")

    call_data = calls.get(call_sid, {})
    system_prompt = call_data.get("system_prompt", load_prompt("plumbing", "the business"))

    ai_response = get_ai_response(call_sid, caller_speech, system_prompt)
    logger.info(f"Sarah says: {ai_response}")

    conversation_length = len(calls.get(call_sid, {}).get("messages", []))
    is_wrapping_up = any(phrase in ai_response.lower() for phrase in [
        "reach out", "call you back", "get back to you",
        "hang in there", "have a great", "take care", "anything else"
    ]) and conversation_length >= 6

    response = VoiceResponse()

    if is_wrapping_up:
        response.say(ai_response, voice="Polly.Joanna")
        gather = Gather(
            input="speech",
            action=f"/voice/wrapup?call_sid={call_sid}",
            timeout=3,
            speech_timeout="auto",
        )
        response.append(gather)
        response.redirect(f"/voice/end?call_sid={call_sid}")
    else:
        gather = Gather(
            input="speech",
            action=f"/voice/continue?call_sid={call_sid}",
            timeout=5,
            speech_timeout="auto",
        )
        gather.say(ai_response, voice="Polly.Joanna")
        response.append(gather)
        response.say("I'm still here if you need anything.", voice="Polly.Joanna")

    return Response(content=str(response), media_type="text/xml")


@app.post("/voice/wrapup")
async def handle_wrapup(request: Request):
    form = await request.form()
    call_sid = request.query_params.get("call_sid", "unknown")
    caller_speech = form.get("SpeechResult", "")

    response = VoiceResponse()
    if caller_speech:
        call_data = calls.get(call_sid, {})
        system_prompt = call_data.get("system_prompt", load_prompt("plumbing", "the business"))
        ai_text = get_ai_response(call_sid, caller_speech, system_prompt)
        response.say(ai_text, voice="Polly.Joanna")

    response.redirect(f"/voice/end?call_sid={call_sid}")
    return Response(content=str(response), media_type="text/xml")


@app.api_route("/voice/end", methods=["GET", "POST"])
async def handle_end(request: Request):
    call_sid = request.query_params.get("call_sid", "unknown")

    response = VoiceResponse()
    response.say("Thanks for calling. Have a great day!", voice="Polly.Joanna")
    response.hangup()

    # Process the call — extract lead and send text
    try:
        call_data = calls.get(call_sid, {})
        customer = call_data.get("customer")
        caller_phone = call_data.get("caller_phone", "Unknown")
        start_time = call_data.get("start_time", datetime.now())
        duration = int((datetime.now() - start_time).total_seconds())

        lead_info = extract_lead_info(call_sid)
        if lead_info and customer:
            texted = send_lead_text(customer, caller_phone, lead_info)
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
                lead_texted=1 if texted else 0,
            )
            logger.info(f"Lead captured: {lead_info}")
        elif customer:
            send_lead_text(customer, caller_phone, {
                "name": "Unknown",
                "issue": "Caller hung up before providing details",
                "urgency": "medium",
                "address": "Not provided",
                "best_time": "Try calling back",
            })

        if call_sid in calls:
            del calls[call_sid]
    except Exception as e:
        logger.error(f"Error processing call end: {e}")

    return Response(content=str(response), media_type="text/xml")


# ── SMS WEBHOOK ────────────────────────────────────────────────────

@app.post("/sms")
async def handle_sms(request: Request):
    form = await request.form()
    from_number = form.get("From", "")
    body = form.get("Body", "").strip().upper()
    to_number = form.get("To", "")

    customer = get_customer_by_number(to_number)
    resp = MessagingResponse()

    if customer and from_number == customer["contractor_phone"]:
        if body == "BOOK":
            resp.message("\u2705 Marked as booked! Don't forget to call them back.")
            conn = sqlite3.connect("nevermiss.db")
            c = conn.cursor()
            c.execute("UPDATE call_log SET booked = 1 WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
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
    return {"status": "ok", "service": "nevermiss", "version": "2.1"}

@app.get("/api/customers")
async def list_customers():
    conn = sqlite3.connect("nevermiss.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM customers WHERE status = 'active'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

@app.get("/api/calls")
async def recent_calls():
    conn = sqlite3.connect("nevermiss.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT cl.*, cu.business_name FROM call_log cl
                JOIN customers cu ON cl.customer_id = cu.id
                ORDER BY cl.id DESC LIMIT 50""")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

@app.post("/api/customers")
async def create_customer(request: Request):
    data = await request.json()
    customer_id = add_customer(
        twilio_number=data["twilio_number"],
        business_name=data["business_name"],
        trade_type=data.get("trade_type", "plumbing"),
        contractor_phone=data["contractor_phone"],
        contractor_name=data.get("contractor_name", ""),
        service_area=data.get("service_area", ""),
        custom_greeting=data.get("custom_greeting", ""),
    )
    return {"id": customer_id, "status": "created"}


# ── RUN ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
