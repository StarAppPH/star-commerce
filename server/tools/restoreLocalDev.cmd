@echo off
REM Restore local dev MongoDB from existing dump (no download step)
for /f "tokens=*" %%i in ('cd /d "%~dp0.." ^&^& python -c "from AppConfig import Project; print(Project().projectName)"') do set "PROJECT_NAME=%%i"
set "DUMP_DIR=C:\backup\mongodump\%PROJECT_NAME%"
set "LOCAL_DB=teststarCommerce"
set "SOURCE_DB=starCommerce"

REM Check that dump directory exists
if not exist "%DUMP_DIR%" (
    echo ERROR: Dump directory %DUMP_DIR% does not exist.
    echo Run dumpProdDataToLocalDev.cmd first to download the dump.
    pause
    exit /b 1
)

REM Determine the dump subfolder deterministically
REM Prefer the explicit SOURCE_DB; otherwise auto-detect the single non-system DB folder
set "DUMP_SUBFOLDER=%SOURCE_DB%"
if "%DUMP_SUBFOLDER%"=="" (
    for /d %%F in ("%DUMP_DIR%\*") do (
        if /i not "%%~nxF"=="admin" if /i not "%%~nxF"=="local" if /i not "%%~nxF"=="config" (
            if defined DUMP_SUBFOLDER (
                echo ERROR: Multiple databases in dump. Set SOURCE_DB explicitly. & pause & exit /b 1
            )
            set "DUMP_SUBFOLDER=%%~nxF"
        )
    )
)
if "%DUMP_SUBFOLDER%"=="" ( echo ERROR: No DB folder found in %DUMP_DIR%. & pause & exit /b 1 )
if not exist "%DUMP_DIR%\%DUMP_SUBFOLDER%" ( echo ERROR: %DUMP_DIR%\%DUMP_SUBFOLDER% missing. & pause & exit /b 1 )
echo Found dump subfolder: %DUMP_SUBFOLDER%

REM Drop existing local database before restore
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

REM Run mongorestore
echo Starting mongorestore...
mongorestore --drop --db "%LOCAL_DB%" "%DUMP_DIR%\%DUMP_SUBFOLDER%"
if errorlevel 1 (
    echo mongorestore failed!
    pause
    exit /b %errorlevel%
)
echo mongorestore completed successfully.

echo Restore process completed.
pause
