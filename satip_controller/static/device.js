/* Appliance-only device settings. Passwords stay in form memory, never browser storage. */
(() => {
    const $ = id => document.getElementById('device-' + id);
    let available = false, pending = false, loaded = false, trial = null;
    let requireAuth = true, sourceSwitchPending = null;
    let storageData = {disks: [], mounted: false};
    let passwordKind = null, storageLimitDevice = null;
    const sensitive = new Set(['ssh', 'restart_player', 'reboot', 'shutdown', 'storage_eject', 'storage_forget', 'source_select', 'satip_tuner']);
    const message = (id, text) => { $(id).textContent = text; };
    function askPassword() {
        if (!requireAuth) return Promise.resolve(undefined);
        return new Promise(resolve => {
            const dialog = $('password-dialog');
            const finish = value => { $('current-password').value = ''; dialog.close(); resolve(value); };
            $('password-prompt').onsubmit = event => { event.preventDefault(); finish($('current-password').value); };
            $('password-cancel').onclick = () => finish(null);
            dialog.oncancel = event => { event.preventDefault(); finish(null); };
            dialog.showModal(); $('current-password').focus();
        });
    }
    function controls() {
        document.querySelectorAll('#device-settings input, #device-settings select, #device-settings button').forEach(control => { control.disabled = pending || !loaded || (!!sourceSwitchPending || !!window.sourceTunerPending) || !!window.sourceImportBusy; });
        if (trial && !pending) {
            document.querySelectorAll('#device-settings form input, #device-settings form select, #device-settings form button').forEach(control => { control.disabled = true; });
            $('ssh').disabled = $('wifi-enabled').disabled = true;
        }
        document.querySelectorAll('#device-change-password-dialog input, #device-change-password-dialog button').forEach(control => { control.disabled = pending || (!!sourceSwitchPending || !!window.sourceTunerPending); });
        for (const kind of ['web', 'root']) $('change-' + kind + '-password').disabled = pending || !loaded || !!trial || (!!sourceSwitchPending || !!window.sourceTunerPending);
        const disk = storageData.disks.find(d => d.id === $('storage-disk').value);
        const blocked = pending || !loaded || !!trial || (!!sourceSwitchPending || !!window.sourceTunerPending);
        for (const id of ["backup-download", "backup-restore", "backup-file", "channels-download", "channels-restore", "channels-file"]) $(id).disabled = blocked;
        $('storage-mount').disabled = $('storage-format').disabled = blocked || storageData.mounted || !disk || disk.busy;
        $('storage-eject').disabled = blocked || !storageData.mounted;
        $('storage-forget').disabled = blocked || storageData.mounted || !storageData.remembered;
        $('storage-choose').disabled = blocked || storageData.mounted || !storageData.disks.some(d => !d.busy);
        $('storage-save').disabled = $('storage-gib').disabled = blocked || !storageData.mounted;
        document.querySelectorAll('#device-storage-dialog select, #device-storage-dialog button').forEach(control => { control.disabled = blocked; });
        $('storage-mount').disabled = $('storage-format').disabled = blocked || storageData.mounted || !disk || disk.busy;
        window.sourceTunerControls();
    }
    window.sourceTunerControls = () => {
        const config = window.sourceConfiguration, satip = config?.satip;
        const blocked = pending || !loaded || !!trial || (!!sourceSwitchPending || !!window.sourceTunerPending) || !!window.sourceImportBusy || !!window.sourceTunerPending;
        const selected = document.getElementById('source-tuner');
        const apply = document.getElementById('source-apply-tuner');
        const activate = document.getElementById('source-activate');
        const available = config?.available?.includes($('source').value);
        activate.textContent = $('source').value === 'enigma2' ? 'Activate Enigma2 source' : 'Activate SAT-IP source';
        activate.disabled = blocked || !config || !available || config.active === $('source').value;
        document.getElementById('source-activation-help').textContent = !config ? 'Loading source configuration…' :
            config.active === $('source').value ? 'This source is already active.' : available ? 'Activate the saved channel lineup for this source.' :
            $('source').value === 'enigma2' ? 'Load Enigma2 TV bouquets here, then import them on Channels before activation.' : 'SAT-IP initial server and channel setup is required.';
        selected.disabled = blocked || !satip?.connected || config?.active !== 'satip';
        apply.disabled = selected.disabled || !selected.value || selected.value === satip?.configured_frontend;
        document.getElementById('source-tuner-help').textContent = config?.active !== 'satip' ? 'Activate SAT-IP before changing its tuner.' :
            !satip?.connected ? 'The configured server is unavailable; tuner changes are disabled.' :
            selected.value === satip.configured_frontend ? '' : 'Apply this tuner to briefly restart playback and this page. Channels and favourites are retained.';
    };
    async function refreshStorage() {
        const data = await receiverRequest('/api/device/storage');
        storageData = {...data, disks: data.disks || [], mounted: data.mounted === true};
        const selected = $('storage-disk').value;
        $('storage-disk').replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = ''; placeholder.textContent = 'Choose a USB disk'; $('storage-disk').append(placeholder);
        for (const disk of storageData.disks) {
            const option = document.createElement('option'); option.value = disk.id;
            option.textContent = disk.name + ' · ' + disk.model + ' · ' + (disk.bytes / 1024**3).toFixed(1) + ' GiB · ' + disk.serial +
                ' · ' + (disk.filesystems.map(f => f.type).join(', ') || 'No filesystem') + (disk.busy ? ' · In use' : '');
            $('storage-disk').append(option);
        }
        $('storage-disk').value = storageData.disks.some(d => d.id === selected) ? selected : '';
        $('storage-choose').hidden = data.mounted || !storageData.disks.some(d => !d.busy);
        $('storage-eject').hidden = $('storage-limit-form').hidden = !data.mounted;
        $('storage-forget').hidden = data.mounted || !data.remembered;
        const used = data.device;
        const identity = used ? used.name + ' · ' + used.model : 'USB storage';
        message('storage-status', data.mounted ? identity + ' · ' +
            (data.total_bytes / 1024**3).toFixed(1) + ' GiB capacity · ' +
            (data.free_bytes / 1024**3).toFixed(1) + ' GiB free' :
            (storageData.disks.some(d => !d.busy) ? 'USB storage is not mounted.' :
                storageData.disks.length ? 'USB storage devices are currently in use.' : 'Please connect a USB storage device to use time shift.'));
        if (data.mounted && !data.ready) message('storage-status', $('storage-status').textContent + ' · Unavailable');
        if (data.mounted) {
            $('storage-gib').max = String(data.total_bytes / 1024**3);
            if (storageLimitDevice !== data.device_id) $('storage-gib').value = String(data.max_bytes / 1024**3);
            storageLimitDevice = data.device_id;
            if ($('storage-dialog').open && !pending) $('storage-dialog').close();
        } else storageLimitDevice = null;
    }
    async function refresh(fill = false) {
        const data = await receiverRequest('/api/device');
        requireAuth = data.require_auth !== false;
        const source = data.tv_source;
        $('source-card').hidden = !source;
        window.configuredTVSources = source?.available || [];
        if (source) window.sourceConfiguration = {...window.sourceConfiguration, active: source.active, available: source.available};
        if (source && fill) {
            $('source').replaceChildren();
            for (const name of ['satip', 'enigma2']) {
                const option = document.createElement('option'); option.value = name;
                option.textContent = name === 'enigma2' ? 'Enigma2 receiver' : 'SAT-IP server';
                $('source').append(option);
            }
            $('source').value = source.active;
        }
        if (sourceSwitchPending && source?.active === sourceSwitchPending) location.reload();
        if (window.sourceTunerPending && (data.message === 'SAT-IP tuner applied.' || data.message?.startsWith('SAT-IP tuner change failed'))) {
            window.sourceTunerPending = false;
            if (data.message === 'SAT-IP tuner applied.') location.reload();
            else message('tuner-message', data.message);
        }
        if (sourceSwitchPending && data.message?.startsWith('TV source selection failed')) {
            sourceSwitchPending = null; message('source-message', data.message);
        }
        if (trial && !data.trial) {
            $('ssid').value = data.wifi_ssid; $('country').value = data.wifi_country;
            message('wifi-message', data.message || 'Wi-Fi trial ended.');
        }
        trial = data.trial; loaded = true;
        $('wifi-trial').hidden = !trial;
        if (trial) message('trial-message', 'Trying ' + trial.ssid + ' · Reverts in ' + trial.seconds_left + ' seconds');
        if (fill) {
            $('hostname').value = data.hostname;
            $('timezone').replaceChildren();
            for (const zone of data.timezones) {
                const option = document.createElement('option'); option.value = zone; option.textContent = zone.replaceAll('_', ' ');
                $('timezone').append(option);
            }
            $('timezone').value = data.timezone;
            $('ssid').value = data.wifi_ssid; $('country').value = data.wifi_country;
            $('ssh').checked = data.ssh_enabled; $('wifi-enabled').checked = data.wifi_enabled;
        }
        try { await refreshStorage(); }
        catch (error) {
            storageData = {disks: [], mounted: false}; storageLimitDevice = null;
            for (const id of ['storage-choose', 'storage-eject', 'storage-forget', 'storage-limit-form']) $(id).hidden = true;
            message('storage-status', error.message);
        }
        controls();
    }
    window.loadDeviceSettings = async enabled => {
        available = enabled; document.getElementById('device-settings').hidden = !enabled;
        $('channel-backup').hidden = !enabled;
        if (!enabled || pending) return;
        try { await refresh(true); }
        catch (error) { message('message', error.message); controls(); }
    };
    async function action(name, args, target) {
        if (pending) return false;
        pending = true; controls();
        let current_password;
        if (sensitive.has(name)) {
            current_password = await askPassword();
            if (current_password === null) { pending = false; controls(); return false; }
        }
        message(target, 'Applying…');
        try {
            const result = await receiverRequest('/api/device', 'POST', {action: name, args, ...(current_password === undefined ? {} : {current_password})}, undefined, name === 'storage_format' ? 180000 : name === 'storage_eject' ? 60000 : 30000);
            message(target, result.message || 'Done.');
            if (name === 'satip_tuner') {
                window.sourceTunerPending = true;
            } else if (name === 'source_select') {
                sourceSwitchPending = args.source;
            } else if (name === 'web_password') {
                message(target, 'Web password changed. Sign in again with the new password.');
            } else if (name !== 'reboot' && name !== 'shutdown') {
                if (result.trial_id) trial = {id: result.trial_id, ssid: args.ssid, seconds_left: 90};
                if (name === 'wifi') {
                    $('wifi-trial').hidden = false;
                    message('trial-message', 'Reconnect, then select Keep settings within 90 seconds.');
                } else await refresh(false);
            }
            if (name === 'scan_wifi') {
                $('networks').replaceChildren();
                for (const network of result.networks) {
                    const option = document.createElement('option'); option.value = network.ssid;
                    option.textContent = network.signal + ' dBm'; $('networks').append(option);
                }
                message(target, result.networks.length + ' networks found.');
            }
            return true;
        } catch (error) { message(target, error.message); return false; }
        finally { current_password = undefined; pending = false; controls(); }
    }
    function backupId() {
        const bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }
    async function backupRequest(path, options) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 120000);
        try {
            const response = await fetch(path, {...options, signal: controller.signal,
                headers: {'X-Satip-Action': '1', ...(options?.headers || {})}});
            if (!response.ok) {
                let detail;
                try { detail = (await response.json()).detail; } catch (_) {}
                throw Error(typeof detail === 'string' ? detail : 'Backup request failed. Please retry.');
            }
            return response;
        } finally { clearTimeout(timer); }
    }
    async function waitForBackup(id, target = 'backup-message', restored = false) {
        const deadline = Date.now() + 420000;
        while (Date.now() < deadline) {
            let result;
            try { result = await receiverRequest('/api/device', 'POST', {action: 'backup_status', args: {id}}, undefined, 10000); }
            catch (_) { /* The web service briefly stops during the snapshot. */ }
            if (result?.state === (restored ? 'restored' : 'ready')) return;
            if (result && ['error', 'missing'].includes(result.state)) throw Error(result.message || 'Backup expired. Create a new backup.');
            message(target, (restored ? 'Restoring channels…' : 'Creating backup…') + ' TV and the web service briefly pause. Reconnecting…');
            await new Promise(resolve => setTimeout(resolve, 2000));
        }
        throw Error('Backup is taking longer than expected. Reconnect and check receiver status before retrying.');
    }
    for (const prefix of ['backup', 'channels']) {
        const channelOnly = prefix === 'channels';
        const route = channelOnly ? 'channels' : 'backup';
        $(prefix + '-download').onclick = async () => {
            if (pending || !loaded || trial) return;
            pending = true; controls();
            try {
                const id = backupId();
                await receiverRequest('/api/device', 'POST', {action: channelOnly ? 'backup_channels_create' : 'backup_create', args: {id}});
                await waitForBackup(id, prefix + '-message');
                const response = await backupRequest('/api/device/' + route + '/download/' + id);
                const url = URL.createObjectURL(await response.blob());
                try {
                    const link = document.createElement('a');
                    link.href = url; link.download = (channelOnly ? 'satip-channels-' : 'satip-backup-') + new Date().toISOString().replaceAll(':', '-') + '.tar';
                    document.body.append(link); link.click(); link.remove();
                } finally { setTimeout(() => URL.revokeObjectURL(url), 60000); }
                message(prefix + '-message', channelOnly ? 'Channel backup downloaded. Includes saved channels and favourite order, not passwords or device settings.' : 'Backup downloaded. Keep this unencrypted file private: it contains passwords and keys.');
            } catch (error) { message(prefix + '-message', error.message); }
            finally { pending = false; controls(); }
        };
        $(prefix + '-restore').onclick = () => {
            if (pending || !loaded || trial) return;
            $(prefix + '-file').value = ''; $(prefix + '-file').click();
        };
        $(prefix + '-file').onchange = async () => {
            const file = $(prefix + '-file').files?.[0];
            if (!file || pending || !loaded || trial) return;
            if (file.size > 32 * 1024**2 || !file.size) {
                message(prefix + '-message', 'Choose a non-empty receiver backup up to 32 MiB.');
                $(prefix + '-file').value = ''; return;
            }
            pending = true; controls();
            let current_password;
            try {
                message(prefix + '-message', 'Uploading and validating backup…');
                const id = backupId();
                const response = await backupRequest('/api/device/' + route + '/upload/' + id,
                    {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file});
                const result = await response.json();
                if (!window.confirm(channelOnly ? 'Restore ' + result.available + ' available channels and ' + result.favourites + ' favourites from ' + result.created_at + '? Current channels and favourites (including unsaved edits) will be replaced. TV briefly pauses; device settings and passwords stay unchanged.' : 'Restore the backup from ' + result.created_at + '? All current receiver settings will be replaced and the receiver will reboot. Network access and passwords may change.')) {
                    message(prefix + '-message', 'Restore cancelled. No settings changed.'); return;
                }
                current_password = await askPassword();
                if (current_password === null) { message(prefix + '-message', 'Restore cancelled. No settings changed.'); return; }
                await receiverRequest('/api/device', 'POST', {action: channelOnly ? 'backup_channels_restore' : 'backup_restore', args: {id}, current_password});
                if (channelOnly) {
                    await waitForBackup(id, prefix + '-message', true);
                    message(prefix + '-message', 'Channels restored. Reloading the saved channel lists…');
                    location.reload();
                } else {
                    loaded = false; available = false;
                    message(prefix + '-message', 'Restoring settings and rebooting… Reconnect using the restored network and web password, then reload this page.');
                }
            } catch (error) { message(prefix + '-message', error.message); }
            finally { current_password = undefined; $(prefix + '-file').value = ''; pending = false; controls(); }
        };
    }
    $('storage-disk').onchange = () => { message('storage-dialog-message', ''); controls(); };
    $('storage-choose').onclick = () => {
        if (pending || !loaded || trial || storageData.mounted || !storageData.disks.some(d => !d.busy)) return;
        message('storage-dialog-message', '');
        $('storage-dialog').showModal(); $('storage-disk').focus();
    };
    function closeStorage() {
        if (pending) return;
        $('storage-dialog').close(); $('storage-choose').focus();
    }
    $('storage-cancel').onclick = closeStorage;
    $('storage-dialog').oncancel = event => { event.preventDefault(); closeStorage(); };
    async function useStorage(format) {
        const disk = storageData.disks.find(d => d.id === $('storage-disk').value);
        if (pending || trial || !disk || disk.busy || storageData.mounted || !$('storage-form').reportValidity()) return;
        const args = {id: disk.id, minutes: 0, gib: Math.min(8, disk.bytes / 1024**3)};
        if (format) {
            if (!window.confirm('ALL data on ' + disk.name + ' (' + disk.model + ', ' +
                (disk.bytes / 1024**3).toFixed(1) + ' GiB, serial ' + disk.serial +
                ') will be permanently lost. Format and use this device?')) return;
            args.confirm = true;
        }
        if (await action(format ? 'storage_format' : 'storage_mount', args, 'storage-dialog-message')) {
            message('storage-message', $('storage-dialog-message').textContent);
            closeStorage();
        }
    }
    $('storage-form').onsubmit = event => { event.preventDefault(); return useStorage(false); };
    $('storage-format').onclick = () => useStorage(true);
    $('storage-limit-form').onsubmit = async event => {
        event.preventDefault();
        if (!storageData.mounted || !$('storage-limit-form').reportValidity()) return;
        if (await action('storage_limit', {id: storageData.device_id, gib: Number($('storage-gib').value)}, 'storage-message')) {
            $('storage-gib').value = String(storageData.max_bytes / 1024**3);
        }
    };
    $('storage-eject').onclick = () => window.confirm('Discard the TV buffer and safely remove USB storage? Its configuration will be remembered.') &&
        action('storage_eject', {}, 'storage-message');
    $('storage-forget').onclick = () => window.confirm('Forget the saved USB device and buffer limits? No files on the USB device will be erased.') &&
        action('storage_forget', {}, 'storage-message');
    document.getElementById('source-tuner').onchange = () => window.sourceTunerControls();
    $('tuner-form').onsubmit = async event => {
        event.preventDefault();
        if (document.getElementById('source-apply-tuner').disabled) return;
        const tuner = document.getElementById('source-tuner');
        if (!window.confirm('Apply ' + tuner.selectedOptions[0].textContent + '? TV playback and this page restart. Channels and favourites are kept; no channel scan is started.')) return;
        await action('satip_tuner', {frontend: tuner.value}, 'tuner-message');
    };
    $('source-form').onsubmit = async event => {
        event.preventDefault();
        if (window.sourceImportBusy || document.getElementById('source-activate').disabled) return;
        if (!window.configuredTVSources?.includes($('source').value)) {
            message('source-message', $('source').value === 'enigma2' ? 'Load Enigma2 TV bouquets and import them on Channels first.' : 'Complete SAT-IP initial server and tuner setup first.');
            return;
        }
        if (!window.confirm('Change TV source? Playback and this page will restart.')) return;
        return action('source_select', {source: $('source').value}, 'source-message');
    };
    $('form').onsubmit = async event => {
        event.preventDefault();
        if ($('form').reportValidity()) await action('device', {hostname: $('hostname').value, timezone: $('timezone').value}, 'message');
    };
    $('wifi-form').onsubmit = async event => {
        event.preventDefault();
        if (!$('wifi-form').reportValidity()) return;
        await action('wifi', {ssid: $('ssid').value, password: $('wifi-password').value, country: $('country').value}, 'wifi-message');
        $('wifi-password').value = '';
    };
    $('wifi-scan').onclick = () => action('scan_wifi', {}, 'wifi-message');
    $('wifi-keep').onclick = () => trial && action('wifi_confirm', {id: trial.id}, 'wifi-message');
    $('wifi-revert').onclick = () => trial && action('wifi_revert', {id: trial.id}, 'wifi-message');
    $('wifi-enabled').onchange = async () => {
        const enabled = $('wifi-enabled').checked;
        if (!await action('wifi_enabled', {enabled}, 'wifi-message')) $('wifi-enabled').checked = !enabled;
    };
    $('ssh').onchange = async () => {
        const enabled = $('ssh').checked;
        if (!enabled && !window.confirm('Disable SSH access?')) { $('ssh').checked = true; return; }
        if (!await action('ssh', {enabled}, 'access-message')) $('ssh').checked = !enabled;
    };
    function clearPasswordFields() {
        for (const id of ['change-new', 'change-confirm']) $(id).value = '';
    }
    function closePasswordChange() {
        if (pending) return;
        const kind = passwordKind;
        clearPasswordFields();
        passwordKind = null;
        $('change-password-dialog').close();
        if (kind) $('change-' + kind + '-password').focus();
    }
    for (const kind of ['web', 'root']) {
        $('change-' + kind + '-password').onclick = () => {
            if (pending || !loaded || trial) return;
            passwordKind = kind;
            clearPasswordFields();
            message('change-password-message', '');
            $('change-password-title').textContent = kind === 'web' ? 'Change web password' : 'Change root password';
            $('change-password-dialog').showModal();
            $('change-new').focus();
        };
    }
    $('change-password-cancel').onclick = closePasswordChange;
    $('change-password-dialog').oncancel = event => { event.preventDefault(); closePasswordChange(); };
    $('change-password-dialog').onclose = () => {
        if (!$('change-password-dialog').open) { clearPasswordFields(); passwordKind = null; }
    };
    $('change-password-form').onsubmit = async event => {
        event.preventDefault();
        if (pending || !passwordKind || trial || !$('change-password-form').reportValidity()) return;
        const password = $('change-new').value, confirm = $('change-confirm').value;
        if (password !== confirm) { message('change-password-message', 'Passwords do not match.'); return; }
        const changed = await action(passwordKind + '_password', {password, confirm}, 'change-password-message');
        clearPasswordFields();
        if (changed) {
            message('access-message', $('change-password-message').textContent);
            closePasswordChange();
        }
    };
    for (const [id, name, question] of [['restart', 'restart_player', 'Restart TV playback?'], ['reboot', 'reboot', 'Reboot the receiver?'], ['shutdown', 'shutdown', 'Shut down? A power cycle is needed to start again.']]) {
        $(id).onclick = () => window.confirm(question) && action(name, {}, 'maintenance-message');
    }
    setInterval(async () => {
        if (!available || pending || document.getElementById('page-settings').hidden) return;
        try { await refresh(false); }
        catch (_) { if (trial) message('wifi-message', 'Reconnecting… Unconfirmed settings revert automatically.'); }
    }, 2000);
    if (['#settings', '#channels'].includes(location.hash)) void receiverRequest('/api/settings').then(data => window.loadDeviceSettings(data.device_settings === true)).catch(() => {});
})();
