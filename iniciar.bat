@echo off
chcp 65001 >nul 2>&1
title Maps Scraper - Instalacao e Inicio
color 0A

echo.
echo  ========================================
echo    Maps Scraper - Setup Automatico
echo  ========================================
echo.

:: Verifica Python
where py >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERRO] Python nao encontrado.
    echo  Baixe em: https://www.python.org/downloads
    echo  Marque "Add python.exe to PATH" ao instalar.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('py --version 2^>^&1') do set PYVER=%%v
echo  [OK] Python %PYVER% encontrado
echo.

:: Instala Playwright (unica dependencia pip)
echo  [1/3] Instalando dependencias...
py -m pip install --quiet --upgrade pip
py -m pip install --quiet -r requirements-browser.txt
if %errorlevel% neq 0 (
    echo  [AVISO] Playwright nao instalou. O scraper funciona sem ele.
) else (
    echo  [OK] Playwright instalado
)

:: Instala navegador do Playwright
echo.
echo  [2/3] Instalando navegador Chromium para Playwright...
py -m playwright install chromium >nul 2>&1
if %errorlevel% neq 0 (
    echo  [AVISO] Navegador Playwright nao instalou. Funciona sem ele.
) else (
    echo  [OK] Chromium instalado
)

:: Cria .env se nao existe
if not exist .env (
    copy .env.example .env >nul 2>&1
    echo  [OK] Arquivo .env criado
)

:: Inicia o engine
echo.
echo  [3/3] Iniciando o engine...
echo.
py scripts/engine.py start
if %errorlevel% neq 0 (
    echo.
    echo  [ERRO] Falha ao iniciar o engine.
    pause
    exit /b 1
)

echo.
echo  ========================================
echo    Tudo pronto!
echo    Acesse: http://localhost:8080
echo  ========================================
echo.
echo  Pressione qualquer tecla para abrir no navegador...
pause >nul
start http://localhost:8080
