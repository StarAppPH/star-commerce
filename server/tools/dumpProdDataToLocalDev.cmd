@echo off
REM Local scope first, so nothing set here (the connection string included) leaks into the
REM calling shell
setlocal
REM The dump folder is named from the project name written here, never from a lookup
REM that could come back empty and leave the shared C:\backup\mongodump parent as the target
set "DUMP_DIR=C:\backup\mongodump\starCommerce"
set "LOCAL_DB=teststarCommerce"
set "SOURCE_DB=starCommerce"

REM The source database, the first one found wins:
REM   1. MONGO_DUMP_URI from the environment: a read-only user's connection string.
REM   2. MONGO_DUMP_URI from server\cloud.env (written by create-star-app cloud-setup; gitignored).
REM   3. MONGO_DUMP_HOST and MONGO_DUMP_USER (from the environment, else the defaults
REM      below); mongodump itself then asks for the password (masked).
REM The script never prints a password. With 1 or 2 the password is inside the connection
REM string, so it is on mongodump's command line, which other processes of the same Windows
REM user and command-line auditing can read: use a read-only user. With 3 it is on no
REM command line.
REM A new project leaves the defaults empty; a fleet repo writes its own cluster host and
REM read-only user here.
if not defined MONGO_DUMP_HOST set "MONGO_DUMP_HOST="
if not defined MONGO_DUMP_USER set "MONGO_DUMP_USER="

set "CLOUD_ENV=%~dp0..\cloud.env"
set "DUMP_SOURCE="
if defined MONGO_DUMP_URI set "DUMP_SOURCE=the MONGO_DUMP_URI environment variable"
if not defined MONGO_DUMP_URI if exist "%CLOUD_ENV%" (
    for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b "MONGO_DUMP_URI=" "%CLOUD_ENV%"`) do set "MONGO_DUMP_URI=%%b"
)
if not defined DUMP_SOURCE if defined MONGO_DUMP_URI set "DUMP_SOURCE=server\cloud.env"

if "%MONGO_DUMP_URI%"=="" (
    if not defined MONGO_DUMP_HOST goto :noSource
    if not defined MONGO_DUMP_USER goto :noSource
    goto :prompt
)
goto :haveUri

:prompt
REM No password in the URI: mongodump is given the user and asks for the password itself
set "MONGO_URI=mongodb+srv://%MONGO_DUMP_HOST%/?appName=starCommerce"
set "DUMP_USER_ARG=1"
goto :dump

:haveUri
set "MONGO_URI=%MONGO_DUMP_URI%"
set "DUMP_USER_ARG="
REM Host and user for the message below, read from the URI (never the password)
set "MONGO_DUMP_HOST=an unparsed host"
set "MONGO_DUMP_USER=an unparsed user"
for /f "usebackq tokens=1,2" %%a in (`powershell -NoProfile -Command "try { $u = [uri]$env:MONGO_URI; $u.Host + ' ' + [uri]::UnescapeDataString($u.UserInfo.Split(':')[0]) } catch { }"`) do (
    set "MONGO_DUMP_HOST=%%a"
    set "MONGO_DUMP_USER=%%b"
)
echo Using MONGO_DUMP_URI from %DUMP_SOURCE%.
goto :dump

:noSource
echo ERROR: no source database to dump. Supply one of:
echo   1. MONGO_DUMP_URI in the environment: the read-only user's connection string
echo   2. MONGO_DUMP_URI in server\cloud.env: run create-star-app cloud-setup, which writes it
echo   3. MONGO_DUMP_HOST and MONGO_DUMP_USER in the environment, or as the defaults in this
echo      script; mongodump then asks for the password
pause
exit /b 1

:dump
REM Delayed expansion from here on: the URI, host and user are only ever read as !NAME!,
REM so the shell symbols a connection string or a decoded user name may hold stay literal
setlocal enabledelayedexpansion
echo Dumping from !MONGO_DUMP_HOST! as !MONGO_DUMP_USER!

REM Only ever delete THIS project's own namespaced subfolder (never the shared parent)
if exist "%DUMP_DIR%" rmdir /s /q "%DUMP_DIR%"
mkdir "%DUMP_DIR%"

REM Step 1: Run mongodump from remote MongoDB, always scoped to SOURCE_DB (the cluster is
REM shared: never dump another service's database)
echo Starting mongodump...
if defined DUMP_USER_ARG (
    mongodump --uri "!MONGO_URI!" --username "!MONGO_DUMP_USER!" --db "%SOURCE_DB%" --out "%DUMP_DIR%"
) else (
    mongodump --uri "!MONGO_URI!" --db "%SOURCE_DB%" --out "%DUMP_DIR%"
)
if errorlevel 1 (
    echo mongodump failed!
    endlocal
    pause
    exit /b %errorlevel%
)
REM The connection string is not needed past the dump: mongosh and mongorestore below must
REM not inherit it
set "MONGO_URI="
set "MONGO_DUMP_URI="
echo mongodump completed successfully.

REM Step 2: Determine the dump subfolder deterministically
REM Prefer the explicit SOURCE_DB; otherwise auto-detect the single non-system DB folder
set "DUMP_SUBFOLDER=%SOURCE_DB%"
if "%DUMP_SUBFOLDER%"=="" (
    for /d %%F in ("%DUMP_DIR%\*") do (
        if /i not "%%~nxF"=="admin" if /i not "%%~nxF"=="local" if /i not "%%~nxF"=="config" (
            if defined DUMP_SUBFOLDER (
                echo ERROR: Multiple databases in dump. Set SOURCE_DB explicitly. & endlocal & pause & exit /b 1
            )
            set "DUMP_SUBFOLDER=%%~nxF"
        )
    )
)
if "%DUMP_SUBFOLDER%"=="" ( echo ERROR: No DB folder found in %DUMP_DIR%. & endlocal & pause & exit /b 1 )
if not exist "%DUMP_DIR%\%DUMP_SUBFOLDER%" ( echo ERROR: %DUMP_DIR%\%DUMP_SUBFOLDER% missing. & endlocal & pause & exit /b 1 )
echo Found dump subfolder: %DUMP_SUBFOLDER%

REM Step 3: Drop the existing local database before restore
echo Dropping existing local database: %LOCAL_DB% ...
where mongosh >nul 2>nul
if %ERRORLEVEL%==0 (
    mongosh --quiet --eval "db.getSiblingDB('%LOCAL_DB%').dropDatabase()"
) else (
    where mongo >nul 2>nul
    if %ERRORLEVEL%==0 (
        mongo --quiet --eval "db.getSiblingDB('%LOCAL_DB%').dropDatabase()"
    ) else (
        echo Warning: No mongo shell found to drop database. Proceeding with restore using --drop.
    )
)

REM Step 4: Run mongorestore into the unique local DB
echo Restoring to standalone MongoDB...
mongorestore --drop --db "%LOCAL_DB%" "%DUMP_DIR%\%DUMP_SUBFOLDER%"
if errorlevel 1 (
    echo mongorestore failed!
    endlocal
    pause
    exit /b %errorlevel%
)
echo mongorestore completed successfully.

endlocal
echo Backup and restore process completed.
pause
