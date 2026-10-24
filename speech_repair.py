"""
speech_repair.py - fixes what the speech recogniser (Whisper) gets wrong, BEFORE the text reaches the engineer.

  repair(text)           -> the cleaned text ("" = not real speech, ignore it)
  repair_verbose(text)   -> (cleaned text, [what was changed])   used for the log line "[STT] heard ... -> ..."

WHAT IT DOES (in this order)
  1. Drops things Whisper invents on silence / noise ("thanks for watching", "uh", "you").
  2. Glues spelled-out letters:            "p s i" -> "psi", "r p m" -> "rpm"
  3. Fixes whole phrases:                  "set the click to ..." -> "set cliff to ..."
  4. Removes his name when it is used to get his attention: "okay Alex what's my fuel" -> "what's my fuel"
     (and understands mishearings of the name: "alix", "alec", "alexa").
  5. Fixes single words, using the neighbouring words so normal English is left alone:
         "whether" -> "weather" only in "what's the whether", never in "whether or not".
  6. Fixes numbers that sound like other words: "set margin to for laps" -> "four laps", "dirty percent" -> "thirty percent".
  7. Fuzzy-matches motorsport words, famous drivers / circuits and the CURRENT car and track names:
         "temprature" -> "temperature", "senah" -> "senna".   (same first letter + very close spelling only)

It is plain Python, standard library only, and costs well under a millisecond per sentence.
Settings (all optional, in config.py):  SPEECH_REPAIR = True/False,  ENGINEER_NAME,  CHAT_KEYWORDS.
NOTE: listener.py already applies config.WORD_FIXES to every sentence. Keep that list for words that are ALWAYS wrong;
the words that depend on context ("whether", "books", "lab") are handled here.

Try it:   python speech_repair.py "okay alex what's the whether"
"""
import re

try:
    import config
except Exception:                      # lets this file run on its own
    config = None

__all__ = ["repair", "repair_verbose"]


# ======================================================================================= 1. not speech
_FILLERS = {"you", "um", "uh", "hm", "hmm", "mm", "mhm", "huh", "ah", "oh", "bye"}
_NOT_SPEECH = re.compile(
    r"^(?:thanks?(?: you)?(?: so much)? for (?:watching|listening)\b.*"
    r"|please (?:like|subscribe)\b.*"
    r"|subtitles? by\b.*"
    r"|see you (?:next time|in the next video)\b.*)$")


# ======================================================================================= 2 + 3. phrases
_PHRASES = [(re.compile(p), r) for p, r in (
    (r"\bp s i\b", "psi"), (r"\bpee ess eye\b", "psi"), (r"\br p m\b", "rpm"), (r"\bare pm\b", "rpm"),
    (r"\bp b\b", "pb"),
    (r"\b(tyre|tire|tyres|tires) (?:click|clip|clef|cleft|clift|kliff|clif|cliffs|cliff's)\b", r"\1 cliff"),
    (r"\bset (?:the )?(?:click|clip|clef|cleft|clift|kliff|clif|cliffs)\b", "set cliff"),
    (r"\bunder steer\b", "understeer"), (r"\bover steer\b", "oversteer"), (r"\bdown force\b", "downforce"),
    (r"\bradio (?:chic|chick|shake|chuck)\b", "radio check"),
    (r"\bpersonal bass\b", "personal best"),
)]


# ======================================================================================= 5. single words
_SIMPLE = {                                  # always safe: these are not real words
    "weathr": "weather", "wether": "weather", "wheather": "weather",
    "fule": "fuel", "fuell": "fuel", "fuul": "fuel", "fewel": "fuel",
    "pitt": "pit", "pitts": "pits", "pitstop": "pit stop", "pitstops": "pit stops", "boxbox": "box box",
    "tyers": "tyres", "tyer": "tyre",
    "clif": "cliff", "kliff": "cliff", "clift": "cliff", "cliffs": "cliff",
    "psy": "psi", "lapse": "laps", "lap's": "laps",
    "dammage": "damage", "damaje": "damage", "enjin": "engine",
}

_TYRE_NEXT = {"pressure", "pressures", "temp", "temps", "temperature", "temperatures", "wear", "health",
              "status", "cliff", "life", "damage", "condition"}
_BRAKE_NEXT = {"bias", "balance", "temp", "temps", "temperature", "temperatures", "pedal", "fade",
               "pressure", "pressures", "pads", "disc", "discs"}
