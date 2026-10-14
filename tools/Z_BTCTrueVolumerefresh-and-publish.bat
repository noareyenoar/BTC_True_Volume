@echo off
REM One-click refresh + publish for the TA+VSA visualizer, made to be run from
REM D:\ itself (double-click, or: D:\refresh-and-publish.bat [flags]).
REM
REM It runs exactly the two steps the Thai guide (BTC_True_Volume\zRefresh_Update.txt)
REM has you run by hand, in the same order, inside the project folder:
REM   1. refresh_before_visualizer.py --no-launch   (all feeds -> latest Vision date)
REM   2. publish_site.py                            (re-export site/ and push to Pages)
REM
REM `--no-launch` is deliberate: this file ends at the published page. It never
REM starts the local server -- open the live URL, or run step 1 without that flag
REM if you want the local one.
REM
REM Extra flags are forwarded to BOTH scripts (e.g. --force-adjudicate,
REM --backfill-cb, --no-push). Unknown flags make argparse exit 2, which lands in
REM the :fail branch below -- publish is then NOT attempted.
REM
REM This copy lives OUTSIDE the project (the one that used to sit in the project
REM folder was removed 2026-09-23), so the project path is hardcoded below.
REM ASCII only on purpose: cmd.exe parses .bat bytes multi-byte-unsafely, so the
REM console messages stay English while the guide keeps the Thai.

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PROJ=D:\kp_ai_agent\Dataset_Fetcher"
cd /d "%PROJ%"
rem Absolute paths throughout: this box sets NoDefaultCurrentDirectoryInExePath=1,
rem so cmd does not search the current directory for programs.
set "PY=%PROJ%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [X] venv python not found: "%PY%"
  echo     this script expects the project at %PROJ%
  pause
  exit /b 1
)

echo === 1/2 refresh  (2-10 min; the window looks frozen while it fetches) ===
"%PY%" "%PROJ%\BTC_True_Volume\refresh_before_visualizer.py" --no-launch %*
if errorlevel 1 goto fail

echo.
echo === 2/2 publish to GitHub Pages ===
"%PY%" "%PROJ%\BTC_True_Volume\publish_site.py" %*
if errorlevel 1 goto fail

echo.
echo OK - the page redeploys in ~1-2 min:
echo      https://noareyenoar.github.io/BTC_True_Volume/
pause
exit /b 0

:fail
echo.
echo FAILED - the step above exited with an error; nothing after it was run.
echo          The message above names the cause. Re-running is safe: both steps
echo          pick up where they left off.
pause
exit /b 1
