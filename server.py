"""
NeverMiss — AI Receptionist for Trades
Main server that handles incoming calls via Twilio, 
uses Claude AI to qualify leads, and texts summaries to the contractor.

ARCHITECTURE:
  Customer calls your Twilio number
    → Twilio hits /voice webhook
    → AI gathers info (name, issue, urgency, address, availability)
    → AI texts contractor a lead summary
    → AI confirms with caller that contractor will reach out

SETUP (you need 3 accounts — all have free tiers):
  1. Twilio  → phone number + voice + SMS
  2. Anthropic → Claude API for the AI brain
  3. Hosting → Railway, Render, or Replit (free/cheap)
"""

import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
import anthropic

# ── CONFIG ──────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nevermiss")

# Load from environment variables (set these in your hosting platform)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Contractor's real phone number (receives text summaries)
CONTRACTOR_PHONE = os.environ.get("CONTRACTOR_PHONE", "+15551234567")

# Business details (customize per contractor)
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Mike's Plumbing")
TRADE_TYPE = os.environ.get("TRADE_TYPE", "plumbing")  # plumbing, electrical, hvac, general

# Initialize clients
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ── IN-MEMORY STORAGE (use a database in production) ───────────────
calls = {}  # call_sid -> conversation state
leads = []  # completed lead records


# ── AI SYSTEM PROMPT ────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a friendly, professional AI receptionist for {BUSINESS_NAME}, a {TRADE_TYPE} business. 

Your job is to:
1. Greet the caller warmly
2. Find out what they need (the {TRADE_TYPE} issue)
3. Assess urgency (emergency/urgent/can wait)
4. Get their name, address, and best callback time
5. Reassure them that the contractor will reach out shortly

RULES:
- Be warm, human, and conversational — NOT robotic
- Use simple language, no jargon
- Be empathetic — they're calling because they have a problem
- Keep responses SHORT (2-3 sentences max) — this is a phone call, not an essay
- If they say it's an emergency, prioritize getting their address and phone number fast
- Don't say you're an AI unless directly asked. Say "I'm the answering service for {BUSINESS_NAME}"
- If asked about pricing, say "{BUSINESS_NAME} will be happy to give you a quote when they call back"
- If it's not related to {TRADE_TYPE}, politely explain what {BUSINESS_NAME} does

After gathering info, say something like:
"Great, I've got everything I need. I'm sending your details to [contractor] right now and they'll reach out to you shortly. Is there anything else?"

RESPOND WITH ONLY YOUR SPOKEN WORDS. No actions, no stage directions, no formatting."""


# ── HELPER FUNCTIONS ────────────────────────────────────────────────

def get_ai_response(call_sid, caller_message):
    """Get Claude's response given the conversation history."""
    if call_sid not in calls:
        calls[call_sid] = {
            "messages": [],
            "caller_phone": "",
            "start_time": datetime.now().isoformat(),
        }
    
    # Add caller's message
    calls[call_sid]["messages"].append({
        "role": "user",
        "content": caller_message
    })
    
    # Get AI response
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,  # Keep responses short for voice
        system=SYSTEM_PROMPT,
        messages=calls[call_sid]["messages"]
    )
    
    ai_text = response.content[0].text
    
    # Store AI response in history
    calls[call_sid]["messages"].append({
        "role": "assistant",
        "content": ai_text
    })
    
    return ai_text


def extract_lead_info(call_sid):
    """Use Claude to extract structured lead data from the conversation."""
    conversation = calls.get(call_sid, {}).get("messages", [])
    if not conversation:
        return None
    
    # Format conversation for extraction
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
        # Clean up response and parse JSON
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        logger.error(f"Failed to parse lead info for call {call_sid}")
        return None


