"""
scripts.py

Two things live here, deliberately separated:

  FLOW    — the conversation state machine. Language-NEUTRAL. States, the
            intents valid in each state, and where each intent transitions to.
            Intent descriptions stay in English because they are only ever read
            by the classifier, never spoken.

  SCRIPTS — every word the borrower actually hears, per language. Adding a
            third language means adding one block here and nothing else.

Design note for the write-up: the LLM never generates spoken text. It only
classifies intent and extracts a slot value. Everything spoken comes from these
pre-approved scripts, which is both a compliance requirement for a lender and
the reason every line can be pre-synthesised at startup for zero-latency
playback.
"""

SUPPORTED_LANGUAGES = {
    "hi-IN": "Hindi",
    "mr-IN": "Marathi",
}
DEFAULT_LANGUAGE = "hi-IN"

# Per-language voice. Sarvam publishes per-language speaker recommendations —
# voice quality genuinely varies by language, so this is worth checking against
# the Voices page rather than assuming one speaker suits every language.
LANGUAGE_VOICES = {
    "hi-IN": "rohan",
    "mr-IN": "rohan",
}


# ---------------------------------------------------------------- FLOW
FLOW = {
    "opening": {
        "context": "The agent has greeted the borrower and asked whether it is speaking to Ravi Kumar.",
        "intents": {
            "confirms": {
                "desc": "confirms identity — yes, speaking, haan, ho, bolat aahe, Ravi here",
                "next": "ask_payment_date",
            },
            "denies": {
                "desc": "says this is the wrong person or wrong number",
                "next": "closed",
                "review": True,
            },
            "asks_details": {
                "desc": "asks who is calling or what this is regarding",
                "next": "opening",
            },
            "refuses_or_busy": {
                "desc": "is busy, cannot talk now, or asks to be called later",
                "next": "closed",
            },
        },
    },
    "ask_payment_date": {
        "context": "The agent has told the borrower their EMI is overdue and asked by when they can pay.",
        "intents": {
            "promise_to_pay": {
                "desc": "commits to paying, with or without naming a date",
                "next": "ask_payment_date",
                "next_with_slot": "confirm_ptp",
            },
            "hardship_request": {
                "desc": "cannot pay right now and wants more time — salary not credited/delayed, job loss, medical emergency, temporary money problem. Examples: 'salary nahi aayi', 'abhi payment nahi kar paunga', 'thoda time chahiye'",
                "next": "offer_extension",
            },
            "payment_dispute": {
                "desc": "says the amount is wrong, or that they already paid",
                "next": "closed",
                "review": True,
            },
            "asks_details": {
                "desc": "asks about the loan — how much is due, which loan, since when, late fee",
                "next": "ask_payment_date",
            },
            "refuses_or_busy": {
                "desc": "is busy or cannot talk right now, asks to be called later",
                "next": "closed",
            },
            "refuses_to_pay": {
                "desc": "flatly refuses to pay or says they will not pay at all — distinct from being busy or asking for more time",
                "next": "closed",
                "review": True,
            },
        },
    },
    "confirm_ptp": {
        "context": "The agent has read back the payment date the borrower gave and asked them to confirm it.",
        "intents": {
            "confirms": {
                "desc": "confirms, agrees — yes / haan / ho / barobar / sahi hai",
                "next": "closed",
                "review": True,
            },
            "denies": {
                "desc": "says no, that is wrong, or corrects the date",
                "next": "ask_payment_date",
            },
            "hardship_request": {
                "desc": "now says they cannot manage that date after all, asks for more time",
                "next": "offer_extension",
            },
        },
    },
    "offer_extension": {
        "context": "The agent has offered a seven-day extension and asked whether that is enough.",
        "intents": {
            "confirms": {
                "desc": "accepts the seven-day extension",
                "next": "closed",
                "review": True,
            },
            "denies": {
                "desc": "says seven days is not enough, or needs longer",
                "next": "closed",
                "review": True,
            },
        },
    },
    "closed": {
        "context": "The call has reached its conclusion.",
        "intents": {},
    },
}


