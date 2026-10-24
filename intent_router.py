"""
intent_router.py - decides WHICH brain answers what the driver said.

  telemetry   crisp race questions ("what's my fuel", "tyre temps", "should I pit")  -> the instant rules in brain.py
  chat        banter, feelings, real people, racing lore                              -> the local LLM, in character
  knowledge   setups, rules, "why / how" questions                                    -> the local LLM, which may ask for a web search

The rules always win for short, clear telemetry questions, so those stay instant. The router only steps in when the
sentence is clearly conversation, so a one-word match ("position", "weather", "engine") inside a chatty sentence
("what do you think about the weather in Monza") no longer triggers a telemetry read-out.

  IntentRouter(telemetry_match)      telemetry_match(tokens) -> True if the instant rules would understand the sentence
  .prefers_chat(text, tokens)        True = skip the one-word telemetry triggers, go to conversation
  .classify(text, tokens)            "chat" or "knowledge" (used once the rules did not answer)

Add your own names / nicknames / in-jokes to config.CHAT_KEYWORDS (list of lower-case words) so they count as banter.
"""
import re

import config

CHAT_PHRASES = (
    "how are you", "how's it going", "hows it going", "how you doing", "good morning", "good evening",
    "good afternoon", "good night", "what do you think", "do you think", "what's your favourite", "whats your favourite",
    "what's your favorite", "whats your favorite", "your favourite", "your favorite", "tell me a joke", "tell me a story",
    "tell a joke", "remember when", "you remember", "did you see", "did you watch", "did you know", "real life",
    "back in", "the old days", "who is", "who was", "greatest of all time", "best driver", "best ever", "worst driver",
    "are you real", "are you human", "what's your name", "whats your name", "who are you", "who made you",
    "you're the best", "you are the best", "nice one", "well done", "good job", "nice job", "shut up",
    "i love", "i hate", "i'm tired", "im tired", "i'm bored", "im bored", "i'm nervous", "im nervous",
)
CHAT_WORDS = {
    "joke", "jokes", "funny", "bored", "boring", "favourite", "favorite", "legend", "legends", "goat", "rivalry", "lore",
    "story", "stories", "mate", "buddy", "pal", "dad", "mum", "mom", "brother", "sister", "girlfriend", "wife", "friend",
    "friends", "love", "hate", "tired", "nervous", "scared", "sorry", "congrats", "congratulations", "lol", "haha",
    "sing", "song", "music", "pizza", "coffee", "tea", "lunch", "dinner", "hungry", "birthday", "weekend", "holiday",
    "senna", "schumacher", "hamilton", "verstappen", "alonso", "vettel", "prost", "lauda", "mansell", "raikkonen",
    "leclerc", "norris", "piastri", "russell", "sainz", "ricciardo", "schumi", "rossi", "ferrari", "mclaren",
    "mercedes", "porsche", "lamborghini",
}
KNOWLEDGE_RE = re.compile(
    r"\b(setups?|set up|camber|toe|caster|aero|wings?|downforce|differential|diff|gear ratios?|gearing|springs?|"
    r"dampers?|anti roll|ride height|brake bias|compound|slicks|wets|regulations?|rules?|how do|how to|how can|why|"
    r"explain|recommend|should i run|pressures?|strategy|undercut|overcut)\b")
QUESTION_START = re.compile(r"^(?:who|whom|whose|what|why|how|when|where|which|can|could|should|would|does|do|did|is|are|was|were|will)\b")

_PHRASE_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(p) for p in CHAT_PHRASES))


class IntentRouter:
    def __init__(self, telemetry_match):
        self.telemetry_match = telemetry_match

    def _words(self):
        extra = {str(w).lower() for w in (getattr(config, "CHAT_KEYWORDS", []) or [])}
        return CHAT_WORDS | extra

    def is_chatty(self, text, tokens):
        return bool(_PHRASE_RE.search(text)) or any(t in self._words() for t in tokens)

    def prefers_chat(self, text, tokens):
        """Chat words present AND (a long sentence OR the rules would not understand it anyway)."""
        if not self.is_chatty(text, tokens):
            return False
        return len(tokens) > 6 or not self.telemetry_match(tokens)

    def classify(self, text, tokens):
        if self.is_chatty(text, tokens):
            return "chat"
        if KNOWLEDGE_RE.search(text):
            return "knowledge"
        if QUESTION_START.match(text) and len(tokens) >= 5:
            return "knowledge"
        return "chat"