def send_lead_text(caller_phone, lead_info):
    """Text the contractor a formatted lead summary."""
    urgency_emoji = {
        "emergency": "🚨 EMERGENCY",
        "high": "🔴 URGENT",
        "medium": "🟡 Medium",
        "low": "🟢 Low priority"
    }
    
    urgency = urgency_emoji.get(lead_info.get("urgency", "medium"), "🟡 Medium")
    
    message = (
        f"{urgency}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📞 {lead_info.get('name', 'Unknown')}\n"
        f"☎️  {caller_phone}\n"
        f"🔧 {lead_info.get('issue', 'No details')}\n"
        f"📍 {lead_info.get('address', 'No address')}\n"
        f"🕐 {lead_info.get('best_time', 'ASAP')}\n"
    )
    
    if lead_info.get("notes"):
        message += f"📝 {lead_info['notes']}\n"
    
    message += f"━━━━━━━━━━━━━━━\nReply BOOK to confirm or PASS to skip"
    
    if twilio_client:
        twilio_client.messages.create(
            body=message,
            from_=os.environ.get("TWILIO_PHONE_NUMBER"),
            to=CONTRACTOR_PHONE
        )
        logger.info(f"Lead text sent to {CONTRACTOR_PHONE}")
    else:
        logger.info(f"[DEV MODE] Would text contractor:\n{message}")
    
    return message


# ── TWILIO WEBHOOKS ─────────────────────────────────────────────────

@app.route("/voice", methods=["POST"])
def handle_incoming_call():
    """
    Twilio hits this URL when someone calls your number.
    This is the entry point — it greets the caller and starts listening.
    """
    call_sid = request.form.get("CallSid", "unknown")
    caller_phone = request.form.get("From", "unknown")
    
    logger.info(f"Incoming call: {call_sid} from {caller_phone}")
    
    # Initialize call state
    calls[call_sid] = {
        "messages": [],
        "caller_phone": caller_phone,
        "start_time": datetime.now().isoformat(),
    }
    
    # Get initial greeting from AI
    greeting = get_ai_response(call_sid, "[Caller just connected — greet them warmly]")
    
    # Build Twilio response
    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/voice/continue?call_sid={call_sid}",
        timeout=5,
        speech_timeout="auto",
        language="en-US",
    )
    gather.say(greeting, voice="Polly.Joanna", rate="95%")
    response.append(gather)
    
    # If no speech detected, prompt them
    response.say("Are you still there? I'm here to help whenever you're ready.", 
                 voice="Polly.Joanna")
    response.redirect(f"/voice?CallSid={call_sid}&From={caller_phone}")
    
    return Response(str(response), mimetype="text/xml")


@app.route("/voice/continue", methods=["POST"])
def handle_conversation():
    """
    Handles each back-and-forth turn of the conversation.
    Twilio sends us what the caller said, we get AI response, send it back.
    """
    call_sid = request.args.get("call_sid", request.form.get("CallSid", "unknown"))
    caller_speech = request.form.get("SpeechResult", "")
    
    logger.info(f"Caller said: {caller_speech}")
    
    if not caller_speech:
        response = VoiceResponse()
        response.say("Sorry, I didn't catch that. Could you repeat that?", 
                     voice="Polly.Joanna")
        gather = Gather(
            input="speech",
            action=f"/voice/continue?call_sid={call_sid}",
            timeout=5,
            speech_timeout="auto",
        )
        response.append(gather)
        return Response(str(response), mimetype="text/xml")
    
    # Get AI response
    ai_response = get_ai_response(call_sid, caller_speech)
    
    # Check if conversation seems complete (AI said goodbye or gathered enough info)
    conversation_length = len(calls.get(call_sid, {}).get("messages", []))
    is_wrapping_up = any(phrase in ai_response.lower() for phrase in [
        "reach out to you", "call you back", "get back to you",
        "anything else", "have a great", "take care", "goodbye"
    ])
    
    response = VoiceResponse()
    
    if is_wrapping_up and conversation_length >= 6:
        # Conversation is done — say final message and hang up
        response.say(ai_response, voice="Polly.Joanna", rate="95%")
        
        # Give them a moment, then end
        final_gather = Gather(
            input="speech",
            action=f"/voice/wrapup?call_sid={call_sid}",
            timeout=3,
            speech_timeout="auto",
        )
        final_gather.say("", voice="Polly.Joanna")
        response.append(final_gather)
        
        # If they don't say anything, end the call
        response.redirect(f"/voice/end?call_sid={call_sid}")
    else:
        # Continue conversation
        gather = Gather(
            input="speech",
            action=f"/voice/continue?call_sid={call_sid}",
            timeout=5,
            speech_timeout="auto",
        )
        gather.say(ai_response, voice="Polly.Joanna", rate="95%")
        response.append(gather)
        
        # Timeout fallback
        response.say("I'm still here if you need anything.", voice="Polly.Joanna")
        response.redirect(f"/voice/continue?call_sid={call_sid}")
    
    return Response(str(response), mimetype="text/xml")


