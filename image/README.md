# Composed image ownership

This repository supplies Governor source and minimal SGLang hooks. It does not
build or publish complete SGLang model-serving images.

Composition recipes and locks live in phala-models-compose under
`image-builds/sglang/`. They pin official upstream and the independent
sglang-serving-patches and Governor revisions, explicitly select model patches,
and publish to `ghcr.io/phala-network/sglang` with OCI source
`https://github.com/Phala-Network/sglang`.

The former mixed-source Dockerfile/installer remain available at historical
commit `b24dbadb3a8a9a1c701bb8cac12bd7648a0bb157`. The corresponding image and
release draft are preserved, but cannot satisfy the revised source ownership
and are not a new deployment candidate.