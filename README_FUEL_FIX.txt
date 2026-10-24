FUEL WARNING-LIGHT FALLBACK  -  what changed and where it goes
==============================================================
Copy ALL files of this folder over your project folder AI_RACE_ENGINNER_3.12\ (Replace). Keep cesium.ico, .venv\, models\, data\.
This zip also contains the earlier audio fix, so it is the complete current project.

CHANGED:  strategy.py (the logic)   main_pitwall.py   brain.py   config.py (new FUEL_WARN_* block under FUEL)
If you keep your own config.py, copy only the block that starts with "Fuel WARNING LIGHT fallback" into the FUEL section.
(An old config.py still works: strategy.py has the same defaults built in.)

CALIBRATE (one time per car class, 2 minutes)
  The game does not publish the icon colour, so yellow / orange / red are worked out from the tank level.
  Drive with a low fuel start; the Log window prints   [FUEL] light level -> orange: 4.8 L left (8% of about 60 L)
  Compare with the icon. If the icon changes earlier or later, change FUEL_WARN_YELLOW / ORANGE / RED in config.py,
  or add the car to FUEL_WARN_BY_CAR = {"ks_audi_r8_lms_2016": (0.20, 0.10, 0.05)}.
