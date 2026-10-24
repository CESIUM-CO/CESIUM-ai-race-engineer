import threading
import time

import config


class BackgroundLearner(threading.Thread):
    """Quietly studies questions he couldn't answer. Never blocks the driver conversation."""

    def __init__(self, memory, brain):
        super().__init__(daemon=True)
        self.memory, self.brain = memory, brain

    def run(self):
        time.sleep(30)
        while True:
            try:
                question = self.memory.next_unanswered()
                if question:
                    answer = self.brain.research(question, question, report=False)
                    self.memory.mark_answered(question)
                    if answer:
                        print(f"[LEARNED] {question!r} -> saved to memory")
            except Exception as e:
                print(f"[LEARNER] {e}")
            time.sleep(config.LEARN_INTERVAL)
