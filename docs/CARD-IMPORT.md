# Automatic card import with safe eject

```bash
uv run photo-room watch-cards ~/Pictures/library            # foreground; Ctrl+C or SIGTERM stops it
uv run photo-room watch-cards ~/Pictures/library --once     # import the cards mounted right now, then exit
```

Insert a card and Photo Room copies everything new into the library, checks every file a
second time, ejects the card, and only then tells you it is safe to pull out. The editor shows
a progress panel while this happens and the new photos appear in the Library grid by themselves.

`photo-room ingest cards` and `ingest watch` use the same locations (macOS `/Volumes`; Linux
`/media/$USER`, `/run/media/$USER`, `/mnt`).

This is separate from `photo-room ingest`, which also converts and auto-edits. `watch-cards`
only brings files in, verifies them and ejects; open the editor afterwards.

## What it does, in order

1. **Detect.** Linux: mounts from `/proc/mounts` under `/run/media`, `/media` or `/mnt` that
   contain a `DCIM` folder (this covers udisks2/GNOME/KDE automounts). macOS: directories in
   `/Volumes` with a `DCIM` folder; `diskutil info -plist` adds the device and skips internal
   disks when available. A card must be seen on two consecutive polls (2 s apart) before it is
   touched, so a half-mounted card is not read. Each insertion is handled once; take the card out
   and put it back to import again.
2. **Plan.** Lists the card (a directory that cannot be read counts as a failure), then checks
   each file against the whole library by content (size, samples, SHA-256), not by name. It needs
   free space for the files that are not already in the library, plus 64 MB; if there is not
   enough it copies nothing.
3. **Import.** Each file goes through the same `ingest.copy_from_card_ex` as the card ingest and
   the in-app Import: copied to a temporary name, fsynced, SHA-256 compared with the source,
   then given its final name by a no-overwrite rename. A different file with the same name is
   kept as `IMG_0001-1.CR2`. Raw files, JPEG/HEIC and video are imported (`--raw-only` limits
   it to raws). Dot-files (`._IMG`, `.Trashes`) and other file types are left alone.
4. **Verify.** A second, independent pass re-reads every card file and its library copy and
   compares SHA-256. This includes files that were skipped as duplicates.
5. **Delete (optional, off by default).** See below.
6. **Eject** (on by default). Linux: `udisksctl unmount -b DEV`, then `udisksctl power-off -b
   DISK` (the whole drive, e.g. `/dev/sdb` for `/dev/sdb1`); without udisks, `umount MOUNT`.
   macOS: `diskutil eject`. Nothing is forced: no `-f`, no lazy unmount.
7. **Notify** with `notify-send` / `osascript`: "imported, N duplicates skipped, N failed" and,
   only if the eject succeeded, "Safe to remove the card."

## Layout

`--layout date` (default, `YYYY/MM/DD` from the EXIF capture date), `month` (`YYYY/MM`), `flat`,
or a template of `{yyyy}`, `{mm}`, `{dd}` such as `--layout '{yyyy}/{yyyy}-{mm}-{dd}'`.
Templates must be relative and cannot contain `..`. A file with no readable date uses its
modification time.

## The card is never ejected when

- any file failed to copy, failed its checksum, or could not be re-read for verification;
- the card listing had a read error;
- the import was interrupted (Ctrl+C, SIGTERM, service stop);
- there was not enough free space;
- the card is busy (unmount refused). The card stays mounted and the reason is in the log,
  the notification and the editor panel;
- `--no-eject` or `"eject": false` is set.

If unmount works but power-off fails, the message says "unmounted, but power-off failed" and
does not say "safe to remove". A failed card is not retried until it is re-inserted.

## Delete from the card (opt-in)

`--delete-after-import` removes photo files (raw, JPEG, HEIC) from the card, and only when the
whole import was clean (zero failures, zero read errors, everything re-verified). Each file is
deleted individually after its own checksum matched, and is skipped if its size or modification
time changed since it was verified. Videos, sidecars, unknown files and folders are never deleted.
Every deletion is logged to the terminal and appended to `<library>/.photo-room-card-deletions.log`.
`--delete-dry-run` logs what would be deleted and changes nothing. Deletion cannot be enabled
from the config file, only by the flag. This is the only thing that ever writes to the card.

## Settings

