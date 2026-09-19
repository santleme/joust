# Review guide

Review in this order:

1. `Dockerfile` and `compose.yml` for supply-chain and
   credential-boundary changes.
2. `hackathon_competitor/state_machine.py` and `task_engine.py` for deterministic
   transition/DAG invariants.
3. migrations and repositories for persistence and restart behavior.
4. capabilities for evidence provenance and untrusted-content handling.
5. skills/persona for claims that outrun executable behavior.

No review may approve embedded secrets, a moving base/client reference,
automatic legal submission, token-minimization logic, or artificial usage loops.
