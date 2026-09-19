FROM rust:1.98.1-bookworm@sha256:c49256cbe5ea0188bc658a689500d70c41eb51f009a7a7be209caf60a944f3ec AS governor-build
WORKDIR /build/governor
COPY rust/ ./
RUN cargo test --locked --offline && cargo build --release --locked --offline

FROM docker.io/lmsysorg/sglang:v0.5.20@sha256:b27fce60bc5494c118c4910702812bcfa8cee67abcdd1ff8b0902f21647552f4
ARG SOURCE_REVISION
ARG UPSTREAM_PREIMAGES_SHA256
LABEL org.opencontainers.image.title="Phala Inference Governor with SGLang" \
      org.opencontainers.image.version="v0.1.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=governor-build /build/governor/target/release/libpig_governor_core.so /opt/pig-governor/lib/libpig_governor_core.so
COPY pyproject.toml LICENSE /opt/pig-governor/source/
COPY python/ /opt/pig-governor/source/python/
COPY patches/sglang/v0.5.20/ /opt/pig-governor/patches/
COPY image/install_sglang_overlay.py /opt/pig-governor/build/install_sglang_overlay.py
# Generated only from pinned upstream Git blobs; hash frozen in build-inputs.json.
COPY build-context/upstream-preimages.tar /opt/pig-governor/build/upstream-preimages.tar
RUN test -n "$SOURCE_REVISION" && test -n "$UPSTREAM_PREIMAGES_SHA256" \
    && python /opt/pig-governor/build/install_sglang_overlay.py \
       --preimages /opt/pig-governor/build/upstream-preimages.tar \
       --preimages-sha256 "$UPSTREAM_PREIMAGES_SHA256" \
       --patches /opt/pig-governor/patches \
       --receipt /opt/pig-governor/build/source-receipt.json \
    && python -m pip install --no-index --no-deps --no-build-isolation --no-compile /opt/pig-governor/source \
    && command -v hf && command -v curl

ENV PIG_GOVERNOR_LIBRARY=/opt/pig-governor/lib/libpig_governor_core.so
# Compose opts in explicitly and provides the existing TOKEN via environment.
ENTRYPOINT ["sglang", "serve"]