# ---------------------------------------------------------------- SCRIPTS
SCRIPTS = {
    # ============================ HINDI ============================
    "hi-IN": {
        "greeting": "Namaste, main SecureFin se bol raha hoon. Kya main Ravi Kumar ji se baat kar raha hoon?",
        "closed": "Dhanyavaad, aapka din shubh ho.",
        "escalation": (
            "Maaf kijiye, main aapki baat theek se samajh nahi paa raha. "
            "Main aapko ek officer se jod raha hoon, woh aapki madad karenge."
        ),
        # Keep fillers ~1-1.5s spoken. Browser audio chunks queue back to back,
        # so an over-long filler stops hiding latency and starts causing it.
        "fillers": {
            "opening": ["Ji, sun raha hoon…", "Haan ji…", "Ji, boliye…"],
            "ask_payment_date": [
                "Ji, ek second…", "Haan ji, main dekh raha hoon…",
                "Ek minute ji, note kar raha hoon…", "Ji, main check kar raha hoon…",
            ],
            "confirm_ptp": ["Ji, ek second…", "Haan ji…", "Ek minute ji…"],
            "offer_extension": ["Ji, ek second…", "Haan ji, dekh raha hoon…", "Ek minute ji…"],
            "default": ["Ji, ek second…", "Haan ji…", "Ek minute ji…"],
        },
        "replies": {
            "opening": {
                "confirms": "Dhanyavaad. Aapka teen hazaar paanch sau rupaye ka EMI chaudah din se baaki hai. Aap kab tak payment kar payenge?",
                "denies": "Maaf kijiye, shayad galat number lag gaya. Aapka samay lene ke liye dhanyavaad. Namaskar.",
                "asks_details": "Main SecureFin se bol raha hoon, aapke personal loan ke EMI ke baare mein. Kya main Ravi ji se baat kar raha hoon?",
                "refuses_or_busy": "Koi baat nahi. Main kal isi samay dobara call karunga. Namaskar.",
            },
            "ask_payment_date": {
                "promise_to_pay": "Theek hai. Aap kaunsi tareekh tak payment kar denge?",
                "hardship_request": "Samajh sakta hoon. Main aapko saat din ka extension de sakta hoon. Kya itne mein ho jayega?",
                "payment_dispute": "Achha, maine note kar liya. Hamari verification team chaubis ghante mein aapko call karegi. Dhanyavaad.",
                "asks_details": "Ji, teen hazaar paanch sau rupaye ka EMI chaudah din se baaki hai. Aap kab tak jama kar payenge?",
                "refuses_or_busy": "Koi baat nahi. Main kal isi samay dobara call karunga. Namaskar.",
                "refuses_to_pay": "Theek hai, maine note kar liya hai. Ek senior officer aapse baat karenge. Dhanyavaad, namaskar.",
            },
            "confirm_ptp": {
                "confirms": "Bahut badhiya. Maine aapka commitment note kar liya hai, aapko reminder SMS aa jayega. Dhanyavaad, namaskar.",
                "denies": "Koi baat nahi. Toh aap kaunsi tareekh tak payment kar payenge?",
                "hardship_request": "Samajh sakta hoon. Main aapko saat din ka extension de sakta hoon. Kya itne mein ho jayega?",
            },
            "offer_extension": {
                "confirms": "Theek hai, maine saat din ka extension note kar diya hai. Ek officer chaubis ghante mein aapko confirm karega. Dhanyavaad.",
                "denies": "Samajh gaya. Main aapka case senior officer ko bhej raha hoon, woh aapse baat karenge. Dhanyavaad.",
            },
        },
        "replies_slot": {
            "ask_payment_date": {
                "promise_to_pay": "Theek hai. Toh aap {slot} tak payment kar denge — sahi samjha maine?",
            },
        },
        "reprompts": {
            "opening": [
                "Maaf kijiye — kya main Ravi Kumar ji se baat kar raha hoon?",
                "Maaf kijiye, main sun nahi paaya. Kya aap Ravi Kumar ji hain?",
            ],
            "ask_payment_date": [
                "Maaf kijiye — aap kaunsi tareekh tak payment kar payenge?",
                "Maaf kijiye, main theek se sun nahi paaya. Aap kis tareekh ko payment karenge?",
            ],
            "confirm_ptp": [
                "Maaf kijiye — kya ye tareekh sahi hai?",
                "Maaf kijiye, main samajh nahi paaya. Kya aap is tareekh ko payment kar denge?",
            ],
            "offer_extension": [
                "Maaf kijiye — kya saat din theek rahenge?",
                "Maaf kijiye, main samajh nahi paaya. Saat din mein payment ho payega?",
            ],
        },
    },

    # ============================ MARATHI ============================
    # Written in Devanagari, which is Marathi's native script and the most
    # reliable input for Bulbul's mr-IN voice.
    # >>> Have a native speaker read these once before recording. They are
    # >>> grammatical, but a native ear will catch anything that sounds stiff.
    "mr-IN": {
        "greeting": "नमस्कार, मी सिक्युअरफिन मधून बोलत आहे. मी रवी कुमार यांच्याशी बोलत आहे का?",
        "closed": "धन्यवाद, तुमचा दिवस चांगला जावो.",
        "escalation": (
            "क्षमस्व, मला तुमचं म्हणणं नीट समजत नाहीये. "
            "मी तुम्हाला एका अधिकाऱ्याशी जोडतो, ते तुम्हाला मदत करतील."
        ),
        "fillers": {
            "opening": ["हो, ऐकतोय…", "हो जी…", "हो, बोला…"],
            "ask_payment_date": [
                "हो, एक सेकंद…", "हो, मी बघतोय…",
                "एक मिनिट, नोंद घेतोय…", "हो, मी तपासतोय…",
            ],
            "confirm_ptp": ["हो, एक सेकंद…", "हो जी…", "एक मिनिट…"],
            "offer_extension": ["हो, एक सेकंद…", "हो, बघतोय…", "एक मिनिट…"],
            "default": ["हो, एक सेकंद…", "हो जी…", "एक मिनिट…"],
        },
        "replies": {
            "opening": {
                "confirms": "धन्यवाद. तुमचा तीन हजार पाचशे रुपयांचा ईएमआय चौदा दिवसांपासून थकीत आहे. तुम्ही कधीपर्यंत पेमेंट करू शकाल?",
                "denies": "क्षमस्व, बहुतेक चुकीचा नंबर लागला. तुमचा वेळ दिल्याबद्दल धन्यवाद. नमस्कार.",
                "asks_details": "मी सिक्युअरफिन मधून बोलतोय, तुमच्या पर्सनल लोनच्या ईएमआय बद्दल. मी रवी जी यांच्याशी बोलतोय का?",
                "refuses_or_busy": "काही हरकत नाही. मी उद्या याच वेळी पुन्हा कॉल करेन. नमस्कार.",
            },
            "ask_payment_date": {
                "promise_to_pay": "ठीक आहे. तुम्ही कोणत्या तारखेपर्यंत पेमेंट कराल?",
                "hardship_request": "मी समजू शकतो. मी तुम्हाला सात दिवसांची मुदतवाढ देऊ शकतो. एवढ्यात होईल का?",
                "payment_dispute": "बरं, मी नोंद घेतली आहे. आमची व्हेरिफिकेशन टीम चोवीस तासांत तुम्हाला कॉल करेल. धन्यवाद.",
                "asks_details": "होय, तीन हजार पाचशे रुपयांचा ईएमआय चौदा दिवसांपासून थकीत आहे. तुम्ही कधीपर्यंत भरू शकाल?",
                "refuses_or_busy": "काही हरकत नाही. मी उद्या याच वेळी पुन्हा कॉल करेन. नमस्कार.",
                "refuses_to_pay": "ठीक आहे, मी नोंद घेतली आहे. एक वरिष्ठ अधिकारी तुमच्याशी बोलतील. धन्यवाद, नमस्कार.",
            },
            "confirm_ptp": {
                "confirms": "उत्तम. मी तुमची नोंद घेतली आहे, तुम्हाला रिमाइंडर एसएमएस येईल. धन्यवाद, नमस्कार.",
                "denies": "काही हरकत नाही. मग तुम्ही कोणत्या तारखेपर्यंत पेमेंट करू शकाल?",
                "hardship_request": "मी समजू शकतो. मी तुम्हाला सात दिवसांची मुदतवाढ देऊ शकतो. एवढ्यात होईल का?",
            },
            "offer_extension": {
                "confirms": "ठीक आहे, मी सात दिवसांची मुदतवाढ नोंदवली आहे. एक अधिकारी चोवीस तासांत तुम्हाला कन्फर्म करेल. धन्यवाद.",
                "denies": "समजलं. मी तुमची केस वरिष्ठ अधिकाऱ्याकडे पाठवत आहे, ते तुमच्याशी बोलतील. धन्यवाद.",
            },
        },
        "replies_slot": {
            "ask_payment_date": {
                "promise_to_pay": "ठीक आहे. म्हणजे तुम्ही {slot} पर्यंत पेमेंट कराल — बरोबर ना?",
            },
        },
        "reprompts": {
            "opening": [
                "क्षमस्व — मी रवी कुमार यांच्याशी बोलतोय का?",
                "क्षमस्व, मला ऐकू आलं नाही. तुम्ही रवी कुमार आहात का?",
            ],
            "ask_payment_date": [
                "क्षमस्व — तुम्ही कोणत्या तारखेपर्यंत पेमेंट कराल?",
                "क्षमस्व, मला नीट ऐकू आलं नाही. तुम्ही कोणत्या तारखेला पेमेंट कराल?",
            ],
            "confirm_ptp": [
                "क्षमस्व — ही तारीख बरोबर आहे का?",
                "क्षमस्व, मला समजलं नाही. तुम्ही या तारखेला पेमेंट कराल का?",
            ],
            "offer_extension": [
                "क्षमस्व — सात दिवस ठीक राहतील का?",
                "क्षमस्व, मला समजलं नाही. सात दिवसांत पेमेंट होईल का?",
            ],
        },
    },
}