Flags: `--layout`, `--no-eject`, `--delete-after-import`, `--delete-dry-run`, `--raw-only`,
`--no-notify`, `--once`, `--interval`, `--config`. Optional JSON at
`~/.config/photo-room/card-watch.json` (`$XDG_CONFIG_HOME`):

```json
{"library": "~/Pictures/photo-room", "layout": "date", "eject": true, "raw_only": false,
 "notify": true, "interval": 2}
```

Only one watcher or import runs per library: a lock file (`.photo-room-card-watch.lock`) is held
for the life of the process, and a second `watch-cards` on the same library exits with an error.
Exit codes with `--once`: 0 clean, 1 something failed, 3 another instance is running.

## In the editor

The watcher writes progress to `<library>/.photo-room-card-status.json`; the editor polls
`GET /api/cards/status` and shows a panel (card name, file counts, the final message, why a
card was not ejected). When an import finishes the server rescans the library once, so the
photos arrive through the live change feed. Run the editor on the same library folder. If the
watcher dies mid-import the panel turns to "Card watcher stopped" after 30 s.

## Run it at login

systemd user unit, `~/.config/systemd/user/photo-room-cards.service`:

```ini
[Unit]
Description=Photo Room card import

[Service]
ExecStart=%h/.local/bin/uv run --project %h/photo-room photo-room watch-cards %h/Pictures/photo-room
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`systemctl --user daemon-reload && systemctl --user enable --now photo-room-cards`; logs with
`journalctl --user -u photo-room-cards -f`. `notify-send` needs the session bus, which user units
normally have. Add `loginctl enable-linger $USER` only if you want it running without a login
(notifications will not show then).

launchd, `~/Library/LaunchAgents/com.photo-room.cards.plist` (adjust the paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.photo-room.cards</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/uv</string><string>run</string>
    <string>--project</string><string>/Users/me/photo-room</string>
    <string>photo-room</string><string>watch-cards</string><string>/Users/me/Pictures/photo-room</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/me/Library/Logs/photo-room-cards.log</string>
  <key>StandardErrorPath</key><string>/Users/me/Library/Logs/photo-room-cards.log</string>
</dict></plist>
```

`launchctl load ~/Library/LaunchAgents/com.photo-room.cards.plist`. macOS may ask once to let the
program access removable volumes.

Optional udev rule, to start an import at insertion instead of polling. Save as
`/etc/udev/rules.d/99-photo-room-card.rules`, then `sudo udevadm control --reload`:

```
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_USAGE}=="filesystem", ENV{ID_BUS}=="usb", \
  TAG+="systemd", ENV{SYSTEMD_USER_WANTS}="photo-room-cards-once.service"
```

with a second user unit `photo-room-cards-once.service` running the same command plus `--once`.
`--once` only sees volumes that are already mounted, so the udisks automount must have finished
(the unit can `ExecStartPre=/bin/sleep 5`). The polling service above is simpler and is the
recommended setup; use the rule only if you do not want a resident process.

## Caveats

- Tested with fake card trees and fake eject commands only. No real card reader, udisks2 or
  `diskutil` was exercised by the test suite. Try it with `--no-eject` first, and with
  `--delete-after-import --delete-dry-run` before ever allowing deletion.
- Verification re-reads the card, so a large card is read twice (fast cards, USB 3, are fine;
  an old USB 2 reader is slow). A flaky reader can return the same wrong bytes twice; the
  checks catch corrupt copies, not a failing card, so keep the card until you have looked at the
  photos.
- `udisksctl power-off` needs polkit permission, which a normal desktop session has; over SSH or
  as a system service it may be denied. Then you get "unmounted, but power-off failed". Plain
  `umount` normally needs root.
- Detection is by `DCIM` on a mounted volume under the mount roots above. Cards mounted
  elsewhere, MTP/PTP cameras connected directly and cards that are not auto-mounted are not seen.
  Detection uses `/proc/mounts` and `diskutil info`; it does not use the udisks2 D-Bus API.
- XMP sidecars and non-DCIM files on a card are not imported, and the converter/auto-edit steps
  of `photo-room ingest` are not run.
- Writing to the library goes through the editor's change feed only via the rescan when an
  import finishes; photos do not appear one by one during the import.

## What the Library shows afterwards

Raws are editable. JPEG and HEIC files copied by `watch-cards` appear as
view-only cells (thumbnail, rating, flag, label; no Develop), and videos as
placeholder cells. A JPEG that has a same-named raw beside it is hidden so the
pair shares one entry. HEIC thumbnails need `pillow-heif`.
