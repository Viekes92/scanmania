UPDATING SCANMANIA
==================

What this does
--------------
Updates the software on the container's computer (the NUC) to the latest
version, and copies over any new sound files.

It does NOT change the mazes, the calibration, or any run history. Those live
on the box and are left alone.


What you need
-------------
  * A MacBook.
  * To be on the SAME network as the container — the venue's network or the
    container's own switch. Not a phone hotspot, not a different building's
    wifi.
  * The NUC password. Whoever sent you this folder has it.
  * The container powered on. If it was only just switched on, wait two
    minutes: the network switch boots more slowly than the computer.


How to run it
-------------
  1. Unzip this folder somewhere you can find it, like the Desktop.

  2. If you were given new sound files, put them in the "sounds" folder
     inside this one. If you weren't, skip this step — the box keeps the
     sounds it already has.

  3. Double-click  update-scanmania.command

  4. A black Terminal window opens. Type the NUC password when it asks.
     You only get asked once. Nothing appears on screen while you type the
     password — that is normal, it is not frozen.

  5. Wait. It takes a minute or two and tells you when it is finished.


"macOS cannot verify the developer of this file"
------------------------------------------------
macOS blocks files downloaded from the internet. Either:

  * Right-click the file -> Open -> Open,  or
  * Open Terminal, type   xattr -d com.apple.quarantine    then drag the
    file into the window and press return.


"Permission denied" or the file opens in TextEdit
--------------------------------------------------
The executable flag was lost, which happens with some unzip tools. Open
Terminal, type   chmod +x    then drag  update-scanmania.command  into the
window and press return. Then try again.


If something goes wrong
-----------------------
The script stops at the first problem and tells you what it found. It is
built so that a failed update leaves the container exactly as it was — the
box backs up its database before changing anything and puts the old version
back if the update does not complete.

If it stops, take a photo or a screenshot of the whole window and send it to
whoever maintains the software. The message on screen says what happened.

Nothing in this folder can break the container on its own. The one thing you
should NOT do is power the container off while the update is running.


For the person who set this up
------------------------------
Defaults are root@172.16.0.10. Override without editing the script:

    SCANMANIA_HOST=10.0.0.5 SCANMANIA_USER=root ./update-scanmania.command

The script authenticates once via an SSH ControlMaster socket and then:
checks reachability on port 22, refuses to continue if the box has
uncommitted work outside config/, copies any sound files additively, and runs
the box's own tools/deploy.sh — which is where the database backup, the pull,
the dependency install, the import check, the unit-file sync, the restart and
the rollback all live. Schema migrations run when the service starts, and the
script greps the journal afterwards so the operator can see that they did.
