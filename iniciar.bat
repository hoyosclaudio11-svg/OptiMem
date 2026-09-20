@echo off
REM OptiMem - levanta el recolector y el panel juntos.
REM
REM OJO: se usa `call` para python. El python de pyenv es un shim (.bat) y sin
REM `call` el control no vuelve nunca: el .bat queda colgado y las corridas
REM terminan sin linea final. Ya nos paso con otros proyectos.
title OptiMem

cd /d "%~dp0"

echo.
echo   OptiMem
echo   =====================================================
echo.

python -c "import psutil, sklearn, fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo   Faltan dependencias. Instalalas con:
  echo       python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo   Datos en: %LOCALAPPDATA%\OptiMem
echo.

REM Primera vez: calibrar para poder traducir paginas a milisegundos.
if not exist "%LOCALAPPDATA%\OptiMem\optimem.db" (
  echo   Primera corrida: midiendo el costo de un trim...
  call python optimem.py calibrar
  echo.
)

echo   Levantando el panel en http://127.0.0.1:5215
start "" /min call python optimem.py panel --sin-abrir

REM Un segundo para que el panel tome el puerto antes de que arranque el
REM recolector, y no se pisen los mensajes en la consola.
timeout /t 2 /nobreak >nul

echo   Recolectando. Cerra esta ventana o Ctrl+C para detener.
echo.
call python optimem.py recolectar

echo.
echo   Recolector detenido. El panel sigue abierto en http://127.0.0.1:5215
echo.
pause
