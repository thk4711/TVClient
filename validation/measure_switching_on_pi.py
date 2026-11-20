"""Opt-in benchmark of the running player; injects menu keys, never another stream."""
import argparse
import asyncio
import contextlib
import json
import math
import os
import signal
import statistics
import time
from pathlib import Path

from satip_controller.backend import Backend
from satip_controller.config import Config, credentials, library
from satip_controller.mpv_ipc import IPCError, MPVIPC
from satip_controller.player import SD_FILTERS, url
from satip_controller.audio import normal_german, choose_track
from satip_controller.switching import SwitchExperiments
from satip_controller.timeshift import READY
from satip_controller.state import runtime_path, read_json, lease_active, atomic_json
from satip_controller.coordination import preview_held


def phase_schedule(phases, blocks=1, first="baseline-first"):
    """Counterbalance independent baseline/candidate pairs, not pooled presets."""
    if blocks < 1 or (blocks > 1 and len(phases) != 2):
        raise ValueError("Repeated blocks require exactly one candidate")
    if first not in ("baseline-first", "candidate-first"):
        raise ValueError("Invalid phase order")
    scheduled = []
    for block in range(1, blocks + 1):
        reverse = (first == "candidate-first") != (block % 2 == 0)
        ordered = list(reversed(phases)) if reverse else phases
        for position, (phase, settings) in enumerate(ordered, 1):
            scheduled.append((block, position, phase, settings))
    return scheduled


def timing_summary(results):
    """Descriptive software timings only; never treat failures as fast samples."""
    measured = [r for r in results if "advancing_seconds" in r]
    def group(rows):
        values = [r["advancing_seconds"] for r in rows]
        return {"samples": len(values), "median_seconds": round(statistics.median(values), 3),
                "min_seconds": min(values), "max_seconds": max(values)}
    return {phase: {"overall": group(rows),
                    "by_block": {str(b): group([r for r in rows if r["block"] == b])
                                 for b in sorted({r["block"] for r in rows})},
                    "by_transition": {t: group([r for r in rows if r["transition"] == t])
                                      for t in sorted({r["transition"] for r in rows})}}
            for phase in sorted({r["phase"] for r in measured})
            if (rows := [r for r in measured if r["phase"] == phase])}


def cache_summary(samples):
    """Sampled post-readiness buffering, not a physical freeze measurement."""
    pauses = 0
    seconds = 0
    previous = None
    durations = []
    for stamp, paused, duration in samples:
        if paused is True and (previous is None or previous[1] is not True):
            pauses += 1
        if previous is not None and previous[1] is True:
            seconds += max(0, stamp - previous[0])
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            durations.append(duration)
        previous = (stamp, paused)
    return {"cache_pause_count": pauses, "sampled_cache_pause_seconds": round(seconds, 3),
            "min_cache_seconds": round(min(durations), 3) if durations else None}


class MuxGrace:
    """Explicit admin-only native comparison; never grant the player admin rights."""
    def __init__(self, backend, frontend, milliseconds):
        from satip_controller.config import uuid
        self.backend, self.frontend = backend, uuid(frontend)
        self.milliseconds = milliseconds
        self.original = None

    async def read(self):
        result = await self.backend.request('idnode/load', uuid=self.frontend)
        entries = result.get('entries', [])
        if len(entries) != 1 or entries[0].get('uuid') != self.frontend:
            raise ValueError('Cannot identify the comparison frontend')
        params = {p['id']: p.get('value') for p in entries[0].get('params', [])}
        value = params.get('idle_grace')
        if type(value) is not int or not params.get('enabled'):
            raise ValueError('Enabled frontend with native idle_grace patch required')
        return value

    async def set(self, value):
        await self.backend.save(uuid=self.frontend, idle_grace=value)
        if await self.read() != value:
            raise ValueError('Backend did not apply idle mux grace')

    async def restore(self):
        if self.original is not None:
            await self.set(self.original)


