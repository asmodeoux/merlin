# merlin_core

Pure-Python core of the Merlin platform. Houses the **domain** (entities,
value objects, regret math), **application** (use cases + ports as
Protocols), and **contracts** (Pydantic wire schemas).

## Layer rules

Enforced by `import-linter` (`[tool.importlinter]` in the root
`pyproject.toml`):

| Submodule                  | Allowed imports                                |
| -------------------------- | ---------------------------------------------- |
| `merlin_core.domain`       | stdlib only                                    |
| `merlin_core.application`  | `merlin_core.domain` + own ports               |
| `merlin_core.contracts`    | stdlib + `pydantic`                            |

**No SQLAlchemy. No FastAPI. No Telegram. No httpx.** All I/O happens in
`merlin_infrastructure` via Protocols defined in
`merlin_core.application.ports`.

## Why these three submodules, not three packages

We considered splitting `domain`, `application`, and `contracts` into three
separate PyPI-style packages. We decided against it:

- One `pyproject.toml` and one wheel for the pure-Python core is simpler
  to install and version.
- `import-linter` enforces the boundaries equally well as separate
  packages would.
- Contributors think "core" as one concept; the hexagonal layering is
  internal mechanics.

See `docs/ARCHITECTURE.md` ADR (d) for the full rationale.

## Layout

Conventional two-layer flat layout:

```
packages/python/merlin_core/
├── pyproject.toml              # build metadata
├── README.md
├── tests/                      # outside the importable package
└── merlin_core/                # the importable package — what `import merlin_core` resolves to
    ├── __init__.py
    ├── py.typed
    ├── domain/
    ├── application/{ports,use_cases}/
    └── contracts/
```

Hatchling picks the inner package up via the conventional `packages =
["merlin_core"]` in `pyproject.toml` — no `sources` rewrites, which
keeps editable installs (`uv sync`) working. See ADR (q) in
`docs/ARCHITECTURE.md` for why we backed out of the prefix-add trick.
