"""Safety rails shared by ingestion, retrieval and answering (guard v2).

Zero dependencies, ~1 ms per check, explainable: every hit returns the rule
that fired so it can go into the audit log.

S0 input guard (caller questions and tool args)
  - prompt injection / prompt extraction
  - outcome prediction ("will we win", "chances of bail", "jeetenge")
  - solicitation (BCI Rule 36: no advertising or touting for the advocate)
S1 ingestion guard (document paragraphs)
  - looks_like_injection(): paragraphs that address an AI or try to change its
    behaviour are flagged and never retrieved
S3 output guard (what Jolly is about to say)
  - check_output(): PII (Aadhaar with Verhoeff check, PAN, Indian mobile),
    case-law citations that are not in the record, prompt leaks, outcome
    language. mask_pii() redacts PII for speech.

Every check runs on a normalised copy of the text: NFKC (folds full-width
letters), zero-width characters removed, "i g n o r e" collapsed, and base64
runs decoded. Patterns cover English, Hinglish (romanised Hindi), Hindi and
Tamil.

Known limits (see evals/redteam.yaml, held-out split): leetspeak, romanised
Tamil, paraphrased "disregard what you were told", and "repeat the text above"
style prompt extraction are not caught by these rules. A small classifier at
ingestion (Llama Prompt Guard 2 86M) is the planned backstop.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"), None)


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_ZW)
    # "I g n o r e" -> "Ignore": collapse runs of single letters separated by single spaces
    t = re.sub(r"\b(?:\w ){3,}\w\b", lambda m: m.group(0).replace(" ", ""), t)
    return re.sub(r"\s+", " ", t).strip()


def _variants(text: str):
    """The normalised text plus any base64 runs inside it, decoded."""
    t = normalise(text)
    yield t
    for tok in re.findall(r"[A-Za-z0-9+/=]{16,}", t):
        try:
            s = base64.b64decode(tok, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if s.isprintable():
            yield normalise(s)


# ---------------------------------------------------------------------------
# S0/S1: injection
# ---------------------------------------------------------------------------

_INJECTION = [
    # v1 (English)
    r"\bignore (all |any )?(the |your )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"\bdisregard (all |any )?(the )?(previous|prior|above|system)",
    r"\b(you are|act as|pretend to be) (now )?(an? )?(ai|assistant|chatbot|model|jolly)\b",
    r"\b(system prompt|developer message|jailbreak)\b",
    r"\b(tell|inform|say to) the (advocate|lawyer|user|counsel|client)\b",
    r"\bnew instructions?\b",
    r"\bdo not (cite|mention|disclose)\b",
    # v2: addressing the model / changing its behaviour
    r"\b(note|message|instruction)s? (to|for) (the )?(ai|assistant|model|bot|llm|jolly)\b",
    r"\b(when|while) (summari[sz]ing|answering|reading)\b.{0,40}\b(say|state|tell|claim)\b",
    r"\boverrides?\b.{0,30}\b(instruction|prompt|message|rules)",
    r"\b(decode|base64)\b.{0,20}\b(follow|execute|obey)",
    r"^\s*(assistant|ai|jolly|model|bot|chatbot)\s*[,:]",
    r"\bfrom now on\b.{0,40}\b(say|tell|answer|respond|reply|state|claim)\b",
    r"\balways (say|answer|respond|reply|state)\b",
    r"\b(respond|reply|answer) (only )?with\s*['\"\u201c\u2018]",
    r"\bignore (your|the|all|any) (rules|guidelines|guardrails|safety)\b",
    r"\b(reveal|print|repeat|read( me| out)?|show( me)?)\b.{0,25}\b(system prompt|your (instructions|prompt|rules))\b",
    # Hinglish (romanised Hindi)
    r"\b(nirdesh|nirdeshon|instructions?|rules?)\b.{0,20}\b(bhool|ignore|nazarandaz)",
    r"\bvakil (ko|se) (batao|bataiye|bataye|bolo|boliye|kaho|kahiye)\b",
    # Hindi
    r"(निर्देश|निर्देशों|नियम|नियमों).{0,25}(अनदेखा|भूल|नज़रअंदाज़|नजरअंदाज)",
    r"(वकील|अधिवक्ता)\s*को\s*(बताएं|बताओ|बताइए|बताना|कहें|कहो|बोलो)",
    # Tamil
    r"(அறிவுறுத்தல்|கட்டளை).{0,25}(புறக்கணி|மறந்து)",
    r"(வழக்கறிஞரிடம்|வக்கீலிடம்).{0,25}(சொல்|கூறு)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.IGNORECASE | re.MULTILINE)


def injection_rule(text: str) -> str | None:
    for v in _variants(text):
        if _INJECTION_RE.search(v):
            return "S0.injection"
    return None


def looks_like_injection(text: str) -> bool:
    return injection_rule(text) is not None


# ---------------------------------------------------------------------------
# S0: outcome prediction
# ---------------------------------------------------------------------------

_OUTCOME = [
    # v1
    r"\b(will|would|can|could|are|am) (we|i|my client|the client|they|he|she)\b.{0,25}\b(win|lose|succeed|get bail|be convicted|be acquitted)",
    r"\b(chances?|odds|probability|likelihood)\b.{0,30}\b(win|winning|success|bail|conviction|acquittal|decree)",
    r"\b(predict|forecast)\b",
    r"\bwhat (will|would) the (judge|court|bench) (do|decide|rule|order)",
    r"\b(is|was) (he|she|the accused|the defendant|my client|they) (guilty|liable|innocent)",
    r"\bwhat sentence\b",
    r"\bwho (will|would) win\b",
    # v2
    r"\b(chances?|odds|probability|likelihood|how likely|likely)\b.{0,40}\b(win|lose|succeed|extend|extended|grant|granted|allow|allowed|dismiss|dismissed|reject|rejected|throw out|thrown out|jail|prison|convict|convicted|acquit|acquitted|bail|stay|decree|quash|quashed)",
    r"\bexpect the (court|judge|bench|magistrate|tribunal)\b",
    r"\b(is|was) (the )?(accused|defendant|respondent|petitioner|client) (guilty|liable|innocent)",
    r"\bjeet(enge|egi|ega|na|oge|enga)\b|\bhaar(enge|egi|ega|oge)\b",
    r"(जीतेंगे|जीतेगा|जीतेगी|हारेंगे|हारेगा)",
    r"(ஜெயிப்போமா|வெற்றி பெறுவோமா|ஜெயிக்குமா)",
]
_OUTCOME_RE = re.compile("|".join(_OUTCOME), re.IGNORECASE)

OUTCOME_REFUSAL = (
    "I don't predict outcomes, verdicts or what the court will decide. "
    "I can tell you exactly what the record says, what is listed today and what is pending."
)


def asks_for_outcome(question: str) -> bool:
    return bool(_OUTCOME_RE.search(normalise(question)))


# ---------------------------------------------------------------------------
# S0: solicitation (BCI Rule 36)
# ---------------------------------------------------------------------------

_SOLICIT_RE = re.compile(
    r"\b(offer|advertis|promot|market)\w*\b.{0,40}\b(services?|clients?|cases?)\b"
    r"|\b(all|every) (residents|people|customers|members)\b",
    re.IGNORECASE,
)

SOLICITATION_REFUSAL = (
    "I can't draft advertising or outreach for new clients; Bar Council rules don't allow advocates to solicit work. "
    "I can help with messages to the client on this matter."
)

INJECTION_REFUSAL = "I can only answer questions about what this case file records."


def asks_for_solicitation(question: str) -> bool:
    return bool(_SOLICIT_RE.search(normalise(question)))


def screen_question(question: str) -> str | None:
    """Input guard for a caller's question. Returns the rule that fired, or None."""
    if asks_for_outcome(question):
        return "S0.outcome"
    if asks_for_solicitation(question):
        return "S0.solicitation"
    return injection_rule(question)