# (heard, meant, previous word must be one of, next word must be one of)  - None = no condition; BOTH given = both must hold
_CTX = [
    ("whether", "weather", {"the", "what's", "whats", "how's", "hows"}, None),
    ("whether", "weather", None, {"report", "forecast", "today", "looks"}),
    ("lab", "lap", {"last", "best", "fastest", "another", "extra", "one", "this", "next", "previous", "final", "every"}, None),
    ("lab", "lap", None, {"time", "times"}),
    ("books", "box", {"should", "i", "we", "can", "to"}, {"this", "now", "next", "lap", "please", ""}),
    ("tier", "tyre", None, _TYRE_NEXT),
    ("tiers", "tyres", None, _TYRE_NEXT),
    ("grit", "grip", {"track", "the", "my", "how's", "what's", "whats", "hows"}, {"level", "percent", "today", "now", ""}),
    ("break", "brake", None, _BRAKE_NEXT),
    ("breaks", "brakes", None, _BRAKE_NEXT),
    ("breaks", "brakes", {"my", "the", "front", "rear"}, {"hot", "cold", "overheating", "fading", "gone", "smoking", "feel"}),
]


def _ctx_fix(tok, prev, nxt):
    for word, repl, prevs, nexts in _CTX:
        if tok != word:
            continue
        if (prevs is None or prev in prevs) and (nexts is None or nxt in nexts):
            return repl
    return None


# ======================================================================================= 6. numbers
_NUMWORDS = set("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety point".split())
_HOMOPHONES = {"for": "four", "fore": "four", "ate": "eight", "won": "one", "tree": "three", "free": "three",
               "fife": "five", "tin": "ten", "dirty": "thirty", "fourty": "forty", "to": "two", "too": "two"}
_STRONG_UNITS = {"percent", "psi", "degrees", "litres", "liters"}      # a number almost surely comes before these
_WEAK_UNITS = {"laps", "minutes", "seconds"}


def _number_fix(tok, prev, nxt):
    new = _HOMOPHONES.get(tok)
    if new is None:
        return None
    if tok == "fourty":
        return new
    if tok in ("to", "too"):                 # "to" is a real word: only "P to" or "to percent" are numbers
        return new if (prev == "p" or nxt in _STRONG_UNITS) else None
    if nxt in _STRONG_UNITS:
        return new
    if nxt in _WEAK_UNITS and (prev in _NUMWORDS or prev in {"to", "at", "by", "than"}):
        return new
    if prev == "p" and nxt in ("", "now", "please"):
        return new
    return None


# ======================================================================================= 7. fuzzy vocabulary
VOCAB = (
    "temperature temperatures pressure pressures weather forecast tyre tyres tire tires cliff grip damage engine "
    "position status report penalty setup camber aero downforce qualifying brake brakes throttle traction understeer "
    "oversteer stint strategy compound slicks wets intermediates overtake spotter telemetry radio blistering graining "
    "degradation apex chicane kerb gearbox differential suspension stiffness undercut overcut safety virtual percent "
    "litres degrees fuel pit pits box lap laps flag").split()
NAMES = (
    "senna schumacher hamilton verstappen alonso vettel raikkonen leclerc norris piastri russell sainz ricciardo "
    "bottas prost lauda mansell hakkinen button massa webber rosberg villeneuve hunt stewart clark fangio "
    "monza silverstone nurburgring nordschleife suzuka imola monaco brands laguna mugello zandvoort interlagos "
    "bathurst sebring daytona barcelona catalunya hungaroring spa").split()
_PROTECT = {"engineer", "engineers", "pitwall", "camera", "cameras", "pressed", "pressing",
            "whether"}          # real words that sit close to a racing word; only the context rules may change them
_VOCAB_SET, _NAMES_SET = set(VOCAB), set(NAMES)
_LONG_VOCAB = [v for v in VOCAB if len(v) >= 5]


def _closest(tok, candidates, cutoff):
    import difflib                                         # loaded the first time a word needs it
    sm = difflib.SequenceMatcher()
    sm.set_seq2(tok)
    best, best_r = None, cutoff - 1e-9
    for c in candidates:
        if c[0] != tok[0] or abs(len(c) - len(tok)) > 3:   # a mishearing keeps its first letter and its length
            continue
        sm.set_seq1(c)
        r = sm.ratio()
        if r > best_r:
            best, best_r = c, r
    return best


