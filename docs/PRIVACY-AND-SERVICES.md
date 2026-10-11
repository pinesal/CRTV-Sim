# Optional services and privacy

The Dashboard binds to 127.0.0.1 by default. No network access is enabled by setup.
The library scanner reads filenames and probes local video/audio metadata. It does
not upload the library. Review and file-processing actions run locally.

Weather, when enabled and configured, sends the selected location to Open-Meteo
for forecasts. Internet Media contacts the source URLs you add, including Internet
Archive for catalog metadata and downloads. HTTPS uses verified Windows trust when
available. Provider services receive your IP address as part of normal networking.

Optional OpenAI graphics generation sends your station identity, creative direction
and selected reference/template images. Announcer generation sends configured
personality instructions and factual upcoming lineup details to the script model;
the generated dialogue is sent to the selected TTS provider (OpenAI or Breeze Blue).
Breeze model/voice catalog requests contact Breeze Blue. These features can incur
provider charges and follow each provider's retention/privacy terms. No key is
required for ordinary TV playback.

Dashboard-entered keys are stored in the current Windows user's Credential Manager,
not ordinary settings, logs or profile backups. OPENAI_API_KEY and BREEZE_API_KEY
environment values override saved keys. A profile moved to another Windows account
or PC needs credentials configured again. The Dashboard reports only configured
status and never returns the saved secret. Removing a saved key does not remove an
environment override. API key save reports storage failures; provider test buttons
verify service access. A configured status alone does not certify account billing
or key validity.

Weather artwork generation sends the selected condition or holiday and your art
direction to OpenAI using the saved OpenAI key. Uploaded Weather artwork stays
local. CRTV Sim shares holiday configuration with its local Weather companion;
this calendar is not sent to an external service except the selected holiday
name when you request holiday artwork generation.

Local logs and database can contain media paths, channel names and schedule details.
Review diagnostic excerpts before sharing them. The distributed ZIP contains no
user database, credentials, cached downloads or generated station artwork.