def refusal_for(rule: str) -> str:
    return {"S0.outcome": OUTCOME_REFUSAL, "S0.solicitation": SOLICITATION_REFUSAL}.get(rule, INJECTION_REFUSAL)


# ---------------------------------------------------------------------------
# S3: output guard
# ---------------------------------------------------------------------------

_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


_AADHAAR_RE = re.compile(r"(?<!\d)(?<!\d[,.])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d|[,.]\d)")
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_PHONE_RE = re.compile(r"(?<!\d)(?<!\d[,.])(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d|[,.]\d)")
_CASELAW_RE = re.compile(
    r"\bAIR\s+\d{4}\s+[A-Z][A-Za-z.]*\s+\d+"
    r"|\(\d{4}\)\s+\d+\s+SCC\s+\d+"
    r"|\b\d{4}\s+SCC\s+OnLine\s+\w+\s+\d+"
    r"|\b\d{4}\s+\(\d+\)\s+[A-Z]{2,6}\s+\d+",
)
_LEAK_RE = re.compile(
    r"\b(my|the) (system prompt|instructions|prompt) (say|says|are|is|reads?)\b|\byou are jolly\b|JOLLY-CANARY",
    re.IGNORECASE,
)
_OUTCOME_SPEECH_RE = re.compile(
    r"\byou (will|are going to) (win|lose)\b|\blikely to (succeed|win|be granted|be allowed|be dismissed)\b"
    r"|\b(bail|stay|injunction) will (surely |definitely )?be granted\b|\bthe court will (allow|dismiss|grant|convict|acquit)\b",
    re.IGNORECASE,
)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def check_output(text: str, record: str = "") -> list[str]:
    """Rules violated by text Jolly is about to speak. `record` is the cited source text."""
    t = normalise(text)
    rules: list[str] = []
    if any(_verhoeff_ok(re.sub(r"\D", "", m.group(0))) for m in _AADHAAR_RE.finditer(t)):
        rules.append("pii.aadhaar")
    if _PAN_RE.search(t):
        rules.append("pii.pan")
    if _PHONE_RE.search(t):
        rules.append("pii.phone")
    rec = _squash(record)
    if any(_squash(m.group(0)) not in rec for m in _CASELAW_RE.finditer(t)):
        rules.append("caselaw")
    if _LEAK_RE.search(t):
        rules.append("prompt_leak")
    if _OUTCOME_SPEECH_RE.search(t):
        rules.append("outcome")
    return rules


def mask_pii(text: str) -> str:
    def aadhaar(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "XXXX XXXX " + digits[-4:] if _verhoeff_ok(digits) else m.group(0)

    t = _AADHAAR_RE.sub(aadhaar, text)
    t = _PAN_RE.sub("[PAN withheld]", t)
    return _PHONE_RE.sub(lambda m: "XXXXXX" + re.sub(r"\D", "", m.group(0))[-4:], t)