@app.route("/voice/wrapup", methods=["POST"])
def handle_wrapup():
    """Handle any final words from the caller before ending."""
    call_sid = request.args.get("call_sid", "unknown")
    caller_speech = request.form.get("SpeechResult", "")
    
    response = VoiceResponse()
    
    if caller_speech:
        ai_response = get_ai_response(call_sid, caller_speech)
        response.say(ai_response, voice="Polly.Joanna", rate="95%")
    
    response.redirect(f"/voice/end?call_sid={call_sid}")
    return Response(str(response), mimetype="text/xml")


@app.route("/voice/end", methods=["POST", "GET"])
def handle_call_end():
    """Call is done — extract lead info and text it to the contractor."""
    call_sid = request.args.get("call_sid", "unknown")
    
    response = VoiceResponse()
    response.say("Thanks for calling. Have a great day!", voice="Polly.Joanna")
    response.hangup()
    
    # Extract lead info and send text (in background ideally)
    try:
        call_data = calls.get(call_sid, {})
        caller_phone = call_data.get("caller_phone", "Unknown")
        
        lead_info = extract_lead_info(call_sid)
        if lead_info:
            lead_info["phone"] = caller_phone
            lead_info["call_sid"] = call_sid
            lead_info["timestamp"] = datetime.now().isoformat()
            
            # Send text to contractor
            send_lead_text(caller_phone, lead_info)
            
            # Store the lead
            leads.append(lead_info)
            logger.info(f"Lead captured: {lead_info}")
        else:
            # Fallback — still notify contractor
            send_lead_text(caller_phone, {
                "name": "Unknown",
                "issue": "Caller hung up before providing details",
                "urgency": "medium",
                "address": "Not provided",
                "best_time": "Try calling back",
            })
    except Exception as e:
        logger.error(f"Error processing call end: {e}")
    
    return Response(str(response), mimetype="text/xml")


@app.route("/call-status", methods=["POST"])
def handle_call_status():
    """Twilio status callback — fires when call ends, fails, etc."""
    call_sid = request.form.get("CallSid", "unknown")
    status = request.form.get("CallStatus", "unknown")
    logger.info(f"Call {call_sid} status: {status}")
    
    # If call ended and we haven't processed it yet
    if status == "completed" and call_sid in calls:
        # Trigger lead extraction if not already done
        pass
    
    return "", 200


# ── SMS WEBHOOK (contractor replies) ───────────────────────────────

@app.route("/sms", methods=["POST"])
def handle_sms():
    """
    Handle text replies from the contractor.
    BOOK = mark as booked, PASS = skip, or free text reply.
    """
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip().upper()
    
    response = VoiceResponse()  # TwiML works for SMS too via MessagingResponse
    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    
    if from_number == CONTRACTOR_PHONE:
        if body == "BOOK":
            resp.message("✅ Marked as booked! Don't forget to call them back.")
        elif body == "PASS":
            resp.message("👍 Lead passed. We'll keep answering your calls.")
        else:
            resp.message(f"Got it. Reply BOOK to confirm a job or PASS to skip.")
    
    return Response(str(resp), mimetype="text/xml")


# ── API ENDPOINTS (for dashboard) ──────────────────────────────────

@app.route("/api/leads", methods=["GET"])
def get_leads():
    """Return all captured leads (for the dashboard)."""
    return jsonify(leads)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Return basic stats."""
    return jsonify({
        "total_calls": len(leads),
        "today": len([l for l in leads if l.get("timestamp", "").startswith(datetime.now().strftime("%Y-%m-%d"))]),
        "emergencies": len([l for l in leads if l.get("urgency") == "emergency"]),
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "nevermiss"})


# ── RUN ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 NeverMiss server starting on port {port}")
    logger.info(f"📞 Answering for: {BUSINESS_NAME} ({TRADE_TYPE})")
    app.run(host="0.0.0.0", port=port, debug=True)
