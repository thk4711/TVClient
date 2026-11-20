# First-boot qualification

**Audience: developers and advanced administrators.** Current setup contracts and
acceptance checklist; user setup belongs in the [manual](user-manual.md#first-time-setup).

[Provisioning](provisioning.md) · [HDMI startup](hdmi-startup-screen.md) · [Validation](validation.md)

## Implementation contracts

- Automatic initialization accepts only the versioned factory-empty marker and
  known-empty contents. Existing/imported data retains overwrite protection;
  owned fresh staging can recover after interruption.
- The root helper owns HDMI onboarding, input enumeration and serialized Wi-Fi
  trials. Current-boot DHCP evidence must match the actual address and carrier.
- Native renderer pages are bounded and root-only, with masked passwords. QR codes
  encode the displayed setup URL directly. Numeric access remains usable without mDNS.
- Setup is password-free; post-setup management stays login-free until a web
  password is selected. Root/SSH credentials remain separate.
- Standard USB keyboards and GPIO infrared work together, including hotplug.
  Selecting an additional USB remote does not disable them.
- SAT-IP scans and Enigma2 bouquet imports share favourite selection/ordering.
  Starter channels come only from validated discoveries; full SAT-IP scanning
  does not impose a ten-minute global cutoff.

## Automated checks

Run provisioning, persistent-store, onboarding, source-browser, setup, mDNS,
input, splash, documentation and image-inventory checks. Use the real-browser
setup fixture for source selection, cancellation/recovery, input feedback,
selection/order retention and mouse/touch sorting. Independently decode native
QR exports at supported sizes. Test populated factory-empty data image contents.

## Device and fresh-card acceptance

Use a separate card to preserve the working installation. Verify:

1. Known-empty initialization, existing-data refusal and interrupted staging recovery.
2. Ethernet carrier/DHCP, numeric setup URL, hardware-derived mDNS name and phone-camera QR.
3. Wi-Fi-only country/layout selection, actual US/German keyboard password entry,
   bad-password rollback, good-password association/DHCP and reconnect after reboot.
4. USB keyboard/GPIO coexistence, physical button feedback and hotplug; readable
   HDMI welcome/progress/error screens from normal viewing distance.
5. SAT-IP discovery/manual description registration, connected tuner selection,
   full scan, cancellation/recovery and saved favourites.
6. Direct Enigma2 first setup without preliminary SAT-IP, bouquet choice/import,
   failed/stopped import retention and saved favourites.
7. HDMI picture/sound, ordinary controls, saved timezone/name/access and reboot persistence.
8. Optional web-password activation and independent root/SSH recovery without losing channels.

A configured-device rehearsal is disruptive and is not fresh-card qualification.
Use [the coordinated rehearsal](operations.md#first-boot-development-deployment)
only after preserving its original store and arranging physical recovery. Wi-Fi-only
rehearsal can disconnect SSH; the detached Wi-Fi exercise must retain private
credentials and bounded cleanup. No device rehearsal was run merely to review documentation.

Full current-source image and physical first-boot qualification remain incomplete.

## Native QR library notice

The native renderer vendors Nayuki QR-Code-generator v1.8.0 under MIT.
Copyright and the full permission/warranty notice remain in the bundled
[qrcodegen.c](../buildroot/external/package/satip-splash/src/qrcodegen.c) and
[qrcodegen.h](../buildroot/external/package/satip-splash/src/qrcodegen.h).
Preserve those notices in source/binary distributions; see
[publication and licensing](development.md#publication-and-licensing).