async def measure(args):
    # evdev is intentionally optional for ordinary fake tests on other platforms.
    from evdev import UInput, ecodes

    if os.geteuid() != 0 or not args.confirm_interrupt:
        raise ValueError('Requires root and --confirm-interrupt; do not use physical controls during the check')
    if args.small_experiments and (args.compare or args.probe_size or args.compare_initial_buffer or args.compare_timeshift):
        raise ValueError('Small experiments must be compared independently of legacy option comparisons')
    if args.phase_blocks > 1 and not (
            args.small_experiments and len(args.small_experiments) == 1
            or args.compare_initial_buffer and not (args.compare or args.probe_size or args.compare_timeshift)):
        raise ValueError("Repeated blocks require one candidate")
    native_grace = args.small_experiments == ['mux_grace']
    if ('mux_grace' in (args.small_experiments or []) and not native_grace
            or native_grace and not args.frontend_uuid):
        raise ValueError('Mux grace requires an independent comparison and --frontend-uuid')
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    config = Config.load(args.config)
    channels, favourites = library(read_json(config.library))
    selected = []
    for name in args.channels:
        matches = [channels[c] for c in favourites if channels[c].name == name]
        if len(matches) != 1:
            raise ValueError('Each channel name must identify exactly one favourite')
        selected.append(matches[0])
    if len(selected) < 2 or len({c.uuid for c in selected}) != len(selected):
        raise ValueError('Select at least two distinct channels')
    catalogue_before = config.library.read_bytes()

    def check_exclusions():
        if config.library.read_bytes() != catalogue_before:
            raise ValueError("Channel catalogue changed during comparison")
        for name in ('scan-lease.json', 'preview-lease.json'):
            try:
                lease = read_json(runtime_path(config.management_dir, name))
            except FileNotFoundError:
                continue
            held = preview_held(lease) if name == 'preview-lease.json' else lease_active(lease)
            if lease.get('recovery_pending') or held:
                raise ValueError('Scan, preview or recovery is active')

    def status():
        check_exclusions()
        return read_json(runtime_path(config.state_dir, 'status.json'))

    initial = status()
    if initial.get('paused') or initial.get('error') or not initial.get('playing'):
        raise ValueError('The production player must already be playing normally')
    original = channels[initial['playing']]
    if args.restore_channel:
        matches = [channels[c] for c in favourites if channels[c].name == args.restore_channel]
        if len(matches) != 1:
            raise ValueError('Restore channel must identify exactly one favourite')
        original = matches[0]
    backend = Backend(credentials(Path(args.admin_credentials)))
    grace = MuxGrace(backend, args.frontend_uuid, args.mux_grace_ms) if native_grace else None
    if grace:
        grace.original = await grace.read()
    ipc = await MPVIPC.connect(config.runtime_dir / 'mpv.sock')
    events = []
    reader, writer = await asyncio.open_unix_connection(str(config.runtime_dir / 'mpv.sock'))

    async def collect_events():
        while line := await reader.readline():
            message = json.loads(line)
            if message.get('event') in ('start-file', 'file-loaded', 'playback-restart'):
                events.append((message['event'], time.monotonic()))

    collector = asyncio.create_task(collect_events())
    options = {}
    ui = None
    switch = None
    results = []
    current_phase = None
    current_block = None
    current_channel = None
    current_stage = "setup"
    grace_restore_failure = None
    readiness = READY.read_bytes() if args.compare_timeshift else None
    readiness_mode = READY.stat().st_mode & 0o777 if args.compare_timeshift else None
    experiment_path = config.runtime_dir / 'switch-experiments.json'
    experiment_before = experiment_path.read_bytes() if experiment_path.exists() else None
    experiment_mode = experiment_path.stat().st_mode & 0o777 if experiment_path.exists() else None
    experiment_owner = (experiment_path.stat().st_uid, experiment_path.stat().st_gid) if experiment_path.exists() else None

    def restore_experiments():
        if not args.small_experiments:
            return
        if experiment_before is None:
            experiment_path.unlink(missing_ok=True)
        else:
            experiment_path.write_bytes(experiment_before)
            experiment_path.chmod(experiment_mode)
            os.chown(experiment_path, *experiment_owner)
    try:
        for name in ('demuxer-lavf-analyzeduration', 'cache-pause-wait', 'demuxer-lavf-probesize',
                     'cache-pause-initial'):
            options[name] = await ipc.get('options/' + name)
        if (args.compare_initial_buffer
                and (await ipc.get('cache-pause-initial') is True
                     or await ipc.get('cache-pause-initial') != options['cache-pause-initial'])):
            raise ValueError('Initial buffering already enabled or file-local; compare application versions')
        ui = UInput({ecodes.EV_KEY: [ecodes.KEY_ENTER, ecodes.KEY_ESC, ecodes.KEY_UP, ecodes.KEY_DOWN,
                                            ecodes.KEY_Q, ecodes.KEY_A, ecodes.KEY_Z, ecodes.KEY_SPACE]},
                    name='satip-switch-validation', vendor=config.keyboard_vendor,
                    product=config.keyboard_product, bustype=ecodes.BUS_USB)
        # Only this virtual keyboard node; generic udev keyboard tagging can
        # differ on older images. Do not alter real input-device permissions.
        import grp
        await asyncio.sleep(.5)
        os.chown(ui.device.path, -1, grp.getgrnam("satip-input").gr_gid)
        os.chmod(ui.device.path, 0o660)
        await asyncio.sleep(2)

        async def tap(code, delay=None):
            ui.write(ecodes.EV_KEY, code, 1); ui.syn()
            await asyncio.sleep(.05)
            ui.write(ecodes.EV_KEY, code, 0); ui.syn()
            # Menu preparation is outside the timed confirmation. Give the
            # controller time to render each step rather than queueing keys
            # that can then be debounced at nearly identical processing times.
            await asyncio.sleep(args.navigation_delay if delay is None else delay)

        async def switch(channel):
            nonlocal current_channel
            current_channel = channel.name
            current = status()
            if current.get('paused') or current.get('owner_pid') != initial['owner_pid']:
                raise ValueError('Player paused or restarted during the check')
            if current.get('playing') == channel.uuid and current.get('requested') == channel.uuid:
                return {'channel': channel.name, 'already_playing': True}
            source = channels[current['playing']]
            await tap(ecodes.KEY_ESC)
            await tap(ecodes.KEY_ENTER)
            index = favourites.index(current['requested'])
            target = favourites.index(channel.uuid)
            down = (target - index) % len(favourites)
            up = (index - target) % len(favourites)
            for _ in range(min(down, up)):
                await tap(ecodes.KEY_DOWN if down <= up else ecodes.KEY_UP)
            old_path = await ipc.get("path")
            events.clear()
            started = time.monotonic()
            await tap(ecodes.KEY_ENTER, .25)
            previous = None
            advancing = None
            max_subscriptions = 0
            next_backend = 0
            request_seen = False
            async with asyncio.timeout(20):
                while True:
                    now = time.monotonic()
                    current = status()
                    if current.get('paused') or current.get('owner_pid') != initial['owner_pid']:
                        raise ValueError('Player paused or restarted during the check')
                    requested = current.get('requested')
                    if requested == channel.uuid:
                        request_seen = True
                    elif request_seen or requested != source.uuid:
                        observed = channels.get(requested)
                        raise ValueError("Intervening channel request during comparison: "
                                         + (observed.name if observed else "unknown"))
                    # Published status can still contain the source request
                    # while confirmation is being processed. Keep that delay
                    # inside the existing deadline and measured duration.
                    if now >= next_backend:
                        subscriptions = await backend.grid('status/subscriptions')
                        max_subscriptions = max(max_subscriptions, len(subscriptions))
                        if max_subscriptions > 1:
                            raise ValueError('Overlapping backend subscriptions detected')
                        next_backend = now + .2
                    with contextlib.suppress(IPCError):
                        media_path = await ipc.get('path')
                        if media_path == url(channel) or (isinstance(media_path, str) and media_path.endswith('/live.ts')
                                and media_path != old_path and current.get('requested') == channel.uuid):
                            position = await ipc.get('time-pos')
                            if isinstance(position, (int, float)):
                                if previous is not None and position > previous + .01 and advancing is None:
                                    advancing = time.monotonic() - started
                                previous = position
                    if (request_seen and current.get('playing') == channel.uuid and current.get('loading') is None
                            and advancing is not None):
                        break
                    await asyncio.sleep(.05)
            tracks = await ipc.get('track-list')
            aid = await ipc.get('aid')
            audio_ok = any(t.get('type') == 'audio' and t.get('id') == aid
                           and normal_german(t) for t in tracks)
            preferred = choose_track(tracks, channel.audio_pid,
                                     await ipc.get('audio-channels') != 'stereo')
            audio_ok = audio_ok and preferred is not None and preferred['id'] == aid
            vf = await ipc.get('vf')
            filter_ok = (any(f.get('name') == 'lavfi' and f.get('params') == SD_FILTERS[0]['params']
                             for f in vf) if channel.video == 'mpeg2' else not vf)
            result = {'channel': channel.name, 'from_channel': source.name,
                      'transition': ('same-transponder' if source.mux_uuid == channel.mux_uuid
                                     else 'different-transponder'),
                      'started_monotonic': started,
                      'advancing_seconds': round(advancing, 3),
                      'confirmed_seconds': round(time.monotonic() - started, 3),
                      'audio_ok': audio_ok, 'filter_ok': filter_ok,
                      'switch_seconds': current.get('switch_seconds'),
                      'hwdec': await ipc.get('hwdec-current'),
                      'max_subscriptions_observed': max_subscriptions,
                      'relay_worker_pid': current.get('health', {}).get('relay_worker_pid'),
                      'relay_capture_pid': current.get('health', {}).get('relay_capture_pid')}
            for event in ('file-loaded', 'playback-restart'):
                times = [at - started for name, at in events if name == event and at >= started]
                result[event + '_seconds'] = round(times[0], 3) if times else None
            if not audio_ok or not filter_ok:
                raise ValueError('Audio PID or HD/SD filter regression')
            # Track startup audio/video behaviour beyond initial readiness.
            # A selected track alone can hide missing timestamps or A/V drift.
            audio_samples = []
            cache_samples = []
            max_avsync = 0
            initial_drops = await ipc.get('frame-drop-count')
            settle_until = time.monotonic() + args.settle_seconds
            while time.monotonic() < settle_until:
                current = status()
                if (current.get('playing') != channel.uuid or current.get('error')
                        or current.get('owner_pid') != initial['owner_pid']):
                    raise ValueError('Playback recovery or channel change during settling')
                with contextlib.suppress(IPCError):
                    audio_pts = await ipc.get('audio-pts')
                    if type(audio_pts) in (int, float):
                        audio_samples.append(audio_pts)
                avsync = await ipc.get('avsync')
                if type(avsync) in (int, float):
                    max_avsync = max(max_avsync, abs(avsync))
                cache_samples.append((time.monotonic(), await ipc.get('paused-for-cache'),
                                      await ipc.get('demuxer-cache-duration')))
                await asyncio.sleep(.1)
            cache_samples.append((time.monotonic(), await ipc.get('paused-for-cache'),
                                  await ipc.get('demuxer-cache-duration')))
            result.update(cache_summary(cache_samples))
            result['audio_timestamp_samples'] = len(audio_samples)
            result['audio_advanced_seconds'] = (round(audio_samples[-1] - audio_samples[0], 3)
                                                if len(audio_samples) > 1 else None)
            result['max_avsync_seconds'] = round(max_avsync, 3)
            result['settle_dropped_frames'] = max(0, (await ipc.get('frame-drop-count')) - initial_drops)
            if len(audio_samples) < 2 or audio_samples[-1] <= audio_samples[0]:
                raise ValueError('Selected audio did not advance during settling')
            return result

        async def rapid_zap():
            # Use real input policy and controller ownership, never a second
            # upstream client. Alternate directions so only an adjacent saved
            # favourite and the current one are requested.
            current = status()
            target = channels[favourites[(favourites.index(current['requested']) + 1) % len(favourites)]]
            await tap(ecodes.KEY_ESC)
            started = time.monotonic()
            for code in (ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_UP):
                ui.write(ecodes.EV_KEY, code, 1); ui.syn()
                await asyncio.sleep(.02)
                ui.write(ecodes.EV_KEY, code, 0); ui.syn()
                await asyncio.sleep(.09)
            maximum = 0
            # Two existing bounded tunes may need to finish (old, latest).
            async with asyncio.timeout(config.load_timeout * 2 + 5):
                while True:
                    current = status()
                    if current.get('owner_pid') != initial['owner_pid'] or current.get('paused'):
                        raise ValueError('Player restarted or paused during rapid zapping')
                    maximum = max(maximum, len(await backend.grid('status/subscriptions')))
                    if maximum > 1:
                        raise ValueError('Overlapping subscriptions during rapid zapping')
                    if (current.get('requested') == current.get('playing') == target.uuid
                            and current.get('loading') is None and not current.get('error')):
                        break
                    await asyncio.sleep(.1)
            tracks = await ipc.get('track-list')
            chosen = choose_track(tracks, target.audio_pid, await ipc.get('audio-channels') != 'stereo')
            if chosen is None or chosen['id'] != await ipc.get('aid'):
                raise ValueError('Rapid zapping selected wrong audio')
            return {'rapid_zap': True, 'channel': target.name, 'requests': 3,
                    'latest_confirmed_seconds': round(time.monotonic() - started, 3),
                    'max_subscriptions_observed': maximum, 'audio_ok': True}

        phases = [('baseline', options)]
        if args.compare:
            phases.append(('analysis-0.5', {**options, 'demuxer-lavf-analyzeduration': .5}))
            phases.append(('analysis-0.5-cache-0.2',
                           {**options, 'demuxer-lavf-analyzeduration': .5, 'cache-pause-wait': .2}))
        if args.probe_size:
            phases.append(('analysis-0.5-probe-' + str(args.probe_size),
                           {**options, 'demuxer-lavf-analyzeduration': .5,
                            'demuxer-lavf-probesize': args.probe_size}))
        if args.compare_initial_buffer:
            phases.append(('initial-buffer', {**options, 'cache-pause-initial': True}))
        if args.compare_timeshift:
            phases = [('direct', options), ('raw-passthrough', options)]
        if args.small_experiments:
            phases = [('baseline', options)] + [(name, options) for name in args.small_experiments]
        schedule = phase_schedule(phases, args.phase_blocks, args.phase_order)
        print(json.dumps({'comparison': True, 'rounds_per_block': args.rounds,
                          'phase_blocks': args.phase_blocks, 'phase_order': args.phase_order,
                          'settle_seconds': args.settle_seconds,
                          'navigation_delay': args.navigation_delay,
                          'matrix': [{'from_channel': source.name, 'channel': target.name,
                                      'transition': ('same-transponder' if source.mux_uuid == target.mux_uuid
                                                     else 'different-transponder')}
                                     for source, target in zip(selected, selected[1:] + selected[:1])]}),
              flush=True)
        for block, phase_position, phase, settings in schedule:
            current_phase = phase
            current_block = block
            current_stage = "warmup"
            print(json.dumps({'phase_begin': phase, 'block': block,
                              'phase_position': phase_position}), flush=True)
            if args.small_experiments:
                flags = SwitchExperiments.comparison_flags(phase)
                if grace:
                    # Stage two is the accepted baseline, not the historical all-off path.
                    flags['persistent_relay'] = True
                    check_exclusions()
                    await grace.set(grace.milliseconds if phase == 'mux_grace' else 0)
                atomic_json(experiment_path, flags, 0o600, durable=False)
                runtime_owner = config.runtime_dir.stat()
                os.chown(experiment_path, runtime_owner.st_uid, runtime_owner.st_gid)
            if args.compare_timeshift:
                READY.write_bytes(readiness)
                READY.chmod(0o666 if phase == 'direct' else readiness_mode)
                # Warm-up switches below load the intended path; they are not
                # included in timing samples and do not modify persisted USB settings.
            for name, value in settings.items():
                if await ipc.get('options/' + name) != value:
                    await ipc.command('set_property', 'options/' + name, value)
            # Warm up each phase; do not include its first tune in the comparison.
            await switch(selected[-1])
            if (args.compare_initial_buffer
                    and await ipc.get('cache-pause-initial') != settings['cache-pause-initial']):
                raise ValueError('Live-load policy overrides initial-cache comparison; compare application versions')
            await switch(selected[0])
            # Repeated comparisons warm every channel under each phase so a
            # saved-audio candidate is not penalized by first-visit cache misses.
            if args.phase_blocks > 1:
                for channel in selected[1:] + selected[:1]:
                    await switch(channel)
            current_stage = "timed"
            for round_number in range(1, args.rounds + 1):
                for channel in selected[1:] + selected[:1]:
                    result = {'phase': phase, 'block': block, 'phase_position': phase_position,
                              'round': round_number, **await switch(channel)}
                    if result.get('already_playing'):
                        raise ValueError("Timed matrix contains a skipped switch")
                    if (phase == 'persistent_relay' or grace) and not result.get('relay_worker_pid'):
                        raise ValueError("Persistent relay candidate fell back or is not installed")
                    results.append(result)
                    print(json.dumps(result), flush=True)
            if args.rapid_zap:
                current_stage = "rapid-zap"
                print(json.dumps({'phase': phase, **await rapid_zap()}), flush=True)
            print(json.dumps({'phase_end': phase, 'block': block}), flush=True)
    except BaseException as failure:
        # Keep phase identity even if a subsequent restoration also fails.
        print(json.dumps({'failed_phase': current_phase,
                          'block': current_block, 'channel': current_channel, 'stage': current_stage,
                          'failure': type(failure).__name__,
                          'completed_switches': len(results)}), flush=True)
        raise
    finally:
        if grace:
            from satip_controller.lifecycle import complete
            try:
                await complete(asyncio.create_task(grace.restore()))
            except Exception as failure:
                grace_restore_failure = failure
                print(json.dumps({'mux_grace_restore_failed': type(failure).__name__}), flush=True)
        restore_experiments()
        if readiness is not None:
            READY.write_bytes(readiness)
            READY.chmod(readiness_mode)
        restart = False
        try:
            for name, value in options.items():
                if await ipc.get('options/' + name) == value:
                    continue
                # mpv reports the default probesize as 0 but rejects setting 0.
                # Restore that sentinel through a controlled service restart.
                if name == 'demuxer-lavf-probesize' and value == 0:
                    restart = True
                else:
                    await ipc.command('set_property', 'options/' + name, value)
            if ui is not None and switch is not None:
                await switch(original)
        finally:
            if ui is not None:
                ui.close()
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await ipc.close()
            if restart:
                for action in ('stop', 'start'):
                    process = await asyncio.create_subprocess_exec(
                        '/usr/bin/python3', '-m', 'satip_appliance.supervisor', 'player', action)
                    if await process.wait() != 0:
                        raise ValueError('Failed to restore default probe size through player restart')
                async with asyncio.timeout(30):
                    while status().get('playing') != original.uuid or status().get('owner_pid') == initial['owner_pid']:
                        await asyncio.sleep(.1)
    if grace_restore_failure is not None:
        raise ValueError('Failed to restore native mux grace; inspect backend before another comparison') from grace_restore_failure
    print(json.dumps({'restored_channel': original.name, 'restored_options': options,
                      'switches': len(results), 'physical_picture_audio_verified': False,
                      'timing_summary': timing_summary(results)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/satip-player/config.json')
    parser.add_argument('--admin-credentials', default='/etc/satip-web/tvheadend-admin.json')
    parser.add_argument('--confirm-interrupt', action='store_true')
    parser.add_argument('--channels', nargs='+', required=True)
    parser.add_argument('--restore-channel',
                        help='Explicit final favourite instead of the initial channel (useful after a failed earlier trial)')
    parser.add_argument('--rounds', type=int, choices=range(1, 11), default=2)
    parser.add_argument('--phase-blocks', type=int, choices=range(1, 11), default=1,
                        help='Repeat an independent baseline/candidate pair, reversing order each block')
    parser.add_argument('--phase-order', choices=('baseline-first', 'candidate-first'), default='baseline-first',
                        help='Starting order; repeated blocks alternate it')
    parser.add_argument('--settle-seconds', type=int, choices=range(1, 16), default=1,
                        help='Keep each channel playing after readiness before switching again')
    parser.add_argument('--navigation-delay', type=float, choices=(.25, .75, 1.), default=.75,
                        help='Untimed menu-key spacing; confirmation timing retains its original spacing')
    parser.add_argument('--compare-timeshift', action='store_true',
                        help='Compare direct TV and raw live relay with identical player options; temporarily revokes RAM readiness, then restores it')
    parser.add_argument('--compare', action='store_true', help='Temporarily test analysis and cache options, then restore')
    parser.add_argument('--compare-initial-buffer', action='store_true',
                        help='Compare startup buffering with the existing cache wait, then restore')
    parser.add_argument('--probe-size', type=int, choices=(32768, 65536, 131072),
                        help='Also test a bounded probe size; may restart player to restore its default')
    parser.add_argument('--small-experiments', nargs='+',
                        choices=(*SwitchExperiments.__dataclass_fields__, 'combined', 'mux_grace'),
                        help='Compare runtime-only candidates against all-off baseline, then restore; combined excludes bounded_probe and the separate stage-two persistent_relay candidate')
    parser.add_argument('--frontend-uuid', help='Enabled SAT-IP frontend for native mux_grace comparison')
    parser.add_argument('--mux-grace-ms', type=int, choices=range(1, 2001), default=1000,
                        help='Candidate idle-mux window; baseline is zero, stage two stays enabled')
    parser.add_argument('--rapid-zap', action='store_true',
                        help='Also alternate three channel keys per phase; visits the next saved favourite and checks latest request/ownership')
    asyncio.run(measure(parser.parse_args()))
