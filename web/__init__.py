"""
web/__init__.py — The four frontends and the socket that feeds them.

Inputs:  HTTP and WebSocket from the GM iPad, admin laptop, two displays
Outputs: a 10 Hz state broadcast; route handlers; static single-file pages
Invariant: the game path never awaits a client — broadcasts are sent
           concurrently with a hard deadline and slow clients are dropped.
"""
