import { defineRailway, preserve, project, service, volume } from "railway/iac";

// This repository manages only its own resources in the environment.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "sovereign-saas";

export default defineRailway(() => {
  // Tenant ledgers, audit trails, trust stores and learned cost calibration. Not a
  // cache: losing it resets every tenant's history and reverts their cost estimates to
  // the uncalibrated heuristic. Declared here because this config is AUTHORITATIVE —
  // anything the file does not describe gets removed on apply, so a volume created out
  // of band would be detached by the next deploy.
  // region and size are declared to match what Railway already provisioned; leaving
  // them out clears them, because this file is the source of truth.
  const data = volume("sovereign-saas-volume", { region: "us-west2", sizeMB: 50000 });

  const saas = service("sovereign-saas", {
    build: {
      // Declared explicitly: this file is authoritative, so omitting `builder` clears
      // it and the build falls back to autodetection instead of the Dockerfile.
      builder: "DOCKERFILE",

      // The hosted control-plane image, deliberately separate from the root Dockerfile
      // (single-tenant self-host, defaults to the TUI). This one holds other people's
      // credentials: it runs non-root and fails closed without a deployment secret.
      dockerfilePath: "deploy/Dockerfile.saas",
    },

    healthcheck: "/saas/health",
    healthcheckTimeout: 30,

    // A correctness constraint, not a cost setting. Rate limiting and the tenant store
    // are both in-process: N replicas enforce N times the configured limit, and two
    // replicas writing the same tenant JSON lose updates with no error anywhere. Shared
    // state (Redis/Postgres) is a prerequisite for raising this.
    replicas: 1,

    // Keyed by mount path. (`volumeAttachments` is the *output* shape and is ignored
    // on input — a declaration there silently detaches the volume instead.)
    volumeMounts: { "/data": data },

    variables: {
      // Declared but never written down here. preserve() keeps whatever is already set
      // in Railway, so the deployment secret stays out of version control — and, just as
      // importantly, is not wiped by an apply. Losing it makes every stored tenant
      // credential permanently undecryptable, since the ciphertext is keyed to it.
      SOVEREIGN_SAAS_SECRET: preserve(),

      // Turns a lost tenant key context into a hard failure instead of a silent
      // fallback to the platform's own API key.
      SOVEREIGN_MULTI_TENANT: "1",

      SOVEREIGN_SAAS_ROOT: "/data/tenants",

      // Refuse to hand work to an external agent unless the task holds a grant carrying
      // the authority it needs. Permissive by default so the self-host keeps working,
      // which is not a posture to serve other people from.
      SOVEREIGN_STRICT_DELEGATION: "1",

      // Railway terminates TLS and sets X-Forwarded-For, so the rate limiter can trust
      // it here. Off by default elsewhere, where a caller could forge it per request.
      SOVEREIGN_TRUST_PROXY: "1",
    },
  });

  return project("sovereign-os", {
    resources: [saas, data],
  });
});
