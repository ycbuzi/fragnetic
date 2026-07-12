@echo off
REM ============================================================
REM  Refresh the Fragnetic KNOWLEDGE GRAPH from the current code.
REM  Pure AST re-extract -- no LLM, no tokens, a few seconds.
REM  The Obsidian vault (vault\*.md) is hand-maintained; see
REM  vault\Keeping this current.md for what to touch per release.
REM ============================================================
setlocal
cd /d "%~dp0"

set "GF=%APPDATA%\Python\Python314\Scripts\graphify.exe"
if not exist "%GF%" set "GF=graphify"

echo Updating code graph -^> graphify-out\ ...
"%GF%" update .

echo.
echo Done. graph.json / graph.html / GRAPH_REPORT.md refreshed.
echo Reminder: update vault\Versions.md + any feature note that changed.
endlocal
