# Agent Index integration

The Plow base image ships the official client and its `agent-index` `s6`
service; this repo does not carry a copy.

`AGENT_ID` is an immutable external identifier. It is not the product name.
It is operator-chosen (for example,
`galahad-hackathon`). Use the same value for Agent Index registration and every
restart. Joust now binds the first configured value into installation state and
fails explicitly if a later runtime tries to use a different id. The service
stands down if it is absent. Verified status is a separate
eligibility step and does not provide or choose the id. The broad Plow
credential is supplied only to the client's one-time official registration
exchange; periodic reports use the stored Agent Index key and do not receive
the Plow token.

The public community entry is
<https://aiworthusing.com/agent-index/galahad-hackathon>. On 2026-09-12 the
rendered page showed Galahad, built by Lucas, powered by Hermes / Plow, with one
active user and 119K reported tokens. The supervised reporter independently
returned HTTP 200 for 119,363 tokens across two rows. This confirms publication
and reporting, but not Verified eligibility; the organizer says verification
opens on 2026-09-14.

On 2026-09-13 the entry was re-registered with the Joust identity: the display
name became Joust, the blurb was replaced with the "Joust it." product line.
The current canonical repository is <https://github.com/santleme/joust>;
older publication records used <https://github.com/baskpascal/joust> and GitHub
may serve a redirect from that former URL. The
`agent_id` stays `galahad-hackathon`, because it is the public URL and the key
the Index counts installs against; renaming it would open a new entry and
strand the recorded usage. The builder line reads "La brava", which comes from
the Plow account profile (`plow-agents profile --name`) rather than from
`--register`; the Index resolves the publisher from Plow authentication. The
re-registration reused install `f4adc0fb60e2fcf5e98df305705d2921`, and a
supervised report afterwards returned HTTP 200 for 409,299 tokens across four
rows.

Generate the credential locally with `plow-agents login`, select a free line
with `plow-agents lines`, and run `plow-agents mint <line-id>`. The resulting
`./plow-credentials` file is secret-bearing and must remain outside GitHub.

The client and its supervised `agent-index` service come from the Plow base
image; this repo carries no copy. To update them, bump the `FROM` line to a
newer immutable `base-<sha>@sha256:<digest>` tag, update the same tag in
`tests/test_image_contract.py`, build the image, and run the tests.
