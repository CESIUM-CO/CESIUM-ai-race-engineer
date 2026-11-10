SETUP ADVISOR UPDATE
====================
1. Close the engineer.
2. Copy ALL files of this folder into AI_RACE_ENGINNER_3.12\ and say "Replace".
   Keep cesium.ico, .venv\, models\, data\, ac_app\ as they are.

NEW      setup_advisor.py        rules for understeer / oversteer / instability; names exact garage parameters
NEW      test_setup_advisor.py   python test_setup_advisor.py  (must say ALL PASSED)
CHANGED  brain.py                calls the advisor before the AI (instant, no AI needed); DRIVER SETUP goes into the AI context
CHANGED  persona.py              the AI prompt now forbids vague setup answers
CHANGED  boot_guard.py           --selftest checks setup_advisor
CHANGED  build_nuitka.bat, build_nuitka_debug.bat   --include-module=setup_advisor

SAY, FOR EXAMPLE
  "my brake bias is fifty eight and front arb is six, the car understeers on entry"
   -> "Understeer on corner entry. Try brake bias rearward, from 58 to 57; front toe-out more by 0.05 degrees; and
       front anti-roll bar softer, from 6 to 5. Change one thing at a time, starting with the first."
  "the rear is loose on exit"   -> differential power lock, preload, rear anti-roll bar, rear toe-in, rear springs
  "my rear wing is five"        -> remembered until the car changes

NOTES
  * Assetto Corsa does not publish the garage setup, so values are what the driver says. Say decimals as "two point
    five" and negatives as "minus two point five": the speech layer strips "." and "-" from digits.
  * front toe = toe-OUT amount, rear toe = toe-IN amount, camber is always read as negative.
  * A change that would leave the usual window (e.g. rear camber past 3.5) is skipped and the log says so.
  * Log lines starting [SETUP] show the full reasoning for every suggestion.
