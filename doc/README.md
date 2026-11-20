# Documentation

## Using the receiver

These guides are for receiver owners. No knowledge of the software internals is
needed. Start with the illustrated [User manual](user-manual.md); the
[printable HTML edition](user-manual.html) is also available through **User manual**
in the receiver's browser interface, without internet access.

| Guide | Purpose |
|---|---|
| [User manual](user-manual.md) · [Printable HTML](user-manual.html) | Complete illustrated setup, everyday operation, settings, backups and basic troubleshooting |
| [Installation](installation.md) | Hardware requirements and image-writing precautions |
| [First boot](buildroot-first-boot-setup.md) | Short setup checklist linking to the manual's detailed steps |
| [Usage](usage.md) | Reference for controls, programme information, channels and browser viewing |
| [Device settings](device-settings.md) | Detailed setting restrictions, backups and advanced password recovery |
| [Infrared hardware](infrared.md) | Safe GPIO23 wiring, Apple Remote checks and ID learning |
| [USB time shifting](timeshift.md) | Pause/seek controls, retention limits and safe removal |
| [Enigma2 source](enigma2.md) | Compatible receivers, bouquet imports and separate source lineups |
| [YouTube TV code](youtube.md) | Phone pairing, supported videos and privacy |

## Developing and administering

These guides are for developers and advanced administrators. They describe the
current implementation, maintenance procedures and qualification limits. Shell
commands may interrupt television or change configuration; follow their stated
safety boundaries. Routine operation belongs in the user guides above.

| Guide | Purpose |
|---|---|
| [Development](development.md) | Source map, dependencies, tests, contribution and documentation maintenance |
| [Architecture](architecture.md) | Component responsibilities, playback and exclusive ownership |
| [Buildroot image](../buildroot/README.md) | Build inputs, artifacts, provisioning and image recovery |
| [Services and persistent data](buildroot-services-persistence.md) | Supervision, storage layout, import and persistence |
| [Operations](operations.md) | Administration, in-place updates, backups and rollback |
| [Troubleshooting](troubleshooting.md) | Advanced diagnosis beyond the manual's browser-based checks |
| [Automatic recovery](recovery.md) | Recovery policies, guardians, watchdog and safe tests |
| [HDMI startup](hdmi-startup-screen.md) · [Boot process](boot-process.md) | Startup, readiness, handoff and diagnostics |
| [Provisioning](provisioning.md) · [Internal configuration](configuration.md) | Boot-file options, setup state and configuration contracts |
| [Tvheadend](tvheadend.md) | Backend, channel policy, scanning and EIT |
| [HTTP API](api.md) | Endpoints, request contracts, authentication and errors |
| [Security](security.md) | Network, credential and privilege boundaries |
| [Channel switching](channel-switching.md) | Current relay/mux defaults, diagnostic overrides and comparisons |
| [Time-shift internals](timeshift-internals.md) | RAM prebuffer, USB copier, retention and failure handling |
| [Enigma2 internals](enigma2-internals.md) | CLI import, persistence, startup buffering and backend builds |
| [YouTube internals](youtube-internals.md) | Resolver, media bridge, dependencies and live checks |
| [Validation](validation.md) | Current acceptance boundaries and qualification procedure |
| [First-boot qualification](first-boot-validation.md) · [Time-shift qualification](timeshift-validation.md) · [Backup qualification](backup-validation.md) | Feature-specific acceptance checklists |
| [Releasing](releasing.md) | Development prerelease checks and licensing |
| [Reference hardware](reference-setup.md) | Non-portable example hardware and identities |

The [validation tools index](../validation/README.md) describes reusable test
commands. Historical investigations are not current guides or image inputs;
preserved records live under `validation/records/` in the source checkout.
