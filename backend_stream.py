"""
backend_stream.py

True streaming voice-agent backend: one WebSocket per call, continuous mic
audio in, continuous agent audio out, real interruption.

Verified against the ACTUAL installed `sarvamai` SDK (not just docs) via
`inspect.signature()` on every method used here:
  - client.speech_to_text_realtime_streaming.connect(...)   [saaras:v3-realtime]
  - stt.send_realtime_audio_input(RealtimeAudioInput(...))
  - stt.recv() -> RealtimeVadSpeechStart/End, RealtimeTranscriptPartial/Final, ...
  - client.chat.completions(...)                            [non-streamed]
  - client.text_to_speech_streaming.connect(...)             [bulbul streaming]
  - tts.configure(...) / tts.convert(text=...) / tts.flush() / tts.recv()

HONESTY NOTE (read this before debugging): I could not run any of the above
against the live Sarvam API myself — my environment's network doesn't reach
api.sarvam.ai. Every method name, parameter, and field name below is taken
directly from the installed SDK's real signatures, not guessed from docs.
The mock-mode message protocol below is IDENTICAL in shape and timing to
live mode, and I tested that path myself end-to-end (see README) — so if
the frontend behaves correctly in mock mode but something breaks in live
mode, the bug is almost certainly in the Sarvam-specific glue in this file,
not in the overall design. The single highest-risk spot is marked below
with "HIGH RISK" — start debugging there.
"""

import os
import re
import uuid
import json
import time
import math
import base64
import struct
import asyncio
import itertools
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
MODE = os.getenv("SARVAM_MODE", "live").strip().lower()
# sarvam-30b has been removed from Sarvam's Chat Completions API.
# sarvam-105b-conversations is a post-trained variant of the 105B flagship,
# specifically built for real-time conversational/voice-agent workloads —
# it trades deep reasoning traces for fast, natural, colloquial replies,
# which is exactly this use case (and should fix "stuck at thinking").
CHAT_MODEL = os.getenv("SARVAM_CHAT_MODEL", "sarvam-105b-conversations").strip()
TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "rohan").strip()
API_KEY = os.getenv("SARVAM_API_KEY")

app = FastAPI()

from scripts import (
    SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, LANGUAGE_VOICES, FLOW, SCRIPTS,
    all_static_lines, build_classification_prompt, build_response_format,
)
import crm

# Rehearsal paths for mock mode: SARVAM_MOCK_PATH=promise|hardship|dispute
MOCK_PATHS = {
    "promise": [
        ("Haan ji, Ravi bol raha hoon.", "confirms", ""),
        ("Main pandrah tareekh tak kar dunga.", "promise_to_pay", "pandrah tareekh"),
        ("Haan bilkul, sahi hai.", "confirms", "pandrah tareekh"),
    ],
    "hardship": [
        ("Haan ji, Ravi bol raha hoon.", "confirms", ""),
        ("Sir is mahine salary late aayi hai, thoda time chahiye.", "hardship_request", "salary delayed"),
        ("Haan, saat din mein ho jayega.", "confirms", ""),
    ],
    "dispute": [
        ("Haan ji, Ravi bol raha hoon.", "confirms", ""),
        ("Nahi ye galat hai, maine pichle hafte hi do hazaar pay kiya tha.", "payment_dispute", "₹2,000 paid last week"),
    ],
}
MOCK_PATH = os.getenv("SARVAM_MOCK_PATH", "promise").strip().lower()

# Cache key is "lang|text" — the same sentence in two languages is two entries.
TTS_CACHE: dict[str, list[str]] = {}


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "static" / "index_stream.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status():
    return {
        "mode": MODE, "has_key": bool(API_KEY),
        "languages": SUPPORTED_LANGUAGES, "default_language": DEFAULT_LANGUAGE,
        "cached_lines": len(TTS_CACHE),
    }


@app.get("/crm", response_class=HTMLResponse)
def crm_dashboard():
    """A real ops dashboard, not raw JSON. Open this in a second tab during the
    demo video — it auto-refreshes, so a ticket appears within ~3s of the
    voice agent creating it, with zero manual action."""
    return (BASE_DIR / "static" / "crm_dashboard.html").read_text(encoding="utf-8")


