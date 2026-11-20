# USB-backed HDMI time shifting

**Audience: users.** Pause/seek reference. The
[User manual](user-manual.md#usb-time-shifting-pause-and-go-back) has the illustrated operating instructions.

[Usage](usage.md) · [Storage settings](device-settings.md#time-shift-and-usb-storage)

## Prepare storage in the browser

Prefer a dedicated USB 3 SSD. Use [Settings → Time shift and USB storage](device-settings.md#time-shift-and-usb-storage)
to select an existing compatible ext4 drive or format a disposable drive, then set
its storage limit. **Formatting erases the entire selected disk.** Rebooting or
reconnecting a saved drive never formats it.

Ordinary live viewing does not continuously write TV data to USB. Pause starts
USB buffering; resume continues the same session. Return to live stops buffering
and discards its history. Seeking close to live does not itself end the session.
Without usable storage, Pause/seek shows an unavailable notice and TV stays live.

## Playback controls

| Action | Keyboard | Apple Remote |
|---|---|---|
| Pause / resume | Space, Pause or multimedia Play/Pause | Play/Pause |
| Back / forward about 30 seconds | Page Up / Page Down, multimedia Rewind / Fast Forward | Not assigned |
| Back / forward about 10 seconds while paused | Not assigned | Left / Right |
| Return to live | Home; Esc with no navigation panel open | Menu with no navigation panel open |
| Volume | Volume keys or Left / Right | Left / Right while playing |

Close an open favourites/details panel before using Esc or Menu to return to live.
While paused, repeated remote seek presses accumulate into a requested position;
five Right presses request about 50 seconds forward, within the available history.
Seek accuracy depends on the broadcast. A short transition may occur when resuming
or seeking. If the buffer-building notice appears, wait and retry Play/Pause.

The lower bar shows pause/delay and the buffered position. Programme information
continues to describe the **live broadcast**, not the delayed picture.

## Rolling retention and oldest-content behaviour

The storage limit bounds history, not session length. Old content is removed as
the buffer fills; reaching the limit does not end the session or return you to live.
If your position expires, it moves to retained content and stays paused if you
were paused. A warning estimates expiry, but changes in bitrate or free space
can shorten that estimate.

Retention depends on channel bitrate, usable free space and internal safety limits.
Even a very large drive does not provide unlimited history: the segment-count
limit is roughly 2.3 hours at two seconds per segment. The receiver reserves
about 1 GiB free space. No particular number of minutes is guaranteed.

History is temporary, not a recording. It is discarded on Return to live,
channel change, search, browser/YouTube viewing, player restart or reboot.
USB or capture failure returns to live with a notice. **TV input interrupted**
means the incoming television stream was interrupted; check the TV source and
network, not just USB storage. **Time-shift capture stopped** indicates a capture
problem. Neither notice is a reason to format the drive. Repeated capture failures
limit automatic retries so direct live TV can continue; Pause requests another attempt.

## Mounting persistence and removal

A saved compatible drive reconnects automatically after reboot or reconnection.
Before unplugging, select **Safely remove USB** and wait for success. This remembers
the drive and storage limit. Do not unplug after a failed removal.
**Forget USB configuration** is separate, available after removal, and does not
erase files. See [Storage settings](device-settings.md#time-shift-and-usb-storage)
for mount/format requirements and warnings.

Developer storage paths, capture policies and tests are in
[Time-shift internals](timeshift-internals.md) and
[Time-shift qualification](timeshift-validation.md).
