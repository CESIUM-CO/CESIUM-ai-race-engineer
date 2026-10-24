"""
test_hybrid.py - checks the new chat system WITHOUT the game, a microphone or a real Ollama.

Run it from the project folder:      python test_hybrid.py
Everything must say PASS. It starts a tiny fake Ollama server on your own PC just for the test, so nothing is
downloaded and nothing leaves the computer.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

config.USE_LLM = False                 # the built-in model must not start (or download anything) during the test
config.ENGINEER_NAME = "Alex"
config.OLLAMA_MODEL = "llama3.2:3b"

FAILS = []


def check(label, got, want):
    ok = got == want
    print("%s  %s" % ("PASS" if ok else "FAIL", label) + ("" if ok else "\n        got : %r\n        want: %r" % (got, want)))
    if not ok:
        FAILS.append(label)


# ------------------------------------------------------------------ 1. speech repair
import speech_repair as sr


def fix(text, **kw):
    return sr.repair(text, name="Alex", **kw)


print("\n== speech_repair ==")
check("name + mishearing of 'weather'", fix("okay alix what's the whether"), "what's the weather")
check("mishearing of 'fuel'", fix("alex what is my fuell level"), "what is my fuel level")
check("'click' -> 'cliff', 'dirty' -> 'thirty'", fix("set the click to dirty percent"), "set cliff to thirty percent")
check("spelled-out psi", fix("what are my tire pressures in p s i"), "what are my tire pressures in psi")
check("'lapse' -> 'laps'", fix("how many lapse of fuel"), "how many laps of fuel")
check("'books' -> 'box' after 'should i'", fix("should i books now"), "should i box now")
check("'lab' -> 'lap'", fix("what was my best lab"), "what was my best lap")
check("'tier' before a tyre word", fix("tier temperatures please"), "tyre temperatures please")
check("number word 'for' before laps", fix("set margin to for laps"), "set margin to four laps")
check("'whether or not' is left alone", fix("i dont know whether or not to pit"), "i dont know whether or not to pit")
check("'for lap' is left alone", fix("what was my time for lap four"), "what was my time for lap four")
check("fuzzy motorsport word", fix("what is the temprature of the tyres"), "what is the temperature of the tyres")
check("'engineer' is never changed", fix("hello engineer how are you"), "hello engineer how are you")
check("'camera' is never changed", fix("move the camera"), "move the camera")
check("Whisper noise is dropped", fix("thanks for watching"), "")
check("filler is dropped", fix("um"), "")
check("'thanks' is a real answer", fix("thanks"), "thanks")
check("only his name -> kept", fix("alex"), "alex")
check("trailing name dropped", fix("what's my fuel alex"), "what's my fuel")
check("current track name (fuzzy)", fix("how is the grip at brandz hatch", extra_terms=["Brands Hatch"]), "how is the grip at brands hatch")
check("famous driver (fuzzy)", fix("do you think senah was quicker"), "do you think senna was quicker")

# ------------------------------------------------------------------ 2. persona
print("\n== persona ==")
import persona

raw = "Engineer: *chuckles* \"Why did the tyre go to therapy?\" Too much pressure. Ha ha. Back to work."
check("tidy: no actions, no label, 2 sentences", persona.tidy_radio(raw, "Alex"), "Why did the tyre go to therapy? Too much pressure.")
check("tidy: SEARCH passes through", persona.tidy_radio("SEARCH: brands hatch gt3 setup"), "SEARCH: brands hatch gt3 setup")
check("tidy: adds a full stop", persona.tidy_radio("Copy that"), "Copy that.")
prompt = persona.build_system_prompt("Alex", '{"car": "Audi R8 LMS"}', True, ["The Audi is called the Silver Bullet."], "Marco")
check("prompt has name, lore, data, search rule", all(x in prompt for x in ("You are Alex", "Silver Bullet", "Audi R8 LMS", "SEARCH:", "The driver is Marco")), True)
check("prompt without search has no SEARCH rule", "reply ONLY with: SEARCH" in persona.build_system_prompt("Alex", "{}", False), False)

# ------------------------------------------------------------------ 3. fake Ollama
CALLS = {"chat": 0, "last_messages": None}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"models": [{"name": "llama3.2:3b"}]})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/chat":
            CALLS["chat"] += 1
            CALLS["last_messages"] = data["messages"]
            self._send({"message": {"role": "assistant", "content":
                        "*chuckles* Senna was the lightning, Prost the chess player. Both legends. Now keep it tidy."}})
        else:
            self._send({})


server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
config.OLLAMA_URL = "http://127.0.0.1:%d" % server.server_address[1]


class FakeRadio:
    quiet, last_text = False, ""

    def __init__(self):
        self.said = []

    def speak(self, text, prio=1, kind="alert", **kw):
        self.said.append(text)


class FakeMemory:
    def get_knowledge(self, q):
        return None

    def save_knowledge(self, *a):
        pass

    def log_unknown(self, q):
        pass


def wait_for(cond, secs=5):
    end = time.time() + secs
    while time.time() < end and not cond():
        time.sleep(0.05)
    return cond()


print("\n== hybrid routing (with a fake Ollama server) ==")
from brain import Brain

radio = FakeRadio()
brain = Brain(radio, FakeMemory())
check("Ollama connects and warms up", wait_for(lambda: brain.ollama.state == "ready"), True)
check("control window state is 'ready'", brain.ai_state(), "ready")
brain.state.update(live=True, car_name="Audi R8 LMS", track_name="Brands Hatch Indy")

# telemetry: instant rules, Ollama must NOT be called
before = CALLS["chat"]
brain.handle("what is my fuel")
check("fuel question answered by the rules", len(radio.said), 1)
check("fuel question did NOT call Ollama", CALLS["chat"], before)

before = CALLS["chat"]
brain.handle("how are my tyre temperatures")
check("tyre temps answered by the rules, no Ollama", (CALLS["chat"], len(radio.said)), (before, 2))

# banter: Ollama, in persona, cleaned up
brain.handle("do you think senna was faster than prost")
check("banter went to Ollama", CALLS["chat"], before + 1)
check("reply is cleaned (2 sentences, no *action*)", radio.said[-1], "Senna was the lightning, Prost the chess player. Both legends.")
check("persona + live data were sent", "You are Alex" in CALLS["last_messages"][0]["content"] and "Audi R8 LMS" in CALLS["last_messages"][0]["content"], True)

# a one-word telemetry trigger inside banter must not read out telemetry
before, said = CALLS["chat"], len(radio.said)
brain.handle("what do you think about the weather in monza")
check("'weather' inside banter goes to the LLM, not the weather read-out", CALLS["chat"], before + 1)

# setups: knowledge route, still Ollama
before = CALLS["chat"]
brain.handle("what should my camber be on this car")
check("setup question goes to Ollama", CALLS["chat"], before + 1)
check("setup question may ask for a web search (rule present)", "SEARCH:" in CALLS["last_messages"][0]["content"], True)
before = CALLS["chat"]
brain.handle("tell me a joke")
check("banter prompt has NO web-search rule", "reply ONLY with: SEARCH" in CALLS["last_messages"][0]["content"], False)

# controls stay instant
before = CALLS["chat"]
brain.handle("radio silence please")
check("quiet command handled by the rules", (CALLS["chat"], brain.radio.quiet), (before, True))
brain.radio.quiet = False

# ------------------------------------------------------------------ 4. Ollama not running
print("\n== Ollama not running: nothing breaks ==")
config.OLLAMA_URL = "http://127.0.0.1:9"
radio2 = FakeRadio()
brain2 = Brain(radio2, FakeMemory())
check("state becomes 'failed'", wait_for(lambda: brain2.ollama.state == "failed"), True)
brain2.state.update(live=True)
brain2.handle("tell me a joke")
check("banter still gets a spoken answer", len(radio2.said), 1)
brain2.handle("what is my fuel")
check("telemetry still works", len(radio2.said), 2)

server.shutdown()
print("\n" + ("ALL TESTS PASSED" if not FAILS else "%d TEST(S) FAILED: %s" % (len(FAILS), "; ".join(FAILS))))
sys.exit(1 if FAILS else 0)
