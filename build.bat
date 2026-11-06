@echo off
REM Builds standalone AULA uploader executables on Windows.
REM Needs Python only on THIS machine, once. The resulting .exe files need nothing.
setlocal
py -m pip install --upgrade pyinstaller hidapi pillow || goto :err
py -m PyInstaller --onefile --windowed --name AULA_Uploader aula_uploader.py -y || goto :err
py -m PyInstaller --onefile --console  --name AULA_Uploader_CLI aula_uploader.py -y || goto :err
echo.
echo Done. Your portable files are in the "dist" folder:
echo   dist\AULA_Uploader.exe      (window)
echo   dist\AULA_Uploader_CLI.exe  (command line: --list, --test, --pick ...)
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
