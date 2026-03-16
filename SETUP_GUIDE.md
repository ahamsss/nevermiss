# NeverMiss — Setup Guide
### Get your AI receptionist answering calls in under 30 minutes.

No coding experience needed. Just follow the steps.

---

## How It All Connects

```
Customer calls your Twilio phone number
         ↓
Twilio sends the call to your server
         ↓
Server asks Claude AI what to say
         ↓
AI talks to the customer (via Twilio voice)
         ↓
AI gathers: name, issue, urgency, address, best time
         ↓
Server texts YOU a formatted lead summary
         ↓
You reply BOOK or PASS via text
```

---

## Step 1: Get Your Accounts (15 min)

You need 3 free accounts. That's it.

### A) Twilio (the phone system)
1. Go to **twilio.com** → Sign up free
2. You get a free trial with $15 credit (enough for ~500 minutes of calls)
3. In your Twilio console, note down:
   - **Account SID** (starts with `AC`)
   - **Auth Token**
4. Click **"Get a phone number"** → pick one with your area code
5. Note down the phone number (format: `+1XXXXXXXXXX`)

### B) Anthropic (the AI brain)
1. Go to **console.anthropic.com** → Sign up
2. Go to **API Keys** → Create a new key
3. Note it down (starts with `sk-ant-`)
4. Add $10 credit to start (will last ~2,000+ calls)

### C) Railway (hosts your server — easiest option)
1. Go to **railway.app** → Sign up with GitHub
2. (If you don't have GitHub: go to github.com, sign up, then come back)

---

## Step 2: Deploy the Server (10 min)

### Option A: Railway (Recommended — easiest)
1. In Railway, click **"New Project"** → **"Deploy from GitHub repo"**
2. If prompted, upload the `nevermiss-prototype` folder to a new GitHub repo
   - Go to github.com → "New repository" → name it `nevermiss`
   - Upload all files from the `nevermiss-prototype` folder
3. Back in Railway, select your `nevermiss` repo
4. Railway will detect it's a Python app automatically
5. Go to **Variables** tab and add each value from your `.env.example`:
   ```
   TWILIO_ACCOUNT_SID     = ACxxxxxxx...
   TWILIO_AUTH_TOKEN       = your_token...
   TWILIO_PHONE_NUMBER     = +1XXXXXXXXXX
   ANTHROPIC_API_KEY       = sk-ant-xxxx...
   BUSINESS_NAME           = Your Business Name
   TRADE_TYPE              = plumbing (or electrical, hvac, general)
   CONTRACTOR_PHONE        = +1XXXXXXXXXX (YOUR real phone)
   ```
6. Click **Deploy** → wait ~2 minutes
7. Go to **Settings** → **Generate Domain** → you'll get a URL like:
   `https://nevermiss-production-xxxx.up.railway.app`

### Option B: Render (free tier)
1. Go to **render.com** → Sign up
2. New → Web Service → Connect your GitHub repo
3. Set **Build Command**: `pip install -r requirements.txt`
4. Set **Start Command**: `gunicorn server:app`
5. Add environment variables (same as above)
6. Deploy

---

## Step 3: Connect Twilio to Your Server (5 min)

This tells Twilio: "When someone calls, send the call to my server."

1. Go to **twilio.com/console**
2. Click **Phone Numbers** → **Manage** → **Active Numbers**
3. Click your phone number
4. Under **Voice Configuration**:
   - **"A call comes in"** → set to **Webhook**
   - **URL**: `https://YOUR-RAILWAY-URL.up.railway.app/voice`
   - **Method**: POST
5. Under **"Call status changes"** (optional but helpful):
   - **URL**: `https://YOUR-RAILWAY-URL.up.railway.app/call-status`
6. Under **Messaging Configuration**:
   - **"A message comes in"** → Webhook
   - **URL**: `https://YOUR-RAILWAY-URL.up.railway.app/sms`
7. Click **Save**

---

## Step 4: Test It! (2 min)

1. Call your Twilio phone number from your personal phone
2. You should hear the AI receptionist answer
3. Talk to it — describe a plumbing/electrical/HVAC problem
4. Hang up
5. Check your phone — you should get a text summary within 30 seconds

**If it works: congratulations, you have a working AI receptionist!**

---

## Step 5: Set Up Call Forwarding (2 min)

So YOUR existing business number forwards missed calls to NeverMiss:

### iPhone:
- Settings → Phone → Call Forwarding → ON → Enter your Twilio number

### Android:
- Phone app → Settings → Calls → Call Forwarding
- Set "When unanswered" → Your Twilio number

### Better option — "Forward when busy/unanswered":
- Dial from your phone: `**61*+1XXXXXXXXXX#` (replace with Twilio number)
- This only forwards when you DON'T pick up (after ~20 seconds)
- Your phone still rings first. NeverMiss is your backup.

---

## Monthly Costs (What You'll Actually Pay)

| Service | Cost | Notes |
|---------|------|-------|
| Twilio phone number | $1.15/mo | Per phone number |
| Twilio voice (inbound) | ~$0.0085/min | ~$5/mo at 600 min |
| Twilio SMS (outbound) | $0.0079/text | ~$2/mo at 250 texts |
| Anthropic Claude API | ~$0.003/call | ~$3/mo at 1,000 calls |
| Railway hosting | $5/mo | After free tier |
| **TOTAL** | **~$15/mo** | Your cost per contractor |

**You charge $79/mo → You keep ~$64 per customer. Pure margin.**

At 50 customers: $3,200/month profit.
At 200 customers: $12,800/month profit.

---

## Troubleshooting

**AI isn't answering:**
- Check Railway logs for errors
- Make sure all environment variables are set correctly
- Verify your Twilio webhook URLs are correct

**Not getting text summaries:**
- Make sure CONTRACTOR_PHONE is your real number with country code (+1)
- Check that Twilio has SMS permissions enabled

**Call quality is poor:**
- This is normal on Twilio trial accounts
- Upgrade to a paid Twilio account ($20) for better quality

**Want to test without real calls:**
- Visit `https://YOUR-URL.up.railway.app/health` — should return `{"status": "ok"}`

---

## What's Next?

Once you have 5-10 paying customers:
- [ ] Add a real database (PostgreSQL) instead of in-memory storage
- [ ] Build the full web dashboard (use the React prototype included)
- [ ] Add Stripe for automated billing
- [ ] Add call recording (Twilio supports this, 1 line of code)
- [ ] Add a customer-facing portal where callers get status updates
- [ ] Add multi-language support (Spanish is a big one for trades)

---

*Built with NeverMiss. Stop losing jobs to missed calls.*
