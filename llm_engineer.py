"""
llm_engineer.py - casual radio chat + long-term memory for the Cesium AI Race Engineer  (memory rewrite).

WHY THE OLD VERSION "FORGOT" OR MADE THINGS UP  (all fixed here)
  1. The prompt's few-shot EXAMPLES contained a concrete story ("my engineer is Luca and he won 20 podiums").
     A small 1.5B / 3B model copies that story when it has nothing better: that is where "Luca" and "20 podiums"
     came from. The examples are now fact-free, and the saved facts sit LAST in the prompt (small models obey the
     end of the prompt most) inside a MEMORY block that says it overrides everything the model believes.
  2. Questions such as "how many podiums does Khaled have" are routed to the "knowledge" route, which never saw the
     memory at all. Now memory is (a) consulted FIRST by deterministic code, with no AI involved, and (b) appended to
     the system prompt of EVERY AI turn, including the knowledge route (Brain._system_prompt is wrapped).
  3. Bad answers were stored in the chat history and then copied by the model on the next turn. History is now
     tagged with a memory revision: any change to the memory (new fact, correction, rename) drops it, in this class
     and in Brain.history.
  4. "My engineer is <new name>" created a SECOND person instead of fixing the first, and a misheard name could never
     be corrected. Now: unique roles (engineer, coach, mechanic ...) have one holder; a new name for the same role,
     or a phonetically close name ("Harleid" ~ "Khaled"), RENAMES the existing person (stats are kept) and the old
     spelling becomes an alias, so speech repair maps it to the right name from then on. Spoken corrections work too:
     "no, it's Khaled", "his name is Khaled", "it's Khaled not Harleid", "no, he has 120 podiums".
  5. Answers were never checked. Now every AI answer on a memory-related turn is validated: a number or a name that
     is not in the memory / live data / question is rejected and replaced by a deterministic memory answer.
  6. The store could be lost: written non-atomically, and a source run (project\\data) and the packaged exe
     (%APPDATA%\\CesiumAIRaceEngineer\\data) use different folders. Now: write -> flush -> fsync -> os.replace (with
     retries for antivirus locks), a rolling .bak, corrupt-file recovery, and a one-time import of a store found
     in the other location.

HOW IT PLUGS IN (main_pitwall.Core does this; no change needed there)
    self.chat = LLMEngineer.from_brain(self.brain)
    self.chat.attach(self.brain)
    text = self.chat.repair(text)
    if not self.chat.handle_if_casual(text):
        self.brain.handle(text)

ORDER INSIDE handle_if_casual   (telemetry questions are never claimed)
    forget -> learn (facts / corrections) -> deterministic memory answer -> AI chat with memory

PACKAGING: standard library + config + persona (+ speech_repair for its word list, optional). No data files.
Nuitka / PyInstaller find everything through ordinary imports. Memory file:
    <config.USER_ROOT>\\data\\memory_store.json      (%APPDATA%\\CesiumAIRaceEngineer\\data in a packaged build)
Override with config.MEMORY_STORE_FILE. Optional: config.CHAT_LLM_ACK = True lets the AI word the "I learned X"
acknowledgement (it is checked against the memory); default False = instant deterministic acknowledgement.

Self-test (no game, no Ollama, temp files only):   python llm_engineer.py
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from collections import deque

import config
import persona

try:                                         # word list only; the module still works without it
    from speech_repair import VOCAB as _SR_VOCAB
except Exception:
    _SR_VOCAB = ()

TAG = "[CHAT]"
APP_DIR = "CesiumAIRaceEngineer"
SCHEMA_VERSION = 2
FILE_NAME = "memory_store.json"


# =====================================================================================================================
#  Where the memory lives
# =====================================================================================================================
def _packaged():
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _profile_root():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR)


def memory_path():
    """Primary location. Same rule as config.USER_ROOT, so a packaged build never writes into its install folder."""
    explicit = getattr(config, "MEMORY_STORE_FILE", None)
    if explicit:
        return explicit
    root = getattr(config, "USER_ROOT", None)
    if not root:
        root = _profile_root() if _packaged() else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "data", FILE_NAME)


def legacy_paths(primary):
    """Other places an older run may have written the store (source run vs exe, old layouts). Read-only, for migration."""
    cands = []
    base = getattr(config, "BASE_DIR", None)
    if base:
        cands.append(os.path.join(base, "data", FILE_NAME))
    for exe_like in (sys.argv[0] if sys.argv else "", sys.executable):
        if exe_like:
            cands.append(os.path.join(os.path.dirname(os.path.abspath(exe_like)), "data", FILE_NAME))
    cands.append(os.path.join(_profile_root(), "data", FILE_NAME))
    cands.append(os.path.join(_profile_root(), FILE_NAME))
    out, seen = [], {os.path.normcase(os.path.abspath(primary))}
    for p in cands:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# =====================================================================================================================
#  Text helpers
# =====================================================================================================================
def normalise(text):
    """lower case, hyphens -> spaces, only letters / digits / apostrophes ("real-life" -> "real life")."""
    t = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'").replace("-", " ")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = re.sub(r"(?<![a-z])'|'(?![a-z])", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens_of(text):
    return normalise(text).split()


_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMW = set(_UNITS) | set(_TENS) | {"hundred", "thousand"}


def to_number(tokens):
    """['one','hundred','and','fifty'] -> 150, ['150'] -> 150, ['a'] -> 1, anything else -> None."""
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0] in ("a", "an"):
        return 1
    total = cur = 0
    seen = False
    for t in tokens:
        if t.isdigit():
            cur += int(t)
        elif t in _UNITS:
            cur += _UNITS[t]
        elif t in _TENS:
            cur += _TENS[t]
        elif t == "hundred":
            cur = max(cur, 1) * 100
        elif t == "thousand":
            total, cur = total + max(cur, 1) * 1000, 0
        elif t in ("and", "a"):
            continue
        else:
            return None
        seen = True
    return total + cur if seen else None


def numbers_in(text):
    """Every number said or written in `text` ('150', 'one hundred and fifty', 'twenty') as a set of ints."""
    toks = tokens_of(text)
    out, i = set(), 0
    isnum = lambda x: x.isdigit() or x in _NUMW
    while i < len(toks):
        if not isnum(toks[i]):
            i += 1
            continue
        j = i
        while j < len(toks) and (isnum(toks[j]) or (toks[j] == "and" and j > i and j + 1 < len(toks) and isnum(toks[j + 1]))):
            j += 1
        n = to_number(toks[i:j])
        if n is not None:
            out.add(n)
        i = j
    return out


# ---------------------------------------------------------------------------------------------------------- names
_COMMON = {  # words that follow "is" but are not names ("my engineer is very fast")
    "a", "an", "the", "my", "our", "very", "so", "really", "quite", "also", "just", "not", "still", "always", "fast",
    "good", "great", "best", "bad", "brilliant", "amazing", "legend", "legendary", "awesome", "nice", "cool", "funny",
    "here", "there", "that", "this", "it", "he", "she", "they", "you", "me", "him", "her", "who", "what", "too", "now",
    "back", "gone", "retired", "old", "new", "busy", "late", "tired", "angry", "happy", "sad", "strict", "clever",
    "smart", "tough", "hard", "calm", "quick", "slow", "like", "with", "from", "one", "two", "and", "but", "when",
    "never", "ever", "sometimes", "usually", "much", "more", "less", "better", "worse", "number",
    "which", "where", "why", "how", "your", "yours", "mine", "yes", "no", "okay", "ok", "fine", "right", "wrong",
    "sure", "thanks", "thank", "please", "correct", "true", "false", "alright", "sorry", "maybe", "again", "today",
}
_PRONOUNS = {"he", "she", "they", "him", "her"}
_STAT_NOUNS = {"podium": "podiums", "podiums": "podiums", "win": "wins", "wins": "wins", "victory": "wins",
               "victories": "wins", "championship": "championships", "championships": "championships",
               "title": "titles", "titles": "titles", "pole": "poles", "poles": "poles"}
_NOT_NAMES = frozenset(_COMMON | set(_STAT_NOUNS) | set(_SR_VOCAB) | {
    "engineer", "coach", "mechanic", "manager", "mentor", "friend", "brother", "sister", "driver", "team", "race"})
_UNIQUE_ROLES = {"engineer", "coach", "mechanic", "manager", "mentor", "instructor", "spotter"}   # one holder at a time


def _skeleton(name):
    """Rough sound of a name: 'Khaled' / 'Hallad' / 'Halid' -> 'hld'. Vowels (except a leading one) are dropped."""
    s = re.sub(r"[^a-z]", "", (name or "").lower())
    for a, b in (("kh", "h"), ("ph", "f"), ("ck", "k"), ("q", "k"), ("c", "k"), ("x", "ks"), ("z", "s"), ("w", "v")):
        s = s.replace(a, b)
    s = s[:1] + re.sub(r"[aeiouy]", "", s[1:])
    return re.sub(r"(.)\1+", r"\1", s)


def _ratio(a, b):
    import difflib                                           # loaded only when two names are compared
    return difflib.SequenceMatcher(None, a, b).ratio()


def name_close(a, b, mode="repair"):
    """Could these two spellings be the same person's name?
         merge   a new name for the same person (stored): spelling >= 0.80 only, never sound-alike
         repair  a speech mis-hearing of a known name: sound-alike OR (spelling >= 0.82 and same first letter)
         query   as repair, plus a looser sound-alike ('harleid' ~ 'khaled'); used to find who a question is about"""
    a, b = (a or "").lower(), (b or "").lower()
    if not a or not b:
        return False
    if a == b:
        return True
    r = _ratio(a, b)
    if mode == "merge":                         # stored identity: spelling only ("Mark" and "Marco" sound alike but are two people)
        return r >= 0.80
    sa, sb = _skeleton(a), _skeleton(b)
    if len(sa) >= 3 and sa == sb:
        return True
    if r >= 0.82 and a[0] == b[0]:
        return True
    if mode == "query":
        return len(sa) >= 3 and len(sb) >= 3 and sa[0] == sb[0] and _ratio(sa, sb) >= 0.80
    return False


def role_base(role):
    """'real-life race engineer' -> 'engineer', 'team mate' -> 'teammate'."""
    t = normalise(role).replace("team mate", "teammate").replace("co driver", "codriver")
    parts = t.split()
    return parts[-1] if parts else ""


def valid_name(n):
    return bool(n) and n.isalpha() and 3 <= len(n) <= 20 and n not in _NOT_NAMES


# =====================================================================================================================
#  Speech repair for casual talk (runs BEFORE learning and BEFORE the AI)
#  Add your own in config.py:   CASUAL_FIXES = {"heard": "meant", ...}.  Names the driver corrects become aliases
#  automatically (stored in the memory file), so a mis-hearing is fixed once and for all.
# =====================================================================================================================
_WORD_FIXES = {
    "hallad": "khaled", "halad": "khaled", "hallid": "khaled", "haled": "khaled", "kaled": "khaled",
    "khaleed": "khaled", "khalad": "khaled", "khalled": "khaled", "kalid": "khaled", "hallead": "khaled",
    "baudium": "podium", "baudiums": "podiums", "boadium": "podium", "boadiums": "podiums",
    "bodium": "podium", "bodiums": "podiums", "podeum": "podium", "podeums": "podiums",
    "poduim": "podium", "poduims": "podiums", "podum": "podium", "podums": "podiums",
    "potium": "podium", "potiums": "podiums", "podiumes": "podiums", "podiom": "podium", "podioms": "podiums",
}
_PHRASE_FIXES = [(re.compile(p), r) for p, r in (
    (r"\bpod ium(s?)\b", r"podium\1"), (r"\bpo dium(s?)\b", r"podium\1"), (r"\bpodi um(s?)\b", r"podium\1"),
    (r"\bhall ad\b", "khaled"), (r"\bha lad\b", "khaled"), (r"\bka led\b", "khaled"),
)]


def is_naming(t):
    """True when this (normalised) sentence introduces or corrects a name."""
    return bool(any(rx.search(t) for rx in (_ROLE_A, _ROLE_B, _ROLE_C, _COR_1, _COR_2, _COR_3, _COR_4, _NAME_RE)))


def _closest(tok, names):
    for n in names:
        if name_close(tok, n, "repair"):
            return n
    return None


def repair_casual(text, names=(), aliases=None):
    """-> (fixed text, [what changed]). `names` = people already in memory, `aliases` = {old spelling: right name}."""
    t = normalise(text)
    if not t:
        return text or "", []
    notes = []
    for pat, rep in _PHRASE_FIXES:
        new = pat.sub(rep, t)
        if new != t:
            notes.append("%s -> %s" % (pat.pattern, rep))
            t = new
    fixes = dict(_WORD_FIXES)
    for k, v in (getattr(config, "CASUAL_FIXES", None) or {}).items():
        fixes[str(k).lower()] = str(v).lower()
    naming = is_naming(t)            # "my engineer is X", "his name is X", "no it's X not Y": the driver is SAYING the
    known = {str(n).lower() for n in names if n}                              # name, so it must never be rewritten to an old one
    if not naming:
        fixes.update({str(k).lower(): str(v).lower() for k, v in (aliases or {}).items()})   # the driver's own corrections win
    else:
        known = set()
    out = []
    for tok in t.split():
        base, poss = (tok[:-2], "'s") if tok.endswith("'s") else (tok, "")
        new = fixes.get(base)
        if new is None and known and len(base) >= 5 and base.isalpha() and base not in known and base not in _NOT_NAMES:
            new = _closest(base, known)
        if new and new != base:
            notes.append("%s -> %s" % (base, new))
            out.append(new + poss)
        else:
            out.append(tok)
    return " ".join(out), notes


# =====================================================================================================================
#  Patterns
# =====================================================================================================================
_ROLE = (r"(?:real life )?(?:(?:race|team|lead|chief|data|pit|performance) )?"
         r"(?:engineer|coach|mechanic|manager|mentor|instructor|teammate|team mate|co driver|spotter|friend|"
         r"brother|sister|dad|father|mum|mom|son|wife|girlfriend|boyfriend|rival|hero)")
_ROLE_A = re.compile(r"\bmy (?P<role>%s)(?:'s name)? (?:is|was|called|named)(?: called| named)? (?P<name>[a-z]+)\b" % _ROLE)
_ROLE_B = re.compile(r"\b(?P<name>[a-z]+) (?:is|was) my (?P<role>%s)\b" % _ROLE)
_ROLE_C = re.compile(r"\b(?:i have|i've got|ive got|i got) (?:a|an|my) (?P<role>%s) (?:called|named) (?P<name>[a-z]+)\b" % _ROLE)
MY_ROLE_RE = re.compile(r"\bmy (?:real life )?(?:(?:race|team|lead|chief|data|pit|performance) )?"
                        r"(?:engineer|coach|mechanic|manager|mentor|instructor|teammate|team mate|co driver|spotter)\b")
_NAME_RE = re.compile(r"\b(?:my name is|call me|i'm called|i am called|people call me|they call me) (?P<n>[a-z]+)\b")
_FAV_RE = re.compile(r"\bmy favou?rite (?P<k>car|track|circuit|driver|team|corner|race|series|game) is (?P<v>[a-z0-9 ]{2,40})")
_TEAM_RE = re.compile(r"\b(?:i race for|i drive for|my team is|my team's called) (?P<v>[a-z0-9 ]{2,40})")
_NOTE_RE = re.compile(r"^(?:please )?(?:remember|don't forget|note|keep in mind)(?: that)? "
                      r"(?P<f>(?:my|i|i'm|i am|our|we)\b.+)$")
_FORGET_RE = re.compile(r"\b(?:forget|wipe|clear|erase|delete)\b.*\b(?:memory|everything|all you know|what you know)\b")
_QUESTION_START = re.compile(r"^(?:who|whom|whose|what|why|how|when|where|which|can|could|should|would|does|do|did|is|are|was|were|will)\b")
_VERBS = {"won", "win", "got", "scored", "achieved", "took", "claimed", "earned", "has", "have", "had", "holds",
          "with", "taken", "collected"}
_AUX = {"has", "have", "had", "also", "just", "already", "ever", "once", "even", "now"}
_FILLERS = {"over", "about", "around", "nearly", "almost", "total", "of", "a", "the", "more", "than", "at", "least",
            "some", "roughly"}

# spoken corrections
_CUES = r"(?:no|nope|wrong|actually|sorry|correction|i mean|i meant|that's wrong|thats wrong)"
_COR_1 = re.compile(r"\b%s\b\W+(?:it'?s|it is|that'?s|thats|that is|his name is|her name is|the name is)\s+(?P<new>[a-z]+)"
                    r"(?:\s+not\s+(?P<old>[a-z]+))?" % _CUES)
_COR_2 = re.compile(r"\b(?:his|her) name is (?P<new>[a-z]+)\b")
_COR_3 = re.compile(r"\bit'?s (?P<new>[a-z]+) not (?P<old>[a-z]+)\b")
_COR_4 = re.compile(r"\bnot (?P<old>[a-z]+)\W+(?:it'?s|it is|its) (?P<new>[a-z]+)\b")
_COR_NUM = re.compile(r"\b%s\b\W+(?:it'?s|it is|that'?s|its) (?P<num>[a-z0-9 ]+?) "
                      r"(?P<what>podiums?|wins?|victor(?:y|ies)|championships?|titles?|poles?)\b" % _CUES)

# questions answered from memory
_Q_ROLE_1 = re.compile(r"\b(?:who|what)(?:'s|s|\s+is|\s+was|\s+are)?\s+(?:the\s+)?(?:name of\s+)?my\s+(?P<role>%s)\b" % _ROLE)
_Q_ROLE_2 = re.compile(r"\bwho\s+my\s+(?P<role>%s)\s+(?:is|was)\b" % _ROLE)
_MYROLE_RE = re.compile(r"\bmy (?P<role>%s)\b" % _ROLE)
_HOWMANY_RE = re.compile(r"\bhow many (?P<what>[a-z]+)\b")
_Q_PERSON = (
    re.compile(r"\b(?:who|what)(?:'s|s|\s+is|\s+was)\s+(?P<n>[a-z]+)\b"),
    re.compile(r"\b(?:tell me about|what do you know about|what can you tell me about|remind me about) (?P<n>[a-z]+)\b"),
    re.compile(r"\bremind me who (?P<n>[a-z]+)\b"),
)
_DRIVER_SUBJECT = re.compile(r"\b(?:do|did|have|had|can|could) i\b|\bi (?:have|got|won|had|scored|achieved|took)\b|\bmy own\b|\bme\b")

_LEADS = {  # normal first words of a radio sentence: a capitalised first word NOT in here (and not a known name) is suspect
    "copy", "roger", "understood", "noted", "right", "good", "nice", "well", "that", "that's", "your", "you", "you're", "the",
    "his", "her", "he", "she", "they", "yes", "no", "okay", "ok", "alright", "glad", "sorry", "we", "let", "let's", "keep",
    "hard", "brilliant", "legend", "fair", "sure", "thanks", "happy", "great", "pleasure", "what", "who", "how", "now",
    "back", "easy", "steady", "focus", "stay", "eyes", "box", "tyres", "fuel", "grip", "lap", "pace", "race", "driver",
    "good", "mate", "proper", "solid", "big", "bit", "some", "one", "two", "it's", "its", "this", "there", "here", "if",
    "and", "but", "so", "i", "i'm", "i'll", "i've", "don't", "do", "did", "is", "are", "was", "will", "can", "could",
    "would", "should", "not", "never", "always", "just", "only", "also", "still", "then", "when", "why", "keep", "take",
    "make", "get", "go", "give", "tell", "ask", "say", "think", "know", "see", "look", "watch", "remember", "stand",
}


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _clean_stats(raw):
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[str(k)[:30]] = int(v)
            except (TypeError, ValueError):
                pass
    return out


# =====================================================================================================================
#  1. Persistent memory (memory_store.json)
# =====================================================================================================================
class FactMemory:
    """Key facts about the driver and the people in his world.

    Thread-safe. Every change is saved at once with an atomic write (temp file -> fsync -> os.replace) and a rolling
    .bak. `rev` goes up on every change and listeners are told, so caches (chat history) never outlive the facts."""
    MAX_ENTITIES, MAX_NOTES, MAX_ALIASES = 40, 20, 80
    FOCUS_S = 180                       # a correction like "no, it's Khaled" refers to the person discussed in the last 3 minutes

    def __init__(self, path=None, legacy=None):
        self.path = path or memory_path()
        self.legacy = list(legacy) if legacy is not None else (legacy_paths(self.path) if path is None else [])
        self._lock = threading.RLock()
        self.data = self._empty()
        self.rev = 0
        self.last_entity = None          # who "he" / "she" means
        self.focus_ts = 0.0
        self._listeners = []
        self.load()

    @staticmethod
    def _empty():
        return {"version": SCHEMA_VERSION, "updated": 0.0, "driver": {"name": None, "stats": {}, "prefs": {}},
                "entities": {}, "aliases": {}, "notes": []}

    def add_listener(self, fn):
        self._listeners.append(fn)

    def _changed(self):
        self.rev += 1
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                traceback.print_exc()

    # ------------------------------------------------------------------ disk
    @staticmethod
    def _read_file(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        return raw

    def load(self):
        """Primary file -> its .bak -> a store left by another run mode. Nothing here may raise."""
        with self._lock:
            sources = [self.path, self.path + ".bak"] + list(self.legacy)
            loaded_from = None
            for src in sources:
                if not os.path.isfile(src):
                    continue
                try:
                    raw = self._read_file(src)
                    self.data = self._empty()
                    self._merge(raw)
                    loaded_from = src
                    break
                except Exception as e:
                    print("%s Could not read %s (%s: %s)." % (TAG, src, type(e).__name__, e))
                    if src == self.path:                                      # keep the damaged file for inspection
                        try:
                            os.replace(src, src + ".bad")
                        except OSError:
                            pass
                    self.data = self._empty()
            if loaded_from is None:
                print("%s No saved memory yet. It will be created at %s" % (TAG, self.path))
                return
            n, notes = len(self.data["entities"]), len(self.data["notes"])
            print("%s Memory loaded from %s: %d people, %d notes." % (TAG, loaded_from, n, notes))
            if loaded_from != self.path:                                      # recovered / migrated: write it where it belongs
                self._save_locked()

    def _merge(self, raw):
        d = raw.get("driver")
        if isinstance(d, dict):
            name = d.get("name")
            self.data["driver"]["name"] = name if isinstance(name, str) and name else None
            self.data["driver"]["stats"] = _clean_stats(d.get("stats"))
            prefs = d.get("prefs")
            if isinstance(prefs, dict):
                self.data["driver"]["prefs"] = {str(k)[:30]: str(v)[:60] for k, v in prefs.items()}
        ents = raw.get("entities")
        if isinstance(ents, dict):
            for key, e in list(ents.items())[:self.MAX_ENTITIES]:
                if not isinstance(e, dict) or not isinstance(e.get("name"), str):
                    continue
                role = e.get("role")
                self.data["entities"][str(key).lower()] = {
                    "name": e["name"][:30], "role": role[:60] if isinstance(role, str) and role else None,
                    "stats": _clean_stats(e.get("stats")), "ts": _num(e.get("ts")), "pinned": bool(e.get("pinned"))}
        al = raw.get("aliases")
        if isinstance(al, dict):
            for a, b in list(al.items())[:self.MAX_ALIASES]:
                if isinstance(a, str) and isinstance(b, str) and b.lower() in self.data["entities"] and a.lower() != b.lower():
                    self.data["aliases"][a.lower()] = b.lower()
        notes = raw.get("notes")
        if isinstance(notes, list):
            for n in notes[-self.MAX_NOTES:]:
                if isinstance(n, dict) and isinstance(n.get("text"), str):
                    self.data["notes"].append({"text": n["text"][:160], "ts": _num(n.get("ts"))})

    def _save_locked(self):
        self.data["version"], self.data["updated"] = SCHEMA_VERSION, time.time()
        payload = json.dumps(self.data, indent=2, ensure_ascii=False)
        last = None
        for attempt in range(4):                                              # antivirus / indexers briefly lock new files
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())                                      # on the disk, not just in the OS cache
                if os.path.isfile(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + ".bak")        # last known-good copy
                    except OSError:
                        pass
                os.replace(tmp, self.path)                                    # swapped in whole: a crash never leaves half a file
                return True
            except OSError as e:
                last = e
                time.sleep(0.15 * (attempt + 1))
        print("%s Could not save %s (%s)" % (TAG, self.path, last))
        return False

    def save(self):
        with self._lock:
            return self._save_locked()

    def _commit(self):
        """A fact changed: save it NOW, then tell the listeners."""
        self._save_locked()
        self._changed()

    def clear(self):
        with self._lock:
            self.data = self._empty()
            self.last_entity = None
            ok = self._save_locked()
            self._changed()
            return ok

    # ------------------------------------------------------------------ reading
    def names(self):
        with self._lock:
            out = [e["name"] for e in self.data["entities"].values()]
            if self.data["driver"]["name"]:
                out.append(self.data["driver"]["name"])
            return out

    def alias_map(self):
        with self._lock:
            return dict(self.data["aliases"])

    def find_entity(self, token, loose=True):
        """Key of the known person this word means: exact name, an old spelling (alias), or a sound-alike."""
        tok = token[:-2] if token.endswith("'s") else token
        if not tok:
            return None
        with self._lock:
            ents, aliases = self.data["entities"], self.data["aliases"]
            if tok in ents:
                return tok
            if aliases.get(tok) in ents:
                return aliases[tok]
            if loose and len(tok) >= 5 and tok.isalpha() and tok not in _NOT_NAMES:
                for key in ents:
                    if name_close(tok, key, "query"):
                        return key
                for alias, target in aliases.items():
                    if target in ents and name_close(tok, alias, "query"):
                        return target
        return None

    def mentioned(self, tokens):
        """Keys of known people named in these tokens ("khaled", "khaled's", "harleid")."""
        hits = []
        for tok in tokens:
            key = self.find_entity(tok)
            if key and key not in hits:
                hits.append(key)
        return hits

    def by_role(self, role):
        base = role_base(role)
        with self._lock:
            return [k for k, e in self.data["entities"].items() if e["role"] and role_base(e["role"]) == base]

    def recent_entity(self):
        with self._lock:
            ents = self.data["entities"]
            if self.last_entity in ents:
                return self.last_entity
            return max(ents, key=lambda k: ents[k]["ts"]) if ents else None

    def focus_fresh(self):
        return self.last_entity in self.data["entities"] and time.time() - self.focus_ts < self.FOCUS_S

    def touch(self, key):
        """`key` is who we are talking about now (for "he", and for "no, it's Khaled")."""
        if key in self.data["entities"]:
            self.last_entity, self.focus_ts = key, time.time()

    def describe(self, key):
        """'Khaled is your real-life race engineer, with 150 podiums'"""
        with self._lock:
            e = self.data["entities"][key]
            stats = " and ".join("%d %s" % (n, what) for what, n in e["stats"].items())
            if e["role"]:
                return "%s is your %s%s" % (e["name"], e["role"], (", with " + stats) if stats else "")
            if stats:
                return "%s has %s" % (e["name"], stats)
            return "I only know the name %s" % e["name"]

    def fact_lines(self, text="", max_chars=900):
        """Facts as short sentences. People named in `text` come first. Capped, so small models stay fast."""
        with self._lock:
            mentioned = self.mentioned(tokens_of(text))
            ents = sorted(self.data["entities"].items(), key=lambda kv: (kv[0] not in mentioned, -kv[1]["ts"]))
            d, lines = self.data["driver"], []
            if d["name"]:
                lines.append("The driver's name is %s." % d["name"])
            for _key, e in ents:
                if e["role"]:
                    lines.append("%s is the driver's %s." % (e["name"], e["role"]))
                for what, n in e["stats"].items():
                    lines.append("%s has %d %s." % (e["name"], n, what))
            for what, n in d["stats"].items():
                lines.append("The driver has %d %s." % (n, what))
            for k, v in d["prefs"].items():
                lines.append("The driver's %s is %s." % (k, v))
            lines += [n["text"] for n in self.data["notes"][-5:]]
            out, size = [], 0
            for ln in lines:
                if size + len(ln) > max_chars:
                    break
                out.append(ln)
                size += len(ln) + 1
            return out

    facts_for = fact_lines              # old name, kept for callers / tests

    # ------------------------------------------------------------------ writing: people
    def _entity(self, name):
        key, ents = name.lower(), self.data["entities"]
        if key not in ents:
            if len(ents) >= self.MAX_ENTITIES:
                gone = min(ents, key=lambda k: ents[k]["ts"])
                del ents[gone]
                self.data["aliases"] = {a: b for a, b in self.data["aliases"].items() if b != gone}
            ents[key] = {"name": name.title(), "role": None, "stats": {}, "ts": time.time(), "pinned": False}
        ents[key]["ts"] = time.time()
        return ents[key]

    def _rename(self, old_key, new_name):
        """Same person, better spelling. Keeps role and stats; the old spelling becomes an alias. Returns the new key."""
        new_key = new_name.lower()
        if new_key == old_key:
            return old_key
        ents, aliases = self.data["entities"], self.data["aliases"]
        ent = ents.pop(old_key)
        if new_key in ents:                                                   # that name already exists: fold into it
            tgt = ents[new_key]
            for k, v in ent["stats"].items():
                tgt["stats"].setdefault(k, v)
            tgt["role"] = tgt["role"] or ent["role"]
            ent = tgt
        else:
            ent["name"] = new_name.title()
            ents[new_key] = ent
        ent["ts"] = time.time()
        for a, tgt_key in list(aliases.items()):
            if tgt_key == old_key:
                aliases[a] = new_key
        aliases[old_key] = new_key
        aliases.pop(new_key, None)
        while len(aliases) > self.MAX_ALIASES:
            aliases.pop(next(iter(aliases)))
        if self.last_entity == old_key:
            self.last_entity = new_key
        return new_key

    def _set_role(self, name, role):
        ents = self.data["entities"]
        role_clean = role.replace("real life", "real-life")
        base = role_base(role)
        target = name.lower() if name.lower() in ents else None               # the exact name already exists
        if target is None and name.lower() in self.data["aliases"]:           # an old spelling, stated again:
            aliased = self.data["aliases"][name.lower()]
            if aliased in ents and ents[aliased].get("pinned"):
                target = aliased                                              # confirmed spelling: only "no, it's X" changes it
            else:
                target = self._rename(aliased, name)                          # otherwise the newest spelling wins
        if target is None:
            holder = (self.by_role(base) or [None])[0] if base in _UNIQUE_ROLES else None
            cand = holder or next((k for k in ents if name_close(name, k, "merge")), None)
            if cand and ents[cand].get("pinned") and name_close(name, cand, "query"):
                self.data["aliases"][name.lower()] = cand                     # confirmed spelling + a sound-alike = speech
                target = cand                                                 # drift: remember it as an alias, change nothing
            elif cand:                                                        # "my engineer is X": there is only one
                target = self._rename(cand, name)
        if target is None:
            self._entity(name)
            target = name.lower()
        ent = ents[target]
        if base in _UNIQUE_ROLES:                                             # nobody else keeps that job
            for k, e in ents.items():
                if k != target and e["role"] and role_base(e["role"]) == base:
                    e["role"] = None
        prev = ent["role"]
        ent["role"], ent["ts"] = role_clean, time.time()
        self.touch(target)
        return {"kind": "role", "name": ent["name"], "role": role_clean, "changed": prev != role_clean}

    def _set_stat(self, key, what, n):
        """key = entity key, or None for the driver. Overwrites (a correction replaces the old number)."""
        if key is None:
            stats = self.data["driver"]["stats"]
            prev, stats[what] = stats.get(what), n
            return {"kind": "stat", "name": None, "n": n, "what": what, "prev": prev}
        ent = self._entity(key)
        prev, ent["stats"][what] = ent["stats"].get(what), n
        self.touch(key)
        return {"kind": "stat", "name": ent["name"], "n": n, "what": what, "prev": prev}

    # ------------------------------------------------------------------ learning
    def learn(self, text):
        """Extract facts / corrections from one sentence and save them at once. Returns a list of dicts."""
        t = normalise(text)
        tokens = t.split()
        learned = []
        with self._lock:
            learned += self._learn_corrections(t)
            learned += self._learn_roles(t)
            learned += self._learn_stats(tokens)
            learned += self._learn_profile(t)
            if not learned:
                note = self._learn_note(t)
                if note:
                    learned.append(note)
            if learned:
                self._commit()
                print("%s Learned: %s" % (TAG, "; ".join(describe_learned(x) for x in learned)))
        return learned

    def _learn_corrections(self, t):
        out, ents = [], self.data["entities"]
        for rx in (_COR_1, _COR_3, _COR_4, _COR_2):
            m = rx.search(t)
            if not m:
                continue
            new, old = m.group("new"), m.groupdict().get("old")
            if not valid_name(new):
                continue
            key = self.find_entity(old) if old else None
            explicit = rx is _COR_2 or old is not None
            if key is None:
                key = self.recent_entity()
            if key is None or new == key:
                continue
            if not explicit and not name_close(new, key, "query"):
                continue                      # "no it's raining" must never rename anyone: only a sound-alike counts here
                                              # (a different name needs "his name is X", "it's X not Y" or "my engineer is X")
            prev = ents[key]["name"]
            k = self._rename(key, new)
            ents[k]["pinned"] = True                                          # the driver confirmed this spelling
            self.touch(k)
            out.append({"kind": "rename", "old": prev, "new": ents[k]["name"]})
            return out
        m = _COR_NUM.search(t)
        key = self.recent_entity() if self.focus_fresh() else None
        if m and key:
            n = to_number(m.group("num").split())
            if n is not None:
                out.append(self._set_stat(key, _STAT_NOUNS.get(m.group("what"), _STAT_NOUNS.get(m.group("what").rstrip("s"), "podiums")), n))
        return out

    def _learn_roles(self, t):
        out, seen = [], set()
        for rx in (_ROLE_A, _ROLE_B, _ROLE_C):
            for m in rx.finditer(t):
                name, role = m.group("name"), m.group("role")
                if not valid_name(name) or (name, role) in seen:
                    continue
                seen.add((name, role))
                out.append(self._set_role(name, role))
        return out

    def _learn_stats(self, tokens):
        out = []
        for i, tok in enumerate(tokens):
            what = _STAT_NOUNS.get(tok)
            if not what:
                continue
            j = i                                                   # walk back over the number: "one hundred and fifty"
            while j > 0:
                p = tokens[j - 1]
                if p.isdigit() or p in _NUMW or p == "a" or (p == "and" and j >= 2 and tokens[j - 2] in _NUMW):
                    j -= 1
                else:
                    break
            n = to_number(tokens[j:i])
            if n is None:
                continue
            pos = j - 1
            while pos >= 0 and tokens[pos] in _FILLERS:             # "a total of", "over", "nearly"
                pos -= 1
            if pos < 0 or tokens[pos] not in _VERBS:
                continue
            pos -= 1
            while pos >= 0 and tokens[pos] in _AUX:
                pos -= 1
            subj = tokens[pos] if pos >= 0 else "i"
            subj = subj[:-2] if subj.endswith("'s") else subj
            if subj == "i":                                         # the driver himself
                out.append(self._set_stat(None, what, n))
                continue
            key = self.recent_entity() if subj in _PRONOUNS else self.find_entity(subj)
            if key is None:
                continue
            out.append(self._set_stat(key, what, n))
        return out

    def _learn_profile(self, t):
        out, d = [], self.data["driver"]
        m = _NAME_RE.search(t)
        if m and valid_name(m.group("n")) or (m and len(m.group("n")) >= 2 and m.group("n") not in _COMMON):
            d["name"] = m.group("n").title()
            if not getattr(config, "DRIVER_NAME", ""):
                config.DRIVER_NAME = d["name"]                      # the old persona path uses it too
            out.append({"kind": "driver_name", "name": d["name"]})
        m = _FAV_RE.search(t)
        if m:
            v = re.split(r"\b(?:and|but|because)\b", m.group("v"))[0].strip()
            v = " ".join(v.split()[:5])
            if v:
                d["prefs"][m.group("k")] = v
                out.append({"kind": "pref", "key": m.group("k"), "value": v})
        m = _TEAM_RE.search(t)
        if m:
            v = " ".join(re.split(r"\b(?:and|but|because)\b", m.group("v"))[0].split()[:5])
            if v:
                d["prefs"]["team"] = v
                out.append({"kind": "pref", "key": "team", "value": v})
        return out

    def _learn_note(self, t):
        m = _NOTE_RE.match(t)
        if not m:
            return None
        f = m.group("f")
        for pat, rep in ((r"\bi'm\b|\bi am\b", "the driver is"), (r"\bmy\b", "the driver's"), (r"\bi\b", "the driver")):
            f = re.sub(pat, rep, f)
        f = f.strip()[:160]
        f = f[0].upper() + f[1:] + ("" if f.endswith(".") else ".")
        self.data["notes"].append({"text": f, "ts": time.time()})
        del self.data["notes"][:-self.MAX_NOTES]
        return {"kind": "note", "text": f}


def describe_learned(x):
    k = x["kind"]
    if k == "role":
        return "%s is the driver's %s" % (x["name"], x["role"])
    if k == "stat":
        return "%s has %d %s" % (x["name"] or "the driver", x["n"], x["what"])
    if k == "rename":
        return "%s is really %s" % (x["old"], x["new"])
    if k == "driver_name":
        return "the driver's name is %s" % x["name"]
    if k == "pref":
        return "the driver's %s is %s" % (x["key"], x["value"])
    return x.get("text", "")


# =====================================================================================================================
#  2. The chat handler
# =====================================================================================================================
_PROMPT = """You are {name}, the driver's race engineer on the pit wall, talking over the team radio during a live Assetto Corsa session.
VOICE: calm, confident, dry wit, warm underneath. British paddock English with contractions.
FORMAT: ONE or TWO short sentences, 30 words at most, spoken radio style. Plain words only: no lists, no markdown, no emojis, no quotation marks, no stage directions.
CHARACTER: you ARE the engineer. Never say you are an AI, a model or a program.
CHAT: when the driver tells you something about himself or people he knows, acknowledge it warmly in character, then steer back to the job if it fits. Never lecture.
TRUTH: names, jobs and numbers about the driver and the people in his life come ONLY from the MEMORY section at the end of this message. Never invent them, never guess them, never reuse a name or number from an example. If MEMORY does not say, tell the driver you do not have that yet.
EXAMPLES (style only: they contain no facts)
Driver: tell me a joke
Engineer: Why did the tyre go to therapy? Too much pressure. Right, back to work.
Driver: i'm nervous about the start
Engineer: Breathe. You've done the laps, so hit your marks and the pace will come.
{just}{live}{memory}"""

_AI_SPEAK = re.compile(r"\b(as an ai|language model|i am an ai|i'm an ai|i cannot|i can't (?:help|assist|do that))\b", re.I)


class LLMEngineer:
    def __init__(self, memory=None, backends=None, engineer_name=None, state_fn=None):
        """backends: list of callables  fn(system, user, history) -> str | None  (tried in order)."""
        self.memory = memory if memory is not None else FactMemory()
        self.backends = list(backends or [])
        self.name = engineer_name or getattr(config, "ENGINEER_NAME", "Alex")
        self.state_fn = state_fn or (lambda: {})
        self._hist = deque(maxlen=4)             # (question, answer, memory revision)
        self._query = ""                         # the sentence the Brain is working on (for the knowledge-route prompt)
        self.brain = None
        self._base_hint = getattr(config, "WHISPER_HINT", "")
        self.memory.add_listener(self._on_memory_change)
        self.sync_config()

    @classmethod
    def from_brain(cls, brain):
        eng = cls(backends=backends_from_brain(brain), state_fn=lambda: brain.state)
        eng.brain = brain
        return eng

    # ------------------------------------------------------------------ keep everything in step with memory
    def _on_memory_change(self):
        """Any new / corrected fact: forget every answer that was given before it (they may be wrong now)."""
        self._hist.clear()
        if self.brain is not None:
            try:
                self.brain.history.clear()
            except Exception:
                pass
        self.sync_config()

    def sync_config(self):
        """Names become 'banter' words for the router, fuzzy words for speech_repair, and hints for Whisper."""
        names = self.memory.names()
        kw = getattr(config, "CHAT_KEYWORDS", None)
        if not isinstance(kw, list):
            kw = list(kw or [])
            config.CHAT_KEYWORDS = kw
        for n in names:
            if n.lower() not in kw:
                kw.append(n.lower())
        hint = (self._base_hint + " podium, podiums.").strip()
        if names:
            hint += " Names: %s." % ", ".join(names)
        config.WHISPER_HINT = hint

    def repair(self, text):
        fixed, notes = repair_casual(text, self.memory.names(), self.memory.alias_map())
        if notes:
            print("%s repaired %r -> %r  (%s)" % (TAG, text, fixed, "; ".join(notes)))
        return fixed

    def flush(self):
        """Write the memory now (the program also does this after every change)."""
        return self.memory.save()

    # ------------------------------------------------------------------ the MEMORY block (strict anchoring)
    def memory_block(self, text=""):
        lines = self.memory.fact_lines(text)
        body = "\n".join("- " + f for f in lines) if lines else "- (nothing saved yet)"
        return ("MEMORY (saved from the driver's own words. It is ALWAYS TRUE and overrides anything you believe):\n"
                + body + "\n"
                "MEMORY RULES: for anything about the driver or the people he mentions, use ONLY the lines above and copy "
                "names and numbers exactly. Never use another name or number. If the line is not there, say you do not "
                "have it yet.\n")

    # ------------------------------------------------------------------ deterministic answers (no AI involved)
    def answer_from_memory(self, text):
        """A spoken answer built ONLY from the saved facts, or None when this is not a question about them.
        Facts present -> the answer is exactly what was saved. A fact about a saved person that is missing -> we say
        so, instead of letting a model make one up."""
        t = normalise(text)
        tokens = t.split()
        if len(tokens) < 3:
            return None
        ans = None
        mt = _Q_ROLE_1.search(t) or _Q_ROLE_2.search(t)
        if mt and not _HOWMANY_RE.search(t):
            ans = self._qa_role(mt.group("role"), t)
        if ans is None and _HOWMANY_RE.search(t):
            ans = self._qa_howmany(t, tokens)
        if ans is None:
            ans = self._qa_person(t)
        if ans is None:
            ans = self._qa_driver(t)
        return persona.tidy_radio(ans, self.name, max_sentences=3, max_chars=320) if ans else None

    def _qa_role(self, role, t):
        keys = self.memory.by_role(role)
        if keys:
            self.memory.touch(keys[0])
            return self.memory.describe(keys[0]) + "."
        if role_base(role) == "engineer" and "real life" not in role and "real life" not in t:
            return "I'm %s, your engineer on the radio. You haven't told me about a real-life one." % self.name
        return "You haven't told me who your %s is yet." % role.replace("real life", "real-life")

    def _subject(self, t, tokens):
        m = _MYROLE_RE.search(t)
        if m:
            keys = self.memory.by_role(m.group("role"))
            return ("entity", keys[0]) if keys else ("missing_role", m.group("role"))
        for tok in tokens:
            key = self.memory.find_entity(tok)
            if key:
                return "entity", key
        if _DRIVER_SUBJECT.search(t):
            return "driver", None
        if set(tokens) & _PRONOUNS:
            key = self.memory.recent_entity()
            if key:
                return "entity", key
        return None

    def _qa_howmany(self, t, tokens):
        what = _STAT_NOUNS.get(_HOWMANY_RE.search(t).group("what"))
        if not what:
            return None                                                       # "how many laps": not ours
        kind, key = self._subject(t, tokens) or (None, None)
        if kind == "missing_role":
            return "You haven't told me who your %s is, so I don't have that." % key.replace("real life", "real-life")
        if kind == "entity":
            self.memory.touch(key)
            ent = self.memory.data["entities"][key]
            n = ent["stats"].get(what)
            if n is not None:
                return "%s has %d %s." % (ent["name"], n, what)
            return "I don't have a %s count for %s yet. Tell me and I'll remember it." % (what.rstrip("s"), ent["name"])
        if kind == "driver":
            n = self.memory.data["driver"]["stats"].get(what)
            return "You have %d %s." % (n, what) if n is not None else "You haven't told me your %s yet." % what
        return None

    def _qa_person(self, t):
        for rx in _Q_PERSON:
            m = rx.search(t)
            if m:
                key = self.memory.find_entity(m.group("n"))
                if key:
                    self.memory.touch(key)
                    return self.memory.describe(key) + "."
        return None

    def _qa_driver(self, t):
        d = self.memory.data["driver"]
        if re.search(r"\b(?:what(?:'s| is)|whats) my name\b|\bwho am i\b", t):
            return "You're %s." % d["name"] if d["name"] else "You haven't told me your name yet."
        m = re.search(r"\bwhat(?:'s| is|s) my favou?rite (?P<k>car|track|circuit|driver|team|corner|race|series|game)\b", t)
        if m:
            v = d["prefs"].get(m.group("k"))
            return "Your favourite %s is %s." % (m.group("k"), v) if v else "You haven't told me your favourite %s yet." % m.group("k")
        if re.search(r"\bwhat team do i (?:race|drive) for\b", t):
            v = d["prefs"].get("team")
            return "You race for %s." % v if v else "You haven't told me your team yet."
        if re.search(r"\bwhat do you (?:know|remember) about me\b|\bwhat have i told you\b|\bwhat do you know about me\b", t):
            lines = self.memory.fact_lines("", max_chars=400)
            return " ".join(lines[:3]) if lines else "Nothing yet. Tell me about yourself."
        return None

    # ------------------------------------------------------------------ deciding
    def _wants(self, text, tokens):
        t = " ".join(tokens)
        return bool(self.memory.mentioned(tokens)) or (bool(MY_ROLE_RE.search(t)) and not _QUESTION_START.match(t))

    def _say_driver(self, text):
        print("\n\U0001F3A7 [DRIVER]: %s" % text)

    def handle_if_casual(self, text):
        """True = handled (spoken). False = not casual talk: let Brain.handle deal with it, exactly as before."""
        brain = self.brain
        if brain is None:
            return False
        t = normalise(text)
        tokens = t.split()
        if len(tokens) < 3:
            return False
        if _FORGET_RE.search(t):
            self._say_driver(text)
            self.memory.clear()
            brain.say("Done. My memory of you is wiped clean.")
            return True
        learned = self.memory.learn(text)                                     # saved to disk before we answer
        if learned:
            self._say_driver(text)
            self._respond(text, learned)
            return True
        ans = self.answer_from_memory(text)                                   # memory FIRST, no AI
        if ans:
            self._say_driver(text)
            print("%s answered from memory: %r" % (TAG, ans))
            brain.last_question = text
            brain.say(ans)
            return True
        if self._wants(text, tokens):
            self._say_driver(text)
            self._respond(text, [])
            return True
        return False

    def attach(self, brain):
        """Also cover everything that reaches Brain._ask_ai, and put the memory into the AI prompt of EVERY route
        (chat AND knowledge). Safe to call twice."""
        self.brain = brain
        if getattr(brain, "_cesium_memory_attached", False):
            return
        brain._cesium_memory_attached = True
        original_ask, original_prompt = brain._ask_ai, brain._system_prompt

        def _system_prompt(route="knowledge"):
            return original_prompt(route) + "\n" + self.memory_block(self._query)

        def _ask_ai(t):
            self._query = t
            try:
                tokens = tokens_of(t)
                if len(tokens) >= 3:
                    ans = self.answer_from_memory(t)
                    if ans:
                        brain.last_question = t
                        brain.say(ans)
                        return
                    if brain.router.classify(t, tokens) == "chat":
                        if not self.memory.mentioned(tokens):
                            known = brain.memory.get_knowledge(t)             # something taught with "remember that"
                            if known:
                                brain.last_question = t
                                brain.say(known[0])
                                return
                        self._respond(t, [])
                        return
            except Exception:
                traceback.print_exc()
            original_ask(t)

        brain._ask_ai, brain._system_prompt = _ask_ai, _system_prompt

    def _respond(self, text, learned):
        brain = self.brain
        if brain is not None:
            brain.last_question = text
        if learned and not getattr(config, "CHAT_LLM_ACK", False):
            answer = self._ack(learned)                                       # instant and exactly what was saved
            print("%s acknowledged from memory: %r" % (TAG, answer))
        else:
            answer = self.reply(text, learned)
            if brain is not None:
                brain.history.append((text, answer))
        if brain is not None:
            brain.say(answer)

    # ------------------------------------------------------------------ answering (AI, checked against memory)
    def _live_line(self):
        try:
            st = self.state_fn() or {}
        except Exception:
            return ""
        if not st.get("live"):
            return ""
        bits = [str(st.get(k)) for k in ("car_name", "track_name") if st.get(k)]
        if st.get("position"):
            bits.append("P%s" % st["position"])
        return ("LIVE: %s.\n" % ", ".join(bits)) if bits else ""

    def system_prompt(self, text, learned=()):
        just = ""
        if learned:
            just = "THE DRIVER JUST TOLD YOU (now saved): %s.\n" % "; ".join(describe_learned(x) for x in learned)
        return _PROMPT.format(name=self.name or "Alex", just=just, live=self._live_line(),
                              memory=self.memory_block(text))

    def _clean(self, text):
        if not text:
            return None
        out = persona.tidy_radio(text, self.name)
        if not out or re.match(r"^\s*(?:SEARCH\s*:|NONE\b)", out, re.I) or _AI_SPEAK.search(out):
            return None
        return out

    def _history_pairs(self):
        rev = self.memory.rev
        return [(q, a) for q, a, r in self._hist if r == rev][-2:]

    def _is_memory_turn(self, text, learned):
        t = normalise(text)
        return bool(learned or self.memory.mentioned(t.split()) or MY_ROLE_RE.search(t)
                    or re.search(r"\b(?:about me|my name|real life)\b", t))

    def _allowed_numbers(self, text):
        blob = " ".join(self.memory.fact_lines("", max_chars=10 ** 6)) + " " + text + " " + self._live_line()
        try:
            blob += " " + json.dumps(self.state_fn() or {}, default=str)
        except Exception:
            pass
        return numbers_in(blob)

    def _allowed_names(self):
        allow = {n.lower() for n in self.memory.names()} | set(self.memory.alias_map())
        allow |= {str(self.name).lower(), str(getattr(config, "DRIVER_NAME", "") or "").lower(), "assetto", "corsa"}
        try:
            st = self.state_fn() or {}
            for k in ("car_name", "track_name"):
                allow |= set(re.findall(r"[a-z]+", str(st.get(k) or "").lower()))
        except Exception:
            pass
        return allow

    def _grounded(self, answer, text, strict):
        """False when the answer states a number or a name that the memory / live data / question do not contain."""
        toks = tokens_of(answer)
        stat_ctx = any(tok in _STAT_NOUNS for tok in toks)
        allowed = self._allowed_numbers(text)
        for n in numbers_in(answer):
            if n not in allowed and ((strict and (n > 10 or stat_ctx)) or (stat_ctx and n > 10)):
                print("%s rejected an AI answer: %d is not in the memory (%r)" % (TAG, n, answer))
                return False
        if strict:
            allow = self._allowed_names()
            for sent in re.split(r"(?<=[.!?])\s+", answer):
                words = re.findall(r"[A-Za-z][A-Za-z']*", sent)
                for i, w in enumerate(words):
                    base = w[:-2] if w.lower().endswith("'s") else w
                    if len(base) < 3 or not base[0].isupper() or base.lower() in allow:
                        continue
                    if i == 0 and base.lower() in _LEADS:
                        continue
                    print("%s rejected an AI answer: the name %r is not in the memory (%r)" % (TAG, base, answer))
                    return False
        return True

    def reply(self, text, learned=None):
        """One or two short in-character sentences. Never raises, never returns an empty string."""
        if learned is None:
            learned = self.memory.learn(text)
        direct = self.answer_from_memory(text)                                # a saved fact answers itself
        if direct:
            return direct
        system = self.system_prompt(text, learned)
        strict = self._is_memory_turn(text, learned)
        history = self._history_pairs()
        answer, t0 = None, time.time()
        for backend in self.backends:
            try:
                answer = self._clean(backend(system, text, history))
            except Exception as e:
                print("%s backend failed (%s: %s)" % (TAG, type(e).__name__, e))
                answer = None
            if answer and not self._grounded(answer, text, strict):
                answer = None
            if answer:
                break
        if answer:
            print("%s AI answered in %.1fs" % (TAG, time.time() - t0))
            self._hist.append((text, answer, self.memory.rev))
        else:
            answer = self._offline(text, learned)
            print("%s no usable AI answer, using the memory reply" % TAG)
        return answer

    # ------------------------------------------------------------------ the answer when no AI is available / trusted
    def _offline(self, text, learned):
        if learned:
            return self._ack(learned)
        keys = self.memory.mentioned(tokens_of(text))
        if keys:
            return "%s. Hard act to follow, so keep it tidy out there." % self.memory.describe(keys[0])
        return persona.fallback_chat()

    @staticmethod
    def _ack(learned):
        for x in learned:
            if x["kind"] == "rename":
                return "Copy that. Correcting my notes: it's %s, not %s." % (x["new"], x["old"])
        roles = [x for x in learned if x["kind"] == "role"]
        stats = [x for x in learned if x["kind"] == "stat"]
        for s in stats:
            if s.get("prev") is not None and s["prev"] != s["n"]:
                return "Corrected. %s has %d %s, not %d." % (s["name"] or "You", s["n"], s["what"], s["prev"]) \
                    if s["name"] else "Corrected. You have %d %s, not %d." % (s["n"], s["what"], s["prev"])
        for r in roles:
            for s in stats:
                if s["name"] == r["name"]:
                    return ("Copy that, %s's %d %s as your %s set the standard on this pit wall. "
                            "Let's make %s proud on this stint." % (r["name"], s["n"], s["what"], r["role"], r["name"]))
        if stats:
            s = stats[0]
            if s["name"]:
                return "%d %s for %s? That's the standard. Let's make %s proud." % (s["n"], s["what"], s["name"], s["name"])
            return "%d %s, nice work. Let's add to the tally." % (s["n"], s["what"])
        if roles:
            verb = "Updated" if roles[0].get("changed") else "Copy that"
            return "%s. %s is your %s. Good man to have in your corner." % (verb, roles[0]["name"], roles[0]["role"])
        for x in learned:
            if x["kind"] == "driver_name":
                return "Copy that, %s. Pleasure to work with you." % x["name"]
            if x["kind"] == "pref":
                return "Noted. Your favourite %s is %s. I'll remember that." % (x["key"], x["value"])
        return "Noted. I'll remember that."


def backends_from_brain(brain):
    """Ollama first (it is warmed up by Brain at start-up), then the built-in model. Both already have timeouts."""
    def ollama(system, user, history):
        return brain.ollama.chat(system, user, history, max_tokens=int(getattr(config, "OLLAMA_MAX_TOKENS", 80)))

    def builtin(system, user, history):
        if getattr(config, "USE_LLM", False) and brain.llm.ready:
            return brain.llm.chat(system, user, history, max_tokens=70)
        return None
    return [ollama, builtin]


# =====================================================================================================================
#  Self-test:   python llm_engineer.py        (no game, no Ollama, uses temporary files only)
# =====================================================================================================================
def _selftest():
    import tempfile
    fails = []

    def check(label, got, want):
        ok = got == want
        print("%s  %s" % ("PASS" if ok else "FAIL", label) + ("" if ok else "\n        got : %r\n        want: %r" % (got, want)))
        if not ok:
            fails.append(label)

    def has(label, text, *needles, absent=()):
        ok = all(n.lower() in (text or "").lower() for n in needles) and not any(a.lower() in (text or "").lower() for a in absent)
        print("%s  %s" % ("PASS" if ok else "FAIL", label) + ("" if ok else "\n        got : %r" % text))
        if not ok:
            fails.append(label)

    class FakeRouter:
        def classify(self, text, tokens):
            return "knowledge"

    class FakeBrain:
        def __init__(self):
            self.said, self.history, self.state, self.last_question = [], deque(maxlen=4), {}, None
            self.router, self.asked_original = FakeRouter(), []
            self.memory = type("M", (), {"get_knowledge": lambda s, q: None})()

        def say(self, text, prio=1):
            self.said.append(text)

        def _ask_ai(self, t):
            self.asked_original.append(t)
            self.said.append("ORIGINAL:" + self._system_prompt("knowledge"))

        def _system_prompt(self, route="knowledge"):
            return "BASE PROMPT"

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "data", "memory_store.json")
    print("== 1. learning + persistence ==")
    eng = LLMEngineer(memory=FactMemory(path, legacy=[]), backends=[])
    fixed = eng.repair("My real-life race engineer is Hallad and he won 150 baudiums")
    check("speech repair", fixed, "my real life race engineer is khaled and he won 150 podiums")
    learned = eng.memory.learn(fixed)
    check("role learned", (learned[0]["name"], learned[0]["role"]), ("Khaled", "real-life race engineer"))
    check("stat learned (he -> Khaled)", (learned[1]["name"], learned[1]["n"], learned[1]["what"]), ("Khaled", 150, "podiums"))
    check("file exists on disk right away", os.path.isfile(path), True)
    eng2 = LLMEngineer(memory=FactMemory(path, legacy=[]), backends=[])      # "restart the app"
    check("memory reloaded after restart", eng2.memory.fact_lines("khaled"),
          ["Khaled is the driver's real-life race engineer.", "Khaled has 150 podiums."])

    print("\n== 2. questions are answered from memory, never by the AI ==")
    boom = lambda s, u, h: "Your real-life race engineer is Luca and he has 20 podiums."
    eng3 = LLMEngineer(memory=FactMemory(path, legacy=[]), backends=[boom])
    has("who is my real life race engineer", eng3.answer_from_memory("who is my real life race engineer"), "khaled", "real-life race engineer", absent=("luca",))
    has("what's my engineer's name", eng3.answer_from_memory("what's my real life engineer's name"), "khaled")
    has("who is my engineer called", eng3.answer_from_memory("who is my real life race engineer again"), "khaled")
    has("how many podiums does khaled have", eng3.answer_from_memory("how many podiums does khaled have"), "150", absent=("20",))
    has("how many podiums does my engineer have", eng3.answer_from_memory("how many podiums does my real life engineer have"), "150")
    has("how many podiums does he have (pronoun)", eng3.answer_from_memory("how many podiums does he have"), "khaled", "150")
    has("who is khaled", eng3.answer_from_memory("who is khaled"), "khaled", "engineer")
    has("mis-heard name in a question still finds him", eng3.answer_from_memory("who is harleid"), "khaled")
    has("missing fact: honest, not invented", eng3.answer_from_memory("how many wins does khaled have"), "don't have", "khaled")
    has("missing role: honest", eng3.answer_from_memory("who is my real life coach"), "haven't told me")
    check("telemetry question is not claimed", eng3.answer_from_memory("how many laps of fuel do i have"), None)
    check("unknown celebrity goes to the AI", eng3.answer_from_memory("who is senna"), None)
    has("AI hallucination is rejected, memory answer used", eng3.reply("tell me about my real life engineer khaled"), "khaled", absent=("luca", "20 podiums"))
    has("AI answer that invents a number is rejected", LLMEngineer(memory=FactMemory(path, legacy=[]), backends=[
        lambda s, u, h: "Khaled has 20 podiums, a real legend."]).reply("is khaled a good engineer"), "khaled", absent=("20 podiums",))
    ok_ai = LLMEngineer(memory=FactMemory(path, legacy=[]), backends=[
        lambda s, u, h: "*laughs* Khaled's 150 podiums speak for themselves. Keep it tidy. Third sentence."]).reply("is khaled a good engineer")
    check("grounded AI answer is kept (and cleaned)", ok_ai, "Khaled's 150 podiums speak for themselves. Keep it tidy.")

    print("\n== 3. the prompt is anchored ==")
    sp = eng3.system_prompt("how many podiums does khaled have")
    has("memory block is in the prompt", sp, "Khaled has 150 podiums.", "ALWAYS TRUE")
    check("memory block comes LAST", sp.rstrip().endswith("have it yet."), True)
    has("no concrete example facts left in the prompt", sp, absent=("luca", "20 podiums"))

    print("\n== 4. corrections update the file at once ==")
    p2 = os.path.join(tmp, "c", "memory_store.json")
    m = FactMemory(p2, legacy=[])
    e = LLMEngineer(memory=m, backends=[])
    m.learn("my real life race engineer is harleid and he has 150 podiums")
    check("misheard name stored first", [x for x in m.names()], ["Harleid"])
    m.learn("my real life race engineer is khaled")
    check("same job, new name = rename, not a 2nd person", m.names(), ["Khaled"])
    check("stats survived the rename", m.fact_lines(""), ["Khaled is the driver's real-life race engineer.", "Khaled has 150 podiums."])
    check("old spelling is now an alias", m.alias_map(), {"harleid": "khaled"})
    check("repair maps the old spelling for ever", e.repair("who is harleid"), "who is khaled")
    check("disk already has the correction", FactMemory(p2, legacy=[]).names(), ["Khaled"])
    m.learn("no he has 120 podiums")
    check("number correction overwrites", FactMemory(p2, legacy=[]).fact_lines("")[1], "Khaled has 120 podiums.")
    m.learn("his name is khalid")
    check("'his name is X' renames the person discussed", m.names(), ["Khalid"])
    m.learn("my real life race engineer is khaled")
    check("confirmed name: a sound-alike is speech drift, not a rename", m.names(), ["Khalid"])
    check("...and it is remembered as an alias", m.alias_map().get("khaled"), "khalid")
    check("repair does not rewrite the driver's own naming sentence",
          e.repair("my real life race engineer is khaled"), "my real life race engineer is khaled")
    m.learn("no it's khaled not khalid")
    check("explicit correction always wins", m.names(), ["Khaled"])
    m.touch("khaled")
    m.learn("no it's raining")
    check("'no it's raining' renames nobody", m.names(), ["Khaled"])
    m.learn("no it's khalid")
    check("'no it's <sound-alike>' fixes the spelling", m.names(), ["Khalid"])
    m.learn("no it's khaled not khalid")
    check("...and back", m.names(), ["Khaled"])
    m.learn("marco is my friend and he has 5 wins")
    check("a second, different person is kept (marco)", sorted(m.names()), ["Khaled", "Marco"])
    m.learn("mark is my friend")
    m.learn("my brother is luca")
    check("non-unique roles do not replace each other", sorted(m.names()), ["Khaled", "Luca", "Marco", "Mark"])
    check("marco's own stats are separate", m.fact_lines("")[-1] if False else FactMemory(p2, legacy=[]).data["entities"]["marco"]["stats"], {"wins": 5})

    print("\n== 5. brain integration (FakeBrain) ==")
    p3 = os.path.join(tmp, "b", "memory_store.json")
    brain = FakeBrain()
    chat = LLMEngineer(memory=FactMemory(p3, legacy=[]), backends=[boom])
    chat.brain = brain
    chat.attach(brain)
    chat.attach(brain)                                                        # twice is harmless
    check("casual statement is learned and acknowledged", chat.handle_if_casual("my real life race engineer is khaled and he has 150 podiums"), True)
    has("deterministic acknowledgement", brain.said[-1], "khaled", "150")
    brain.history.append(("old q", "Luca 20 podiums"))
    check("question handled before any AI", chat.handle_if_casual("how many podiums does khaled have"), True)
    has("answer is the saved number", brain.said[-1], "150", absent=("luca",))
    check("telemetry sentence is left to the Brain", chat.handle_if_casual("what is my fuel"), False)
    brain._ask_ai("how many podiums does khaled have")
    has("patched _ask_ai answers from memory", brain.said[-1], "150")
    check("original _ask_ai was not even called", brain.asked_original, [])
    brain._ask_ai("what should my camber be on this car")
    has("knowledge route: prompt now carries the memory", brain.said[-1], "ORIGINAL:", "Khaled has 150 podiums.")
    chat.handle_if_casual("his name is khalid")
    check("a correction wipes the old chat history", len(brain.history), 0)
    chat.handle_if_casual("forget everything you know")
    check("forget wipes the file", FactMemory(p3, legacy=[]).names(), [])

    print("\n== 6. robustness ==")
    p4 = os.path.join(tmp, "d", "memory_store.json")
    FactMemory(p4, legacy=[]).learn("my real life race engineer is khaled and he has 150 podiums")
    FactMemory(p4, legacy=[]).learn("remember that my favourite car is the audi")   # 2nd save creates the .bak
    check(".bak exists after a second save", os.path.isfile(p4 + ".bak"), True)
    with open(p4, "w", encoding="utf-8") as f:
        f.write("{ this is not json")                                         # simulate a crash / corruption
    check("corrupt file: recovered from .bak", FactMemory(p4, legacy=[]).names(), ["Khaled"])
    check("corrupt file was kept for inspection", os.path.isfile(p4 + ".bad"), True)
    old = os.path.join(tmp, "old", "memory_store.json")
    FactMemory(old, legacy=[]).learn("my real life race engineer is khaled")
    p5 = os.path.join(tmp, "new", "memory_store.json")
    mig = FactMemory(p5, legacy=[old])
    check("store from another run mode is imported", mig.names(), ["Khaled"])
    check("...and written to the primary location", os.path.isfile(p5), True)
    check("memory_path() ends in data/memory_store.json", memory_path().replace("\\", "/").endswith("data/memory_store.json") or bool(getattr(config, "MEMORY_STORE_FILE", None)), True)
    check("number words", to_number("one hundred and fifty".split()), 150)
    check("numbers_in", numbers_in("he has one hundred and fifty podiums and 3 wins"), {150, 3})
    check("'my engineer is very fast' is not a name", FactMemory(os.path.join(tempfile.mkdtemp(), "m.json"), legacy=[]).learn("my engineer is very fast"), [])
    check("question word is not a name", FactMemory(os.path.join(tempfile.mkdtemp(), "m.json"), legacy=[]).learn("who is my real life engineer"), [])

    print("\n" + ("ALL PASSED" if not fails else "%d FAILED: %s" % (len(fails), "; ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest())
