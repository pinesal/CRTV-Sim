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

FFmpeg: https://ffmpeg.org/ ; the current binary is Gyan's 2025-02-02 essentials
build from FFmpeg commit `957eb2323a`. Its upstream `README.txt` contains the
configuration and external-library versions; the portable build now includes that
file as `licenses/FFmpeg-BUILD-README.txt`. The binary is statically linked and
enables GPL and version 3 components, including x264 and x265. Gyan's archived
binary release is at
https://github.com/GyanD/codexffmpeg/releases/tag/2025-02-02-git-957eb2323a .
The FFmpeg core source is at
https://github.com/FFmpeg/FFmpeg/tree/957eb2323a .

mpv: https://mpv.io/ ; the current binary is the official 0.41.0 Windows CI build
at mpv commit `41f6a645068483470267271e1d09966ca3b9f413`. The package includes
mpv's copyright notice and GPL license text. It also includes `vulkan-1.dll`,
whose Vulkan Loader license is included. The build statically links FFmpeg and
other libraries; see the upstream build recipe at
https://raw.githubusercontent.com/mpv-player/mpv/41f6a645068483470267271e1d09966ca3b9f413/ci/build-win32.ps1 .
The mpv source is at
https://github.com/mpv-player/mpv/tree/41f6a645068483470267271e1d09966ca3b9f413 .
Its embedded FFmpeg reports revision `f853d12`; the release build recipe obtains
that source from the GStreamer Meson ports `meson-8.0` branch.

These notices improve attribution but do not, by themselves, satisfy the source
requirements for distributing the exact binaries. The Gyan README identifies
FFmpeg's external-library versions, but the complete source archive, each
dependency's applicable notices, and build recipe for every statically linked
dependency still need to be assembled. mpv's release build recipe references
moving dependency branches, and the exact CI-resolved revisions have not yet
been recovered. The packaging script will add the available notices and build
inventory to future packages; do not publish a portable ZIP containing these
tools until the corresponding source package is complete, or replace them with
builds whose full source inputs and recipes can be pinned and distributed.
Codec patent obligations also need separate review for the planned distribution
regions.

This candidate does not invent a source offer or claim license clearance.
CRTV Sim's original source code and documentation are licensed under the MIT
License; that license does not replace the separate terms for bundled third-party
tools and assets. Code signing, hosting, and a clean standard-account machine
test are also pending public-release choices.

DejaVu font source/license: https://github.com/dejavu-fonts/dejavu-fonts ; the bundled
font was obtained from Matplotlib 3.10.7's unmodified DejaVu font resources.
