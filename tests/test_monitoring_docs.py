"""Every log event the package emits is documented."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
DOC = Path(__file__).resolve().parent.parent / "docs" / "monitoring.md"


#: Events are emitted two ways: an inline `extra={"event": "..."}`, and as
#: the first argument to Worker._log_extra. Matching only the first missed
#: two thirds of them and made this file's own coverage claim wrong.
_INLINE = re.compile(r'"event":\s*"([a-z_]+)"')
_HELPER = re.compile(r'_log_extra\(\s*"([a-z_]+)"')


def emitted_events() -> set[str]:
    events: set[str] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        events |= set(_INLINE.findall(text)) | set(_HELPER.findall(text))
    return events


def test_every_event_is_documented():
    # An operator alerts on these names. One that exists and is written down
    # nowhere is a signal nobody knows to watch for, which is how five of
    # them shipped undocumented.
    documented = DOC.read_text()
    missing = sorted(e for e in emitted_events() if f"`{e}`" not in documented)
    assert not missing, f"events with no entry in docs/monitoring.md: {missing}"


# There is deliberately no test for the reverse direction, a documented
# event the code never emits. monitoring.md holds several tables and the
# field and metric names in them are indistinguishable from event names by
# any cheap parse, so every version of that check either needed the doc
# structure hard-coded or passed by matching too little. A test that has to
# be weakened until it passes is worse than the gap it was covering.
