# Building and publishing a release candidate

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Image build guide](../buildroot/README.md) · [Validation](validation.md)

The implementation and qualification work needed for the first downloadable and
qualified image release is tracked in the [image release plan](image-release-plan.md).
That plan does not change the current release status or authorize publication.

The current profile produces **development installation images**. Its metadata
and wrapper deliberately require `release=false`. There is no qualified release
profile, data-preserving firmware updater, or demonstrated bit-for-bit reproducible
build. Publishing source or passing CI does not change those boundaries.

## Published image

The [v0.1.1 GitHub release](https://github.com/thk4711/TVClient/releases/tag/v0.1.1)
is published as a normal release, not a draft or prerelease. Its four uploaded assets are
the image ZIP, `SHA256SUMS`, `RELEASE_NOTES.md` and one
`satip-sources-and-licences-v0.1.1.tar.gz` bundle. The source tag identifies commit
`1e94c75d3cbe761064cd7bfdf96ca44f04eb3446`; publication does not change the image
qualification boundaries described above. The image bytes and source tag are unchanged by asset consolidation.
Release-specific capabilities, installation precautions and limitations are in
the notes; the image already includes its offline user manual.

## 1. Select the candidate and builder

Review and commit the intended public source before selecting a candidate revision.
Include newly added source files, documentation and licences; inspect the candidate
file list and history for private content. Keep generated packages/images out of the
source tree. The [examples](../examples/README.md) are reference files, not the image's
runtime configuration.

Synchronize the version in `pyproject.toml`, local Buildroot package recipes and
application-build metadata when changing versions. Record the candidate commit,
Buildroot pin, kernel/firmware pins and FFmpeg backport revision. Preserve the
current development metadata until a separately reviewed release policy exists.

Use an isolated Linux amd64 Debian build account, following the prerequisites in
the image guide. Record Debian release, architecture, installed host package
versions, compiler/make/Python versions, build paths, environment and job count.
No specific builder image digest, minimum RAM/disk capacity, or build duration has
been qualified for the current source; measure these during candidate builds.
Do not imply that the helper's platform preflight establishes that qualification.

### Clean public source is required for legal-info

**Do not collect legal-info from a private working repository.** The pinned
Buildroot `package/pkg-generic.mk` local/override legal-info archiver rsyncs the
entire package source directory with VCS exclusions, without the package-specific
normal-build rsync allowlist. `satip-app` uses the repository root. Ignored private
backups, host settings, credentials and generated distributions could therefore
be included even though ordinary image staging excludes them.

Use a fresh clone of the reviewed public repository/tag, never a filesystem copy
of the private working directory. Keep outputs, artifacts and package-building
metadata outside it. For example, on the build host (replace all placeholders):

~~~sh
PUBLIC_REPOSITORY=https://github.com/OWNER/REPOSITORY.git
CANDIDATE_TAG=v0.1.1
SOURCE=/home/BUILD_USER/releases/candidate/source
export BUILDROOT_DIR=/home/BUILD_USER/releases/candidate/buildroot
export SATIP_OUTPUT_DIR=/home/BUILD_USER/releases/candidate/output
export BR2_DL_DIR=/home/BUILD_USER/releases/downloads
ARTIFACTS=/home/BUILD_USER/releases/candidate/artifacts

git clone --branch "$CANDIDATE_TAG" --single-branch "$PUBLIC_REPOSITORY" "$SOURCE"
git -C "$SOURCE" rev-parse HEAD
git -C "$SOURCE" status --porcelain --untracked-files=all
~~~

Require an empty status and compare the commit to the reviewed candidate. Also
inspect the directory inventory: Git status omits ignored files. Do not copy
private INI/provisioning files into this clone. Use fresh source/output paths for
each candidate; share only the download cache. The commands below create outputs.

## 2. Run checks and build the image

Run the automated checks documented in [Development](development.md) from a
separate clean test checkout. Include Python 3.14 with the Buildroot dependency
fixture, native splash compilation/tests, browser/device tests and package checks.
Dependency fixtures approximate target dependencies; they do not execute AArch64
binaries. Record commands, results and any skipped coverage.

On the isolated build host, configure and build from the clean source:

~~~sh
"$SOURCE/buildroot/build.sh" --configure-only
python3 "$SOURCE/scripts/build_image.py" --execution local \
  --repository "$SOURCE" --buildroot-dir "$BUILDROOT_DIR" \
  --output-dir "$SATIP_OUTPUT_DIR" --download-cache "$BR2_DL_DIR" \
  --artifacts-dir "$ARTIFACTS" --jobs 16
~~~

Adjust the job count to the builder. The wrapper is CLI/INI configured; exported
shell-helper variables are not substitutes for its explicit path arguments.
Do not run the direct helper and wrapper concurrently. The output must be fresh
for a candidate, even though the wrapper supports incremental development builds.

Require the completed artifact directory's `VERIFIED.json`, matching source/Git
identity, resolved configuration and checksums. Inspect image partition/layout and
installed contents. The hooks refresh board boot configuration and validate exact
application source inventories; they are not a filesystem-wide attestation or
hardware acceptance test. Preserve logs, but review host paths and other metadata
before publishing them.

## 3. Collect third-party licences and corresponding sources

Run against the **same clean checkout, output and configuration** used above:

~~~sh
make -C "$BUILDROOT_DIR" O="$SATIP_OUTPUT_DIR" \
  BR2_EXTERNAL="$SOURCE/buildroot/external" BR2_DL_DIR="$BR2_DL_DIR" legal-info
make -s -C "$BUILDROOT_DIR" O="$SATIP_OUTPUT_DIR" \
  BR2_EXTERNAL="$SOURCE/buildroot/external" BR2_DL_DIR="$BR2_DL_DIR" \
  show-info > "$ARTIFACTS/package-info.json"
~~~

Review `$SATIP_OUTPUT_DIR/legal-info/README`, warnings, target/host manifests,
licence files, source archives and applied patch series. Inspect archive member
inventories, including the local application package archive, before publishing.
A successful `legal-info` command is not a completeness certificate.

The image includes separately licensed Linux, BusyBox, FFmpeg/x264, Tvheadend,
Python/dependencies, fonts and Raspberry Pi/Wi-Fi firmware. The original SAT-IP
code is MIT; that does not replace their licences. In particular:

- Provide the applicable complete corresponding sources for the distributed
  copyleft binaries, including local patches, build/configuration scripts and
  the selected external tree. Publish them alongside the versioned binary with
  equally usable download access; an upstream homepage alone is insufficient.
- Check external Bootlin toolchain source coverage, including the runtime libc
  and other redistributed toolchain components; review legal-info's omissions.
- Review firmware redistribution conditions and preserve required notices.
- Review yt-dlp's vendored components, not just its top-level Unlicense notice.
- Preserve hls.js's Apache-2.0 attribution and full licence, the QR encoder's MIT
  notice, and the MIT native-splash licence. The hls.js notice records the verified
  bundled version/origin and the absence of a separate upstream NOTICE at that tag.
- Preserve the FFmpeg backport's imported copyrights, licences and patch provenance.

Archive the complete public project revision as well as Buildroot legal-info.
GitHub's automatic source archive of this project does not contain downloaded
third-party corresponding source. For example:

~~~sh
git -C "$SOURCE" archive --format=tar --prefix=satip-source/ HEAD \
  > "$ARTIFACTS/satip-source.tar"
gzip -n "$ARTIFACTS/satip-source.tar"
tar -C "$SATIP_OUTPUT_DIR" -czf "$ARTIFACTS/third-party-legal-info.tar.gz" legal-info
~~~

Include the exact Buildroot source revision/build machinery and any additional
sources identified by the review. Retain the reviewed source/license bundles and
all required accompanying material for as long as the corresponding binaries are
available, and follow any longer retention required by the chosen distribution
method. A machine-readable package inventory is useful for an SBOM, but
`show-info` by itself is not a complete SBOM or licence/source audit.

## 4. Verify Python distributions separately

Build Python packages in a separate clean public checkout, not the source tree
used by legal-info. Packaging creates egg-info/build metadata. With Python 3.13+
and the development dependencies installed:

~~~sh
python -m build --outdir /absolute/path/to/new-package-artifacts
python scripts/check_package.py \
  /absolute/path/to/new-package-artifacts/*.whl \
  /absolute/path/to/new-package-artifacts/*.tar.gz
~~~

Use a new output directory so globs cannot pick up old versions. The checker
compares both archives' application payloads with current source, checks licence
metadata/notices and rejects unexpected archive paths. It extracts the sdist to a
temporary directory, builds a wheel with isolated PEP 517 build dependencies, and
installs each checked wheel in a fresh environment outside the checkout. It checks
both entry points and installed file hashes with Python path overrides removed.
Dependency/build-backend installation needs network access unless a reviewed
package mirror is configured. Current lower-bound dependencies are not a lockfile.

The Python sdist intentionally excludes tests, Buildroot, examples and appliance
image tooling. Use the complete project source archive for contributor tests and
image reconstruction. Both wheel and sdist carry the original MIT licence plus
bundled third-party notices; their combined license expression is
`MIT AND Apache-2.0`. This does not relicense original project code.

## 5. Fresh-card acceptance and reproducibility

Use the exact candidate image on a separate test card and record its SHA-256.
Follow [first boot](buildroot-first-boot-setup.md) and the outstanding acceptance
items in [Validation](validation.md#capability-and-acceptance-matrix):

- Automatic fresh initialization, password-free setup, lineup save and reboot persistence.
- Ethernet and applicable Wi-Fi setup, UART/SSH recovery and disabled-access policy.
- Physical HDMI picture/audio, HD/SD playback, keyboard/remote controls and OSD.
- Cold-start splash, selected/fallback connector and handoff to playback.
- Exclusive browser preview/takeover and return to HDMI.
- Bounded service recovery, repeated switching/endurance and relevant optional
  feature/storage scenarios, with their untested limits recorded explicitly.

Record hardware identities and distinguish physical observations from programmatic
checks. In-place updates on an older installation do not qualify a fresh image.
Failures or untested areas belong in candidate release notes.

For future byte-identical reproducibility, first define and pin a builder image,
canonical paths and environment, enable/review Buildroot reproducibility support,
set a stable `SOURCE_DATE_EPOCH`, and normalize filesystem identifiers/timestamps
and ZIP member metadata. Then perform two independent clean builds and compare
artifacts, investigating differences. Current input hashes, checked-hash Python
bytecode and ZIP integrity tests do not establish that property. Document actual
results rather than inventing requirements or promising reproducibility.

## 6. Publish the versioned GitHub image release

For `v0.1.1`, publish directly as a normal GitHub release with no draft or
prerelease. Prepare all assets locally first. Keep the existing development build
profile and `release=false` metadata truthful; a GitHub release is not by itself
a claim of complete fresh-image/hardware qualification. All current features
are tested and working within their compatibility limits. Record outstanding
exact-image checks in the release notes.

Bind every published artifact to the candidate commit and image digest. Upload
exactly four assets:

- Versioned image ZIP, retaining its single `sdcard.img` member.
- `SHA256SUMS` covering the three other assets and the extracted raw image.
- `RELEASE_NOTES.md` with capabilities, hardware, qualification limits and the
  whole-card data replacement warning; use the same text as the release body.
- One versioned source/licence bundle with target copyleft corresponding sources,
  applied patches, target notices, relevant build scripts/configurations, and
  external-toolchain runtime sources/notices omitted by ordinary legal-info.

Keep build logs and qualification evidence privately. Do not attach separate
Python distributions, offline manuals, build manifests/evidence, signature keys,
signatures, verification guides or reconstruction guides to image releases.
GitHub's automatic project-source links are additional platform-generated links,
not replacements for the bundled third-party corresponding sources.

Source selection is based on the actual distributed target components and their
licences, not every build download. Preserve GPL/LGPL/MPL corresponding sources
and relevant local changes, all required target notices (including permissive
components and firmware), the exact public project/Buildroot build machinery,
and necessary generated build configuration. External glibc/GCC runtime coverage
requires its sources, licence texts, patches and toolchain build machinery.
Build-host tools not distributed in the image and permissive-only source archives
need not all be published. Review alternate and package-specific licence terms;
a name/version/upstream-link inventory alone is not complete corresponding source.

Checksums provide integrity, not publisher authentication; this simplified release
layout does not publish a separate signature. Do not change `release=false` or
claim hardware qualification just to publish files. Build-profile promotion still
requires a separately implemented and reviewed policy/profile.

Release automation should build from reviewed revisions, use fresh outputs,
retain logs and legal evidence, and publish only completed candidate sets. Do not
expose private build-host settings or device provisioning data in workflow inputs,
logs, assets or source archives.

## Recipient verification

Download the image ZIP and `SHA256SUMS` from the same GitHub release over HTTPS.
The checksum file has entries for the ZIP, extracted `sdcard.img`, release notes
and source/licence bundle. Check only the entries for files you downloaded, or
extract the image and download the full asset set before checking the whole file.
A missing listed file is not a successful full-set check. There is no separate
signature or verification guide in the simplified release.

For your own wrapper builds, the existing `SHA256SUMS` covers collected artifacts
and `BUILD_SHA256SUMS` preserves the raw-image digest. These build manifests are
not additional downloadable release assets.

Write the extracted image using Raspberry Pi Imager's **Use custom** option and
write verification. This replaces all card partitions and data. Use the project's
provisioning instructions instead of Imager OS customization, then follow
[installation](installation.md).
