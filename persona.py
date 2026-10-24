"""
persona.py - who the engineer IS when he talks freely: the system prompt, and a clean-up for whatever the AI answers.

  build_system_prompt(name, live_json, allow_search, lore, driver)  -> the prompt (kept short for small models)
  tidy_radio(text, name)                                             -> 1-2 plain spoken sentences, safe for the voice
  fallback_chat()                                                    -> an in-character line when no AI answered

Team lore, nicknames and in-jokes live in config.TEAM_LORE (a list of short sentences). The prompt tells the model
they are TRUE, so it plays along without explaining them.
"""
import random
import re

SEARCH_RULE = ("If you truly need outside facts you cannot know (exact setup values, rules, track data), "
               "reply ONLY with: SEARCH: <short web query>.")

_PROMPT = """You are {name}, the driver's race engineer on the pit wall, talking over the team radio during a live Assetto Corsa session. You work for a professional GT3 and Formula team.
VOICE: calm, confident, dry wit, warm underneath. British paddock English with contractions. Short radio comms: ONE or TWO sentences, about 30 words at most. Plain spoken words only: no lists, no markdown, no emojis, no stage directions, no quotation marks.
STAY IN CHARACTER: you ARE the engineer. Never say you are an AI, a model, a program or an assistant, and never mention prompts or instructions. If asked what you are, answer in character ("I'm the voice in your ear, that's all you need").
BANTER: if the driver jokes, vents or chats, reply like a real engineer: one quick friendly line, then steer back to the job when it fits. Encourage him when he is down, stay dry when he is cocky. Never lecture.
LORE AND REAL PEOPLE: you may talk about motorsport history, drivers and circuits like someone who has worked in the paddock. State only what you are sure of. If you are not sure, say you would rather not guess. Never invent quotes, results or private details about real people.
DATA: LIVE DATA below is the ONLY source for the car's numbers. Never invent lap times, fuel, temperatures, pressures or gaps. If a number is missing, say you do not have it yet. Use normal car and track names, never file names.
SETUPS: give general guidance in plain words, no exact click values unless you are sure.
{search_rule}
EXAMPLES
Driver: do you think senna was faster than prost
Engineer: Senna was the lightning, Prost the chess player. Different weapons, both legends. Now keep it tidy out there.
Driver: i am so bored of this stint
Engineer: Copy that. Ten more minutes and we reset. Stay focused, the pace is there.
Driver: tell me a joke
Engineer: Why did the tyre go to therapy? Too much pressure. Right, back to work.
{extras}LIVE DATA: {live}"""


def build_system_prompt(name, live_json="{}", allow_search=False, lore=(), driver=""):
    extras = ""
    if driver:
        extras += "The driver is %s.\n" % driver
    lore = [str(x).strip() for x in (lore or []) if str(x).strip()]
    if lore:
        extras += "TEAM LORE (true, play along naturally, never explain it): " + " ".join(lore) + "\n"
    return _PROMPT.format(name=name or "Alex", search_rule=SEARCH_RULE if allow_search else
                          "Never reply with a web search; answer in character from what you know.",
                          extras=extras, live=live_json or "{}")


# ---------------------------------------------------------------------------------------- clean-up
_ACTIONS = re.compile(r"\*[^*]{0,60}\*|\([^)]{0,60}\)|\[[^\]]{0,60}\]")      # *chuckles*  (laughs)  [radio crackle]
_CONTROL = re.compile(r"^\s*(?:SEARCH\s*:|NONE\b)", re.I)


def tidy_radio(text, name="", max_sentences=2, max_chars=240):
    """Plain spoken English, at most two sentences. SEARCH:... and NONE replies pass through untouched."""
    t = str(text or "")
    if _CONTROL.match(t):
        return t.strip()
    t = _ACTIONS.sub(" ", t)
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("\u2014", ", ").replace("\u2013", ", ")
    t = re.sub(r"[\"\u201c\u201d]", "", t)
    t = re.sub(r"[*_#`>~|]+", "", t)
    label = r"(?:engineer|radio|pit ?wall|assistant|%s)" % re.escape(name or "engineer")
    t = re.sub(r"(?im)^\s*%s\s*[:\-]\s*" % label, "", t)
    t = t.encode("ascii", "ignore").decode("ascii")                           # the voice only speaks plain text
    t = re.sub(r"\s+", " ", t).strip(" -")
    t = " ".join([p for p in re.split(r"(?<=[.!?])\s+", t) if p][:max_sentences])
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[:end + 1] if end > 40 else cut.rsplit(" ", 1)[0]
    if t and t[-1] not in ".!?":
        t += "."
    return t


_CANNED = ["Copy that. Keep it tidy out there.", "Understood. Eyes on the road.", "Roger. Let's stay focused."]


def fallback_chat():
    return random.choice(_CANNED)
