"""The Python attestation loop is retired: core4's attester does this job.

Owner ruling 2026-10-03 (S55 item 80): the loop in agent/attestation_loop.py is
superseded by the core4 attester and must not be deployed. The README section
that documents the loop must carry that banner so nobody packages it.
"""

import pathlib

README = pathlib.Path(__file__).resolve().parents[1] / "README.md"
HEADING = "## Attestation loop (`agent/attestation_loop.py`)"
BANNER = "superseded by the core4 attester; do not deploy"


def test_attestation_loop_section_says_superseded_do_not_deploy():
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADING)
    nxt = text.find("\n## ", start + len(HEADING))
    section = text[start : nxt if nxt != -1 else len(text)]
    assert BANNER in section.lower(), section[:300]
