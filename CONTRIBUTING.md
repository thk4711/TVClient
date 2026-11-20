# Contributing

Start with [development](doc/development.md), [architecture](doc/architecture.md),
and the [image build guide](buildroot/README.md). The appliance targets Raspberry
Pi 4; host tests do not establish physical HDMI, audio, remote or fresh-image
acceptance.

Before opening a pull request:

1. Keep changes focused and add regression tests for changed behavior.
2. Run the documented Python, JavaScript, packaging and relevant native checks.
3. Update current behavior and limitations in the documentation.
4. Describe verification performed and any hardware checks still outstanding.

Do not include credentials, device exports, private hostnames, logs, screenshots,
browser profiles, build outputs or historical experiment results. Use synthetic
fixtures and sanitized reproductions. New contributions must be compatible with
the project’s [MIT licence](LICENSE); preserve third-party attribution and terms.

See [security reporting](SECURITY.md) before discussing a vulnerability publicly.