@app.get("/api/crm/tickets")
def crm_tickets():
    """Raw JSON — kept for scripting/testing. For the demo, use /crm instead."""
    return {"tickets": crm.list_tickets()}


@app.post("/api/crm/reset")
def crm_reset():
    crm.reset_tickets()
    return {"ok": True}


def _extract_json(raw: str):
    """Pull a JSON object out of an LLM reply robustly: try it whole, then
    stripped of markdown fences, then the span between the first { and last }."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for cand in [raw, re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()]:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    a, b = raw.find("{"), raw.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(raw[a:b + 1])
        except json.JSONDecodeError:
            pass
    return None


def pcm16_rms(b64_audio: str) -> float:
    """RMS volume of a base64 16-bit PCM chunk. Used by mock mode's local VAD;
    in live mode Sarvam's own server-side VAD does this job."""
    try:
        raw = base64.b64decode(b64_audio)
    except Exception:
        return 0.0
    n = len(raw) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", raw[: n * 2])
    return (sum((x / 32768.0) ** 2 for x in samples) / n) ** 0.5


@app.on_event("startup")
async def prewarm_tts_cache():
    """Synthesise every fixed line, in EVERY supported language, once at server
    start over a single reused socket. After this a scripted reply costs zero
    network time. Two languages doubles startup (~20-40s) and saves ~1s on
    every single turn thereafter."""
    if MODE != "live" or not API_KEY:
        print(f"[prewarm] skipped (mode={MODE})")
        return
    from sarvamai import AsyncSarvamAI
    client = AsyncSarvamAI(api_subscription_key=API_KEY)
    total = sum(len(all_static_lines(l)) for l in SUPPORTED_LANGUAGES)
    print(f"[prewarm] synthesising {total} lines across {len(SUPPORTED_LANGUAGES)} languages…")
    done = 0
    for lang in SUPPORTED_LANGUAGES:
        voice = LANGUAGE_VOICES.get(lang, TTS_SPEAKER)
        try:
            async with client.text_to_speech_streaming.connect(
                model="bulbul:v3", send_completion_event="true"
            ) as tts:
                await tts.configure(
                    target_language_code=lang,
                    speaker=voice,
                    output_audio_codec="linear16",
                    speech_sample_rate=16000,
                )
                for text in all_static_lines(lang):
                    chunks = []
                    await tts.convert(text=text)
                    await tts.flush()
                    while True:
                        msg = await asyncio.wait_for(tts.recv(), timeout=20)
                        if getattr(msg, "type", None) == "audio":
                            chunks.append(msg.data.audio)
                        else:
                            break
                    if chunks:
                        TTS_CACHE[f"{lang}|{text}"] = chunks
                    done += 1
                    if done % 8 == 0 or done == total:
                        print(f"[prewarm] {done}/{total}")
        except Exception as e:
            print(f"[prewarm] {lang} FAILED ({e}) — that language falls back to live synthesis")
    print(f"[prewarm] cached {len(TTS_CACHE)}/{total} lines")


