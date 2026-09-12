# deploy/

systemd units for the NUC. Install with:

```bash
sudo cp deploy/scanmania.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanmania.service
```

`/etc/default/scanmania` holds the environment (mode 0600). It is not in git.

## Why these files are here

They used to exist only on the NUC. Nothing in the repo defined them, so a
rebuild from a clean clone could not reproduce the running system, and
`Restart=always` was documented in prose that nothing enforced.

`docs/architecture.md` describes `scanmania-sync.service` as a separate unit.
On the NUC it is collapsed into `scanmania.service`, which runs the outbox and
the snapshot loop in-process. Only `scanmania.service` is authoritative.

## Known gap: scanmania-kiosk.service

`tools/deploy.sh` restarts `scanmania-kiosk.service`, but neither the unit nor
the `kiosk.sh` script it runs is in this repo — both live only on the NUC.

Copy them in before treating this directory as complete:

```bash
scp root@172.16.0.10:/etc/systemd/system/scanmania-kiosk.service deploy/
scp root@172.16.0.10:/opt/scanmania/tools/kiosk.sh tools/
```

`kiosk.sh` carries real operational knowledge that exists nowhere else — no
window manager is installed, so `--kiosk` does nothing and geometry must be set
explicitly; each Chromium needs its own `--user-data-dir`; `--test-type` is what
hides the `--no-sandbox` infobar. Losing that file means rediscovering all of it.
