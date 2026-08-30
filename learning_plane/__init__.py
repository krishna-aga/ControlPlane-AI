"""
Learning Plane — offline/asynchronous. Reads what the Data Plane wrote to the audit
ledger and turns it into visibility (ledger explorer), ground truth (reviewer queue),
a false-negative estimate (shadow eval) and better thresholds (calibration).

Nothing here runs on the request path. See docs/ for the Data Plane and Control Plane
this reads from, and the Learning Plane spec PDF in docs/ for the four-component design.
"""
