@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY="

rem 1) py launcher (vem junto com o instalador oficial do python.org)
py -3 --version >nul 2>&1 && set "PY=py -3"

rem 2) python no PATH (ignora o atalho falso da Microsoft Store)
if not defined PY (
    python -c "import sys" >nul 2>&1 && set "PY=python"
)

rem 3) python3
if not defined PY (
    python3 -c "import sys" >nul 2>&1 && set "PY=python3"
)

rem 4) locais mais comuns de instalacao
if not defined PY (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
    ) do (
        if not defined PY if exist %%P set "PY=%%P"
    )
)

if not defined PY (
    echo.
    echo ============================================================
    echo   O Python nao esta instalado nesta maquina.
    echo.
    echo   Instale de uma destas formas e rode este arquivo de novo:
    echo.
    echo   [A] No PowerShell:   winget install -e --id Python.Python.3.12
    echo.
    echo   [B] Baixe em https://www.python.org/downloads/windows/
    echo       IMPORTANTE: marque "Add python.exe to PATH" na 1a tela
    echo       do instalador.
    echo.
    echo   Depois de instalar, FECHE e abra este arquivo novamente.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

echo Usando Python: %PY%
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo Falha ao instalar o Flask. Verifique sua conexao com a internet.
    pause
    exit /b 1
)

echo.
echo Servidor rodando em http://127.0.0.1:5000
echo Feche esta janela ou aperte Ctrl+C para parar.
echo.
%PY% app.py
pause
