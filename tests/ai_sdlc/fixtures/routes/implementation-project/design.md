# Approved implementation design

The implementation route fixture has two independent branches, an intentionally
conflicting branch, and a fan-in integration step.  The source is immutable
input for the route and is never written by a worker.