def all_static_lines(lang: str):
    """Every line the agent can say in this language that has NO dynamic slot.
    These are pre-synthesised at startup and served from memory thereafter."""
    s = SCRIPTS[lang]
    lines = [s["greeting"], s["closed"], s["escalation"]]
    for group in s["fillers"].values():
        lines.extend(group)
    for state_replies in s["replies"].values():
        lines.extend(state_replies.values())
    for group in s["reprompts"].values():
        lines.extend(group)
    return list(dict.fromkeys(lines))


def build_classification_prompt(state_name: str) -> str:
    """State-scoped classification. The model is only offered the intents valid
    right here, which is far more reliable than one global list. Deliberately in
    English regardless of the spoken language — the classifier reads it, the
    borrower never hears it."""
    state = FLOW[state_name]
    options = "\n".join(f"- {n}: {c['desc']}" for n, c in state["intents"].items())
    return f"""You classify a borrower's spoken reply on a live Indian-language loan-collections call.
The borrower may speak Hindi, Marathi, English, or a code-mixed blend of them.

WHERE THE CALL IS RIGHT NOW: {state['context']}

Classify the borrower's most recent reply as exactly one of:
{options}
- unclear: unrelated, unintelligible, or fits none of the above

Also extract `details`: any concrete value the borrower stated (a date, an amount,
a reason) as a SHORT phrase in their own words. Empty string if none.

You are NOT writing anything the borrower will hear. Classification only."""


def build_response_format(state_name: str) -> dict:
    intents = list(FLOW[state_name]["intents"].keys()) + ["unclear"]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "collections_turn",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": intents},
                    "details": {"type": "string"},
                    "requires_human_review": {"type": "boolean"},
                },
                "required": ["intent", "details", "requires_human_review"],
                "additionalProperties": False,
            },
        },
    }