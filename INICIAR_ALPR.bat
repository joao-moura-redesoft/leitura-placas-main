@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Leitura de Placas (ALPR)
cd /d "%~dp0"

REM ============================================================================
REM  INICIAR_ALPR.bat  -  prepara o ambiente na 1a execucao e sobe o servidor.
REM
REM  1a vez numa maquina nova : confere Windows 64-bit, (re)cria o .venv a partir
REM                             de um Python 3.12 x64 do sistema, instala o
REM                             requirements.txt, resolve o VC++ Redistributable
REM                             se as libs nao carregarem, baixa os modelos e
REM                             inicia o ALPR.exe.
REM  Proximas vezes           : detecta que ja esta pronto e so inicia.
REM
REM  Deixe este arquivo na RAIZ do projeto (junto de ALPR.exe, app\, config.txt).
REM ============================================================================

set "RAIZ=%CD%"
set "VENV_PY=%RAIZ%\.venv\Scripts\python.exe"
set "MARKER=%RAIZ%\.config_ok"
set "REQ=%RAIZ%\requirements.txt"
set "EXE=%RAIZ%\ALPR.exe"
set "IMPORT_TESTE=import onnxruntime, cv2, fastapi, uvicorn"

echo(
echo ================================================================
echo   Leitura de Placas (ALPR)
echo   Pasta: %RAIZ%
echo ================================================================
echo(

REM ---------- 0. Arquivos essenciais ----------
if not exist "%EXE%"              ( echo [ERRO] ALPR.exe nao esta nesta pasta.   & goto :fim_erro )
if not exist "%RAIZ%\app\main.py" ( echo [ERRO] app\main.py nao encontrado.      & goto :fim_erro )
if not exist "%REQ%"             ( echo [ERRO] requirements.txt nao encontrado.  & goto :fim_erro )

REM ---------- 1. Windows 64-bit ----------
if /I not "%PROCESSOR_ARCHITECTURE%"=="AMD64" if /I not "%PROCESSOR_ARCHITEW6432%"=="AMD64" (
  echo [ERRO] E necessario Windows 64-bit ^(x86-64^). Arquitetura: %PROCESSOR_ARCHITECTURE%
  goto :fim_erro
)

REM ---------- 2. Atalho: ja configurado e .venv funcional ----------
if exist "%MARKER%" if exist "%VENV_PY%" (
  "%VENV_PY%" -c "%IMPORT_TESTE%" 1>nul 2>nul
  if not errorlevel 1 (
    echo [OK] Ambiente ja configurado.
    goto :iniciar
  )
  echo [AVISO] Marcado como pronto, mas as bibliotecas nao carregam. Refazendo a config...
)

REM ---------- 3. Ambiente Python (.venv) ----------
echo [1/4] Ambiente Python (.venv)...
set "VENV_OK="
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "%IMPORT_TESTE%" 1>nul 2>nul && set "VENV_OK=1"
)
if defined VENV_OK (
  echo       .venv ja funcional - pulando a instalacao.
  goto :verificar_import
)

echo       procurando um Python 3.12 de 64 bits no sistema...
set "BASEPY="
for %%C in ("py -3.12" "python" "python3.12") do (
  if not defined BASEPY (
    cmd /c %%~C -c "import sys,struct;raise SystemExit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 else 1)" 1>nul 2>nul && set "BASEPY=%%~C"
  )
)
if not defined BASEPY (
  echo(
  echo [ERRO] Nenhum Python 3.12 x64 encontrado no sistema.
  echo        Instale o Python 3.12.x ^(64-bit, marque "Add python.exe to PATH"^) e
  echo        rode este INICIAR_ALPR.bat de novo:
  echo        https://www.python.org/downloads/release/python-31210/
  start "" https://www.python.org/downloads/release/python-31210/
  goto :fim_erro
)
echo       usando: !BASEPY!

if exist "%RAIZ%\.venv" (
  echo       removendo .venv antigo/incompativel...
  rmdir /s /q "%RAIZ%\.venv"
)
echo       criando .venv...
cmd /c !BASEPY! -m venv "%RAIZ%\.venv"
if not exist "%VENV_PY%" ( echo [ERRO] Falha ao criar o .venv. & goto :fim_erro )

echo [2/4] Instalando dependencias - baixa ~2 GB, pode levar de 10 a 30 min...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%REQ%"
if errorlevel 1 ( echo [ERRO] Falha ao instalar o requirements.txt. & goto :fim_erro )
echo       fixando o OpenCV contrib como unica fonte do modulo cv2...
"%VENV_PY%" -m pip uninstall -y opencv-python-headless 1>nul 2>nul
"%VENV_PY%" -m pip install --force-reinstall --no-deps opencv-contrib-python==4.10.0.84

:verificar_import
echo [.] Testando o carregamento das bibliotecas nativas...
"%VENV_PY%" -c "%IMPORT_TESTE%" 1>nul 2>nul
if not errorlevel 1 (
  echo       OK.
  goto :modelos
)

REM ---- import falhou: quase sempre e o Visual C++ Redistributable x64 ----
echo       FALHOU. Causa mais provavel: Microsoft Visual C++ Redistributable x64 ausente.
echo       baixando o instalador oficial...
curl -L -# -o "%TEMP%\vc_redist.x64.exe" https://aka.ms/vs/17/release/vc_redist.x64.exe
if exist "%TEMP%\vc_redist.x64.exe" (
  echo       instalando ^(pode pedir permissao de administrador^)...
  "%TEMP%\vc_redist.x64.exe" /install /passive /norestart
  set "VCRC=!errorlevel!"
  REM 0 = instalado   1638/3010 = ja presente / requer reboot   ambos OK
  if "!VCRC!"=="1638" ( echo       ja estava instalado. ) else if "!VCRC!"=="3010" ( echo       instalado ^(reinicie o Windows depois^). ) else if "!VCRC!"=="0" ( echo       instalado. ) else ( echo       instalador retornou codigo !VCRC!. )
) else (
  echo       [AVISO] nao consegui baixar. Instale manualmente:
  echo               https://aka.ms/vs/17/release/vc_redist.x64.exe
)
"%VENV_PY%" -c "%IMPORT_TESTE%" 1>nul 2>nul
if errorlevel 1 (
  echo(
  echo [ERRO] As bibliotecas ainda nao carregam.
  echo        Instale o Visual C++ Redistributable x64, reinicie o Windows e
  echo        rode este INICIAR_ALPR.bat de novo.
  goto :fim_erro
)
echo       OK apos instalar o VC++ Redistributable.

:modelos
echo [3/4] Modelos...
if not exist "%RAIZ%\models\vehicle_detector.onnx" (
  echo       baixando o detector de veiculo...
  "%VENV_PY%" "%RAIZ%\scripts\baixar_modelo.py" --veiculo
)
echo       pre-carregando os modelos de leitura ^(baixa ~330 MB no primeiro uso^)...
"%VENV_PY%" -c "from app.core import config; from app.visao.detector import obter_detector_leitura; from app.visao.ocr import obter_ocr_leitura; c=config.carregar(); obter_detector_leitura(c); obter_ocr_leitura(c); print('       modelos de leitura OK')"
if errorlevel 1 (
  echo       [AVISO] nao foi possivel pre-baixar os modelos agora ^(sem internet?^).
  echo               O primeiro start do ALPR.exe tenta de novo automaticamente.
)

echo [4/4] Configuracao concluida.
> "%MARKER%" echo configurado em %DATE% %TIME%

:iniciar
echo(
echo ================================================================
echo   Iniciando o servidor (ALPR.exe)
echo   Painel:  http://localhost:14000   (o navegador abre sozinho)
echo   Encerrar: feche a janela do servidor ou tecle Ctrl+C
echo ================================================================
echo(
"%EXE%"
set "RC=%ERRORLEVEL%"
echo(
echo Servidor encerrado (codigo %RC%).
pause
endlocal & exit /b %RC%

:fim_erro
echo(
echo Configuracao NAO concluida. Corrija o item acima e rode o INICIAR_ALPR.bat de novo.
echo(
pause
endlocal & exit /b 1
