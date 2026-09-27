"""Remote Worker Birbs: a Worker Birb running on another machine and OS.

The main CoBirb opens one TLS WebSocket to each configured remote and never
listens itself. A remote runs ``cobirb remote-worker`` and takes everything —
the work, its files, its settings, and by default its model — from the main
machine. See ``protocol`` for the conversation, ``client`` and ``server`` for
the two ends, and ``pool`` for how a flock uses them.
"""
