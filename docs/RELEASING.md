# Release policy

Integrate tested changes into `main`. Development branches carry candidates;
long-term maintenance branches need a real compatibility requirement. Create an
immutable annotated version tag from a verified commit in `main` history.
Never move published tags or overwrite existing image versions.

Record the source commit, version/tag, relevant component/engine identities,
validation scope and published artifact digest. Component tests, final-image
checks and target deployment acceptance are separate results. Version metadata
alone does not establish release qualification.

Check existing workflows before pushing a tag. This repository's current CI
runs source checks, not automatic image publication. Use the authorized release
builder for image work, validate the built artifact, publish it once and read
back its immutable digest. Historical tag backfills must match existing artifact
source; they do not authorize a rebuild or deployment.

Keep release notes concise. Put raw logs and large build artifacts in release
assets or evidence storage; link to immutable records instead of duplicating
execution logs in the repository's landing page.
