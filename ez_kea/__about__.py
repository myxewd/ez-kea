# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Single source of truth for EZ-KEA's version.

Kept in its own module with no imports so anything can read it -- the app
factory, packaging metadata, a release script -- without pulling in Flask or
touching application config. `git describe` is deliberately not used: an
install is usually an rsync or a tarball, not a git checkout, and a version
string that reads "unknown" on exactly the deployments you most want to
identify is worse than one that is occasionally a commit behind.

Bump this in the same commit that tags the release, so a checkout at the tag
and the tag itself never disagree.
"""

__version__ = "0.10.2.1"
