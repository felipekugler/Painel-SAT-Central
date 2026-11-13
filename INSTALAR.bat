@echo off
rem ============================================================
rem  INSTALAR — Painel SAT Central numa maquina nova (Windows)
rem  Cria o .venv, instala dependencias, baixa o Chromium do
rem  Playwright e prepara o .env. Rode UMA vez apos descompactar.
rem ============================================================
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

echo ============================================================
echo   Instalando o Painel SAT Central
echo ============================================================
echo.

rem 1) Python no PATH
where python >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    echo         Instale Python 3.11+ ^(https://www.python.org^) e marque
    echo         "Add python.exe to PATH" durante a instalacao. Depois rode este .bat de novo.
    echo.
    pause
    exit /b 1
)

rem 2) Ambiente virtual
if exist ".venv\Scripts\python.exe" (
    echo [1/4] .venv ja existe — mantido.
) else (
    echo [1/4] Criando ambiente virtual .venv ...
    python -m venv .venv
    if errorlevel 1 ( echo [ERRO] Falha ao criar o .venv & pause & exit /b 1 )
)
set PY=.venv\Scripts\python.exe

rem 3) Dependencias
echo [2/4] Instalando dependencias ^(requirements.txt^) ...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 ( echo [ERRO] Falha no pip install & pause & exit /b 1 )

rem 4) Chromium do Playwright
echo [3/4] Baixando o Chromium do Playwright ^(pode demorar^) ...
"%PY%" -m playwright install chromium
if errorlevel 1 ( echo [ERRO] Falha ao instalar o Chromium do Playwright & pause & exit /b 1 )

rem 5) .env a partir do modelo
echo [4/4] Preparando o .env ...
if exist ".env" (
    echo        .env ja existe — mantido ^(nao sobrescrevo^).
) else (
    copy /Y ".env.example" ".env" >nul
    echo        .env criado a partir do .env.example.
)

echo.
echo ============================================================
echo   Instalacao concluida.
echo.
echo   AINDA FALTA ^(manual^):
echo    - Cadastrar as credenciais do SISDPU pelo Cofre de credenciais,
echo      em Configuracoes ^(dentro do painel ja aberto^).
echo    - Editar o .env somente se quiser mudar SAT_DADOS_DIR
echo      ^(pasta onde ficam PDFs/estado — padrao: "dados" irma de "Projeto/"^).
echo    - OPCIONAL — 2FA automatico do SAT ^(GERID^): rode
echo      "%PY% scripts\setup_totp_sat.py" e siga as instrucoes ^(precisa da
echo      "chave manual" do 2FA, mostrada junto do QR code no GERID^). Sem
echo      isso, o codigo 2FA continua sendo digitado manualmente no login.
echo    - OPCIONAL — resolucao automatica de CAPTCHA pelo CapSolver: cadastre
echo      a chave de API no Cofre de credenciais ^(sistema "CapSolver", em
echo      Configuracoes^) e ligue a opcao em Configuracoes. Sem isso, o
echo      CAPTCHA continua sendo resolvido manualmente na janela do SAT.
echo.
echo   Para abrir o painel: de duplo clique em Painel-SAT-Central.bat
echo ============================================================
echo.
pause
endlocal
