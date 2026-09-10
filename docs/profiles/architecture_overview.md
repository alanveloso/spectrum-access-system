# Visão de arquitetura para authors

Este documento é o mapa de extensão da plataforma. Não substitui claims oficiais
WInnForum nem a matriz em `compliance/`.

## Camadas

```text
Profile YAML (reference | custom)
    │  seleciona mechanisms + declara capabilities
    ▼
Primitive / Mechanism catalog (registry)
    │
    ├── Device / Network adapters (plugins)
    ├── Protocol adapters (plugins)
    ├── Data providers (plugins)
    └── RF models (plugins)

Coordination Core
    snapshot → evaluate → decision → apply
    fail-closed · ProfileContext por decisão
```

O **Coordination Core** não deve ganhar `if country` / `if profile`. Novo regime
com comportamento já conhecido → YAML + dados + testes. Comportamento novo →
plugin ou primitive registrada, depois YAML.

## Profile = composição completa

A banda vive **dentro** do profile (`spectrum.ranges` / channelization). Não há
`BandProfile` separado. Reference e custom usam o **mesmo schema**; a diferença é
`metadata.status` (`reference` | `custom`) e proveniência — não capacidade.

`based_on` é metadado/proveniência apenas. **Não** há herança/merge YAML no
documento de profile.

## ProfileContext (decisão)

Toda decisão genérica carrega identidade imutável: `profile_id`, `profile_version`,
`profile_hash`, `dataset_versions`, `mechanism_versions`, `rf_provenance`.
Isso não é um singleton global de processo.

## Trust de load

| Tier | Como carrega |
| --- | --- |
| `builtin` | `load_profile(id)` sob `profiles/definitions/` |
| `operator_explicit` | path YAML do operador (doctor / custom) |

IDs de profile e nomes de plugin são tokens `[a-z][a-z0-9_]*` (sem path-like).
YAML usa `yaml.safe_load` — configuração, não código executável.

## Profiles de referência neste repositório

| Id | Papel |
| --- | --- |
| `cbrs_winnforum` | Design / request-path CBRS (RF-heavy, `dynamic_lease`) |
| `br_anatel_slp_3700` | Regime adicional via YAML + primitives |
| `eu_elsa` | Network / availability-centric (não fake CBSD/Grant) |
| `us_tvws_15_711` | Holdout §15.711 — representação condicional |

## O que o YAML não faz

- `if` / `else`, loops, expressões Python
- listar nomes de vendor/adapter como regra principal (use capabilities)
- registrar mechanisms via entry point (`spectrum_access.mechanisms` →
  `MechanismDiscovery` / `discovered_mechanism_registry()`)

Ver também: [reference_and_custom.md](reference_and_custom.md),
[creating_plugins.md](../plugins/creating_plugins.md).
