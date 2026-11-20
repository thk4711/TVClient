# Plan: first qualified Raspberry Pi 4 image release

**Audience: developers and advanced administrators.** Proposed work and release gates,
not a claim that qualification has been completed.

[Documentation index](README.md) · [Release procedure](releasing.md) ·
[Validation](validation.md)

## Goal and current gap

Deliver a GitHub image release that another person can download, verify, flash,
configure and use without a source checkout, SSH or developer assistance.
The unit of acceptance is the **exact distributed SD-card image**, identified by
its raw SHA-256, not the current application checkout or an in-place installation.

The existing build wrapper already collects images, validates input/configuration
hashes and writes `VERIFIED.json`. We should extend this foundation, not replace it.
At planning time:

- GitHub has a `v0.1.0` source tag, but no published release. Do not move that tag.
- Both locally collected verified image sets record `source_dirty=true`. The
  running Pi's image metadata also records a dirty build. These are development
  evidence, not clean release candidates; installed files may have changed since
  their original image metadata was written.
- `buildroot/build-metadata.py` and `scripts/build_image.py` require the
  `development-foundation` profile with `release=false`. There is no reviewed
  qualified-image policy.
- `.github/workflows/checks.yml` runs extensive checks and Buildroot configuration
  validation, but neither a full image build nor release publication.
