# cipher.py — REMOVED
#
# This file previously contained a Cipher class with stub implementations of
# YouTube's signature-cipher and n-parameter deobfuscation. Both were
# explicitly non-functional (comments in the original code admitted as much),
# and the class was never called anywhere in main.py.
#
# All actual stream URL resolution is handled by yt-dlp, which implements
# real deobfuscation internally. This file is kept as an empty placeholder
# so existing imports don't cause ImportError if any external tool references
# it, but the Cipher class has been removed entirely.
#
# Safe to delete this file completely if desired.
