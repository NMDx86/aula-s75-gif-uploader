AULA uploader, portable build
=============================

Fastest way, no Python anywhere (GitHub builds it for you, free):
  1. Create a GitHub repo, upload these files (keep .github/workflows/build.yml).
  2. Actions tab -> "Build portable executables" -> Run workflow.
  3. Download the artifact for your OS (Windows / macOS / Linux). Unzip, run.

Or build on one machine that has Python (once), then copy the result anywhere:
  Windows:      double-click build.bat      -> dist\AULA_Uploader.exe
  macOS/Linux:  ./build.sh                  -> dist/AULA_Uploader

AULA_Uploader      = the window (no console)
AULA_Uploader_CLI  = command line (--list, --test, --pick, my.gif --cli ...)

Notes
 - PyInstaller can't cross-compile: build on the OS you're targeting.
 - Windows SmartScreen may warn about an unsigned exe: More info -> Run anyway.
 - macOS: right-click -> Open the first time; grant Input Monitoring to the app.
 - Linux: needs hidraw permission (udev rule or sudo).