- The initial assessment found no pinned qualified builder, published legal/source
  bundle, signing identity, or versioned fresh-card acceptance report. Release
  preparation now uses the four-asset layout in [Releasing](releasing.md#published-image),
  with checksums and one corresponding-source/licence bundle.

Previous builds and real-device experience reduce the risk and should inform the
checklist. Recover any existing test notes, but do not silently convert informal
or in-place tests into acceptance of a different image digest.

## Release strategy

Publish `v0.1.1` directly as a normal GitHub release, with no draft or prerelease,
as requested by the maintainer. Do not move or reuse `v0.1.0`. Prepare and verify
the complete asset set locally before publication.

GitHub publication status and image qualification are separate: the existing
`development-foundation` metadata with `release=false` describes the build profile,
not whether a GitHub download exists. Preserve that truthful metadata until the
qualified-image policy and exact-image hardware evidence are complete. The
maintainer confirms that all current features are tested and working; release
notes must still disclose any remaining exact-image/fault/endurance acceptance.
Publishing a download alone does not close every qualification gate below.

## 1. Freeze scope and supported hardware

**Owner: maintainer, with physical tester.**

Release the current Raspberry Pi 4 feature set without adding features. Document
Astra 19.2°E free-to-air H.264 HD/MPEG-2 SD with the German-audio policy, SAT-IP
and Enigma2 sources, Ethernet and Wi-Fi, browser setup/channel management and
playback, HDMI playback, keyboard/Apple Remote controls, USB time shifting,
YouTube, backups and recovery. Use the Telestar Digibit Twin reference SAT-IP
server if it matches the actual test installation.

Confirm and publish exact Pi model/RAM, power supply, cooling/case, SD-card
model/capacity, tuner firmware, satellite/LNB arrangement, HDMI display/AV receiver,
cable/port and control device identities. Measure the minimum usable card size;
do not infer it solely from the partition sizes. The identities in
[reference hardware](reference-setup.md) are examples, not a qualified bill of materials.

All current features are tested and working within their documented compatibility
limits, including USB time shifting, Apple Remote, Wi-Fi, browser playback,
Enigma2 and YouTube. Include all of them in the release scope; do not downgrade
features or narrow the product to SAT-IP/Ethernet-only operation. Optional means
user-selected, not unfinished. Record tested hardware, phone/video and browser
combinations and carry those results forward into exact-image acceptance.
Security/data-loss problems in any feature still block release.

Initially qualify the supplied Apple Remote mapping only if the physical remote
works without SSH or learning. Other remote IDs can remain explicitly outside
the first qualified combination; browser-based learning is separate work.

**Done:** one agreed compatibility table and hardware list, no new feature work.

## 2. Establish clean builds and provenance

**Owner: build/release maintainer.**

- Select a Linux amd64 Debian builder and pin its container/VM/base-image digest,
  host package versions and build tools. A container alone does not pin the host
  kernel; record the host too. Record paths, environment, job count, elapsed time,
  peak RAM and disk use. Publish a repeatable clean-build recipe.
- Build from a fresh clone of a reviewed, committed public revision with an empty
  tracked/untracked/ignored private-file inventory. Keep outputs and package
  metadata outside the checkout; share only the download cache.
- Preserve the existing Buildroot, kernel, firmware and package pins, external
  security overrides and patch hashes. Avoid unrelated dependency upgrades.
- Run the existing CI-equivalent suites and a full fresh-output image build.
  Require the wrapper's verified artifact set and review image contents,
  factory-empty data, installed dependencies and disabled-by-default SSH policy.
- Add builder identity and image-version provenance without losing the existing
  input/configuration hashes. Synchronize application version recipes/metadata;
  distinguish image version from application version where needed.
- Repeat a clean build using the pinned recipe, ideally by another person/runner,
  to establish procedural repeatability. Each build can have a different digest;
  only the selected artifact is the acceptance candidate.

Bit-for-bit reproducibility is not a first-release gate. Do not claim it without
independent byte comparisons. Retain logs privately and publish sanitized summaries.

**Done:** a clean build can be repeated from public instructions and all candidate
assets are traceable to a source commit, builder and configuration.

## 3. Implement a reviewed candidate/release policy

**Owner: code maintainer.**

Update `buildroot/build-metadata.py`, `scripts/build_image.py` and their tests
with an explicit candidate mode. Development stays the default. A candidate must
reject unknown/dirty source identity, mismatched version/commit/input hashes,
unqualified builder configuration and incomplete artifacts.

Separate build-time eligibility from post-build acceptance. **Recommended policy:**
a candidate has its image version and candidate profile baked in, but does not
claim completed physical qualification. A privately retained qualification
report records acceptance, bound to the raw-image and ZIP digests. Document the exact meaning of any `release` field before changing it;
never set `hardware_tested=true` merely because compilation passed.

Promote the already-tested bytes, not a new build. If instead stable status or a
stable tag must be baked into the filesystem, produce that final image **before**
physical sign-off and qualify it again. Never edit metadata inside a qualified
image after testing. A stable tag can point at the same reviewed candidate commit;
notes must explain any baked-in candidate label.

Add tests for candidate guards, version consistency, digest-bound qualification,
missing assets, secret/foreign-file rejection and refusal to publish partial sets.
Update the build/release guides and user-visible version descriptions together.

**Done:** passing a build cannot accidentally claim physical acceptance or publish
an unreviewed release.

## 4. Prepare a complete, lawful download set

**Owner: release maintainer, with licensing review.**

Run `legal-info` and `show-info` on the same clean source/output/configuration
that built the candidate. Follow [the legal-source procedure](releasing.md#3-collect-third-party-licences-and-corresponding-sources).
Review warnings and local-package archive inventories; never run this against the
private development checkout. Resolve toolchain/runtime-libc source omissions,
firmware redistribution conditions, vendored notices and patched copyleft sources.
A successful command or automatic GitHub source archive is not sufficient.

Publish exactly four uploaded assets:

| Asset | Purpose |
|---|---|
| Versioned Pi 4 image ZIP | Flashable image; retain the single `sdcard.img` ZIP member |
| `SHA256SUMS` | ZIP, extracted image, release notes and legal-bundle integrity checks |
| `RELEASE_NOTES.md` | Capabilities, hardware, qualification limits and installation/reflash warning |
| One versioned source/licence bundle | Target copyleft corresponding sources, patches, target notices and relevant project/Buildroot/toolchain-runtime build/configuration material |

GitHub also displays automatic project-source links. They do not replace the
third-party corresponding sources in the consolidated bundle. Retain build logs,
builder/version inventories and qualification evidence privately, not as separate
release downloads. Do not publish separate Python distributions, manuals,
manifests/evidence, signature/key assets, verification guides or reconstruction
guides with the image.

Select required material according to the actual distributed target components'
licences. Include all required target notices and target GPL/LGPL/MPL source/patch
material, plus relevant build/configuration scripts and external glibc/GCC runtime
source/licence coverage. Omit unrelated build-host downloads and permissive-only
source archives where their licences do not require them. Preserve all required
material with the binary; a successful legal-info run is not itself a completeness
certificate.

Checksums provide integrity, not publisher authentication. This simplified layout
does not publish a separate signature. Inspect archive inventories and avoid
private logs, credentials, backups and build-host provisioning data.

**Done:** a reviewed, complete, checksummed four-asset set exists.

## 5. Qualify the exact image on a fresh card

**Owner: physical tester; independent recipient for final usability sign-off.**

Preserve the working card and a private backup. Use a separate disposable card,
verify the candidate's raw digest and flash with write verification. Do not enable
SSH or import development settings for the ordinary-user setup run. Arrange
separate recovery access before disruptive tests.

Proposed minimum gates (agree them before testing; these are not current guarantees):

| Gate | Required observation |
|---|---|
| Fresh install | Factory-empty initialization; visible URL/QR; browser discovery, tuner selection, scan, favourites and first picture/sound without SSH |
| Setup interruptions | Cancel/retry scan, browser reconnect and graceful reboot; usable setup or saved lineup afterwards |
| HD/SD playback | Representative progressive/interlaced and aspect-ratio samples, German audio, stereo HDMI and confirmed HD hardware decoding |
| Controls/management | Actual keyboard/buttons, volume/OSD, favourites edits, timezone, web password, offline manual and persistence |
| Startup/persistence | 20 cold boots across configured and unconfigured states; settings/access persist; tested connector/display behavior documented |
| Switching/ownership | 500 mixed HD/SD and same/different-transponder switches; preview/scan takeover and return; no stale leases or abandoned owned children |
| Endurance | 72 hours across HD/SD with recorded segment durations, temperature/memory/storage and physical A/V samples; no unexplained reset, persistent A/V drift or unbounded resource growth |
| External outages | Tuner unavailable and network disconnect/reconnect; clear state and useful recovery without a local reboot storm |
| Local recovery | Bounded player/web/backend crash/freeze tests using the existing safe tools; saved configuration intact and physical playback restored |
| Backup lifecycle | Browser backup, reflash the same candidate, restore and verify settings/channels/access; no cross-version compatibility claim |
| All current features | Named Wi-Fi/browser/remote/USB/YouTube/Enigma2 acceptance checks on the exact image, including safe exit and documented compatibility limits |
| Recipient journey | A person other than the main developer downloads, verifies, flashes, configures and watches using only release instructions |

Use the detailed [validation matrix](validation.md#capability-and-acceptance-matrix)
and linked first-boot/time-shift/backup checklists. Record physical observations
separately from API/counter evidence, actual durations, recovery times, steps,
expected/actual results, tester, version, image digest and hardware identities.
Report skips as untested, never as passed.

Deliberate power cuts, watchdog resets and full/read-only/corrupted storage tests
need a separately approved disposable-card procedure and physical supervision.
Release claims about these faults require that evidence. Writable root/boot,
no persistent reboot-storm suppression, no recovery web mode and no data-preserving
updater remain explicit limitations. Any observed data loss, repeatable boot loop,
security regression or inability to recover ordinary core use blocks stable
publication until resolved. Broader appliance hardening remains separate work.

A fix may be rehearsed on the running Pi, but it changes the candidate: commit it,
build a new clean image, identify its new digest and repeat affected checks plus
fresh setup/core regression. Do not stitch results from different image digests
into one sign-off.

**Done:** all mandatory gates pass for one exact image; deviations are reviewed and
published without weakening the core no-developer-assistance acceptance criterion.

## 6. Publish and verify GitHub delivery

**Owner: maintainer; publication requires explicit approval.**

Add a manually dispatched release workflow or reviewed packaging/publishing helper
under `.github/workflows/` / `scripts/`. Full builds may initially run on the
pinned external Debian builder; a large hosted-CI build is not a prerequisite.
The publishing job must validate commit/version, asset hashes, required legal
material and accurate qualification claims before uploading. Keep ordinary CI
read-only and scope write permission to publication.

1. Prepare the complete asset set locally against the reviewed commit. Verify
   manifest completeness, ZIP and extracted-image digests, and source bundles.
2. Record available exact-image acceptance and obtain maintainer sign-off.
   Clearly disclose checks that remain outstanding; never invent a passed gate.
3. Publish directly as the normal `v0.1.1` release with all assets in one
   invocation, without `--draft` or `--prerelease`. Never replace the image bytes
   under the same version. Ancillary asset corrections require explicit approval
   and must preserve the image digest and legally required source availability.
4. Verify public unauthenticated downloads and recipient checksum instructions.
5. Add a prominent **Download image** link in the root README and installation/manual
   guides using the explicit version URL. Regenerate printable HTML when the manual changes.
6. Publish version-specific hardware/capabilities, limitations, backup → reflash →
   restore instructions and the whole-card data-erasure warning. No automatic update
   or cross-version migration promise is implied.

**Done:** public assets are complete and downloadable, release notes describe the
actual tested image, and user docs no longer require a source build to start.

## Test-device and repository workflow

All implementation changes must remain in the local repository as image inputs.
For each compatible runtime fix, compare existing device files, preserve a private
rollback copy, install only the affected paths and documentation, verify hashes,
then run the relevant live checks. Use the existing coordinated deployment tool
only when its broader scope is appropriate. Do not overwrite unrelated device work,
configuration or credentials. Documentation-only updates need no service restart.

Build/release workflows and metadata tooling run on the builder/orchestration host,
not the TV receiver. Native media/kernel/firmware changes cannot be fully tested by
copying Python files to the Pi. In-place testing and setup rehearsals speed iteration
but cannot replace the one necessary final image build and fresh-card acceptance.

Release preparation may update compatible application metadata and documentation
on the test Pi. Do not reset setup or run disruptive hardware tests without
separately arranging physical recovery and a viewing interruption.

## Immediate next work package

1. Maintainer approves version, release strategy, hardware and qualification scope.
2. Implement candidate/provenance policy and tested release-set packaging, together
   with a qualification-report template and pinned-builder recipe.
3. Rehearse compatible changes on the existing Pi; run CI and clean candidate build.
4. Review legal/source material, qualify a separate card, verify local assets and
   record recipient acceptance when available.
5. Publish after sign-off and update all download/installation entry points.

Do not start recovery-mode, updater, read-only-root or new-control feature projects
as prerequisites unless the candidate evidence identifies an actual release blocker.
