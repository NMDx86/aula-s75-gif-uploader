#!/usr/bin/env bash
# Builds a standalone AULA uploader on macOS or Linux (needs Python on THIS machine only).
set -e
python3 -m pip install --upgrade pyinstaller hidapi pillow
python3 -m PyInstaller --onefile --windowed --name AULA_Uploader aula_uploader.py -y
python3 -m PyInstaller --onefile --console  --name AULA_Uploader_CLI aula_uploader.py -y
echo "Done. See the dist/ folder."