def _fuzzy(tok, extra):
    if len(tok) < 5 or not tok.isalpha() or tok in _PROTECT or tok in _VOCAB_SET or tok in _NAMES_SET or tok in extra:
        return None
    if any(tok.startswith(v) for v in _LONG_VOCAB):        # "engines", "pressured", "positions": already fine
        return None
    return (_closest(tok, VOCAB, 0.80) or _closest(tok, NAMES, 0.78) or (_closest(tok, sorted(extra), 0.80) if extra else None))


def _extra_words(extra_terms):
    """Words from the current car / track names and the user's CHAT_KEYWORDS, lower case, letters only."""
    words = set()
    sources = list(extra_terms or []) + list(getattr(config, "CHAT_KEYWORDS", []) or [])
    for s in sources:
        for w in re.findall(r"[a-z]+", str(s or "").lower()):
            if len(w) >= 4:
                words.add(w)
    return words


# ======================================================================================= 4. his name
_GREET = {"hey", "hi", "hello", "yo", "okay", "ok", "oi", "right"}


def _ratio(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_name(tok, name, threshold):
    n = name.lower()
    if tok == n:
        return True
    return len(tok) >= 3 and tok[0] == n[0] and _ratio(tok, n) >= threshold


def _strip_address(tokens, name, notes):
    if not name or not tokens:
        return tokens
    i = 1 if (tokens[0] in _GREET and len(tokens) > 1) else 0
    if _is_name(tokens[i], name, 0.72):
        rest = tokens[i + 1:]
        if rest:
            notes.append("dropped the name %r" % tokens[i])
            return rest
        return tokens[i:]                       # only his name was said: keep it, so he answers "Say again?"
    if len(tokens) > 2 and _is_name(tokens[-1], name, 0.85):
        notes.append("dropped the name %r" % tokens[-1])
        return tokens[:-1]
    return tokens


# ======================================================================================= the public functions
def _normalise(text):
    t = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = re.sub(r"(?<![a-z])'|'(?![a-z])", " ", t)           # quote marks that are not apostrophes
    return re.sub(r"\s+", " ", t).strip()


def _sub(pat, rep, text, notes):
    def fn(m):
        out = m.expand(rep)
        if out != m.group(0):
            notes.append("%s -> %s" % (m.group(0), out))
        return out
    return pat.sub(fn, text)


def repair_verbose(text, extra_terms=(), name=None):
    """-> (cleaned text, [changes]). An empty text means: not real speech, ignore it."""
    raw = (text or "").strip()
    if config is not None and not getattr(config, "SPEECH_REPAIR", True):
        return raw, []
    if name is None:
        name = getattr(config, "ENGINEER_NAME", "") if config is not None else ""
    notes = []
    t = _normalise(raw)
    if not t:
        return "", []
    if _NOT_SPEECH.match(t) or all(w in _FILLERS for w in t.split()):
        return "", ["ignored (not speech)"]
    for pat, rep in _PHRASES:
        t = _sub(pat, rep, t, notes)

    tokens = _strip_address(t.split(), name, notes)
    fixes = _SIMPLE                              # config.WORD_FIXES is already applied by listener.py; not repeated here
    extra = _extra_words(extra_terms)

    out = []
    for i, tok in enumerate(tokens):
        prev = out[-1] if out else ""
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        new = fixes.get(tok)
        if new is None:
            new = _ctx_fix(tok, prev, nxt)
        if new is None:
            new = _number_fix(tok, prev, nxt)
        if new is None:
            new = _fuzzy(tok, extra)
        if new is not None and new != tok:
            notes.append("%s -> %s" % (tok, new))
            out.extend(new.split())
        else:
            out.append(tok)
    return " ".join(out), notes


def repair(text, extra_terms=(), name=None):
    return repair_verbose(text, extra_terms, name)[0]


if __name__ == "__main__":
    import sys
    samples = sys.argv[1:] or ["okay alex what's the whether", "set the click to dirty percent",
                               "what are my tire pressures in p s i", "how many lapse of fuel",
                               "should i books now", "thanks for watching"]
    for s in samples:
        fixed, notes = repair_verbose(s, name="Alex")
        print("%-45r -> %-40r %s" % (s, fixed, notes))
