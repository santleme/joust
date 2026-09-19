# Joust is a Plow Hermes variant. Generic runtime behavior stays in the
# immutable upstream base, including the supervised Agent Index reporter;
# this image owns only its persona, skills, and mission package.
# Latest published base from the official main branch. Keep this immutable;
# hosted provisioning injects the tenant environment at runtime.
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-ef0019372ff8bca593611b31ebd2e08f9f1458ff@sha256:a8a2f97ad78b8192d80a984dce81d3bf5a9a883d18cb7b677704913a09b56aee

# GitHub is part of Joust's execution/observation plane. Keep the package
# version explicit so a rebuild cannot silently change the CLI contract.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gh=2.46.0-3 \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 runtime/persona.md /opt/hermes/plow-seed/persona.md
COPY --chmod=0644 LICENSE NOTICE /usr/share/doc/joust/

COPY --chmod=0644 pyproject.toml LICENSE README.md compose.yml Dockerfile /opt/joust/
COPY hackathon_competitor/ /opt/joust/hackathon_competitor/
RUN find /opt/joust -type d -exec chmod 0755 {} + \
 && find /opt/joust -type f -exec chmod 0644 {} +
# plow-init deliberately leaves the root-owned shared home traversable and
# writable by the hermes group. Hermes CLI commands call _secure_dir(), so
# carry that contract into every subprocess instead of letting a root-run
# diagnostic silently revert the volume to 0700 root:root/root:hermes.
ENV PYTHONPATH=/opt/joust \
    HERMES_HOME_MODE=3770 \
    GH_CONFIG_DIR=/var/lib/hermes/.config/gh

COPY skills/ /opt/hermes/skills/
RUN find /opt/hermes/skills -mindepth 1 -type d -exec chmod 0755 {} + \
 && find /opt/hermes/skills -mindepth 1 -type f -exec chmod 0644 {} +

RUN install -d -o 10000 -g 10000 -m 0700 /var/lib/hermes/hackathon_competitor
