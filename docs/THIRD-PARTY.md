# Third-party distribution inventory

BUILD-INVENTORY.json records installed Python package versions, Python runtime,
and the exact bundled media-tool versions, build flags and SHA256 hashes.
The licenses folder contains available package license notices, Python's license,
and installed FFmpeg/mpv notices. Public assets use DejaVu Sans Mono with its
Bitstream/DejaVu license in assets/fonts. The owner's VCR OSD Mono font, weather
music, weather artwork and runtime weather data are not included.

Python is from https://www.python.org/ ; the frozen application uses PyInstaller
(https://pyinstaller.org/) and its bootloader exception. Breeze Blue SDK, truststore,
certifi and timezone data retain their included upstream notices. Installed package
inventory may include build tools which are not loaded by the frozen application.

FFmpeg: https://ffmpeg.org/ ; installed build from https://www.gyan.dev/ffmpeg/builds/ .
mpv: https://mpv.io/ ; Windows binary obtained from the installed mpv-player.mpv-CI.MSVC WinGet package. Confirm its precise upstream build provenance before publication.
The bundled FFmpeg enables GPL and version 3 components; mpv and linked libraries
have their own GPL/LGPL obligations. Tool version/build flags in the inventory
identify their commits. License notices and upstream links alone are not a complete
corresponding-source package for these exact prebuilt tools and all linked codecs.

Before publishing publicly, obtain and host the exact corresponding source and
build recipes for the tool commits and linked dependencies, or replace them with
builds whose complete source materials you can distribute. Verify codec/patent
obligations applicable to your distribution. This candidate does not invent a
source offer, claim license clearance, or grant a license to CRTV Sim itself.
Choose CRTV Sim's own license before public redistribution. Code signing, hosting
and a clean standard-account machine test are also pending public-release choices.

DejaVu font source/license: https://github.com/dejavu-fonts/dejavu-fonts ; the bundled
font was obtained from Matplotlib 3.10.7's unmodified DejaVu font resources.
