TRACK & CORNER INTELLIGENCE UPDATE  (builds on the setup advisor update)
=======================================================================
1. Close the engineer.
2. Copy ALL files of this folder into AI_RACE_ENGINNER_3.12\ and say "Replace"
   (it includes the earlier setup advisor files, so it is the complete current set).
3. python test_setup_advisor.py   -> must end with ALL MODULES PASSED.

NEW      track_intel.py       corner notes for Monza, Spa, Nordschleife, Imola, Brands Hatch (Indy), Mugello, Silverstone,
                              Laguna Seca, Barcelona, Red Bull Ring, Zandvoort + a playbook for every corner TYPE
                              (chicane, hairpin, heavy braking, crest, compression, banking, fast corner) for any track
NEW      incident_monitor.py  keeps ~8 s of telemetry; on an impact / going off / spin it finds the CAUSE from your inputs
                              (brake, throttle, steering, front vs rear slip, body slip angle, tyre temperature and PSI)
NEW      corner_coach.py      combines telemetry + track + setup_advisor into one spoken answer
CHANGED  brain.py             crash questions and corner questions handled before the AI; track notes + last incident go into
                              the AI context
CHANGED  main_pitwall.py      3 lines: sets the track, feeds telemetry (silent), resets with the session
CHANGED  persona.py           the AI prompt now uses track + incident data and must not invent corner facts
CHANGED  boot_guard.py, build_nuitka.bat, build_nuitka_debug.bat    new modules bundled and checked by --selftest

SAY, FOR EXAMPLE
  "why did I crash"                   -> cause from the seconds before the hit, corner type, one driving fix,
                                          and one exact setup change ("If it keeps happening, try brake bias rearward ...")
  "that was the parabolica"           -> teaches where that corner is on the lap (saved, per track and layout)
  "why do I keep spinning at ascari"  -> uses the corner notes, and says when it is your 2nd / 3rd incident near that spot
  "how do I take eau rouge"           -> corner notes + entry / exit advice
  "my car understeers at the parabolica on exit" -> setup advice (setup_advisor) plus that corner's note

CAUSES IT CAN TELL APART
  over_braking, rear_braking (unstable under brake), lift_off (lift-off oversteer), power_oversteer, understeer,
  oversteer (mid-corner), ran_wide (no slide first: line or speed), unknown (impact while still in control: contact or wall)
  Cold tyres and a too-high PSI are added when the data shows them.

NOTES AND LIMITS
  * Corner notes are general knowledge of the real layouts, kept qualitative. Mods and unusual layouts may differ, and a track
    with no notes still gets the corner-type playbook.
  * The game does not tell the engineer which corner you are in. It learns it: after a crash say "that was <corner>" once.
    If you name the corner in your question, no teaching is needed.
  * Optional config.py settings (defaults built in): CORNER_MAP_FILE (where the learned corners are saved).
  * Learned corners and incident counts: <USER_ROOT>\data\corner_map.json (%APPDATA%\CesiumAIRaceEngineer\data in a packaged build).
  * Log lines: [INCIDENT] (what was detected and why), [CORNER] (track notes chosen), [SETUP] (setup reasoning).
