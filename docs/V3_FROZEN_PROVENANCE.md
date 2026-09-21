# Governor v3 frozen provenance

This record preserves the immutable Governor v3 source and SGLang hook inputs.
The Governor v4 candidate and its future manifest must add new release identities
without changing or reusing these values as v4 evidence.

| Identity | Frozen value |
| --- | --- |
| Governor main commit | `47f31552dcddac424f8c860e75d5f9c280fc98d9` |
| Governor source tree | `b08ef1df0d04e47f8abe8f6c6b595d06eba216c8` |
| SGLang Governor hook SHA256 | `ded58601757c2c68569f49e4204d1d9fbff38b8056cd710416b1fa12359a92f2` |

These identifiers are historical provenance. They do not qualify the Governor
v4 package, ABI v4 hook, complete engine source or a new runtime image.

The exact historical hook bytes, manifest and README remain available under
[`patches/sglang/v0.5.20/history/v3`](../patches/sglang/v0.5.20/history/v3).
Their files are copied from the frozen v3 state; the active v4 manifest checks
the retained hook bytes against the SHA256 above.
