"""
llm_worker.py - runs the built-in AI model in its OWN process.

Why a separate process: the model is native code (llama.cpp). If it ever crashes (e.g. a CPU that lacks a
required instruction) or hangs, only THIS process dies. The race engineer, the telemetry loop and every
alert keep running, and local_llm.py simply falls back. A hung answer is stopped by killing this process.

Protocol (one JSON object per line):
    parent -> worker : {"id": 1, "messages": [...], "max_tokens": 70, "temperature": 0.5}
    worker -> parent : {"event": "ready"}  |  {"event": "error", "error": "..."}
                       {"id": 1, "text": "..."}  |  {"id": 1, "error": "..."}

Started as   python llm_worker.py <model.gguf> <n_ctx> <n_threads>
or, inside a packaged .exe,   pitwall.exe --llm-worker <model.gguf> <n_ctx> <n_threads>
"""
import json
import os
import sys


def main(argv):
    # The protocol goes out on a private copy of stdout. Anything native code prints to the normal stdout
    # is redirected to stderr, so it can never corrupt the protocol.
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)

    def send(obj):
        proto.write(json.dumps(obj) + "\n")
        proto.flush()

    try:
        model_path, n_ctx, n_threads = argv[0], int(argv[1]), int(argv[2])
    except Exception:
        send({"event": "error", "error": "bad worker arguments"})
        return
    try:
        from llama_cpp import Llama
    except Exception as e:
        send({"event": "error", "error": f"llama-cpp-python is not installed ({type(e).__name__}: {e})"})
        return
    try:
        llm = Llama(model_path=model_path, n_ctx=n_ctx, n_threads=n_threads, n_gpu_layers=0, verbose=False)
        llm.create_chat_completion(messages=[{"role": "user", "content": "OK"}], max_tokens=2)   # warm-up
    except Exception as e:
        send({"event": "error", "error": f"model failed to load ({type(e).__name__}: {e})"})
        return
    send({"event": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            out = llm.create_chat_completion(messages=req["messages"], max_tokens=int(req.get("max_tokens", 70)),
                                             temperature=float(req.get("temperature", 0.5)))
            send({"id": rid, "text": out["choices"][0]["message"]["content"]})
        except Exception as e:
            send({"id": rid, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main(sys.argv[1:])
