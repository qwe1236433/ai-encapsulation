# EVOLUTION.md — System Evolution Log

This document records the architectural decisions, cognitive pivots, and engineering audits of the Traffic-Lab system. It serves as the "truth source" for **why** the system is built the way it is. Per-task execution notes live in `任务进程与结果总结.md`.

---

## Phase 1: The Strategic Pivot (From HVAC to Traffic-Lab)

**Date:** April 2026

**Decision:** Shifted core domain from "HVAC Engineering" to "Traffic-SOP Intelligence".

**Logic:** HVAC provided a T-A-V-C proof-of-concept, but Traffic Intelligence offers faster feedback loops and higher economic value. The architecture evolved from a "Toolbox" to a "Laboratory".

### Final Architecture Blueprint (v1.0)

- **Topological Split:** `Hermes (Strategist)` ↔ `OpenClaw (Tactician)`.
- **Core Loop:** T (Think) → A (Act) → V (Verify) → C (Correct).
- **Cognitive Engine:**
  - ε-greedy exploration for local-optima escape.
  - Negative pool with strike-count and semi-decay.
  - Dynamic scoring with P90 ceiling.
- **Execution Layer:** Action-based routing with GPU sensing and compound operators (e.g. `publish_and_monitor`).

---

## Phase 2: Engineering Maturity (The Modularization)

**Date:** April 2026

**Audit grade:** Industrial-ready baseline (see audit entry in `任务进程与结果总结.md`).

**Key changes:**

1. **De-monolithization:** Split large executor surface into `brain`, `runner`, `scoring`, `negative_pool`, `verify`, `metrics`, `client`, `storage`.
2. **State persistence:** Session recording and `case_library` archival.
3. **T-A-V-C closure:** Full loop with decoupled async task polling (HTTP202 + polling) so the orchestrator does not block.

---

## Phase 3: Cognitive Evolution (Memory & Fingerprinting)

**Date:** April 2026

**Decision:** Implementing "cognitive memory" over raw data storage.

**Implemented mechanisms:**

- **Environment fingerprinting:** Jaccard-style similarity for context alignment instead of exact-only match.
- **Cognitive conflict (S4):** Forced exploration when case-library successes fail under environment drift.
- **TTL decay:** Case weights decay over time so outdated successes do not dominate.

**Current status:** Infrastructure is locked. Transitioning from simulation to real-world traffic data; mock high pass rates are an artifact of the lab, not proof of market fit.

---

## Phase 4: Execution Plane — Self-Hosted OpenClaw vs. Official Gateway

**Date:** April 2026

**Decision:** Keep **this repo’s `openclaw/`** as a **thin, self-owned FastAPI executor** in Docker. Call **MiniMax with API key directly** via `minimax_client.py`. **Do not** fold in the upstream **openclaw.ai Gateway** (OAuth, plugin host) for this lab.

**Rationale (ops / latency / control):**

- Shorter path: Hermes → OpenClaw container → MiniMax, avoiding extra Gateway hops for T-A-V-C iteration.
- Full control over timeouts, temperature, JSON shaping, and future intercept/cache without vendor lock-in on Gateway APIs.
- Deploy stays `docker-compose` + `.env`; no OAuth callback URLs or separate Gateway lifecycle.

**Cognitive note:** The directory name `openclaw` here is **not** a mirror of the commercial OpenClaw product; it is our **tactical execution surface** only. See `openclaw/config.yaml` header comment.

---
