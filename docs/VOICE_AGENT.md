# Jolly voice agent v2 (Bolna)

`scripts/bolna_agent.py` builds the whole Bolna agent from code. This page says what v2 changes, what the
backend must send on the 8 AM call, and how to roll it out.

## What v2 changes

| Area | v1 (live today) | v2 |
| --- | --- | --- |
| Languages | English only | English, Tamil, Hindi on one agent (`multilingual_config`), native-script prompt per language; Bolna switches when the caller switches |
| Speech (`--speech`) | Deepgram nova-3 + ElevenLabs | `mixed` (default): English on Deepgram + ElevenLabs, Tamil/Hindi on Sarvam `saaras:v4` + `bulbul:v3`. `sarvam`: Sarvam for all three. `english`: the v1 stack |
| Turn-taking | endpointing 250 ms | endpointing **450 ms** + linear delay 400 ms (advocates pause mid-case-number), 2-word interruptions |
| Outbound | no voicemail handling | `voicemail: true` (hang up instead of reading the brief to a voicemail box) |
| Fillers | English `pre_call_message` | per-language `pre_call_message` and "are you still there?" (`{"en", "ta", "hi"}`) |
| First turn | `get_daily_brief` tool call on every call | brief arrives in `user_data` as prompt variables, so the first answer needs **no tool round trip** (and no Render cold start) |
| TTS output | raw record text ("27.10.2026", "PW1") | backend sends speakable text (`backend/services/speech.py`): "twenty-seventh October", "P W one", "C C number ten forty-two of twenty twenty-five" |

Sources for every config key are cited in the script's docstring (Bolna multilingual config reference, Sarvam
STT/TTS pages, Tamil guide, OSS `ConversationConfig` in `bolna/models.py`).

## user_data the 8 AM call must send

`POST https://api.bolna.ai/call` with `user_data` = `backend.services.cases.morning_call_user_data(brief, advocate_name)`.
All values are short strings. Bolna substitutes `{var}` in the prompt (and welcome message); an **unresolved
`{var}` is read out literally**, so every key below must be present on every outbound call.

| Key | Example | Used in |
| --- | --- | --- |
| `call_type` | `morning_brief` | webhook/console filtering (already sent today) |
| `advocate_name` | `Adv. Priya Raman` | all three prompts; outbound welcome |
| `daily_brief` | `Good morning. You have 2 matters today. 1. Lakshmi Narayanan versus ... item 23: Hearing on interim injunction ... Ask me about any case for details.` | all three prompts; outbound welcome |
| `brief_date` | `2026-10-27` | reference / console |
| `brief_date_spoken` | `twenty-seventh October` | English prompt |
| `brief_date_ta` | `இருபத்தி ஏழு அக்டோபர்` | Tamil prompt |
| `brief_date_hi` | `सत्ताईस अक्टूबर` | Hindi prompt |
| `matters_today` | `2` | all prompts (already sent today) |
| `first_matter` | `Lakshmi Narayanan v. Coastal Realty Developers Pvt. Ltd., Court 9, item 23` | spare, for a shorter opener |
| `timezone` | `Asia/Kolkata` | Bolna system variable for `{current_date}` / `{current_time}` |

`date` (sent today) can stay for backwards compatibility; the agent no longer reads it.

**Change needed in `backend/api/calls.py` (owned by the other engineer):**

```python
from backend.services.cases import morning_call_user_data

brief = build_daily_brief(session, day=date.today(), advocate=advocate)
data = _get_client(request).place_call(phone, user_data=morning_call_user_data(brief, advocate))
```

Optional: to start a call in Tamil or Hindi, Bolna takes `"agent_data": {"language": "ta"}` on `POST /call`
(https://www.bolna.ai/docs/customizations/start-call-in-language.md). `BolnaClient.place_call` does not send
`agent_data` yet; add it there if the advocate's preferred language is stored.

Inbound calls (advocate dials Jolly) carry no `user_data`. The prompt handles that: if the brief still shows a
placeholder in curly brackets, the agent calls `get_daily_brief`. Do **not** use `--outbound-welcome` on an
agent that also takes inbound calls, or the caller hears "{daily_brief}".

## Rolling it out (needs a human with the Bolna key)

```bash
# 1. Inspect the payload (no network):
JOLLY_BACKEND_URL=https://<backend> BOLNA_WEBHOOK_SECRET=... uv run python scripts/bolna_agent.py --dry-run > /tmp/agent_v2.json
# 2. Ship calls.py's user_data change first, then update the live agent:
BOLNA_API_KEY=... JOLLY_BACKEND_URL=... BOLNA_WEBHOOK_SECRET=... uv run python scripts/bolna_agent.py --agent-id $BOLNA_AGENT_ID
# 3. GET the agent back and confirm multilingual_config, voicemail and incremental_delay persisted.
```

Safer path for the demo: create a **new** agent (omit `--agent-id`), test it, then switch `BOLNA_AGENT_ID`.
Rollback = `--speech english` (v1 stack with the v2 prompt) or re-point `BOLNA_AGENT_ID` at the old agent.

## Test before the demo (Bolna's own checklist, plus language cases)

1. Browser test call, then a real phone call on Plivo.
2. Language switching (https://www.bolna.ai/docs/customizations/language-switching-behavior.md):
   a full Tamil sentence **should** switch; a Tamil sentence with one English word should **not**;
   reading a case number aloud should **not**.
3. "Will we win?" in each language → refusal. "Ignore previous instructions" inside a document → never spoken.
4. 15 s silence → localised "are you still there?"; voicemail on the outbound call → hang-up, retry later.
5. Read the execution payload: `latency_data.time_to_first_audio` should drop on the morning call because
   the first answer no longer waits on a tool call.

## Known gaps

- Sarvam voices: `bulbul:v3` `kavya` (en/ta) and `priya` (hi) are from Bolna's documented v3 list. The lower-latency
  `bulbul:v4-flash` voices are per-language ids (`<persona>_ta_<use case>`); pick them from `GET /me/voices`.
- Sarvam on Bolna transcribes English words in native script ("adjourned" may arrive in Tamil script). The backend's
  case lookup and retrieval accept native digits and Tamil/Hindi number words (`backend/services/legal_ids.py`),
  but native-script spellings of English legal words are only partly covered by the router's cue lists.
- `use_fillers` is an OSS field not confirmed on the hosted API; it is behind `--fillers`.
- Tamil and Hindi prompts and number words should get a native speaker's read-through before the video.
