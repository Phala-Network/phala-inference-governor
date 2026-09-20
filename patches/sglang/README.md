# Governor integration patches

Use the [versioned minimal hook series](v0.5.20/README.md).
Independent SGLang serving repairs and explicitly selected model compatibility
packs are maintained in
[sglang-serving-patches](https://github.com/Phala-Network/sglang-serving-patches).

Historical mixed development/release patches are retained in Git history at
`b24dbadb3a8a9a1c701bb8cac12bd7648a0bb157`; they are not current build inputs.
Deployment configuration pins the two repositories independently and publishes
the combined runtime to `ghcr.io/phala-network/sglang`.
