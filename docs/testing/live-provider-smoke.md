# Live provider smoke suite

The live provider smoke suite is a manual, pre-release operation. It is excluded
from CI because it requires maintainer-owned credentials, incurs provider cost,
and must test each provider independently rather than allowing substitution.

The runner writes a dated JSON report containing per-provider pass/fail,
latency, resolved model identity, and reported/unreported usage. A release may
not treat mock conformance as evidence that live model grants are available.

