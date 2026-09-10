# Sovereign Voice Collections Agent — Sarvam AI

A real-time, multilingual voice agent for NBFC loan collections, with an
agentic backend that creates CRM tickets and notifies operations when a call
needs a human.

Built on **Saaras v3 (STT)**, **Sarvam-105B (LLM)**, and **Bulbul v3 (TTS)**.
Speaks **Hindi and Marathi**, handles **Hinglish code-mixing**, supports
**barge-in** (interrupt the agent mid-sentence), and escalates to a human
through a real downstream tool call.
## Quick start

## 📂 Architecture & Collateral
- 🏗️ **System Architecture:** [`docs/architecture/Sarvam Architecture Diagram.pdf`](docs/architecture/Sarvam%20Architecture%20Diagram.pdf)
- 🔀 **Decision State Machine Flowchart:** [`docs/architecture/Sarvam Flowchart.pdf`](docs/architecture/Sarvam%20Flowchart.pdf)
- 💼 **Business & Executive Strategy Writeup:** [`docs/business/Business Writeup - Sarvam.pdf`](docs/business/Business%20Writeup%20-%20Sarvam.pdf)


```bash
cd src
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                # then edit .env and paste your API key
```

### Step 1 — Rehearse with no API calls (recommended first run)

Set this in `.env`:
```
SARVAM_MODE=mock
SARVAM_MOCK_PATH=promise            # or: hardship | dispute
```
Then:
```bash
uvicorn backend_stream:app --port 8000
```
Open **http://localhost:8000** in **Chrome**. Click **Start call**, allow the
microphone, and speak. Mock mode plays a scripted conversation, so you can test
your mic, the barge-in sliders, and the whole UI without spending a single API
call.

### Step 2 — Run live

In `.env`:
```
SARVAM_MODE=live
SARVAM_API_KEY=your_real_key
```
```bash
uvicorn backend_stream:app --port 8000
```

**Wait for the pre-warm to finish before you start a call.** The console prints:
```
[prewarm] synthesising 64 lines across 2 languages…
[prewarm] 8/64
...
[prewarm] cached 64/64 lines
```
This takes roughly 20–40 seconds and only happens once, at startup. It
pre-synthesises every scripted line in both languages so that during the call
the agent's replies play with **zero TTS network latency**.

### Step 3 — Talk to it

| You say (Hindi) | Agent does |
|---|---|
| "Haan ji, Ravi bol raha hoon." | Confirms identity, states the overdue amount, asks for a date |
| "Main pandrah tareekh tak kar dunga." | Repeats **your** date back and asks you to confirm |
| "Haan bilkul, sahi hai." | Logs the commitment, closes the call |

For the hardship path say *"Salary late aayi hai, thoda time chahiye"* — the
agent offers a 7-day extension, and on acceptance **creates a CRM ticket**.
For a dispute say *"Maine pichle hafte hi pay kiya tha"* — immediate escalation.

Full run-by-run script, including Marathi lines and the barge-in demo:
**`docs/DEMO_SCRIPT.md`**.

### Step 4 — Show the downstream system

Open a second tab at **http://localhost:8000/api/crm/tickets** to show the
tickets the conversation created. They also appear live in the right-hand
console under *Downstream actions (CRM)*.

To reset between takes:
```bash
curl -X POST http://localhost:8000/api/crm/reset
```

---

## Important notes

**Use headphones.** The microphone stays open for the entire call so you can
interrupt the agent. Without headphones the agent's own voice reaches your mic
and can trigger a false interruption.

**Use Chrome.** The mic capture uses AudioWorklet; Chrome is the most reliable.

**Run from inside `src/`.** `backend_stream.py` imports `scripts.py` and
`crm.py` from the same directory.

**If it interrupts on background noise:** open the **⚙︎ Live controls** panel on
the call screen and raise *Interrupt sensitivity*. No restart needed. If the
room is very loud, untick *Allow interruption* entirely.

---
**The LLM never writes what the borrower hears.** It classifies intent and
extracts one slot value against a strict JSON schema. Every spoken word comes
from a pre-approved script in `scripts.py`. This is a compliance requirement for
a lender — and it is also what makes the zero-latency audio cache possible,
since every possible line is known in advance.

### Files

| File | What it does |
|---|---|
| `src/backend_stream.py` | WebSocket server, Sarvam integration, call session state |
| `src/scripts.py` | Conversation state machine + Hindi/Marathi scripts |
| `src/crm.py` | The downstream tool: ticket creation, routing policy, webhook |
| `src/static/index_stream.html` | Call UI, agent console, live control panel |

---

## Which Sarvam APIs are used, and why

| API | Where | Why this one |
|---|---|---|
| **Saaras v3 realtime STT** (`saaras:v3-realtime`) | Continuous mic stream | Streaming with server-side VAD, so the agent knows when the borrower started and stopped speaking without a fixed timeout. `language_code="auto"` + `mode="codemix"` because real callers switch between Hindi, Marathi and English mid-sentence — a single-language transcriber degrades badly on exactly the calls this product exists to serve. |
| **Chat Completions** (`sarvam-105b-conversations`) | Intent classification | The conversational variant is post-trained for real-time voice workloads rather than deep reasoning traces. `reasoning_effort=None` and a small token cap keep the turn fast. Uses `response_format` with a **strict JSON schema**, which guarantees a valid, in-vocabulary intent rather than hoping the model returns parseable JSON. |
| **Bulbul v3 TTS** (streaming) | Agent speech | Streaming so audio starts before the full clip exists. Indic-native voices in both `hi-IN` and `mr-IN`. Every fixed line is pre-synthesised at startup and cached. |

Sovereignty note: borrower PII — name, loan amount, repayment history — never
leaves Indian infrastructure at any point in this pipeline. For an RBI-regulated
lender that is a procurement blocker, not a preference.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SARVAM_API_KEY` | — | Required in live mode |
| `SARVAM_MODE` | `live` | `mock` runs a scripted conversation with no API calls |
| `SARVAM_MOCK_PATH` | `promise` | `promise` / `hardship` / `dispute` |
| `SARVAM_CHAT_MODEL` | `sarvam-105b-conversations` | |
| `SARVAM_TTS_SPEAKER` | `rohan` | Per-language voices in `scripts.py` |
| `CRM_WEBHOOK_URL` | — | Optional: n8n / Make / webhook.site endpoint |
| `VAD_SILENCE_MS` | `400` | Pause length before the agent takes its turn |
| `VAD_MIN_SPEECH_MS` | `250` | Raise in a noisy room |
| `VAD_THRESHOLD` | `0.6` | Raise in a noisy room |

---

## Known limitations

- **Browser mic, not telephony.** Production swaps the transport for a SIP trunk
  (Exotel/Twilio) at 8kHz mulaw — Saaras handles 8kHz natively, so it is a
  transport change, not a rewrite.
- **Latency floor ~2–4s per turn.** Inherent to a cascaded STT→LLM→TTS pipeline;
  Sarvam's own production guide documents the same range. Sub-second requires a
  native speech-to-speech model.
- **Hand-rolled orchestration.** Production should use Pipecat or LiveKit with
  Sarvam's official plugins, which already solve reconnection and turn-taking.
- **CRM is a local JSON file.** Swap `crm.create_ticket()` for a Salesforce /
  LeadSquared / core-banking call; nothing else changes.
