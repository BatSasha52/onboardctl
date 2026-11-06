# Remediation responder

A small webhook service that receives Alertmanager notifications and performs
one remediation: a rollout restart of a Deployment, the same change
`kubectl rollout restart` makes.

```
kubectl apply -k responder/
kubectl -n onboardctl-system logs deploy/onboardctl-responder   # the audit log
```

## How a service opts in

```yaml
remediation:
  restartDeployment: checkout-api
```

For a spec with this block, onboardctl additionally renders:

- labels `remediation: rollout-restart` and `deployment: checkout-api` on the
  fast availability burn-rate alert only (`ErrorBudgetBurn`,
  `slo=availability`, `long_window=1h`);
- a route in the service's AlertmanagerConfig that sends alerts with that
  label to the responder, with `continue: true` so the team is still paged;
- a Role and RoleBinding in the service's namespace that let the responder's
  ServiceAccount `get` and `patch` that one Deployment (`resourceNames`), and
  nothing else.

The responder's own ServiceAccount has no permissions. Without a spec opting
in, it cannot touch anything.

## What it checks before acting

1. The notification is for a firing `ErrorBudgetBurn` alert with
   `remediation=rollout-restart`, `slo=availability`, `long_window=1h` and
   valid `namespace` and `deployment` labels. Anything else is logged as
   `ignored`.
2. Alertmanager's API reports that alert as active, not silenced and not
   inhibited. A forged call to the webhook therefore cannot cause a restart,
   and a silence stops remediation as well as notifications.
3. The Deployment has not been restarted within the cooldown (30 minutes by
   default). The time of the last restart is read from the Deployment's
   `kubectl.kubernetes.io/restartedAt` pod-template annotation, so the limit
   survives restarts of the responder and also counts restarts done by hand.
   Otherwise it logs `rate_limited`.

Then it restarts the Deployment and logs `restarted`. With `DRY_RUN=true` it
logs `would_restart` instead. Every decision, including errors, is one JSON
line on stdout.

## Why only this one alert

A restart is a reasonable first response to one failure shape: a process that
has got into a bad state (exhausted connection pool, wedged worker, leaked
memory) while the rest of the system is fine. A fast availability burn on a
single service is the alert most likely to mean that, and the one where
waiting for a human costs the most error budget.

For the other alerts a restart is the wrong tool or makes things worse:

- Slow burns and the ticket tier develop over hours. A person has time to
  look, and the cause is more often a bad deploy or a dependency.
- Latency burns are usually load or a slow dependency. Restarting drops warm
  caches and capacity exactly when the service is busiest.
- `ScrapeTargetMissing` means Prometheus cannot see the service. Restarting
  something you cannot observe gives no signal on whether it helped.

Even for the one alert, a restart does not fix a bad release or a failing
dependency. The cooldown makes sure it is tried once and then left to the
person who was paged in parallel, instead of restarting in a loop.

## Limits

- One replica, because the lock that serialises decisions is in-process.
- The NetworkPolicy only allows ingress from Alertmanager, but it is only
  enforced where the network plugin supports NetworkPolicy. On the local kind
  cluster it was not enforced (a pod in `default` reached the webhook), which
  is why the Alertmanager confirmation in step 2 does not depend on it.
- The audit log goes to stdout and lives as long as the pod's logs unless the
  cluster ships logs somewhere durable.
