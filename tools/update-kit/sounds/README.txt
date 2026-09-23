PUT SOUND FILES HERE
====================

Drop any .mp3 / .wav / .ogg files you were given into this folder, then run
update-scanmania.command in the folder above.

If this folder is empty, that is fine — the update skips the sound step and
the container keeps the sounds it already has.

Copying is additive: a sound already on the box that is not in this folder is
left alone, never deleted. So you only ever need to put the NEW or CHANGED
files here, not the whole soundtrack.

The names matter — the game looks for these exactly:

  ambient.mp3    the idle music between runs
  game.mp3       starts on COUNT IN; the spoken "3 - 2 - 1" is inside it
  sector2.mp3    sting when the player reaches checkpoint 1
  sector3.mp3    sting when the player reaches checkpoint 2
  break.mp3      sting when a beam is broken and a time penalty is given
  end.mp3        plays when the run ends

A file with a different name is copied but never played.
