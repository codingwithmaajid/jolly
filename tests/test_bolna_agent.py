"""Bolna agent v2 payload (scripts/bolna_agent.py). No network: build and --dry-run only."""

import json

import pytest

import scripts.bolna_agent as agent

BACKEND = "https://jolly.example"


def _task(body):
    return body["agent_config"]["tasks"][0]


def test_default_is_multilingual_with_sarvam_for_tamil_and_hindi():
    body = agent.build_agent(BACKEND, "s3cret")
    tc = _task(body)["tools_config"]
    ml = tc["multilingual_config"]
    assert ml["enabled"] and ml["active_language"] == "en" and set(ml["languages"]) == {"en", "ta", "hi"}
    for lang in ("ta", "hi"):
        entry = ml["languages"][lang]
        assert entry["transcriber"] == {**entry["transcriber"], "provider": "sarvam", "model": "saaras:v4", "language": lang}
        assert entry["synthesizer"]["provider"] == "sarvam"
        assert entry["synthesizer"]["provider_config"]["language"] == lang  # required by Bolna for Sarvam
        assert entry["synthesizer"]["provider_config"]["model"] == "bulbul:v3"
    assert tc["transcriber"]["provider"] == "deepgram" and ml["languages"]["en"]["transcriber"] == {"language": "en"}
    assert "தமிழ் எழுத்தில்" in ml["languages"]["ta"]["system_prompt"]
    assert "देवनागरी" in ml["languages"]["hi"]["system_prompt"]


def test_sarvam_everywhere_and_english_only_modes():
    sar = agent.build_agent(BACKEND, None, speech="sarvam")
    tc = _task(sar)["tools_config"]
    assert tc["transcriber"]["provider"] == "sarvam" and tc["synthesizer"]["provider"] == "sarvam"
    assert tc["multilingual_config"]["languages"]["en"]["synthesizer"]["provider"] == "sarvam"
    eng = agent.build_agent(BACKEND, None, speech="english")
    tc = _task(eng)["tools_config"]
    assert "multilingual_config" not in tc
    assert isinstance(tc["api_tools"]["tools"][0]["pre_call_message"], str)
    with pytest.raises(ValueError):
        agent.build_agent(BACKEND, None, speech="klingon")


def test_engine_settings_voicemail_and_localised_messages():
    body = agent.build_agent(BACKEND, None)
    task = _task(body)
    assert task["tools_config"]["transcriber"]["endpointing"] == 450
    assert all(l["transcriber"].get("endpointing", 450) == 450
               for l in task["tools_config"]["multilingual_config"]["languages"].values())
    cfg = task["task_config"]
    assert cfg["voicemail"] is True and cfg["number_of_words_for_interruption"] == 2
    assert cfg["incremental_delay"] == 400 and set(cfg["check_user_online_message"]) == {"en", "ta", "hi"}
    for t in task["tools_config"]["api_tools"]["tools"]:
        assert set(t["pre_call_message"]) == {"en", "ta", "hi"} and t["key"] == "custom_task"
    assert _task(agent.build_agent(BACKEND, None, voicemail=False))["task_config"]["voicemail"] is False
    assert "use_fillers" not in cfg and _task(agent.build_agent(BACKEND, None, fillers=True))["task_config"]["use_fillers"]


def test_sarvam_keyword_limits():
    assert len(agent.KEYWORDS) <= 50 and all(len(k) <= 64 for k in agent.KEYWORDS)


def test_prompt_uses_brief_variables_so_first_turn_needs_no_tool():
    body = agent.build_agent(BACKEND, None)
    prompt = body["agent_prompts"]["task_1"]["system_prompt"]
    assert "{daily_brief}" in prompt and "{advocate_name}" in prompt
    assert body["agent_config"]["agent_welcome_message"] == agent.WELCOME and "{" not in agent.WELCOME
    out = agent.build_agent(BACKEND, None, outbound_welcome=True)
    assert "{daily_brief}" in out["agent_config"]["agent_welcome_message"]


def test_tool_urls_carry_secret_and_dry_run_redacts(capsys, monkeypatch):
    monkeypatch.setenv("BOLNA_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("JOLLY_BACKEND_URL", BACKEND)
    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: pytest.fail("dry run must not call Bolna"))
    monkeypatch.setattr(agent.requests, "put", lambda *a, **k: pytest.fail("dry run must not call Bolna"))
    assert agent.main(["--dry-run", "--agent-id", "abc"]) == 0
    printed = capsys.readouterr().out
    assert "s3cret" not in printed
    body = json.loads(printed)
    params = _task(body)["tools_config"]["api_tools"]["tools_params"]
    assert params["ask_case_question"]["url"] == f"{BACKEND}/bolna/tools/ask-case-question?secret=***"
    assert json.loads(params["ask_case_question"]["param"]) == {"case_id": "%(case_id)s", "question": "%(question)s"}
