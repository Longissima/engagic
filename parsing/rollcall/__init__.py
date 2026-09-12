"""Roll-call parsing for the minutes route.

Two tiers. Per-city template drivers (the validated spike, loaded by
``spike``) for cities whose minutes are generated verbatim by a legislative
system and print a file number beside every motion. The generic ``engine``
for everyone else: it aligns the minutes to the meeting's own agenda items,
reads the attendance roster, and publishes whatever the clerk actually
recorded, per-member when explicit names resolve deterministically,
outcome-and-tally otherwise. Unresolved evidence is retained internally.
"""

from parsing.rollcall.spike import DIALECTS, load_spike_parser, norm_file

__all__ = ["DIALECTS", "load_spike_parser", "norm_file"]
