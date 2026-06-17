---
date: 2026-06-09
trigger: correction-phrase
matched: "hold on"
status: captured
related: plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: envsubst substitutes empty for unset/expired env vars without warning

## Trigger

While preparing for the ramjac otel-collector v2 deploy: I asked Donna to render the OTel Collector config from `infrastructure/observability/otelcol-config.yaml.tmpl` and create the `otel-collector-config-dev` Secret Manager secret. She did so. Then — before I ran `pulumi up` — she sent a "hold on" relay: the AWS SSO session had expired between the `aws ec2 describe-instances --query 'Reservations[0].Instances[0].PublicIpAddress'` call (which authed earlier) and the `envsubst` render. The describe-instances call returned an empty string; `envsubst` substituted that empty string into `${EC2_PUBLIC_IP}` without warning; the rendered config silently pointed the OTLP exporters at no host. The secret was created. The exporter would have looked valid in YAML, deployed cleanly, and silently failed at runtime — same class as the pubsub double-prefix silent-hang we'd just spent the night on.

## Observation

`envsubst` performs no validation. An unset variable substitutes to the empty string. An expired auth-credential producing an empty AWS API response is indistinguishable from a "valid" empty string at the shell layer. The config renders, the secret writes, and the failure mode is "outputs silently routed to nowhere."

The pattern matched the rest of the night's silent-failure class: the rendering step accepted the bad input and produced a syntactically valid downstream artifact. The only signal would have been at runtime, after `pulumi up`, after the collector deployed, after the chain services tried to export their first spans — and even then the exporter's "I tried to POST to nothing" log would have been one line in the noise.

## Generalization

Any templating step that consumes env vars without strict-mode validation hides upstream-credential-failure modes (auth expiry, missing var, typo). The default-empty behavior makes the downstream artifact look fine. If the artifact is a config file routing observability or traffic, the silent-failure surface is hours of debugging.

Three sibling failure modes I've now seen in 24 hours:
1. `gh api ...` to a private repo with an expired token returns an empty JSON `[]` instead of erroring — looks like "no PRs" to a downstream caller.
2. `aws ec2 describe-instances --query ...` with expired SSO returns empty string instead of erroring — `envsubst` makes the empty look real.
3. `ramjac_events.Consumer.subscribe()` with a malformed sub-name returns a future that terminates immediately — caller discards the future, no log.

In all three: the API contract is "return a value or signal an error," and the silent-empty path violates the implicit second half.

## Proposed rule

**Templating steps that consume env vars must either (a) use strict mode that errors on unset/empty, or (b) post-validate the rendered output before the artifact is consumed downstream.** For `envsubst` specifically, that's either `set -u` in the surrounding shell (errors on unset *variables* but not on empty *values*) plus an explicit `[[ -n "$VAR" ]]` guard for each required substitution, OR a stronger templating tool (`gomplate --missing-key=error`, `yq` with `strict` mode, `kustomize` configMapGenerator with `required` markers).

For secret-rendering specifically — when the rendered config decides where traffic / spans / data flow — the post-validate step should `grep` the rendered output for any literal `${...}` remnants (template variables that didn't substitute) and check that the placeholder fields contain valid-looking values (non-empty, expected shape) before the secret is written.

## Proposed remediation

Add a guard to `infrastructure/observability/render-otelcol-config.sh` (or its equivalent) along the lines of:

```bash
# Before envsubst:
: "${EC2_PUBLIC_IP:?EC2_PUBLIC_IP is required — check AWS SSO is fresh}"
: "${DD_SITE:?DD_SITE is required}"

# After envsubst:
rendered=$(envsubst '$EC2_PUBLIC_IP $DD_SITE' < otelcol-config.yaml.tmpl)
echo "$rendered" | grep -qE 'endpoint:\s*"":' && {
    echo "ERROR: empty endpoint in rendered config — substitution failed" >&2
    exit 1
}
echo "$rendered"
```

Then the upstream shell never produces an empty-IP'd secret. The "AWS SSO expired" error surfaces at the rendering stage with a clear message instead of three days later as an "exporter not reaching Tempo" investigation.

## What I'm doing right now

Holding the `pulumi up` until Donna confirms secret v2 (with the real IP after AWS SSO refresh) lands. The hold is the right operator instinct — the secret v1 would have produced exactly the silent-failure mode I'd just helped Donna debug on the streaming-pull side. Refusing to advance the deploy while a known-broken artifact is in place is cheaper than the alternative by hours of debugging.
