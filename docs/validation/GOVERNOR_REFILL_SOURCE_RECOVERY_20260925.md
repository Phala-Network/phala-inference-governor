# Governor v4 refill source recovery

The GPU805 source archive `gpu805-governor-soak-green-v4.tar.gz` has SHA-256
`e4d1b5eb3e3a111d7908d5a0fe2dc2c113761415d729d1203287290d1071a89a`.
It contains the Rust core, Python plugin, and focused tests used for the
refill-recovery CPU validation. Its contents were restored over clean Governor
commit `675c5364bb103a6aacb12c26af406217cdd93622` on this branch.

The GPU805 overlay was labeled with Governor commit
`a30e8c6aa60231b418595e20c94ee00c660b9eb4`, but that Git object is not
available in the local repository or advertised by its remote. This branch
records the verified archive contents under a new Git identity; it does not
claim to reproduce that historical commit object. The overlay replaced the
native library and `profile.py` on its base image, so source-archive tests and
overlay runtime checks remain separate evidence.

The archived source passed 50 Rust, 14 profile, and 22 real-library FFI tests
on GPU805's fixed CPU image. See the execution plan's
`GOVERNOR_V4_SOAK_REFILL_RECOVERY_20260925.md` for the exact run and log hashes.
Any subsequent executable edit requires its own affected checks and a new
source identity before image acceptance.
