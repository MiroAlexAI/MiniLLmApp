@echo off
rem Safe-but-fast server profile for the builder agent (Gemma-4-E2B, RX 6800M):
rem  - GPU with the proven-safe load pattern: short prompts, small generations,
rem    mandatory cooldown pauses between requests (built into builder\agent.py)
rem  - context 32768 is enough: builder prompts stay under ~8K tokens
rem  - thinking enabled with budget 512: used only for the planning phase
rem  - agent sampling: temp 0.3, repeat_penalty 1.05 (anti-loop)
cd /d "%~dp0.."
powershell -NoProfile -Command "try{Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing -TimeoutSec 2|Out-Null;exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 (
    echo Server already running at http://127.0.0.1:8080 - reusing it.
    timeout /t 2 >nul
    exit /b 0
)
echo Starting llama-server (GPU RX 6800M, ctx 32768, think budget 512)...
start "llama-server (builder)" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\start.ps1" -Mode gpu -Ctx 32768 -ThinkBudget 512 -Temp 0.3 -TopK 40 -TopP 0.9 -MinP 0.05 -RepeatPenalty 1.05 -NoBrowser
powershell -NoProfile -Command "for($i=0;$i -lt 120;$i++){try{Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing -TimeoutSec 2|Out-Null;Write-Host 'Server is UP: http://127.0.0.1:8080';exit 0}catch{Start-Sleep 1}};Write-Host 'Server did not start in 120 s';exit 1"
