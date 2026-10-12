# CRTV Sim portable Windows build

Build from the project root with `.\scripts\build-portable-windows.ps1`.
The current build target is `dist/CRTV-Sim-Portable-0.9.48`. Add
`-SkipArchive` for internal testing when you only need the unpacked build.
Without that option, the build also creates an adjacent
`CRTV-Sim-0.9.48-Windows.zip` and SHA256 checksum. The ZIP includes the empty
`Media` scaffold, CRTV Sim, optional RetroWeather, FFmpeg, FFprobe, mpv, fonts,
migrations and clean portable defaults. Run `CRTV-Sim.exe` after extracting.
The existing RetroTV packages remain unchanged until a new build is requested.

The package excludes installed user-data, personal media and development
settings. Fresh installations run setup before indexing. Public downloads do
not automatically import sibling installations. Existing profiles skip setup;
their inherited defaults and personal settings are preserved. Updates can be
copied over an existing installation while retaining its user-data and media.
Update shortcuts to the new launcher rather than starting a leftover RetroTV.exe.

See [DISTRIBUTION.md](DISTRIBUTION.md) for setup, safe shutdown, updates,
rollback and the remaining public-release gates. The build script creates a
clean release package: never target a live installation containing user-data.

The internal retrotv package, database paths, environment variables, credential
targets and production markers retain their existing names for compatibility.
Recovery recognizes both CRTV-Sim.exe and legacy RetroTV.exe. RetroWeather
keeps its separate name.