class CallSession:
    """One instance per connected browser tab. Owns the conversation history,
    the current 'generation' id (bumped on every interruption so stale async
    work can detect it's been superseded and stop), and dispatches to either
    the live Sarvam pipeline or the offline mock pipeline — both speak the
    exact same JSON message protocol to the browser."""

    SPEECH_RMS = 0.02
    SILENCE_HOLD_S = 0.75
    MIN_SPEECH_S = 0.4

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.generation = 0
        self.history = []
        self.closed = False
        self.client = None  # AsyncSarvamAI, set in run_live()

        # --- conversation state (the thing that was missing) ---
        self.state = "opening"   # greeting confirms identity first
        self.no_match_count = 0
        self.slots = {}                   # e.g. {"payment_date": "pandrah tareekh"}
        self._last_filler = None          # so we never play the same filler twice running
        # Live TTS overrides from the on-screen controls. The startup cache was
        # built with (TTS_SPEAKER, pace=1.0); any other combination has to be
        # synthesised live, so we track it explicitly.
        self.pace = 1.0
        self.lang = DEFAULT_LANGUAGE
        self.speaker = LANGUAGE_VOICES.get(self.lang, TTS_SPEAKER)
        self.auto_switch = True      # switch language when STT detects another
        self.call_id = uuid.uuid4().hex[:8]
        self.tickets = []
        # The turn that TRIGGERS escalation is often a bare "haan" — the ticket
        # must carry the substantive reason from earlier in the call, or it
        # routes to the wrong ops queue.
        self.escalation_reason = None

        # mock-mode local VAD state
        self._mock_speaking = False
        self._mock_speech_start = 0.0
        self._mock_last_above = 0.0
        self._mock_step = 0

    def resolve_turn(self, intent: str, details: str):
        """Given a classified intent, decide what the agent says and where the
        conversation goes next. Shared by live and mock so both behave
        identically. Returns (reply_text, requires_review)."""
        sc = SCRIPTS[self.lang]
        if self.state == "closed":
            return sc["closed"], False

        state = FLOW[self.state]
        cfg = state["intents"].get(intent)

        # no-match ladder: restate shorter, then show effort, then hand to a human
        if cfg is None:
            reprompts = sc["reprompts"].get(self.state, [])
            if self.no_match_count < len(reprompts):
                reply = reprompts[self.no_match_count]
                self.no_match_count += 1
                return reply, False
            self.no_match_count = 0
            self.state = "closed"
            return sc["escalation"], True

        self.no_match_count = 0
        if intent in crm.ESCALATION_POLICY:
            self.escalation_reason = intent
            self.slots["reason_detail"] = details or self.slots.get("reason_detail", "")
        if details:
            self.slots["last_detail"] = details

        slot_tmpl = sc.get("replies_slot", {}).get(self.state, {}).get(intent)
        if details and slot_tmpl:
            reply = slot_tmpl.format(slot=details)
            self.state = cfg.get("next_with_slot", cfg["next"])
        else:
            reply = sc["replies"][self.state][intent]
            self.state = cfg["next"]

        return reply, cfg.get("review", False)

    async def _switch_language(self, lang: str, auto: bool):
        """Switching language swaps the whole script AND the voice. Because both
        languages were pre-synthesised at startup, this costs nothing at runtime
        — the reply just comes out of the other half of the cache."""
        if lang not in SUPPORTED_LANGUAGES or lang == self.lang:
            return
        self.lang = lang
        self.speaker = LANGUAGE_VOICES.get(lang, TTS_SPEAKER)
        print(f"[lang] switched to {lang} ({'auto-detected' if auto else 'manual'})")
        await self.send({
            "type": "language_changed", "lang": lang,
            "label": SUPPORTED_LANGUAGES[lang], "auto": auto,
        })

    async def maybe_escalate(self, gen, intent, details, review):
        """The agentic step: a turn needing a human triggers a real tool call
        that writes to the CRM and fires the ops webhook."""
        if not review:
            return None
        reason = self.escalation_reason or intent
        ticket = await crm.create_ticket(
            call_id=self.call_id, intent=reason,
            details=details or self.slots.get("reason_detail", ""),
            language=SUPPORTED_LANGUAGES.get(self.lang, self.lang),
            transcript=[f"{t['speaker']}: {t['text']}" for t in self.history],
        )
        self.tickets.append(ticket)
        await self.send({"type": "crm_ticket", "gen": gen, "ticket": ticket})
        return ticket

    async def send(self, obj: dict):
        if self.closed:
            return
        try:
            await self.ws.send_json(obj)
        except Exception:
            self.closed = True

    async def run(self):
        await self.send({"type": "session_ready", "mode": MODE})
        if MODE == "live":
            if not API_KEY:
                await self.send({"type": "error", "message": "SARVAM_API_KEY not set on server"})
                return
            await self.run_live()
        else:
            await self.greet()
            await self.run_mock()

    # ---------------------------------------------------------------- greet
    async def greet(self):
        self.generation += 1
        gen = self.generation
        await self.send({"type": "greeting_text", "text": SCRIPTS[self.lang]["greeting"], "lang": self.lang})
        self.history.append({"speaker": "agent", "text": SCRIPTS[self.lang]["greeting"]})
        await self.stream_tts(gen, SCRIPTS[self.lang]["greeting"])

    # ----------------------------------------------------------------- mock
    async def run_mock(self):
        try:
            while True:
                msg = await self.ws.receive_json()
                t = msg.get("type")
                if t == "mic_chunk":
                    await self._mock_handle_audio(msg["audio"])
                elif t == "client_barge_in":
                    self.generation += 1
                elif t == "update_tts_config":
                    self.speaker = msg.get("speaker", self.speaker)
                    self.pace = float(msg.get("pace", self.pace))
                elif t == "set_language":
                    await self._switch_language(msg.get("lang", DEFAULT_LANGUAGE), auto=False)
                elif t == "set_auto_switch":
                    self.auto_switch = bool(msg.get("enabled", True))
                elif t == "end_call":
                    break
        except WebSocketDisconnect:
            pass

    async def _mock_handle_audio(self, audio_b64: str):
        rms = pcm16_rms(audio_b64)
        now = time.monotonic()
        if rms > self.SPEECH_RMS:
            if not self._mock_speaking:
                self._mock_speaking = True
                self._mock_speech_start = now
                self.generation += 1  # any fresh speech onset can cancel a stale reply
                await self.send({"type": "vad_speech_start"})
            self._mock_last_above = now
        else:
            if self._mock_speaking:
                silence_for = now - self._mock_last_above
                speech_dur = now - self._mock_speech_start
                if speech_dur > self.MIN_SPEECH_S and silence_for > self.SILENCE_HOLD_S:
                    self._mock_speaking = False
                    await self.send({"type": "vad_speech_end"})
                    path = MOCK_PATHS.get(MOCK_PATH, MOCK_PATHS["promise"])
                    if self._mock_step < len(path):
                        transcript, intent, details = path[self._mock_step]
                        self._mock_step += 1
                    else:
                        transcript, intent, details = ("Theek hai, dhanyavaad.", "unclear", "")
                    await self.send({"type": "final_transcript", "text": transcript})
                    self.history.append({"speaker": "borrower", "text": transcript})
                    self.generation += 1
                    gen = self.generation
                    asyncio.create_task(self._respond_mock(gen, intent, details))

    async def _respond_mock(self, gen, intent, details):
        await asyncio.sleep(1.0)  # simulate LLM latency
        if gen != self.generation:
            return
        reply_text, review = self.resolve_turn(intent, details)
        await self.send({
            "type": "agent_text_done", "gen": gen, "full_text": reply_text,
            "intent": intent, "details": details,
            "requires_human_review": review, "state": self.state, "lang": self.lang,
        })
        await self.maybe_escalate(gen, intent, details, review)
        self.history.append({"speaker": "agent", "text": reply_text})
        if gen != self.generation:
            return
        await self.stream_tts(gen, reply_text)

    # ----------------------------------------------------------------- live
    async def run_live(self):
        from sarvamai import AsyncSarvamAI
        self.client = AsyncSarvamAI(api_subscription_key=API_KEY)

        # HIGH RISK: this is the one block I could not test against the live
        # API. If the call never connects, print(repr(e)) here first.
        async with self.client.speech_to_text_realtime_streaming.connect(
            language_code="auto",     # was hi-IN — hardcoding one language wrecks
                                      # transcription the moment the caller code-mixes,
                                      # which is most of the time on a real Indian call.
            model="saaras:v3-realtime",
            stream_type="fast",
            mode="codemix",           # was "transcribe" — codemix is built for
                                      # Hindi/English switching mid-sentence.
            endpointing="vad",
            encoding="linear16",
            sample_rate="16000",
            # ---- Sarvam server-side VAD (the SECOND interrupt path) ----
            # silence_duration_ms: dead time between you stopping and the agent
            #   starting. Lower = snappier, too low = it cuts you off mid-pause.
            # min_speech_duration_ms: how long a sound must last to count as
            #   speech at all. RAISE this if background noise interrupts.
            # threshold: VAD confidence, 0..1. RAISE for a noisy room.
            silence_duration_ms=os.getenv("VAD_SILENCE_MS", "400"),
            min_speech_duration_ms=os.getenv("VAD_MIN_SPEECH_MS", "250"),
            threshold=os.getenv("VAD_THRESHOLD", "0.6"),
        ) as stt:
            self.stt = stt
            reader_task = asyncio.create_task(self._live_stt_reader())
            await self.greet()
            try:
                while True:
                    msg = await self.ws.receive_json()
                    await self._handle_live_client_msg(msg)
            except WebSocketDisconnect:
                pass
            finally:
                reader_task.cancel()
                try:
                    from sarvamai.types.realtime_end import RealtimeEnd
                    await stt.send_realtime_end(RealtimeEnd())
                except Exception:
                    pass

    async def _handle_live_client_msg(self, msg):
        t = msg.get("type")
        if t == "mic_chunk":
            from sarvamai.types.realtime_audio_input import RealtimeAudioInput
            await self.stt.send_realtime_audio_input(RealtimeAudioInput(audio=msg["audio"]))
        elif t == "client_barge_in":
            self.generation += 1  # fast local hint from the browser
        elif t == "update_tts_config":
            self.speaker = msg.get("speaker", self.speaker)
            self.pace = float(msg.get("pace", self.pace))
            print(f"[tts] now speaker={self.speaker} pace={self.pace}")
        elif t == "set_language":
            await self._switch_language(msg.get("lang", DEFAULT_LANGUAGE), auto=False)
        elif t == "set_auto_switch":
            self.auto_switch = bool(msg.get("enabled", True))
        elif t == "end_call":
            raise WebSocketDisconnect()

    async def _live_stt_reader(self):
        try:
            while True:
                msg = await self.stt.recv()
                ev = getattr(msg, "event", None)

                if ev == "vad.speech_start":
                    self.generation += 1  # authoritative barge-in: cancel anything in flight
                    await self.send({"type": "vad_speech_start"})

                elif ev == "vad.speech_end":
                    await self.send({"type": "vad_speech_end"})
                    # Fire a cached filler immediately. Costs nothing (already
                    # synthesised at startup) and covers STT finalisation + LLM
                    # classification, which is where the dead air used to be.
                    asyncio.create_task(self._play_filler(self.generation))

                elif ev == "transcript.partial":
                    await self.send({"type": "partial_transcript", "text": getattr(msg, "text", "")})

                elif ev == "transcript.final":
                    text = getattr(msg, "text", "")
                    # Sarvam returns the detected language per utterance. If the
                    # borrower answered in another supported language, follow them.
                    detected = getattr(msg, "language", None)
                    if self.auto_switch and detected:
                        for code in SUPPORTED_LANGUAGES:
                            if detected.lower().startswith(code.split("-")[0]):
                                await self._switch_language(code, auto=True)
                                break
                    await self.send({"type": "final_transcript", "text": text})
                    self.history.append({"speaker": "borrower", "text": text})
                    self.generation += 1
                    gen = self.generation
                    asyncio.create_task(self._respond_live(gen))

                elif ev == "error":
                    await self.send({"type": "error", "message": getattr(msg, "message", "STT error")})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self.send({"type": "error", "message": f"STT reader failed: {e}"})

    async def _respond_live(self, gen):
        try:
            # Capture the state at the moment this turn started — the classifier's
            # options and the transition must both use the same state.
            turn_state = self.state
            if turn_state == "closed":
                reply_text, review = self.resolve_turn("unclear", "")
                await self.send({
                    "type": "agent_text_done", "gen": gen, "full_text": reply_text,
                    "intent": "closed", "details": "", "requires_human_review": False,
                    "state": self.state,
                })
                await self.stream_tts(gen, reply_text)
                return

            # Only the last few turns are sent — a long history is a latency cost
            # for a classification task that only needs recent context.
            messages = [{"role": "system", "content": build_classification_prompt(turn_state)}]
            for turn in self.history[-4:]:
                role = "assistant" if turn["speaker"] == "agent" else "user"
                messages.append({"role": role, "content": turn["text"]})

            try:
                resp = await asyncio.wait_for(
                    self.client.chat.completions(
                        model=CHAT_MODEL,
                        messages=messages,
                        temperature=0.2,
                        # reasoning_effort has NO "off" value — it accepts only
                        # low/medium/high, and passing None means "omit the
                        # parameter", which leaves the SERVER DEFAULT (thinking
                        # ON) in force. "low" is the actual minimum.
                        reasoning_effort="low",
                        # Must be large enough to cover reasoning tokens AND the
                        # JSON answer. Too small and reasoning eats the whole
                        # budget, content comes back empty, and every turn
                        # silently degrades to "unclear".
                        max_tokens=1200,
                        # Schema-enforced structured output — Sarvam's docs state this
                        # GUARANTEES the output matches the schema, unlike asking for
                        # JSON in the prompt and hoping. The Python SDK doesn't expose
                        # response_format as a first-class param yet; Sarvam's own docs
                        # say to pass it this way. Scoped to the CURRENT state, so the
                        # model can only pick an intent that makes sense right here.
                        request_options={
                            "additional_body_parameters": {
                                "response_format": build_response_format(turn_state),
                            }
                        },
                    ),
                    timeout=15,  # was unbounded — a hang here used to look like a
                                 # frozen call with zero feedback. Now it fails loud
                                 # and fast instead of leaving you staring at "Thinking…".
                )
            except asyncio.TimeoutError:
                if gen == self.generation:
                    await self.send({
                        "type": "error",
                        "message": f"Chat completion timed out after 15s (model={CHAT_MODEL})",
                    })
                return

            if gen != self.generation:
                return  # interrupted while the LLM was thinking

            message = resp.choices[0].message
            raw = message.content or ""
            reasoning = getattr(message, "reasoning_content", None) or ""

            if not raw.strip():
                print(f"[gen {gen}] EMPTY content. reasoning_content was: {reasoning[:300]!r}")
                await self.send({
                    "type": "error",
                    "message": "Model returned empty content (see server console for reasoning_content dump)."
                })

            parsed = _extract_json(raw)
            if parsed is None:
                # Never fail silently here. A classification that quietly becomes
                # "unclear" looks like a conversation bug when it is really an
                # API/parsing bug — that distinction has to be visible.
                print(f"[gen {gen}] !! CLASSIFY FAILED — could not parse model output")
                print(f"[gen {gen}]    raw content      : {raw[:400]!r}")
                print(f"[gen {gen}]    reasoning_content: {reasoning[:200]!r}")
                parsed = {}
            intent = parsed.get("intent", "unclear")
            details = parsed.get("details", "") or ""
            if intent not in FLOW[turn_state]["intents"]:
                if intent != "unclear":
                    print(f"[gen {gen}] !! Unexpected intent {intent!r} in state {turn_state!r}. Raw: {raw[:300]!r}")
                else:
                    print(f"[gen {gen}] classified as 'unclear' in state {turn_state!r} "
                          f"(borrower said: {self.history[-2]['text'][:80]!r})" if len(self.history) >= 2 else "")
                intent = "unclear"
            else:
                print(f"[gen {gen}] intent={intent} details={details!r} state={turn_state}->{FLOW[turn_state]['intents'][intent]['next']}")

            reply_text, review = self.resolve_turn(intent, details)
            review = review or parsed.get("requires_human_review", False)
            await self.send({
                "type": "agent_text_done", "gen": gen, "full_text": reply_text,
                "intent": intent, "details": details,
                "requires_human_review": review,
                "state": self.state, "lang": self.lang,
            })
            await self.maybe_escalate(gen, intent, details, review)
            self.history.append({"speaker": "agent", "text": reply_text})

            if gen != self.generation:
                return
            await self.stream_tts(gen, reply_text)
        except Exception as e:
            await self.send({"type": "error", "message": f"Response generation failed: {e}"})

    async def _play_filler(self, gen: int):
        """Instant, zero-latency acknowledgement while the real turn is computed.
        Picks from the pool for whichever state the call is in, and never repeats
        the previous one — hearing the identical filler twice in a row is the
        thing that makes an agent sound robotic. Skipped silently if the cache
        is cold (mock mode, or prewarm failed): never worth adding a network
        round-trip to a latency-hiding trick."""
        import random
        f = SCRIPTS[self.lang]["fillers"]
        pool = f.get(self.state) or f["default"]
        choices = [l for l in pool if l != self._last_filler] or pool
        line = random.choice(choices)
        self._last_filler = line
        chunks = TTS_CACHE.get(f"{self.lang}|{line}")
        if not chunks:
            return
        for chunk in chunks:
            if gen != self.generation:
                return  # borrower started talking again — drop it
            await self.send({"type": "agent_audio_chunk", "gen": gen, "audio": chunk})

    # ------------------------------------------------------------- TTS (both)
    async def stream_tts(self, gen: int, text: str):
        if MODE == "mock":
            await self._mock_stream_tts(gen, text)
            return

        # FAST PATH: scripted lines were synthesised at startup, so there is no
        # network call here at all — push the cached PCM straight to the browser.
        # Cache is only valid for the exact voice/pace it was built with at
        # startup. Changing either in the UI correctly falls through to live
        # synthesis — the UI badge tells the user that's happening.
        default_voice = LANGUAGE_VOICES.get(self.lang, TTS_SPEAKER)
        using_defaults = (self.speaker == default_voice and abs(self.pace - 1.0) < 1e-6)
        cached = TTS_CACHE.get(f"{self.lang}|{text}") if using_defaults else None
        if cached:
            for chunk in cached:
                if gen != self.generation:
                    return
                await self.send({"type": "agent_audio_chunk", "gen": gen, "audio": chunk})
            if gen == self.generation:
                await self.send({"type": "agent_audio_done", "gen": gen})
            return

        # HIGH RISK: exact completion-signal shape from tts.recv() after
        # flush() is inferred from the SDK's declared return type
        # (Union[AudioOutput, ErrorResponse, EventResponse]) but untested
        # live. If audio doesn't start/stop cleanly per reply, add
        # `print(repr(msg))` in the loop below and check what actually
        # comes back, then adjust the `is_audio` check accordingly.
        try:
            async with self.client.text_to_speech_streaming.connect(
                model="bulbul:v3", send_completion_event="true"
            ) as tts:
                configure_kwargs = dict(
                    target_language_code=self.lang,
                    speaker=self.speaker,   # live-controllable from the UI panel
                    pace=self.pace,         # bulbul:v3 range is 0.5-2.0
                    output_audio_codec="linear16",
                    speech_sample_rate=16000,
                )
                # min_buffer_size=20 was reverted: added last for latency, but its
                # addition is exactly when "Input parameters has to be a valid
                # dictionary" started appearing — strong circumstantial evidence
                # it's the trigger. Re-add only after confirming against a live
                # call with the improved error detail below turned on.
                # configure_kwargs["min_buffer_size"] = 20
                await tts.configure(**configure_kwargs)
                await tts.convert(text=text)
                await tts.flush()

                while True:
                    msg = await asyncio.wait_for(tts.recv(), timeout=10)
                    if gen != self.generation:
                        continue  # drain silently, this reply was interrupted

                    msg_type = getattr(msg, "type", None)
                    if msg_type == "audio":
                        await self.send({
                            "type": "agent_audio_chunk", "gen": gen,
                            "audio": msg.data.audio,
                        })
                    elif msg_type == "error":
                        data = getattr(msg, "data", None)
                        detail = {
                            "message": getattr(data, "message", None),
                            "code": getattr(data, "code", None),
                            "details": getattr(data, "details", None),
                            "request_id": getattr(data, "request_id", None),
                        }
                        print(f"[gen {gen}] TTS ERROR — full detail: {detail}")
                        await self.send({
                            "type": "error",
                            "message": f"TTS error: {detail['message']} "
                                       f"(code={detail['code']}, details={detail['details']})",
                        })
                        break
                    else:
                        break  # 'event' — completion signal, this utterance's audio is done
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            await self.send({"type": "error", "message": f"TTS failed: {e}"})
        finally:
            if gen == self.generation:
                await self.send({"type": "agent_audio_done", "gen": gen})

    async def _mock_stream_tts(self, gen: int, text: str):
        """Fakes a real streaming TTS reply: a low tone chunked into ~200ms
        pieces sent progressively, respecting interruption exactly like the
        live path — lets the whole frontend playback/barge-in pipeline be
        tested with zero API calls."""
        sr = 16000
        chunk_s = 0.2
        n_chunks = max(4, min(10, len(text) // 8))
        for i in range(n_chunks):
            if gen != self.generation:
                return
            n = int(sr * chunk_s)
            samples = [int(0.12 * math.sin(2 * math.pi * 220 * ((i * n + j) / sr)) * 32767) for j in range(n)]
            raw = struct.pack(f"<{n}h", *samples)
            await self.send({
                "type": "agent_audio_chunk", "gen": gen,
                "audio": base64.b64encode(raw).decode(),
            })
            await asyncio.sleep(chunk_s * 0.6)
        if gen == self.generation:
            await self.send({"type": "agent_audio_done", "gen": gen})


@app.websocket("/ws/call")
async def ws_call(websocket: WebSocket):
    await websocket.accept()
    session = CallSession(websocket)
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        session.closed = True