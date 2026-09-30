@echo off
rem Phase 8 build helper: fresh MinGW configure + build of the buddydoom target.
rem PATH is pinned to the portable toolchain on F: (no admin, no sh.exe interference).
set PATH=F:\Project-Nemesis\tools\w64devkit\w64devkit\bin;F:\Project-Nemesis\tools\cmake-3.31.6-windows-x86_64\bin;%PATH%
set CMAKE_PREFIX_PATH=F:\Project-Nemesis\SDL3
cd /d F:\Project-Nemesis
if not exist BuddyDoom\build\CMakeCache.txt (
    cmake -B BuddyDoom\build -S BuddyDoom -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
    if errorlevel 1 exit /b 1
)
cmake --build BuddyDoom\build --target buddydoom
